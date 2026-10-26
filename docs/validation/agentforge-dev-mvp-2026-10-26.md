# Primera ejecución multiagente Developer + QA, 2026-10-26

Flujo ejecutado con `scripts/development_mvp.py` desde la Mac del operador,
sobre el repositorio desechable `sandbox-dev` (paquete `mailcheck`, tarea:
implementar `validate_email` según el README del fixture y añadir pruebas
unitarias; las pruebas de aceptación del fixture no son editables). Los
agentes actuaron como `agentforge:developer` y `agentforge:qa` a través del
Tool Gateway; cada programa se ejecutó en el Runner `agentforge-runner-01`
(Proxmox, Podman rootless); los checkpoints, la fila `runs`, los eventos y la
auditoría de herramientas quedaron en el PostgreSQL local del proyecto. Los
informes completos (JSON y parche) están fuera del repositorio, en
`~/.local/share/agentforge-dev-mvp/`.

## Estado previo

- Runner: `verified=true`, `ready=true`, `backend=podman-rootless`, 16 de 16
  controles del preflight con `refresh=true`; `401` sin credencial; inventario
  administrativo sin ejecuciones abiertas. El token de API local coincide con
  `.env` y su SHA-256 con el registro de la rotación del 25 de octubre; el token
  de API es rechazado (`401`) en los endpoints administrativos.
- Las ocho pruebas de aislamiento con Podman real se repitieron después, en la
  Fase 6.6, como UID 110 desde una sesión administrativa interactiva del
  operador (ver la sección final).

## Prueba en seco con modelo simulado (fontanería real)

`--fake-model --yes`, run `551d88ab-bbbd-4562-8ee6-31dd032c70a1`: estado
`approved`, revisión 0, 20 invocaciones (19 `ok`; la única `failed` es la
lectura auditada `path_not_found` del archivo de pruebas aún inexistente),
cinco ejecuciones en el Runner (`ruff_check` y `ruff_format_check` de
Developer; `ruff_check`, `ruff_format_check` y `pytest` de QA) todas
`finished/completed` con código 0 según la API del Runner; `pytest` informó
36 pruebas aprobadas dentro del contenedor; 5 checkpoints; eventos
`run_created`, `developer_completed`, `developer_applied`, `qa_reviewed`,
`run_approved`. El paso de lectura nunca llegó al Runner (`404` al consultarlo).

## Ejecución real 1 con Claude (Vertex, `claude-sonnet-4-6`)

Run `45ce36db-4ac7-4e9c-8a60-a5735c49dd96`. Resultado: `failed/correction_limit`
tras la revisión 3; ocho llamadas al modelo (41 331 tokens de entrada, 9 560 de
salida; coste estimado 0,27 USD con las tarifas de referencia; reserva
conservadora 3,66 USD de un techo de 8); 77 invocaciones (76 `ok`, una
`path_not_found`); 20 ejecuciones en el Runner confirmadas `finished/completed`;
17 checkpoints; 14 eventos. Ronda 0: `pytest` y `ruff_check` fallaron (QA
devolvió cuatro observaciones). Rondas 1 a 3: `pytest` 75 de 75 aprobadas y
formato limpio, pero `ruff_check` repitió `tests/test_validator.py:3:1: I001`.
Developer alternó el orden de los imports sin éxito; la causa real, comprobada
después con `ruff --diff` sobre una copia del worktree, era una línea en blanco
de más entre el bloque de imports y un comentario. QA diagnosticó mal la causa.
Conclusiones: el límite de correcciones y la regla "el fallo técnico prevalece
sobre el dictamen del modelo" funcionaron; la retroalimentación era pobre
porque la lista de programas del Runner fija `--output-format concise`.
Se reforzaron los prompts (citar la salida literal; cambiar de hipótesis ante
el mismo fallo; regla explícita para I001) y el Developer recibe ahora
`repeated_failures` cuando QA observa el mismo fallo en dos rondas seguidas.

## Ejecución real 2 con Claude (tras reforzar la retroalimentación)

