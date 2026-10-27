# 0009. Repositorios reales: política por proyecto, aprobación humana por etapas y publicación de Pull Requests

- Status: proposed (diseño aprobado por el usuario el 2026-10-26; Paso 1 implementado sobre repositorios `sandbox-*`; ningún repositorio real conectado; sin cambios de infraestructura)
- Date: 2026-10-26
- Deciders: martin, Claude (Fase 7)

## Context

La Fase 6 dejó un flujo Developer → QA que funciona de extremo a extremo con modelo, Runner y PostgreSQL reales, pero acotado por diseño (ADR 0008): solo lo lanza el operador local, solo sobre repositorios desechables `sandbox-*` bajo `GIT_REPOSITORIES_ROOT`, con la lista de archivos escribibles decidida a mano, sin `push` (el adaptador lo rechaza incondicionalmente) y sin ningún camino hacia un Pull Request. El objetivo de la Fase 7 es que AgentForge reciba una tarea sobre un repositorio real, la desarrolle y valide, y entregue un Pull Request revisable, sin tocar nunca `main` ni producción.

Lo que ya existe y se reutiliza tal cual: Tool Gateway con auditoría por paso (ADR 0006); identidad de automatización sin credenciales; `RepositoryRegistry` con raíz única y referencias protegidas; `LocalGitAdapter` sin shell, con entorno saneado y escritura solo en `run/<run_id>`; un worktree por ejecución con marcador fuera del checkout; `exec.run` en el Runner remoto con lista de programas fija (ADR 0007); `DevelopmentScope` revocable, `workspace.write_file` acotado y el grafo con tres correcciones, presupuesto y fin explícito (ADR 0008); aprobaciones humanas durables con `interrupt()` y `run_approvals` (ADR 0004, 0005); `workflow_kind` y el estado `completed` (migración 0005).

### Brechas de seguridad identificadas

1. **Una sola decisión humana por ejecución.** `run_approvals` es única por `run_id`; el flujo real necesita al menos dos puertas: autorizar el alcance antes de escribir y autorizar la publicación.
2. **Sin credenciales remotas ni `push`.** No hay identidad hacia GitHub, no existe cómo refrescar un repositorio real ni cómo publicar una rama; el único camino "obvio" (credenciales en el entorno de los agentes) está prohibido.
3. **Alcance de escritura decidido por el operador.** No existe una política por proyecto (qué rutas pueden escribirse, cuáles nunca, cuántos archivos, qué programas, qué imagen), ni un paso que la aplique antes de que un humano apruebe.
4. **Exclusiones por nombre, no por política.** `pack_worktree` y las herramientas de lectura excluyen nombres sensibles convencionales; un repositorio real puede guardar secretos o datos personales en rutas arbitrarias.
5. **Sin inspección del diff antes de publicar.** Nada impide publicar un cambio que incluya una credencial copiada del propio repositorio o inventada por el modelo.
6. **Identidad del remoto no fijada.** Un error de configuración podría hacer que un alcance autorizado para un repositorio empujara a otro.
7. **Texto del modelo hacia revisores humanos.** El cuerpo del PR es un canal de inyección hacia las personas que revisan; debe ir plantillado, escapado y marcado como generado.
8. **Dependencias de terceros.** Las pruebas de un repositorio real necesitan sus dependencias; el sandbox no tiene red, así que no puede instalarlas en el momento.
9. **Tamaño.** El archivo del worktree tiene un tope de 50 MB; un monorepositorio lo supera.
10. **Permiso `repos:write` sin uso.** No está definido quién puede crear ejecuciones que escriban en repositorios reales ni quién puede aprobar su alcance y su publicación.
11. **Publicación no idempotente.** Una caída entre el `push` y la creación del PR debe terminar en un único PR, nunca en dos ni en ninguno sin rastro.
12. **Puerta de riesgo global.** La API comparte un gateway de solo lectura; una ejecución de desarrollo por la API necesita su propia política por ejecución, sin ampliar la global.

## Decisiones del usuario (2026-10-26)

