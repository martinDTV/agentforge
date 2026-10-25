# 0007. SandboxPort y ejecución aislada en un runner Linux remoto

- Status: proposed (Fase 5, Pasos 3, 4A y 4B: contratos, adaptador remoto, `exec.run` y el servicio `runner/` con cuota por ejecución, sonda y reconciliación, probados con backend simulado; VM, Podman real y red privada no desplegados)
- Date: 2026-10-25
- Deciders: martin, Claude (Fase 5)

## Context

Los agentes necesitarán ejecutar linters y pruebas sobre el código que producen. Eso es ejecución de código no confiable: `pytest` importa y ejecuta todo lo que haya en el repositorio, incluidas pruebas o `conftest.py` escritos por un modelo. Decisiones del usuario:

- Sistema objetivo: Ubuntu Server 24.04 LTS en una VM dedicada de Proxmox; backend de aislamiento Podman rootless dentro de la VM.
- Comunicación privada por Tailscale; identidad de servicio **AgentForge Runner** con autenticación independiente; sin acceso del runner a credenciales personales, Proxmox, GitHub ni cloud.
- Red de cada sandbox deshabilitada por defecto. El host macOS nunca ejecuta código no confiable.

## Decision

### Contratos (`app/sandbox/`)

- `SandboxRequest`: identificador de ejecución (igual al identificador de la invocación del gateway, estable entre reintentos), ejecución, intento, paso, proyecto, agente, sujeto actor, repositorio, rama y `HEAD`, programa permitido, `argv` ya construido, perfil de imagen por nombre, `SandboxLimits`, `network="none"` (único valor que existe), entorno como lista cerrada de variables permitidas (sin prefijos `GIT_`, `SSH_`, `AWS_`, `GITHUB_`, `GOOGLE_`, `ANTHROPIC_`, `LD_`, ni `PYTHONPATH`), digest y tamaño del workspace y `untrusted=True` como literal: todo programa es código no confiable.
- `SandboxLimits`: CPU (segundos), memoria, procesos, tiempo de pared, almacenamiento escribible y tamaño máximo de salida, todos con cotas superiores e inferiores.
- `SandboxResult`: identificador, estado (`completed`, `timed_out`, `killed`, `cancelled`, `failed`, `rejected`), código de salida, `stdout`, `stderr`, marcas de truncado, duración, tiempos, identidad del runner, backend y uso de recursos.
- `SandboxPort` (Protocol síncrono, las herramientas corren en un hilo): `health()`, `run(request, archive)`, `cancel(execution_id)`.
- `WorkspaceArchive`: `tar.gz` determinista del worktree (metadatos fijos, entradas ordenadas, gzip sin fecha) sin `.git`, cachés, enlaces simbólicos ni archivos de credenciales; el contenido nunca se serializa en JSON y viaja como multipart.

### Programas permitidos (`app/sandbox/programs.py`)

No existe un campo "comando". El modelo elige un identificador (`ruff_check`, `ruff_format_check`, `pytest`) y opciones tipadas (rutas, `keyword`, `fail_fast`); la aplicación compone `argv` a partir de un prefijo fijo más rutas validadas: relativas, existentes bajo el worktree, sin `..`, sin forma de opción, sin caracteres de shell. `pytest` solo acepta rutas bajo las `authorized_test_paths` del repositorio y su `-k` pasa un patrón estricto; corre con `python -I -P -m pytest -p no:cacheprovider`. La lista es del runner también: el runner valida su propia copia y la suya es la autoritativa.

### `exec.run` (`app/tools/exec_tools.py`)

Herramienta de clase `exec`. Comprueba que el `workspace_root` es un worktree administrado de esta ejecución y proyecto, exige la referencia de invocación que el gateway sella en el contexto, construye el plan, empaqueta el worktree, crea la petición con los límites de la plataforma y la entrega al `SandboxPort`. Redacta `stdout` y `stderr` antes de devolverlos (el gateway vuelve a redactar y acota). Implementa `cancel`: al expirar el timeout del gateway se pide al runner que detenga el contenedor. Nadie la tiene concedida por defecto y la puerta global de riesgo sigue en `read`: aunque estuviera concedida, no se ejecuta.

### Adaptadores (`app/infrastructure/sandbox/`)