Run `b2457586-50b2-4210-860c-baab6813a543`. Resultado: `approved` en la
revisión 0. Developer propuso dos archivos (implementación y
`tests/test_validator.py`); la primera respuesta del modelo no cumplió el
contrato JSON y el adaptador la reparó en un segundo turno (tres peticiones al
proveedor para dos llamadas lógicas, ambas contabilizadas). QA ejecutó
`ruff_check`, `ruff_format_check` y `pytest` en el Runner: los tres con código
0 y 55 pruebas aprobadas dentro del contenedor (aceptación más las propias del
Developer), y aprobó sin observaciones. Uso: 10 437 tokens de entrada y 3 320
de salida, coste estimado 0,08 USD (reserva conservadora 0,67 USD). 20
invocaciones (19 `ok`; la `failed` es la lectura `path_not_found` del archivo de
pruebas aún inexistente), 5 checkpoints, eventos `run_created`,
`developer_completed`, `developer_applied`, `qa_reviewed` y `run_approved`.

Confirmaciones independientes: consulta directa a PostgreSQL (fila `approved`,
sin lease, `idempotency_key=development:<run_id>`, `owner_subject`
`operator:development-mvp`, invocaciones por riesgo y agente: Developer 6
lecturas, 2 escrituras, 2 ejecuciones; QA 7 lecturas, 0 escrituras, 3
ejecuciones) y la API del Runner (las cinco ejecuciones `finished/completed`
con código 0, `ready=true`, inventario administrativo sin ejecuciones abiertas,
lo que descarta contenedores inciertos o residuales según el propio servicio).
El worktree se conservó para inspección en
`WORKTREES_ROOT/sandbox-dev/<run_id>/tree`; el parche tiene 1,7 KB y solo toca
los dos archivos autorizados.

## Qué quedó demostrado y qué no

Demostrado con agentes y modelo reales: Developer y QA colaboran a través del
Gateway con identidades de automatización separadas; la escritura queda
limitada a la lista autorizada y al worktree de la ejecución; cada programa se
ejecuta en el Runner remoto y el veredicto técnico prevalece sobre el modelo;
el bucle de corrección está acotado y termina con un código explícito; la
auditoría por paso, los eventos y los checkpoints quedan en PostgreSQL; el
Runner no retiene ejecuciones abiertas al terminar.

Demostrado solo con pruebas automatizadas (fakes): rechazo de rutas peligrosas,
enlaces y conflictos de hash en `write_file`; denegación fuera del alcance y
tras la caducidad; agotamiento de presupuesto; tiempo de pared con cancelación
en el sandbox; fallo del Runner; recuperación conservadora de una ejecución de
desarrollo interrumpida.

No demostrado en esta sesión: repetición de las ocho pruebas de aislamiento con
Podman real (requiere UID 110), concurrencia de varias ejecuciones, cancelación
externa durante una ejecución real, exposición del flujo por la API y uso sobre
un repositorio real.

## Comprobaciones finales

`ruff check .`, `ruff format --check .`, `mypy app`, `mypy runner/agentforge_runner`,
`mypy scripts/development_mvp.py`, `pytest -q` (537 unitarias) y
`pytest tests/integration -q` (68 contra PostgreSQL) en verde; `git diff --check`
limpio. No hubo commits ni push.

## Fase 6.6: verificación definitiva del Runner y estabilización

El operador ejecutó en la VM, desde una sesión administrativa interactiva y con
una única invocación delegada como el usuario del Runner (UID 110), dos
comprobaciones preparadas por AgentForge. La primera, de solo lectura, informó
`deployed_hashes_match=true`, `runner_service_active=true`, `podman_ps_ok=true`
y `residual_container_count=0`. La segunda repitió las ocho pruebas de
aislamiento con Podman real del repositorio tras comprobar los hashes de los
módulos instalados: `test_ruff_check_runs_inside_the_sandbox`,
`test_pytest_sees_no_network_and_a_read_only_root`,
`test_memory_limit_kills_the_process`,
`test_wall_clock_timeout_terminates_the_container`,
`test_kill_stops_a_running_container`, `test_work_quota_is_enforced_by_the_tmpfs`,
`test_each_container_gets_its_own_workspace` y
`test_no_containers_are_left_behind`, todas `PASS`, con
`real_podman_tests_passed=8` y `residual_container_count=0`. Desde la Mac, el
preflight con `refresh=true` volvió a aprobar los 16 controles con `ready=true`,
`rootless=true`, `backend=podman-rootless`, identidad esperada y `401` sin
credencial. No se modificaron permisos ni protecciones del sistema.

Cambios de la estabilización, todos cubiertos por pruebas automatizadas:

- Alcance de desarrollo revocable (`revoke()`), con rutas escribibles canónicas
  y acotadas validadas al construirlo; el script revoca al terminar, cancelar o
  rechazar.