1. **Repositorio de prueba**: `agentforge-sandbox-demo`, privado y desechable, distinto de AgentForge y Clivana; no se crea ni conecta hasta el Paso 2. El Paso 1 usa los repositorios locales `sandbox-*`.
2. **GitHub App dedicada**: aprobada; runbook, permisos mínimos y configuración en el Paso 2; sin credenciales ni clave privada todavía.
3. **Publicación**: `manual` es la única modalidad admitida en esta fase; `after_qa` queda reservada y la política la rechaza. Abrir un PR, incluso en borrador, exige aprobación humana explícita.
4. **`repos:write`**: exclusivo de humanos autorizados (`project_operator`) y administradores dentro de sus proyectos; Developer y QA no lo reciben ni reciben credenciales de GitHub. La escritura dentro del worktree (`repos:write`) se distingue de la publicación (`repos:publish`, sin rol todavía).
5. **Migración 0006**: aditiva; `run_decisions` con etapas `plan`, `scope` y `publish`; `run_approvals` se conserva y se sigue escribiendo; metadatos de publicación preparados. Downgrade: restaura la restricción de estados de 0005, elimina `pending_stage`, `approval_deadline`, `publication` y la tabla `run_decisions` (las decisiones `scope` y `publish` se pierden; las `plan` sobreviven en `run_approvals`), y **se niega sin reescribir datos mientras exista alguna ejecución `cancelled` o `expired`**, indicando cuántas; el operador debe resolverlas o eliminarlas explícitamente antes de bajar. Comprobado con ambas condiciones por separado y juntas en `tests/integration/test_development_governance.py`.

## Options considered

- **Publicación por los agentes** (`git.push` como herramienta con credenciales en el entorno): descartada. Rompe la regla de que los agentes nunca tienen credenciales y convierte una inyección de prompt en un `push`.
- **Edición remota por la API de GitHub** (crear blobs, árboles y commits sin Git local): posible más adelante como alternativa al `push`, pero duplica el modelo de worktrees, pierde el historial local y complica la validación en el Runner. Se descarta para esta fase.
- **Clonar el repositorio real en cada ejecución**: lento y costoso con repositorios grandes; descartado en favor de un espejo local refrescado.
- **Publicación por un componente de plataforma** tras aprobación humana, con credenciales de corta vida inyectadas por invocación y nunca visibles para los agentes: **elegida**.

## Decision

### 1. Política por proyecto (`app/projects/`)

Un archivo de configuración del operador (`PROJECTS_FILE`, JSON validado con Pydantic y `extra="forbid"`) describe cada proyecto y sus repositorios: identificador, remoto fijado (`provider=github`, `owner`, `name`, `installation_id`), rama por defecto, referencias protegidas, `authorized_test_paths`, **globs escribibles** y **globs prohibidos** (por defecto prohibidos `.github/**`, `**/*.lock`, `Dockerfile*`, `**/secrets*`, `**/*.pem`, `**/.env*` y la configuración del propio proyecto), máximo de archivos y bytes por ejecución, perfil de imagen del sandbox, subconjunto de rutas que viaja al sandbox, presupuesto por ejecución y política de publicación (`manual` por defecto, `after_qa` opcional; rama base del PR; etiquetas; revisores; siempre PR en borrador, nunca fusión automática). La política es dato de confianza: se carga en `lifespan`, nunca llega al modelo y solo el operador la cambia. Los proyectos `sandbox-*` de la Fase 6 son un caso particular con remoto ausente.

### 2. Decisiones humanas por etapa (migración 0006)

Nueva tabla `run_decisions(run_id, stage, approved, decided_by, decided_at, applied_at)` con unicidad por `(run_id, stage)`; las filas de `run_approvals` se copian como etapa `plan` y la tabla antigua se conserva en esta fase como lectura de compatibilidad. Etapas del flujo de desarrollo: `scope` (autoriza qué archivos puede escribir Developer y con qué presupuesto) y `publish` (autoriza empujar la rama y abrir el PR). Los estados `waiting_approval` y `resume_pending` se reutilizan; la etapa pendiente vive en el payload del `interrupt()` y en la fila. `POST /api/v1/runs/{id}/approve` admite `stage`; si falta se aplica a la etapa pendiente y si no coincide responde `409`. Separación de funciones como hoy: el creador no aprueba en `staging`/`production`. Nueva columna `runs.publication` (JSONB: rama remota, `head_sha`, número y URL del PR, fecha) para que la API exponga el resultado.