- `UnconfiguredSandbox`: el valor por defecto. Rechaza con `sandbox_unconfigured`. **No hay ejecución local de reserva**, ni en desarrollo ni en producción.
- `RemoteSandboxAdapter`: cliente `httpx` del runner. Solo acepta `https://` o `http://` hacia direcciones del tailnet (CGNAT `100.64.0.0/10` o `*.ts.net`). Ignora proxies y `.netrc` del host. Antes de la primera ejecución verifica por `/v1/health` que el runner declara el identificador esperado, backend `podman-rootless` y modo rootless; si no, `runner_unverified`. Envía el plan y el archivo como multipart con `Idempotency-Key` igual al identificador de ejecución; traduce 409 a `execution_in_progress`, 401/403 a `runner_auth_failed`, 422 a `runner_rejected`, 5xx a `runner_failed`, errores de transporte a `runner_unreachable`; un timeout de lectura cancela en el runner y responde `runner_timeout`. Rechaza resultados de otro runner o de otra ejecución.
- `FakeSandbox`: guionizado para pruebas; no ejecuta nada.

### Cancelación y reintentos

El gateway respeta el timeout que declara cada herramienta, acotado por `TOOL_MAX_TIMEOUT_SECONDS`, y al expirar llama a `cancel` con el mismo contexto. Dos reintentos no ejecutan a la vez la misma operación: la clave `(ejecución, paso, intento)` de la auditoría impide el segundo en el mismo intento; un intento anterior aún `running` deja el paso `uncertain` y, como `exec` no es `read`, el gateway no repite; y el runner rechaza con 409 un identificador de ejecución en curso.

### Gateway

La referencia de invocación se sella en el contexto por llamada (`ToolContext.invocation`), de modo que las herramientas con efectos tienen una clave de idempotencia sin que el modelo la vea ni la elija.

## Frontera de seguridad propuesta

Tres capas, de fuera adentro:

1. **API (este repositorio).** Identidad de automatización, concesiones por agente, puerta global de riesgo, validación de planes, worktree administrado, auditoría antes de ejecutar. Un modelo nunca produce `argv`, rutas absolutas, imágenes, límites ni variables.
2. **Red y runner.** Solo nodos del tailnet; ACL de Tailscale que permite únicamente API → runner en su puerto; el runner escucha solo en la interfaz de Tailscale; token de servicio propio del runner, distinto de cualquier credencial personal; el runner revalida la lista de programas y los límites.
3. **Contenedor.** Podman rootless con `--network none`, `--read-only`, el workspace en un `tmpfs` acotado (`--tmpfs /work:size=…,noexec?` no: `pytest` necesita ejecutar Python, no el workspace; `noexec` se aplica a `/tmp`), `--cap-drop ALL`, `--security-opt no-new-privileges`, `--userns` de rootless, `--pids-limit`, `--memory`, `--cpus` o `--cpu-quota`, `--timeout`, sin montar sockets de Podman ni Docker, sin volúmenes del host salvo el archivo extraído, sin `--privileged`, usuario sin privilegios dentro del contenedor.

**Lo que Podman rootless y la VM garantizan y lo que no.** Rootless significa que el demonio no existe y que el proceso del contenedor corre como un usuario sin privilegios de la VM con un mapa de `subuid`; un escape del contenedor da, como mucho, ese usuario en la VM, no `root`. `--network none` elimina interfaces: no hay exfiltración por red desde el sandbox. Los cgroups v2 hacen efectivos los límites de memoria, procesos y CPU; el tiempo de pared lo impone el runner matando el contenedor. Lo que **no** garantiza: un exploit del kernel de la VM compromete la VM entera (de ahí la VM dedicada, sin credenciales y sin acceso a Proxmox); los canales laterales y el consumo de recursos de la VM no están aislados entre ejecuciones concurrentes; una imagen maliciosa o un paquete envenenado en la imagen del perfil ejecuta con los permisos del sandbox; el runner es software nuestro y un fallo suyo es un fallo de la frontera. El token del runner concede exactamente ejecutar programas permitidos en el sandbox: su compromiso no alcanza repositorios, Proxmox ni cuentas.

## Estrategia de comunicación por Tailscale

- La VM y el host de la API son nodos del mismo tailnet, con etiquetas `tag:agentforge-api` y `tag:agentforge-runner`.
- ACL: `tag:agentforge-api` → `tag:agentforge-runner:8787` y nada más; el runner no inicia conexiones hacia la API.
- MagicDNS: `SANDBOX_RUNNER_URL=http://agentforge-runner.<tailnet>.ts.net:8787`; el adaptador solo acepta `http://` hacia el tailnet, donde WireGuard cifra y autentica el salto. Opcionalmente `tailscale cert` para HTTPS y `SANDBOX_RUNNER_CA_FILE`.
- Autenticación de aplicación independiente de la red: token de servicio del runner (`SANDBOX_RUNNER_TOKEN`), rotado desde la VM; el runner guarda solo su hash.
- Tailscale SSH desactivado para el usuario del runner; administración de la VM por una ruta separada.

