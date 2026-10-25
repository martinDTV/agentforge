# 0006. Tool Gateway: registro tipado, contexto confiable, autorización por agente y auditoría de invocaciones

- Status: proposed (Fase 5 en curso; Pasos 1 y 2 implementados: registro, política, herramientas de solo lectura, auditoría por paso lógico, adaptador Git local y worktrees por ejecución; sin sandbox ni push)
- Date: 2026-10-25
- Deciders: martin, Claude (Fase 5)

## Context

Hasta la versión 0.4.0 los agentes solo producen texto estructurado: nunca leen un repositorio ni ejecutan nada. Las fases siguientes necesitan que Developer, QA y Security operen sobre código, y eso abre la superficie más peligrosa de la plataforma: un modelo que pide "ejecuta esto" o "lee aquel archivo". Las decisiones del usuario que acotan esta fase son:

- Desarrollo en macOS (Apple Silicon) sin Docker Desktop; el código no confiable se ejecutará únicamente en una VM Linux dedicada (homelab Proxmox), a la que se llegará mediante un `SandboxPort` con runner remoto. Nada generado por un LLM se ejecuta en el host.
- Repositorios Git locales de prueba al principio, con un repositorio base administrado, un worktree por ejecución, ramas `run/<run_id>` y referencias protegidas; GitHub mediante una GitHub App con permisos mínimos más adelante.
- Identidad de automatización **AgentForge**, separada de la identidad personal; los agentes no heredan credenciales SSH, GitHub, cloud ni variables del entorno.

## Decision (Paso 1)

### Una única puerta, con el orden de comprobaciones fijado

Toda herramienta se invoca a través de `ToolGateway.invoke(context, call)` (`app/tools/gateway.py`). El orden es inamovible y cada paso queda auditado: nombre registrado → política → validación de argumentos → fila de auditoría *antes* de ejecutar → ejecución acotada → validación, redacción y digest de la salida → cierre de la auditoría. Los fallos de la herramienta se devuelven como resultados `failed`; el gateway no los propaga como excepciones. Una prueba estática garantiza que `gateway.py` es el único sitio que llama a `tool.run`.

### Dos objetos, dos niveles de confianza

- `ToolCall` (`app/contracts/tools.py`) es lo único que puede producir un modelo: un nombre, que es una clave de búsqueda, y un diccionario de argumentos que se valida contra el esquema cerrado de la herramienta (`extra="forbid"`, exigido al registrar). Un argumento `project_id`, `agent` o `workspace_root` es `invalid_input`, no una escalada.
- `ToolContext` (`app/tools/ports.py`) lo construye la orquestación: ejecución, intento, proyecto, tipo de agente, principal y raíz del workspace. Es inmutable y no se deriva nunca de texto del modelo. No lleva entorno: las herramientas no reciben nada del proceso anfitrión.

### Identidad de automatización y política compuesta

`AutomationIdentity` (`app/security/automation.py`) acuña por ejecución un principal de servicio `agentforge:<agente>` con emisor interno, un solo rol (`agent` → `tools:execute`) y un solo proyecto. Ese rol no concede ningún permiso sobre ejecuciones, y ningún rol humano concede `tools:execute`. `ToolAuthorizationPolicy` (`app/security/tools.py`) compone la `AuthorizationPolicy` del ADR 0005 y decide en orden: principal de servicio de un emisor confiable, permiso `tools:execute`, pertenencia al proyecto (el proyecto reservado `legacy` queda fuera), concesión del agente (`AgentGrant`: nombres exactos o prefijos `familia.*`, dentro de clases de riesgo) y, por último, la **puerta global de riesgo**: en esta fase solo `read` es ejecutable, digan lo que digan las concesiones. `write`, `exec` y `network` existen en el vocabulario para que la política no cambie de forma cuando lleguen el adaptador Git y el sandbox.

Matriz por defecto: el Planner no tiene herramientas; Architect, Developer, QA y Security tienen `workspace.*` en clase `read`. La matriz es configuración, no código: se inyecta al construir la política.

### Herramientas de solo lectura confinadas

`workspace.list_dir`, `workspace.read_file` y `workspace.find` (`app/tools/filesystem.py`) operan solo bajo `context.workspace_root`. Las rutas se validan relativas, sin `..`, se resuelven siguiendo enlaces simbólicos y el resultado debe seguir bajo la raíz; `.git`, `.hg`, `.svn` y los nombres que convencionalmente guardan credenciales (`.env`, `*.pem`, `*.key`, `id_rsa*`, `.netrc`, …) se rechazan antes de abrirlos, con `.env.example` como excepción documental. Las lecturas se acotan en bytes y entradas, detectan binarios y pasan por `redact()` (`app/tools/redaction.py`), una segunda red para claves privadas, JWT, tokens conocidos y pares clave=valor sensibles. El gateway vuelve a redactar y acota la salida serializada.

### Auditoría en tabla propia, por paso lógico, escrita antes de ejecutar