### 2b. Atribución de eventos

Cada evento distingue quién pidió, quién autorizó y qué identidad ejecutó. `actor_subject` conserva su significado histórico, el humano que causó el evento directamente (`run_created`, `approval_recorded`, `approval_applied`, `run_approved`/`run_rejected`, `run_cancelled`, `run_resumed`), y es nulo para los eventos de nodo y del sistema. El payload añade `initiated_by` en `run_created`, `approved_by` o `rejected_by` en las decisiones y en los eventos terminales causados por una decisión (`run_completed`, `run_failed`, `run_approved`, `run_rejected`), `cancelled_by` en `run_cancelled` y `executed_by` (`agentforge:<agente>`) en todo evento de nodo, también en los reconstruidos desde el historial. Los eventos de Developer y QA de una reanudación ya no se atribuyen al operador que aprobó el alcance. Los eventos anteriores a este cambio no tienen esas claves y siguen siendo válidos; la API expone `initiated_by`, `approved_by` y `executed_by` como campos opcionales junto a `actor`.

### 2c. Una política endurecida gana a una aprobación anterior

Antes de cada ronda de escritura, el alcance aprobado se vuelve a comprobar contra la política vigente del repositorio (`check_against_policy`: forma canónica, nombres sensibles, globs prohibidos y escribibles). Si el operador restringió la política después de la aprobación, la ejecución termina con `scope_outside_policy` sin escribir nada.

### 3. Flujo de desarrollo v2

`START → scoper → approval(scope) → developer → apply_changes → qa → (revise ≤ 3) → report → approval(publish) → publish → END`.

- **`scoper`** (nuevo agente, solo herramientas de lectura: `workspace.list_dir`, `find`, `read_file`, `git.log`) propone a partir de la tarea la lista de archivos a modificar o crear, las pruebas a ejecutar y una justificación; el orquestador la recorta a los globs escribibles del proyecto, rechaza los prohibidos y acota el número de archivos. Nada de esa propuesta se ejecuta hasta la aprobación.
- **`approval(scope)`**: `interrupt()` con la propuesta ya filtrada; el humano la aprueba o la rechaza (fin `rejected`). Solo entonces el orquestador construye el `DevelopmentScope` **a partir de la lista aprobada**, nunca de una respuesta posterior del modelo. El alcance caduca con el tiempo de pared y se revoca al terminar.
- **Developer, apply, QA, revise**: el grafo de la Fase 6 sin cambios de fondo. `workspace.write_file` y las herramientas de lectura aplican además los globs del proyecto (defensa en profundidad), y `pack_worktree` solo empaqueta el subconjunto configurado.
- **`report`** compone el informe de la ejecución (tarea, resumen de Developer, veredicto de QA, programas con códigos de salida, archivos cambiados, uso y coste, identificador de la ejecución) y ejecuta el **escaneo del diff**: patrones de credenciales, claves privadas y entropía alta bloquean con `secret_suspected`; el escaneo es código de plataforma, no un dictamen del modelo.
- **`approval(publish)`**: segundo `interrupt()` con el informe y el parche; con `publish=after_qa` se omite solo si el proyecto lo configuró así y QA aprobó con todos los programas en cero.
- **`publish`**: nodo de plataforma, no herramienta de agente (ver 5). Fin `completed` con `publication` relleno; cualquier fallo deja un código explícito y el worktree conservado.

Las ejecuciones de desarrollo se crean por la API (`workflow_kind=development`, `repository_id`) y requieren el permiso `repos:write` además de `runs:create`; `repos:write` lo concede un rol nuevo `developer` y `admin` dentro de sus proyectos. La política de herramientas de la ejecución se construye por ejecución (`DevelopmentToolPolicy`), como hace hoy el script; el gateway global de la API sigue en `read`.

### 4. Repositorios remotos y credenciales (`app/infrastructure/github/`)