- Cancelación de herramientas con efectos auditada como `uncertain/cancelled`
  (las de lectura siguen en `failed/cancelled`).
- Presupuesto que reserva todos los turnos posibles del adaptador (reparaciones
  incluidas) antes de la primera petición; tiempo por llamada al modelo nunca
  inferior al peor caso del adaptador.
- Migración 0005: `runs.workflow_kind` y estado terminal `completed`; downgrade
  reversible salvo que existan filas `completed`, en cuyo caso se niega sin
  reescribir datos. La recuperación decide por la columna, ya no por un prefijo
  en `idempotency_key`; `RunState` declara las claves de todos los flujos y el
  grafo de la API proyecta y reconstruye la auditoría de un checkpoint de
  desarrollo (probado con hilos terminados e interrumpidos).
- Diagnóstico de Ruff: programa `ruff_check_diff` (`ruff check --diff`, solo
  lectura, sin `--fix`) en las listas de la aplicación y del Runner, con la
  prueba de conformidad entre ambas; QA lo ejecuta con `--ruff-diff` cuando
  `ruff_check` falla y adjunta la corrección exacta. El Runner instalado aún no
  lo conoce: la preparación del despliegue (`scripts/stage_runner_deploy.py`,
  `scripts/deploy_runner_files.py`, self-test `scripts/runner_programs_selftest.py`)
  queda lista y requiere autorización administrativa.
- El script de rotación de tokens ya no contiene identificadores personales;
  recibe el administrador y el directorio privado como argumentos.

Comprobaciones finales de la Fase 6.6: `ruff check .`, `ruff format --check .`,
`mypy app`, `mypy runner/agentforge_runner`, `pytest -q` (552 unitarias) y
`pytest tests/integration -q` (70 contra PostgreSQL) en verde; `git diff --check`
limpio; sin secretos ni identificadores personales en el árbol. No hubo
commits ni push.

## Cierre: despliegue de `ruff_check_diff` en el Runner y validación

El operador autorizó el despliegue controlado de `protocol.py` y `programs.py`.
Preparación local con `scripts/stage_runner_deploy.py`: digests `base` iguales a
los instalados (`protocol.py` coincide con el registrado por el despliegue de
limpieza; `programs.py` con `HEAD`), `desired` iguales al árbol de trabajo,
compatibilidad con `podman.py` y `service.py` instalados; los cinco archivos
copiados a la VM con el usuario SSH tuvieron los mismos SHA-256 que los locales.
`backend.py` instalado sigue siendo la versión de `HEAD`. Evaluación de la
deriva: el árbol local solo cambia la anotación `remove() -> None` por
`-> bool` en la clase `Backend`, que es un `typing.Protocol` no
`runtime_checkable`; el servicio llama a `backend.remove(...)` sobre la
instancia concreta `PodmanBackend`, que ya devuelve `bool` en la versión
instalada, y las anotaciones de un Protocol no se evalúan ni se comprueban en
ejecución. Es una diferencia de tipado para `mypy`, no un riesgo funcional; se
alinea en el próximo despliegue completo del paquete.

El operador ejecutó una única invocación delegada como UID 110 del instalador
`deploy_runner_files.py install`, que informó `baseline_match=True`,
`payload_verified=True`, `runtime_path_verified=True`, `backup_verified=True`
(respaldo `20261026T095532Z`), `programs_selftest_ok=true` e
`install_applied=True; runner_service_active=True; config_unchanged=True`: solo
se reemplazaron los dos archivos autorizados, solo se reinició
`agentforge-runner.service`, y la unidad y `runner.env` conservaron su digest.

Verificación posterior desde la Mac: `verified=true`, `ready=true`,
`rootless=true`, `backend=podman-rootless`, identidad esperada, preflight 16 de
16 con `refresh=true`, `401` sin credencial e inventario administrativo sin
ejecuciones abiertas. Compatibilidad de listas: la prueba de conformidad
unitaria compara los prefijos de la aplicación con los del paquete del Runner, y
el self-test ejecutado en la VM comprobó los cuatro programas instalados.

Solo lectura comprobada contra el Runner real: peticiones construidas como las
de `exec.run` pero con `--fix`, con `--fix` sin `--diff` y con
`--unsafe-fixes` fueron rechazadas con `argv_rejected` (HTTP 422, ninguna
llegó a ejecutarse); la petición válida `ruff check --no-cache --diff -- src`
terminó `completed` con código 1 y el diff como salida. Además, el sandbox
trabaja sobre una copia empaquetada del worktree en un tmpfs: ningún programa
puede modificar el worktree del operador.