Migración `0004`: `tool_invocations` guarda una fila por `(run_id, step, attempt)`, con herramienta, versión, clase de riesgo, agente, sujeto actor, proyecto, estado, código de error, digests SHA-256 de entrada y salida, tamaño, duración y marcas de tiempo. **Nunca** argumentos, contenidos ni salidas.

El **paso lógico** (`step`) lo elige la orquestación (nodo, iteración, propósito) y es estable entre reinicios; el Paso 1 usaba un contador en memoria que se perdía al reiniciar y se sustituyó en el Paso 2. El identificador de la fila es `uuid5(run_id, attempt, step)`. Antes de ejecutar, el gateway consulta todos los intentos anteriores del mismo paso:

- el **digest de los argumentos** debe coincidir: si difiere, `invocation_conflict` (no es un reintento, es otra llamada con el mismo nombre);
- el mismo paso dentro del mismo intento es `duplicate_invocation` (un bucle o un error, nunca un reintento);
- una fila `running` de un intento anterior significa que el proceso murió ejecutando: pasa a `uncertain`; una fila `pending` murió antes de ejecutar: pasa a `failed` (`abandoned_before_start`);
- para herramientas de clase `read` un reintento siempre ejecuta de nuevo; para cualquier otra clase, un paso con un intento `uncertain` responde `uncertain_effects` y uno ya `ok` responde `already_completed`, sin ejecutar.

Estados de una fila: `pending` (reclamada, sin ejecutar), `running`, `ok`, `failed`, `denied`, `invalid_input`, `uncertain`. Solo `pending` y `running` carecen de hora de cierre; la base lo exige con un `CHECK`. Si la auditoría no responde, no se ejecuta nada.

**Límites de las garantías.** La idempotencia depende de que la orquestación elija claves de paso deterministas: dos pasos distintos con la misma clave se verán como conflicto, y la misma operación con claves distintas se repetirá. Un `uncertain` no caduca solo ni se resuelve automáticamente; requiere que un operador compruebe el efecto (por ejemplo, si el commit existe) y lo registre, acción que llegará con las aprobaciones del Paso 4. Entre la fila `running` y el efecto Git real no hay transacción común: la auditoría dice "se intentó", no "ocurrió". Una herramienta de lectura cuyo hilo sobrevive al timeout puede seguir leyendo tras un resultado `timeout`.

### Ejecución síncrona en hilo, con límite de tiempo

Las herramientas son síncronas (como `LLMPort`) y el gateway las ejecuta en un hilo con `fail_after`. El hilo no puede matarse: al expirar, la tarea sigue y el hilo se abandona. Es aceptable para lecturas; la ejecución de programas no pasará por aquí sino por el `SandboxPort`.

## Decision (Paso 2): adaptador Git administrado y worktrees por ejecución

### Repositorios administrados por configuración confiable

`RepositoryConfig` (`app/git/ports.py`) es configuración del operador: identificador, proyecto, ruta, rama por defecto, referencias protegidas e identidad de servicio. El modelo solo nombra un repositorio por su identificador; nunca una ruta ni una URL. `RepositoryRegistry` exige que toda ruta sea absoluta, exista, parezca un repositorio Git y esté **dentro de una única raíz** (`GIT_REPOSITORIES_ROOT`); un enlace simbólico que salga de la raíz se rechaza. Puede cargarse de un JSON (`GIT_REPOSITORIES_FILE`).

### Operaciones nominales sobre el binario, sin shell y sin entorno heredado

`GitRepositoryPort` define operaciones con nombre: lectura (`head`, `current_branch`, `log`, `diff`, `status`, `rev_parse`, `branch_exists`), worktrees (`worktree_add`, `worktree_list`, `worktree_prune`) y contratos de escritura (`create_branch`, `commit`, `push`). `LocalGitAdapter` (`app/infrastructure/git/local.py`) construye cada lista de argumentos en la aplicación, con `--end-of-options` delante de cualquier valor influenciable y `--` delante de los pathspecs, sin shell, con `cwd` elegido por la aplicación y timeout. El entorno se sanea por completo: `HOME` vacío, `GIT_CONFIG_GLOBAL=/dev/null`, `GIT_CONFIG_NOSYSTEM=1`, `GIT_SSH_COMMAND` y `GIT_ASKPASS` apuntando a `false`, `GIT_TERMINAL_PROMPT=0`, sin `SSH_AUTH_SOCK`; hooks desactivados, diff externo y textconv desactivados, firma desactivada. Las referencias y rutas pasan por `app/git/refs.py`: sin guion inicial, sin `..`, sin `@{`, sin `.lock`; las etiquetas están siempre protegidas.

### Identidad de servicio por repositorio

Los commits salen con la `GitIdentity` configurada en el repositorio (por defecto `AgentForge <agentforge@noreply.agentforge.local>`), inyectada por variables `GIT_AUTHOR_*`/`GIT_COMMITTER_*` en cada llamada. La identidad personal del operador es invisible para el adaptador.

### Worktrees: uno por ejecución, bajo una raíz propia