- **GitHub App** con permisos mínimos: `contents: write`, `pull_requests: write`, `metadata: read`, instalada solo en los repositorios configurados y **sin** permiso `workflows`, de modo que GitHub rechaza cualquier cambio bajo `.github/workflows` aunque los globs fallaran. `GITHUB_APP_ID` y `GITHUB_APP_PRIVATE_KEY_FILE` (modo 0600, fuera del repositorio) en Settings; el `installation_id` vive en la política del repositorio. `InstallationTokenProvider` firma el JWT de la App con PyJWT (ya dependencia) y obtiene tokens de instalación de una hora, restringidos al repositorio; se guardan solo en memoria del proceso de la API.
- **Espejo local**: cada repositorio real es un clon `--bare` bajo `GIT_REPOSITORIES_ROOT/<project>/<repo>.git` cuyo remoto `origin` apunta a la URL HTTPS canónica derivada de `owner/name` (nunca de un dato del modelo ni de la petición). `MirrorService.refresh()` hace `fetch` de las ramas antes de crear el worktree, bajo un cerrojo consultivo de PostgreSQL por repositorio. Los worktrees se crean desde la rama por defecto del espejo, como hoy.
- **Credenciales por invocación**: `fetch` y `push` reciben el token mediante un ayudante `GIT_ASKPASS` que lo lee de un archivo temporal 0600 creado para esa invocación y borrado al terminar; nunca en `argv`, en la URL ni en el entorno heredado. El adaptador que usan los agentes conserva `GIT_ASKPASS=false` y `GIT_SSH_COMMAND=false`: estructuralmente no puede autenticar.
- **`push` real** solo en el `Publisher`: refspec explícito `run/<run_id>:refs/heads/agentforge/run-<run_id>`, sin `--force`, con `--no-verify`; las referencias protegidas del proyecto siguen vetadas en el adaptador y la rama por defecto queda además protegida en GitHub (PR obligatorio, revisión, comprobaciones de estado, la App sin derecho a saltárselas).

### 5. Publisher

Componente de plataforma invocado por `RunService` bajo el lease de la ejecución, después de la decisión `publish`. Pasos idempotentes y auditados como eventos (`publication_requested`, `branch_pushed`, `pull_request_opened`, `publication_failed`): confirma el worktree en `run/<run_id>` con la identidad del bot y un mensaje plantillado; empuja la rama; busca un PR abierto con esa rama de cabecera y, si no existe, lo crea **en borrador** con cuerpo plantillado (texto del modelo escapado y delimitado, aviso de generado por AgentForge, identificador de la ejecución, enlace a la auditoría), etiquetas configuradas y sin fusión automática; persiste `runs.publication`. Una caída entre pasos se recupera por la búsqueda por rama y por el `head_sha` registrado. El Publisher no es una herramienta del gateway: ningún agente puede nombrarlo.

### 6. Imágenes por proyecto y aislamiento

Cada repositorio real declara un perfil de imagen del Runner con sus dependencias preinstaladas, construido por el operador y fijado por digest (el Runner ya exige digests); el sandbox sigue sin red. Worktrees por ejecución sin cambios; cuota y retención por proyecto; límite de ejecuciones concurrentes por repositorio.

### 7. Lo que no cambia

Los agentes siguen sin elegir herramientas ni argumentos fuera de los esquemas cerrados; `exec.run` conserva la lista de programas fija; el Runner no se modifica en esta fase salvo por nuevas imágenes de perfil; ninguna credencial humana ni de la App entra en un prompt, un checkpoint, un evento ni una fila de auditoría.

## Plan por etapas

**Paso 1 — Política por proyecto, etapas y API (sin remotos). Implementado.** Entregado en `feat/phase-7-real-repositories`:
`app/projects/` (política por repositorio con globs escribibles y prohibidos, límites, `manual` como única publicación; globs anclados propios; filtro de alcance con denegación por defecto y digest), `app/contracts/scope.py`, agente Scoper de solo lectura (`AgentKind.SCOPER`, sin `exec.run` registrado en su gateway), grafo gobernado `app/orchestration/scoped_development.py` (Scoper → `interrupt` de alcance → Developer/QA de la Fase 6 reconstruido por nodo desde el checkpoint), `DevelopmentScope` construido solo desde la lista aprobada, migración 0006, estados `cancelled` y `expired`, `RunService` con grafos por flujo, decisiones por etapa ligadas al digest y con caducidad, cancelación, listado de decisiones y liberación del worktree al terminar; API: `POST /runs` con `workflow_kind` y `repository_id`, `POST /runs/{id}/approve` con `stage` y `scope_digest`, `GET /runs/{id}/decisions`, `POST /runs/{id}/cancel`; rol `project_operator`, permisos `runs:cancel`, `repos:write` y `repos:publish` (este último sin rol). El flujo termina en `completed` tras QA: la etapa `publish` llega en el Paso 3. Criterios y evidencia en `docs/validation/fase7-paso1-criterios.md`.