## Plan de despliegue del runner en Proxmox (no ejecutado)

1. VM Ubuntu Server 24.04 LTS (cloud image), 4 vCPU, 8 GB RAM, 60 GB; sin agente de Proxmox con privilegios; sin pasar a la VM ninguna credencial del host.
2. Base: `unattended-upgrades`, `ufw` con denegación por defecto y solo `tailscale0`, `tailscale up` con la etiqueta `tag:agentforge-runner`, hora sincronizada.
3. Usuario `agentforge-runner` sin `sudo`, con `subuid`/`subgid` y `loginctl enable-linger`; `podman` del repositorio de Ubuntu; cgroups v2 (predeterminado en 24.04).
4. Imagen `agentforge/python-3.13`: Python 3.13, `ruff`, `pytest` y las dependencias del repositorio objetivo a partir de su `uv.lock`, construida con red solo en tiempo de construcción y publicada en el almacenamiento local del runner; una imagen por perfil.
5. Servicio runner: aplicación pequeña (FastAPI) como unidad `systemd --user` del usuario del runner, escuchando en la IP de Tailscale, con el protocolo v1 de este ADR, validación propia de programas y límites, ejecución con `podman run` y los flags de la frontera, limpieza del `tmpfs` al terminar, registro de cada ejecución.
6. Token: generado en la VM, hash en el runner, valor en el secreto de la API (`SANDBOX_RUNNER_TOKEN`), junto a `SANDBOX_RUNNER_ID` y `SANDBOX_RUNNER_URL`.
7. Verificación: `/v1/health` desde la API, una ejecución `ruff_check` sobre un repositorio de prueba, una `pytest` que intente abrir una conexión de red y falle, una que exceda memoria y sea `killed`, una cancelación.

## Consequences

- La aplicación tiene `app.state.sandbox`: `RemoteSandboxAdapter` si hay runner configurado, `UnconfiguredSandbox` si no. `exec.run` se registra solo cuando existen worktrees administrados.
- La suite unitaria bloquea cualquier subproceso que no sea `git`: una regresión que ejecutara `ruff`, `pytest` o `python` localmente falla de inmediato.
- `httpx` pasa a dependencia de ejecución.

## Limitaciones y riesgos conocidos

- El runner no existe todavía: este ADR fija su protocolo y su frontera; el código del runner, la imagen y la VM son un entregable aparte.
- `exec` no está habilitado para ningún agente; habilitarlo requiere concesiones explícitas y, para `write`, las aprobaciones del Paso 4.
- El archivo del worktree viaja completo en cada ejecución (límite `SANDBOX_MAX_ARCHIVE_MB`); repositorios grandes necesitarán sincronización incremental o montaje por rama en el runner.
- `uncertain` sigue requiriendo resolución manual; para ejecuciones efímeras es conservador por diseño.
- La verificación del runner se basa en lo que el runner declara más el token y la red; una atestación más fuerte (certificado del nodo, firma de resultados) queda para más adelante.
- Las pruebas usan un transporte simulado de `httpx`; ninguna prueba toca Tailscale, Proxmox ni Podman.

## Decision (Paso 4A): el servicio AgentForge Runner

El runner vive en `runner/` como paquete independiente (`agentforge-runner`, miembro del workspace de `uv`) y no importa nada de `app/`. Implementa el protocolo v1 con una copia propia de los contratos; una prueba de conformidad en la suite de la API comprueba que ambas copias se intercambian sin pérdida y que las listas de programas coinciden. Decisiones:

- **Lista autoritativa en el runner.** Recompone el `argv` esperado para el programa pedido y rechaza cualquier desviación (`argv_rejected`); comprueba que las rutas existen dentro del workspace extraído.
- **Autenticación por hash.** Guarda solo el SHA-256 del token y compara en tiempo constante; además restringe los orígenes a una lista de CIDR (tailnet y loopback). La dirección nunca es la única comprobación.
- **Extracción controlada.** Solo archivos regulares y directorios con rutas relativas normalizadas dentro del destino; rechaza absolutas, `..`, enlaces simbólicos y duros, dispositivos, FIFO, duplicados, miembros mayores que el límite, más entradas que el límite y más bytes descomprimidos que el menor entre el techo del runner y el `storage_bytes` de la solicitud; no aplica propietario, fecha ni bits especiales del archivo. Antes de extraer verifica tamaño y digest contra la solicitud.
- **Idempotencia persistente.** SQLite con una fila por identificador de ejecución reclamada con `INSERT OR IGNORE`; la misma solicitud devuelve el resultado guardado, una en curso responde `409 execution_in_progress`, un identificador reutilizado con otro contenido `409 execution_conflict`. Al arrancar, las filas abiertas pasan a `uncertain`, los contenedores `agentforge-*` huérfanos se eliminan y la solicitud repetida recibe `409 execution_uncertain`: nunca se relanza sola.
- **Forma fija del contenedor** (`podman.py`): red deshabilitada, raíz de solo lectura, `/tmp` en tmpfs acotado y `noexec`, workspace montado `noexec,nosuid,nodev`, `--userns=keep-id` con el uid sin privilegios del runner, todas las capacidades retiradas, `no-new-privileges`, límites de procesos, memoria sin swap, CPU, `ulimit cpu`, `--timeout` más un temporizador propio, sin `pull`, sin hooks del host, imagen fijada por digest. `preflight` exige rootless, cgroups v2 con `memory`, `pids` y `cpu`, Podman ≥ 4 e imágenes presentes; si no, `503 isolation_unavailable`.
- **Cancelación real.** `DELETE` mata el contenedor por nombre y marca la fila; el resultado se registra como `cancelled`. El temporizador de pared también mata el contenedor y el cliente de podman si se queda colgado.
- **Capacidad acotada.** Semáforo de `RUNNER_MAX_CONCURRENT`; por encima, `503 runner_busy`.

El adaptador de la API expone ahora los códigos del runner en 409 y 503 y acepta `http://` hacia loopback para una API y un runner en la misma VM.

### Resultados de pruebas, separados

- **Simuladas, ejecutadas**: backend guionizado y `podman info` guionizado en la suite unitaria: autenticación, conformidad del protocolo, validación, extracción, idempotencia, concurrencia, cancelación, timeouts, rechazo por aislamiento, forma del contenedor y un extremo a extremo del adaptador contra el servicio real por loopback.
- **Reales, NO ejecutadas**: `tests/runner_integration/` requiere Linux con Podman rootless y una imagen fijada por digest; fallan de forma explícita si falta algo. No se han ejecutado en este entorno.

## Decision (Paso 4B): cuotas efectivas, sonda, reconciliación con evidencia y portabilidad

- **Cuota de `/work` por ejecución.** En rootless no hay cuotas de proyecto ni montajes de bucle; la vía efectiva es un tmpfs creado por el motor con `tmpfs-size=storage_bytes`, al que el workspace llega por `stdin` y donde lo extrae un bootstrap fijo (`python -I -c`, `tarfile` con filtro `data`). El host ya no escribe el árbol no confiable: el runner solo lo **escanea** (`extract.py`) y rechaza lo mismo que antes. Consecuencia documentada: las páginas de tmpfs se cargan al cgroup de memoria, así que `memory.max = memory_bytes + storage_bytes`.
- **Sonda de aislamiento en el preflight.** Un contenedor con la forma real comprueba desde dentro uid, raíz de solo lectura, tmpfs y su tamaño, red bloqueada, capacidades a cero, `no_new_privs` y los límites del cgroup. Si la sonda falla, no se acepta trabajo. Es verificable en la máquina concreta y no sustituye a la suite real.
- **Ejecuciones inciertas con evidencia.** Al arrancar, el runner consulta al motor por cada fila abierta: contenedor terminado → cierre con su código de salida y OOM como evidencia; corriendo → se mata y queda `cancelled`; desaparecido → `uncertain`. Un operador, con una credencial distinta de la de la API, puede listar, reconciliar con evidencia del motor o declarar fallida; no existe la resolución manual como éxito.
- **Portabilidad.** El dominio y la orquestación no conocen Tailscale ni Proxmox (hay una prueba estática). El adaptador acepta `https://`, `http://` hacia loopback o redes superpuestas privadas y, solo fuera de producción y con opción explícita, `http://` hacia direcciones privadas de una VM local o una VPC. El runner acepta cualquier dirección privada de escucha, nunca la comodín, y recibe la lista de orígenes por configuración.
- **CI.** El workflow principal tipa el runner y ejecuta sus pruebas unitarias, etiquetadas como backend simulado. Un workflow manual (`runner-podman`) construye la imagen y ejecuta la suite real con Podman rootless; hasta que tenga una ejecución en verde, el aislamiento no está demostrado.

## Pending for the next steps

- Paso 4B: aprovisionar la VM, construir la imagen del perfil, aplicar la ACL de Tailscale y ejecutar la suite real; después aprobaciones humanas para habilitar clases de riesgo, resolución de invocaciones inciertas, inyección de secretos por invocación, eventos de herramientas en la auditoría de la ejecución, este ADR y el 0006 a `accepted`, versión 0.5.0.
