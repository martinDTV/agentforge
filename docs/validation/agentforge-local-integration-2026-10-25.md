# Integración local de AgentForge, 2026-10-25

## Estado observado

- MacBook Apple Silicon: Colima activo con runtime Docker; Docker CLI usa el contexto
  `colima`. `docker compose` 5.6.1 funciona después de sustituir el enlace roto
  `~/.docker/cli-plugins/docker-compose` por el ejecutable de Homebrew.
- PostgreSQL del proyecto: solo el servicio `db` de `docker-compose.yml` está
  iniciado. Publica `127.0.0.1:5434` y su healthcheck está sano. La contraseña
  aleatoria y `DATABASE_URL` están únicamente en `.env`, ignorado por Git y con
  modo `0600`. Los PostgreSQL existentes en 5432 y 5433 no se tocaron.
- Esquema de negocio: Alembic `0004 (head)`. Checkpoints de LangGraph: `v9`.
  La conexión de AgentForge y `verify_schema` pasaron contra esta base.
- La aplicación FastAPI local inició con su configuración real y respondió
  `200` en `/health/live` y `/health/ready` mediante `TestClient`.
- Repositorio bare desechable `sandbox-demo.git` y registro de confianza bajo
  `~/.local/share/agentforge-sandbox-smoke/`, fuera del repositorio de AgentForge.
  El fixture contiene `src/app.py` con un `F401` intencional y no tiene remoto.
- `Settings` local valida con `AUTH_MODE=oidc` y un JWKS de desarrollo bajo
  `.local/`, ignorado por Git. La clave privada tiene modo `0600`; no se
  desactivó la autenticación de la API.
- VM: Tailscale `Running`, nodo en línea. El Runner está activo y escucha en
  la dirección del tailnet del Runner, puerto `8787`,; la petición sin credencial recibe `401`. El gestor
  `user@110.service` delega `cpu`, `memory` y `pids`.

## Rotación y preflight remoto

Los dos tokens del Runner que estuvieron en una conversación se consideran
comprometidos. No se recuperaron ni se usaron. Se generaron dos tokens nuevos
en `~/.local/share/agentforge-rotation-20261025/` (`0700`, archivos `0600`),
fuera de Git. Solo sus hashes se copiaron a la VM. El operador ejecutó el
[script de rotación](../../scripts/runner_token_rotation.py) con `sudo` en una
sesión SSH interactiva y obtuvo `rotation_applied=True` y
`runner_service_active=True`. El script reemplazó únicamente las dos entradas
`RUNNER_*_TOKEN_SHA256`, reinició únicamente `agentforge-runner.service` como
UID 110 y eliminó el archivo temporal de hashes. No hubo cambios a `sudoers`.
La Mac incorporó el nuevo token de API en `.env` (`0600`); el token de
administración permanece en su archivo local privado.

`RemoteSandboxAdapter.health()` devolvió `verified=True`, `rootless=True`,
`ready=True`, `backend=podman-rootless` y el ID esperado
`agentforge-runner-01`. Una consulta autenticada con `refresh=true` devolvió
HTTP `200` y aprobó todas las comprobaciones del Runner:
`unprivileged_user`, `rootless`, `cgroups_v2`, `cgroup_controllers`,
`podman_version`, `image:python-3.13`, `probe:uid_unprivileged`,
`probe:root_readonly`, `probe:work_writable`, `probe:work_quota`,
`probe:network_blocked`, `probe:no_capabilities`, `probe:no_new_privs`,
`probe:memory_max` y `probe:pids_max`.

## Prueba de humo del Tool Gateway

`scripts/sandbox_smoke.py` permite solo `ENVIRONMENT=local`, proyecto de prueba
`sandbox-*`, repositorio registrado y `ruff_check`. Construye un Tool Gateway
con una política de operador temporal que concede `exec.run` a Developer solo
para esa invocación. Exige identidad y disponibilidad del Runner antes de
enviar el trabajo; el Runner real no se sustituye por ejecución local.

La ejecución real (`run_id=b3ff0eb5-c62e-48ca-a3a7-f08e31260755`,
`invocation_id=b25b3ba6-36af-586d-90ab-8f2cdb750e9d`) devolvió
`outcome=ok`, `status=completed`, código de salida `1` y
`src/app.py:1:8: F401` por el import sin uso. El Runner confirmó en su propio
registro `state=finished`, programa `ruff_check`, resultado `completed` y
código `1`.

La segunda llamada con el mismo paso devolvió `denied/duplicate_invocation`
sin una tercera fila de auditoría. La ruta `../etc` devolvió
`failed/path_invalid`: el Runner respondió `404` al consultar el ID de esa
invocación, prueba de que no llegó a él. El script informó
`worktree_removed=true`.

Una consulta independiente a PostgreSQL encontró exactamente dos filas para
el run: `smoke:ruff_check` en `ok` con digest de salida y
`smoke:path_rejected` en `failed/path_invalid` sin digest de salida. La fila
`runs` quedó en `failed/sandbox_smoke`, sin lease activo, tal como exige la
prueba de operador. La API del Runner informó cero ejecuciones `running`,
`pending` y `uncertain`. Una consulta directa de solo lectura ejecutada por el
operador como UID 110, desde `/tmp`, devolvió salida vacía dos veces para
`podman ps -a --filter 'name=^agentforge-'`: no quedaron contenedores de
prueba, ni activos ni detenidos. La política global conservó únicamente el riesgo
`read`; aunque la API registra `exec.run`, Developer y QA no pueden invocarlo
(`tool_not_granted`).

## Comprobaciones realizadas