Prueba en seco con modelo simulado y Runner real sobre el nuevo repositorio
desechable `sandbox-lint` (paquete `textkit` con un `I001` deliberado:
miembros del import de `collections.abc` desordenados y dos líneas en blanco
antes de un comentario), run `5170c3a1-527b-4d68-831e-3a87fe4e31a1`: la
revisión 0 conservó la trampa, `ruff_check` de QA falló, QA ejecutó
`qa:0:ruff_check_diff` (el Runner confirma `finished/completed`, código 1), la
revisión 1 aplicó la corrección y el run terminó `completed` con 44
invocaciones auditadas (43 `ok`, una lectura `path_not_found` del archivo de
pruebas aún inexistente), 9 checkpoints y ningún `uncertain`.

## Ejecución real 3 con Claude: trampa `I001` y diff de Ruff (`sandbox-lint`)

Run `f03d2703-dac3-4caf-ae4d-2187b38437e7`, `--profile lint --ruff-diff`,
modelo `claude-sonnet-4-6`. Revisión 0: Developer añadió `slugify_many` y sus
pruebas, ordenó los miembros del import y eliminó el comentario, pero dejó dos
líneas en blanco entre el bloque de imports y la primera constante; `pytest` y
`ruff format --check` de QA terminaron en cero y `ruff check` en 1
(`src/textkit/slug.py:3:1: I001`). QA ejecutó `qa:0:ruff_check_diff` (el
Runner confirma `finished/completed`, código 1) y recibió el diff exacto
(eliminar una línea en blanco); su revisión citó la salida literal y la
corrección indicada por Ruff. Revisión 1: Developer resumió "remove the extra
blank line between the import block and the first module-level code", aplicó
el diff, y QA aprobó: `ruff check`, `ruff format --check` y `pytest` en cero,
18 pruebas aprobadas dentro del sandbox. Estado final `completed`, 4 llamadas
al modelo, 11 738 tokens de entrada y 2 095 de salida (coste estimado 0,07 USD,
reserva conservadora 2,64 USD), 45 invocaciones (44 `ok`, una lectura
`path_not_found` del archivo de pruebas aún inexistente), ningún `uncertain`,
9 checkpoints, eventos `developer_completed`, `developer_applied` y
`qa_reviewed` por revisión más `run_completed`.

Confirmaciones independientes: consulta directa a PostgreSQL (`status=completed`,
`workflow_kind=development`, sin lease, `qa:0:ruff_check_diff` con riesgo
`exec` y agente `qa`; Developer 14 lecturas, 4 escrituras, 4 ejecuciones; QA 16
lecturas, 7 ejecuciones), checkpoints con la revisión de QA y el `fix_preview`
tal como los vieron los agentes, y la API del Runner (`ready=true`, inventario
administrativo sin ejecuciones abiertas al terminar). El parche final (1 KB) solo
toca `src/textkit/slug.py` y `tests/test_slug_many.py`; los archivos de
aceptación no cambiaron.

## Compatibilidad de la migración 0005

Verificada por `tests/integration/test_migrations.py` (subida desde 0004 con
filas existentes, que quedan en `planning`; aceptación de `completed`; rechazo
de un `workflow_kind` desconocido; downgrade rechazado mientras existan filas
`completed`; downgrade posible tras resolverlas, con la restricción anterior
restaurada y la columna eliminada; subida repetible) y por
`test_models_match_the_migrated_schema_exactly`. Comprobación en vivo sobre la
base local de desarrollo, que contiene tres ejecuciones `completed`: `alembic
downgrade 0004` terminó con `downgrade rechazado: 3 ejecuciones en estado
'completed'...` y la base permaneció en `0005 (head)` con las restricciones
`ck_runs_run_status` (ocho estados) y `ck_runs_run_workflow_kind` intactas. La
API expone `workflow_kind` como campo adicional y `completed` como valor nuevo de
`status`; ningún valor existente cambia de significado.

## Estado al cierre de la Fase 6

`ruff check .`, `ruff format --check .`, `mypy app`, `mypy runner/agentforge_runner`,
`mypy` de los scripts de operador, `pytest -q` (556 unitarias) y
`pytest tests/integration -q` (70 contra PostgreSQL) en verde; `git diff --check`
limpio. Permisos globales de Developer y QA sin cambios (riesgo `read`, sin
`exec.run` ni escritura). Sin commits ni push.