Diseño original del paso: `app/projects/` con esquema y carga; globs aplicados en `DevelopmentScope`, lecturas, escritura y `pack_worktree`; migración 0006 (`run_decisions`, `runs.publication`); `scoper` y las dos puertas en el grafo; creación y aprobación por etapas por la API con `repos:write`; política de herramientas por ejecución desde `RunService`. Criterios de aceptación: una ejecución creada por la API sobre `sandbox-lint` se detiene en `scope`, el alcance aprobado se convierte en el `DevelopmentScope`, Developer no puede escribir fuera de él ni de los globs, el flujo termina en `publish` pendiente y una decisión negativa lo cierra `rejected`; pruebas unitarias e integración de la migración (arriba y abajo) y de la separación de funciones; el gateway global de la API sigue en `read`.

**Paso 2 — Espejos y GitHub App en solo lectura.** `InstallationTokenProvider`, `MirrorService`, `fetch` con `GIT_ASKPASS` por invocación, identidad del remoto fijada, cerrojo por repositorio. Criterios: un repositorio de prueba del usuario en GitHub (no Clivana) se refleja y refresca; el token no aparece en logs, auditoría, checkpoints ni `ps`; un `owner/name` distinto al configurado se rechaza; pruebas con una API de GitHub simulada y una comprobación real de solo lectura.

**Paso 3 — Publisher y Pull Request.** `push` restringido y `Publisher` idempotente; escaneo del diff; cuerpo del PR plantillado; `runs.publication` expuesto. Criterios: sobre el repositorio de prueba, una tarea termina en un PR en borrador con el informe, la rama `agentforge/run-<id>` y ningún cambio en `main`; un intento de empujar a `main` lo rechaza el adaptador antes de la red y GitHub lo rechazaría por protección de rama; una caída simulada entre `push` y creación del PR produce un único PR; un diff con una credencial sembrada queda bloqueado con `secret_suspected`.

**Paso 4 — Endurecimiento y primer repositorio real.** Imagen de perfil con dependencias, límites de concurrencia, retención, runbook de alta de un repositorio (App, protección de rama, política), revisión de seguridad y ADR aceptado. Solo entonces, con autorización explícita, se da de alta Clivana con una tarea pequeña y `publish=manual`.

## Riesgos

- **Credenciales de la App**: la clave privada en el host de la API es el activo más sensible; mitigación: archivo 0600 fuera del repositorio, tokens de instalación de una hora, permisos mínimos, rotación documentada, nunca en el entorno de los agentes ni del Runner.
- **Inyección desde repositorios reales**: más superficie que en los fixtures; mitigación: salidas con esquema cerrado, alcance aprobado por humano antes de escribir, PR en borrador revisado por humano, cuerpo del PR escapado.
- **Fuga de secretos del propio repositorio**: mitigación: globs prohibidos, redacción en lecturas, escaneo del diff antes de publicar, exclusión del subconjunto que viaja al sandbox.
- **Deriva del espejo**: una rama base desactualizada produce conflictos en el PR; mitigación: `fetch` antes de cada ejecución y rebase manual por el humano, nunca automático en esta fase.
- **Coste y tiempo**: repositorios grandes multiplican tokens y archivos; mitigación: subconjunto configurado, presupuesto por proyecto, `max_files_per_run`.
- **Dependencias**: imágenes por proyecto requieren mantenimiento; mitigación: digests fijados y runbook.
- **Compatibilidad**: la migración 0006 copia `run_approvals` a `run_decisions`; el downgrade conserva la tabla antigua y elimina la nueva, documentado como en 0005.

## Fuera de alcance

Fusión automática, despliegues, proveedores distintos de GitHub, comentarios de agentes en PR, revisión por agentes de seguridad (Fase 8+), paralelismo entre agentes.