`WorktreeManager` (`app/workspaces/manager.py`) mantiene `<WORKTREES_ROOT>/<project_id>/<run_id>/` con un marcador JSON (`worktree.json`) junto al checkout en `tree/`. El marcador queda fuera del árbol a propósito: nunca aparece en `git status` ni en un commit. `ensure` es idempotente: devuelve el worktree sano si existe, y reconstruye si hay un directorio sin marcador (creación parcial), un marcador sin checkout o un marcador que nombra otro repositorio. La rama es siempre `run/<run_id>`, creada desde la rama por defecto; si ya existe se reutiliza, de modo que una reanudación tras reinicio encuentra su trabajo. `release(status)` marca el fin de la ejecución y `sweep` elimina lo caducado: `WORKTREE_RETENTION_SECONDS` para ejecuciones terminadas y `WORKTREE_INTERRUPTED_RETENTION_SECONDS` para `interrupted`, que se conservan para la reanudación manual. Cuotas: `WORKTREE_MAX_PER_PROJECT` y `WORKTREE_MIN_FREE_BYTES`, comprobadas al crear. La rama `run/<run_id>` se conserva al eliminar el worktree: el historial no se pierde.

Aislamiento: los identificadores se validan antes de convertirse en componentes de ruta; toda ruta se resuelve y debe quedar exactamente a dos niveles bajo la raíz; un directorio de ejecución que sea un enlace simbólico no se confía ni se borra; al eliminar no se siguen enlaces, así que un enlace plantado hacia otro worktree o hacia el host no arrastra nada. Un marcador copiado a otro proyecto o con otro `run_id` no se reconoce. Las raíces de repositorios y de worktrees no pueden anidarse.

### Herramientas Git en el gateway

`git.log`, `git.diff` y `git.status` (clase `read`) exponen historial, diferencias y estado con argumentos nominales y acotados; `git.commit` (`write`) y `git.push` (`network`) existen como contratos para que política y auditoría ya los conozcan. Toda herramienta Git comprueba primero que `workspace_root` es un worktree administrado de **esta** ejecución y **este** proyecto, respaldado por un repositorio registrado del proyecto (`workspace_not_managed`, `workspace_mismatch`). Las concesiones por defecto dan `git.*` en clase `read` a Architect, Developer, QA y Security; la puerta global sigue en `read`, así que ni `commit` ni `push` se ejecutan aunque una concesión los nombre. El adaptador, además, rechaza `push` incondicionalmente en esta fase y rechaza escribir en cualquier rama que no sea `run/<run_id>`.

### Preparado para GitHub, sin implementarlo

La integración remota llegará como una GitHub App con permisos mínimos (contenidos y pull requests sobre repositorios concretos), con tokens de instalación de corta vida inyectados por invocación y nunca presentes en el entorno de los agentes. El `push` del adaptador y la rama protegida en el servidor serán las dos barreras.

## Consequences

- Hay una capa `app/tools/` que solo importa `contracts` y `security`; la orquestación la llamará y la infraestructura implementa su auditoría. Una prueba estática protege la dirección de dependencias.
- `create_app` construye el gateway en `lifespan` (`app.state.tool_gateway`) con el registro de solo lectura y la auditoría en PostgreSQL o en memoria. **Ningún agente lo invoca todavía**: el Planner y el Architect siguen trabajando solo con texto.
- Las herramientas reales de escritura, ejecución y red no existen; registrar una no basta para ejecutarla.

## Limitaciones conocidas tras el Paso 2

- La redacción es heurística: reduce fugas accidentales, no las elimina. La primera defensa es no leer archivos sensibles y no tener secretos en el workspace.
- El timeout no detiene el hilo de una herramienta de lectura bloqueada en E/S, ni mata un proceso `git` ya lanzado más allá de su propio timeout.
- `workspace_root` sigue asignándolo el llamador: el `WorktreeManager` existe y está cableado (`app.state.worktrees`), pero ningún nodo crea todavía un worktree al iniciar una ejecución ni lo libera al terminar; eso llega cuando un agente use herramientas.
- El marcador del worktree es un archivo en disco: un operador con acceso al almacenamiento puede alterarlo. Es la misma frontera de confianza que la base de datos y los repositorios base.
- Un commit en un worktree añade todo el árbol (`git add --all`) salvo que se pasen rutas; los archivos sensibles que el adaptador de lectura rechaza no están excluidos del commit. Hasta que la clase `write` se habilite hay que decidir una política de exclusión.
- El barrido de worktrees no está programado: `sweep()` existe y está probado, pero nadie lo invoca periódicamente todavía.
- `uncertain` requiere resolución manual y aún no hay API para ello.
- No hay endpoint HTTP de consulta de la auditoría de herramientas ni descripción de herramientas para el modelo en los prompts: llegan cuando un agente las use.

## Pending for the next steps

- Paso 3: `SandboxPort` con runner remoto en la VM Linux (límites de CPU, memoria, disco, tiempo, procesos y red; captura de stdout, stderr y código de salida); habilitar `exec` solo a través de él.
- Paso 4: aprobaciones humanas para operaciones sensibles reutilizando el flujo de interrupción, inyección de secretos por invocación, eventos de herramientas en la auditoría de la ejecución, este ADR a `accepted` y versión 0.5.0.