- El 25 de octubre, `pytest tests/unit`: 500 aprobadas.
- `pytest tests/integration`: 66 aprobadas contra bases temporales del servidor
  PostgreSQL local; las bases de prueba se eliminaron al finalizar.
- Ruff: `ruff check .` aprobado; formato de los archivos modificados aprobado.
- `git diff --check`: aprobado.

## Despliegue y nueva validación, 2026-10-26

La revisión detectó que `PodmanBackend.remove()` ignoraba el código de salida
de `podman rm`; `list_containers()` trataba un fallo de `podman ps` como una
lista vacía. Se corrigieron ambos casos. Un fallo al limpiar el contenedor
del probe invalida el preflight; un fallo durante una ejecución la registra
como fallida y mantiene `ready=false` hasta la reconciliación al reiniciar;
un fallo al enumerar o eliminar contenedores durante el arranque impide
declarar el servicio listo. La reconciliación administrativa conserva el
estado `uncertain` si no logra eliminar el contenedor.

La comparación previa encontró una sola diferencia propia de la VM en
`podman.py`: siete líneas de diagnóstico para un fallo de `podman info`. Su
SHA-256 instalado era
`89c0de124cddd14610e3ae1063a58d0320be41343a78b4d9cc43baf0ef4bcd3f`,
frente a `962761effdace93f0d206e18ff48e5107e43fa3dbc84a77ed72adf86cf194133`
de la base Git local. `service.py` coincidía exactamente con la base Git local
(`ac90a13180dd6b604eeb3d02945873d6afa13c1fbffe6c46508b20a920271eee`).
El primer intento de instalación se detuvo antes de modificar archivos por
esta diferencia. Se comprobó que el parche aplicaba sin conflictos a la
versión instalada y que el resultado, tras darle formato, coincidía con los
dos archivos preparados localmente. El bloque de diagnóstico de la VM se
conservó.

El operador ejecutó la instalación como `agentforge-runner` (UID 110), sin
conceder privilegios permanentes. El instalador verificó el código de origen,
la ruta importada por el servicio y el payload, y respaldó ambos archivos en
`/home/agentforge-runner/.local/share/agentforge-runner/deploy-backups/20261026T080317Z`.
Reemplazó solo `podman.py` y `service.py` y reinició solo
`agentforge-runner.service`. Informó `install_applied=True`,
`runner_service_active=True` y `config_unchanged=True`: ni la unidad ni
`runner.env` cambiaron. La verificación posterior de los archivos instalados
devolvió `deployed_hashes_match=true`. Los SHA-256 finales coinciden con los
archivos locales: `2e9d64516ded57088403062cd34818eb61573c6a11132ac91be7de6771652356`
para `podman.py` y
`3dd1c4e66237699251a7058f38adf98481c1fa7c8bae4da2c38a4d86ffa7f1ee`
para `service.py`.

La prueba controlada ejecutada en la VM inyectó respuestas simuladas de
Podman: fallo de `rm`, fallo de `ps`, fallo de limpieza del probe y fallo de
limpieza de una ejecución. Devolvió `controlled_cleanup_failures_detected=4`;
no provocó errores reales del motor ni creó contenedores. Un preflight real
posterior aprobó los 16 controles, incluido `probe:cleanup`; informó
`ready=true`, `rootless=true` y `backend=podman-rootless`. El adaptador local
confirmó `verified=true` y el ID `agentforge-runner-01`.

La nueva ejecución real por el Tool Gateway tuvo
`run_id=3f9806b4-b445-458b-a4bc-e04770502ced` e
`invocation_id=df52fc36-8199-5186-86df-df577e5dc059`. `ruff_check`
devolvió `src/app.py:1:8: F401`, `status=completed` y código de salida `1`.
El duplicado fue `denied/duplicate_invocation`, la ruta `../etc` fue
`failed/path_invalid` y el worktree temporal se eliminó. La consulta
independiente a PostgreSQL encontró exactamente dos invocaciones: la de Ruff
en `ok` con digest de salida y la ruta rechazada en `failed/path_invalid`
sin digest. La fila `runs` quedó en `failed/sandbox_smoke` y sin lease activo.
El Runner confirmó la ejecución `finished/completed`, código `1`, y ninguna
ejecución `running`, `pending` o `uncertain`.

La comprobación directa de solo lectura como UID 110 devolvió
`podman_ps_ok=true` y `residual_container_count=0` para
`podman ps -a --filter 'name=^agentforge-'`: no quedaron contenedores activos
ni detenidos. Una nueva consulta al adaptador conservó `verified=true` y
`ready=true`. La política global siguió con solo el riesgo `read`; las
concesiones predeterminadas de Developer y QA no incluyen `exec.run`.

Tras el cambio, `pytest tests/unit tests/integration -q` aprobó 571 pruebas
(505 unitarias y 66 de integración). `ruff check .`, el control de formato
de los archivos modificados y `git diff --check` aprobaron. Docker mantuvo
el contexto `colima` y Compose mostró únicamente `db` activo. Un análisis de
los 214 archivos rastreados o no ignorados no encontró los secretos locales;
`.env` sigue ignorado por Git y con modo `0600`. No hubo commits ni push.

## Riesgos pendientes

Las ocho pruebas de integración con Podman real en la VM no se repitieron
como suite completa tras este despliegue; el preflight y la nueva ejecución
real de `ruff_check` sí se realizaron. La escucha vinculada a la IP de
Tailscale evita un socket en la interfaz LAN; esta comprobación no sustituye
una auditoría de reglas de firewall y ACL. Developer y QA aún requieren una
decisión de política explícita y pruebas adicionales antes de habilitarles
ejecución.
