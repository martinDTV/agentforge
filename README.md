# AgentForge

Plataforma multiagente para planificación y revisión arquitectónica de tareas de desarrollo, con FastAPI, LangGraph, Claude (Anthropic) vía Vertex AI, aprobación humana y contratos Pydantic.

**Versión 0.3.0 — Fase 3 completada.** AgentForge planifica una tarea de desarrollo, revisa la arquitectura del plan con un ciclo acotado de retroalimentación entre dos agentes y detiene el flujo hasta una aprobación humana. Todo el estado vive en PostgreSQL y sobrevive a reinicios y caídas. **No ejecuta código, no usa herramientas y no toca repositorios**: todavía no es una plataforma de desarrollo autónomo — ver [Qué hace y qué no](#qué-hace-y-qué-no-hace-todavía), [Persistencia](#persistencia-fase-3) y [Roadmap](#roadmap-por-fases).

## Qué hace y qué no hace todavía

Implementado y verificado (197 pruebas unitarias sin servicios externos, 55 de integración contra PostgreSQL 16, ninguna llamada real a Claude):

- Grafo Planner → Architect → aprobación humana, con hasta `MAX_PLAN_REVISIONS` rondas de corrección y un presupuesto de llamadas por ejecución.
- Errores del LLM tipados, reintentos acotados y reparación de salidas malformadas; una ejecución nunca termina en 500 por un fallo del proveedor.
- Checkpoints de LangGraph en PostgreSQL con serialización estricta y cifrado opcional; `thread_id` igual al `run_id`.
- Proyección de cada ejecución a `runs`, decisiones humanas durables en `run_approvals`, auditoría estructurada en `run_events`.
- Leases con heartbeat y token de fencing, barrido de recuperación al arrancar y periódico, apagado ordenado, `Idempotency-Key` en la creación, errores HTTP como Problem Details.

Pendiente, por fase del roadmap:

- Autenticación, autorización, cancelación y reconducción manual de ejecuciones interrumpidas (Fase 4).
- Ejecución de herramientas en sandbox, agente desarrollador sobre worktrees, agentes de QA, seguridad y revisión (Fases 5 a 7).
- Varias instancias en producción con cercado de checkpoints, paralelismo entre agentes, observabilidad y despliegue (Fases 8 y 9).
- Ámbito por principal y caducidad de las claves de idempotencia; endpoint de auditoría; retención de checkpoints.

## Requisitos

- Python 3.11+ y `uv`
- PostgreSQL 16 accesible (local vía Homebrew o `docker compose up -d db`)
- Credenciales Application Default Credentials (ADC) de Google Cloud con acceso a Vertex AI
- Un proyecto de Google Cloud con el modelo Claude habilitado en Vertex AI Model Garden

## Inicio

```bash
cp .env.example .env
# editar .env: ANTHROPIC_VERTEX_PROJECT_ID, CLOUD_ML_REGION, CLAUDE_MODEL y DATABASE_URL
uv sync
createdb agentforge                 # o: docker compose up -d db
uv run alembic upgrade head                  # tablas de negocio (paso de despliegue)
uv run python -m scripts.setup_checkpoints   # tablas de checkpoints de LangGraph (paso de despliegue)
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
curl -s http://127.0.0.1:8000/health/ready
```

Abre http://127.0.0.1:8000/docs. La API está versionada bajo `/api/v1`.

## Flujo

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"task":"Diseña el módulo de pacientes en FastAPI con pruebas unitarias"}'
```

Copia el `run_id` que devuelve la API (código `201`). Opcionalmente envía `Idempotency-Key: <clave>` (hasta 128 caracteres): repetir la misma clave con la misma tarea devuelve la ejecución existente con `200` y `Idempotency-Replayed: true`, sin ejecutar nada; la misma clave con otra tarea responde `409 idempotency_key_reused`. La clave es global al despliegue y queda ligada al run aunque este falle.

```bash
curl -s http://127.0.0.1:8000/api/v1/runs/TU_RUN_ID

curl -s -X POST http://127.0.0.1:8000/api/v1/runs/TU_RUN_ID/approve \
  -H 'Content-Type: application/json' \
  -d '{"approved":true}'
```

El grafo ejecuta `planner -> architect -> approval` con retroalimentación:

1. Planner genera un `DevelopmentPlan`.
2. Architect lo revisa y produce un `ArchitectureReview` con `approved` e `issues`.
3. Si el Architect no aprueba, el plan vuelve al Planner con los `issues` como feedback. Esto se repite como máximo `MAX_PLAN_REVISIONS` veces y solo mientras quepa otra ronda en el presupuesto `MAX_LLM_CALLS_PER_RUN`.
4. En cualquier caso el flujo queda en `waiting_approval`: la persona siempre tiene la última palabra, con `architect_approved` y los `issues` visibles en la respuesta.

### Respuesta de un run

| Campo | Significado |
|---|---|
| `status` | `running`, `waiting_approval`, `resume_pending`, `interrupted`, `approved`, `rejected` o `failed` (ver tabla siguiente) |
| `revision` | Cuántas veces el Architect devolvió el plan al Planner (0 = primer plan) |
| `llm_calls` | Llamadas a agentes consumidas por el run |
| `architect_approved` | Veredicto del Architect sobre el plan actual; `null` si nunca llegó a ejecutarse |
| `plan`, `architecture` | Último plan y última revisión; `null` si el run falló antes de producirlos |
| `error` | Solo en `failed`: `{ "code", "message" }`, sin trazas ni payloads del proveedor |

Estados:

| `status` | Significado |
|---|---|
| `running` | Algún proceso tiene un lease vigente sobre el run y está conduciendo el grafo |
| `waiting_approval` | Pausa real en el nodo de aprobación humana |
| `resume_pending` | La decisión humana ya está registrada en la base de datos pero su aplicación al grafo aún no se confirmó. Repetir la misma decisión devuelve `200`; la contraria, `409` |
| `interrupted` | Hay trabajo pendiente, no hay interrupción y ningún lease vigente: el proceso que lo conducía desapareció. Recuperable desde el último checkpoint; el barrido lo reanuda solo si `RECOVERY_AUTO_RESUME=true` y quedan intentos |
| `approved` / `rejected` | Decisión humana aplicada. Terminal |
| `failed` | Error registrado. Terminal, con `error.code` |

Códigos de `error.code`:

| Código | Causa |
|---|---|
| `llm_timeout` | El proveedor no respondió dentro de `LLM_TIMEOUT_SECONDS` |
| `llm_unavailable` | Límite de tasa, sobrecarga, error de conexión o 5xx tras agotar los reintentos del SDK |
| `llm_request_rejected` | El proveedor rechazó la solicitud (credenciales, modelo, política de la organización) |
| `llm_malformed_output` | La respuesta no cumple el contrato Pydantic tras los intentos de reparación |
| `llm_budget_exceeded` | Un nodo intentó llamar al LLM con el presupuesto del run agotado |
| `internal_error` | Excepción inesperada dentro del grafo (también produce un `500`) |

Un run que falla dentro del grafo se crea igualmente (`201`) con `status: "failed"`; se puede consultar con `GET` pero no aprobar.

### Errores HTTP

Los errores se devuelven como [RFC 9457 Problem Details](https://www.rfc-editor.org/rfc/rfc9457) (`application/problem+json`) con un campo `code` estable:

| HTTP | `code` | Cuándo |
|---|---|---|
| `404` | `run_not_found` | El `run_id` no existe |
| `409` | `run_already_decided` | El run no está en `waiting_approval` |
| `422` | — | Validación de la petición (formato por defecto de FastAPI) |
| `503` | `database_unavailable` | PostgreSQL no responde (conexión rechazada, backend terminado, pool agotado). Lleva `Retry-After: 5`. Solo errores de conexión: un error de programación o de integridad sigue siendo `500` |
| `503` | `shutting_down` | La instancia está en apagado ordenado y ya no acepta trabajo nuevo. Lleva `Retry-After` |
| `409` | `run_lease_lost` | Otro proceso tomó la ejecución mientras esta instancia la conducía; el run sigue vivo en manos del nuevo propietario |
| `409` | `idempotency_key_reused` | Misma `Idempotency-Key` con una tarea distinta |
| `500` | `internal_error` | Error inesperado |

## Sin gasto en modelo externo

Define `USE_FAKE_MODEL=true` en `.env` y reinicia el servidor para usar un `FakeLLMAdapter` con respuestas fijas (sin red, sin consumo de tokens). Luego ejecuta:

```bash
uv run pytest -q
```

Ese comando ejecuta solo la suite unitaria (`tests/unit`), que no necesita ningún servicio externo. Las pruebas que requieren PostgreSQL viven en `tests/integration` y se ejecutan explícitamente:

```bash
uv run pytest tests/integration -q
```

Crean bases temporales `agentforge_test_<hex>` en el servidor de `TEST_DATABASE_ADMIN_URL` (por defecto `postgresql+psycopg://localhost:5432/postgres`) y las borran al terminar. Si no hay servidor accesible la suite **falla** con un mensaje explícito; nunca se salta en silencio.

La integración continua (`.github/workflows/ci.yml`) ejecuta los cinco comandos de verificación con un servicio PostgreSQL 16.10, aplica las migraciones y el setup de checkpoints como pasos de despliegue separados, y comprueba la ida y vuelta de las migraciones sobre una base limpia. Usa valores de prueba, nunca credenciales de Vertex AI.

Ninguna prueba llama a Vertex AI: una fixture automática hace fallar la construcción del cliente real durante todo pytest, y los agentes reciben siempre `FakeLLMAdapter` (ver `tests/conftest.py`). El adaptador de Vertex se prueba contra un cliente stub (`tests/unit/test_vertex_adapter.py`).

## Resiliencia frente al LLM

| Variable | Default | Efecto |
|---|---|---|
| `LLM_TIMEOUT_SECONDS` | `60` | Timeout por llamada en el cliente de Anthropic |
| `LLM_MAX_RETRIES` | `2` | Reintentos con backoff del SDK ante 429, 5xx y errores de conexión |
| `LLM_MAX_REPAIR_ATTEMPTS` | `1` | Veces que se le muestra al modelo su salida inválida y el error de validación para que la corrija |
| `MAX_PLAN_REVISIONS` | `2` | Rondas Planner ↔ Architect permitidas por run |
| `MAX_LLM_CALLS_PER_RUN` | `8` | Tope absoluto de llamadas a agentes por run |

La política completa está en `docs/adr/0002-politica-de-errores-reintentos-y-presupuesto-llm.md`.

## Persistencia (Fase 3)

Dos responsabilidades separadas sobre la misma base PostgreSQL:

| | Checkpoints de LangGraph | Estado de negocio |
|---|---|---|
| Qué guarda | Estado exacto del grafo por `thread_id`, para reanudar | Proyección del run, aprobaciones durables, auditoría |
| Tablas | `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations` | `runs`, `run_approvals`, `run_events` |
| Esquema | `scripts/setup_checkpoints.py` → `AsyncPostgresSaver.setup()` | Alembic (`migrations/`) |

Estado: Pasos 1 a 3 completados. El grafo se ejecuta sobre `AsyncPostgresSaver` con `thread_id` igual al `run_id`, y cada run se proyecta a `runs`, sus decisiones a `run_approvals` y su traza a `run_events`.

Consistencia entre checkpoint y proyección:

- La fila de `runs` se crea en `running` **antes** de ejecutar el grafo; un fallo posterior deja siempre algo que reconciliar, nunca un checkpoint huérfano.
- Escribir el checkpoint y actualizar la fila son transacciones distintas. La fila guarda `last_checkpoint_id`; cualquier lectura de un run no terminal compara ese valor con el checkpoint y, si difiere, reproyecta. Las filas terminales no se vuelven a tocar.
- Toda proyección es una actualización condicional: solo se aplica si la transición desde el estado almacenado está permitida (`app/contracts/lifecycle.py`). Una decisión registrada (`resume_pending`) nunca vuelve a `waiting_approval` aunque el checkpoint todavía lo diga.
- Nunca hay una transacción abierta mientras corre un agente o una llamada al LLM.

Aprobaciones durables: `POST /approve` ejecuta en una sola transacción la transición condicional `waiting_approval → resume_pending`, la inserción en `run_approvals` (única por run) y el evento `approval_recorded`. Solo después de confirmar se envía `Command(resume=...)`; `applied_at` se marca cuando el run quedó `approved` o `rejected`. Una caída entre ambas fases no pierde la decisión: `RunService.apply_pending_approval` la aplica más tarde (el barrido que lo invoca llega en el Paso 4).

Auditoría (`run_events`, clave única por run, tipo y ordinal, así los reintentos no duplican): `run_created`, `planner_completed`, `architect_reviewed`, `revision_requested`, `waiting_approval`, `approval_recorded`, `run_approved` / `run_rejected`, `approval_applied`, `run_failed`, `run_interrupted`, `run_resumed`. Los payloads contienen contadores, códigos y banderas; nunca prompts, planes, credenciales ni datos sensibles. Si el proceso murió entre un checkpoint y su evento, la recuperación reconstruye los eventos de nodo desde el historial de checkpoints, solo con lo que hay evidencia.

Leases y recuperación (`docs/adr/0004`): cada run se conduce bajo un lease en su propia fila (`lease_owner`, `lease_expires_at`, `lease_generation`), renovado por un heartbeat cada tercio de `RUN_LEASE_TTL_SECONDS`. Toda escritura del propietario está cercada por `(owner, generation)` y toda proyección por un `last_checkpoint_id` monotónico. Perder el lease cancela la ejecución local. Un barrido al arrancar y cada `RECOVERY_SWEEP_SECONDS` reclama con `FOR UPDATE SKIP LOCKED` los runs con lease libre o vencido y decide desde el checkpoint: reproyectar, aplicar una decisión pendiente, dejar `interrupted`, reanudar si `RECOVERY_AUTO_RESUME=true`, o `failed`/`abandoned` al agotar `MAX_RUN_ATTEMPTS`. El apagado deja de aceptar trabajo, espera `SHUTDOWN_GRACE_SECONDS`, cancela lo que siga, libera solo sus leases y cierra los pools; nunca marca nada como terminado.

Checkpointer:

- Serialización estricta: msgpack con la allowlist interna de tipos seguros de LangGraph y **sin** fallback a pickle. El estado del grafo es JSON nativo; un tipo desconocido falla al escribir, nunca al leer de la base.
- Cifrado en reposo opcional (`CHECKPOINT_ENCRYPTION_KEY`, AES-EAX). La clave vive en el gestor de secretos, nunca en el repositorio ni en logs.
- Pool propio (`CHECKPOINT_POOL_SIZE`) abierto en `lifespan` sin esperar: si PostgreSQL está caído al arrancar la app queda degradada y `/health/ready` responde `503`. El apagado espera hasta `SHUTDOWN_GRACE_SECONDS` a las ejecuciones activas antes de cerrar los pools.
- `uv run python -m scripts.setup_checkpoints --check` comprueba sin modificar nada; sin `--check` crea o actualiza las tablas. La app verifica al arrancar que existen y están en la versión que espera la librería instalada; si no, aborta.

| Variable | Default | Efecto |
|---|---|---|
| `DATABASE_URL` | obligatoria | `postgresql+psycopg://usuario:clave@host:5432/db`. `postgresql://` se normaliza; otros drivers se rechazan |
| `DATABASE_POOL_SIZE` / `DATABASE_MAX_OVERFLOW` | `5` / `5` | Tamaño del pool de SQLAlchemy por proceso |
| `DATABASE_CONNECT_TIMEOUT_SECONDS` | `5` | Tiempo máximo para abrir una conexión |
| `DATABASE_STATEMENT_TIMEOUT_MS` | `30000` | `statement_timeout` por conexión |
| `DATABASE_SSL_REQUIRED` | derivado | `true` en `staging`/`production` salvo que se fije |
| `ENVIRONMENT` | `local` | Fuera de `local`/`test` deshabilita `/docs` y exige TLS |
| `CHECKPOINT_POOL_SIZE` | `5` | Tamaño del pool del checkpointer por proceso. Suma con el de SQLAlchemy bajo el límite del servidor |
| `CHECKPOINT_ENCRYPTION_KEY` | vacío | 16, 24 o 32 bytes activan el cifrado AES de los checkpoints |
| `SHUTDOWN_GRACE_SECONDS` | `30` | Espera máxima a ejecuciones activas durante el apagado |
| `RUN_LEASE_TTL_SECONDS` / `RUN_HEARTBEAT_SECONDS` | `60` / un tercio | Vigencia del lease de ejecución y cadencia de renovación |
| `RECOVERY_SWEEP_SECONDS` / `RECOVERY_BATCH_SIZE` | `60` / `20` | Periodo y tamaño de lote del barrido de recuperación (`0` desactiva el periódico) |
| `RECOVERY_AUTO_RESUME` | `false` | Reanudar automáticamente runs interrumpidos desde su checkpoint |
| `MAX_RUN_ATTEMPTS` | `3` | Intentos de conducción antes de `failed`/`abandoned` |

Arranque: si PostgreSQL no responde, la app arranca degradada y `/health/ready` devuelve `503` hasta que vuelva. Si responde con una revisión de Alembic distinta a la del código, o sin las tablas de checkpoints, el arranque aborta indicando el comando que falta. `/health/ready` devuelve `{"status":"ok","database":"ok","schema":"ok","checkpoints":"ok"}` cuando todo está en orden. `/health/live` nunca toca la base. Decisiones y seguridad en `docs/adr/0003-persistencia-postgres-y-checkpoints.md`.

## Salida estructurada

El adaptador `VertexClaudeAdapter` restringe la salida del modelo mediante instrucciones de prompt y valida la respuesta con Pydantic (`model_validate_json`), tolerando bloques de código Markdown alrededor del JSON. El mecanismo nativo del SDK (`output_config.format`) existe en `anthropic` 2.3.0, pero está bloqueado actualmente por una política de organización de Google Cloud (`constraints/vertexai.allowedPartnerModelFeatures`) para el modelo configurado — confirmado con una llamada real que devuelve `400 FAILED_PRECONDITION`. Si esa política se habilita, el cambio queda aislado a `app/infrastructure/llm/vertex_claude.py`.

## Scripts de demostración

`scripts/demo_vertex.py` y `scripts/demo_planner_architect.py` son scripts manuales (no se ejecutan en `pytest`, que solo escanea `tests/`). Hacen llamadas reales a Vertex AI:

```bash
uv run python -m scripts.demo_vertex
uv run python -m scripts.demo_planner_architect
```

Se invocan como módulos (`-m`) porque el proyecto no se instala en el entorno virtual: así `app` queda importable desde la raíz del repositorio.

## Importante

No existe todavía ejecución de código, herramientas ni aislamiento de repositorios. Checkpoints, proyección de negocio, auditoría, decisiones humanas, leases y recuperación viven en PostgreSQL. Con varias instancias simultáneas queda un riesgo residual documentado en el ADR 0004: el fencing protege las tablas de negocio pero no los checkpoints de LangGraph. Ejecuta **un solo proceso** de servidor y usa únicamente `127.0.0.1`; la API todavía no tiene autenticación ni controles multiusuario.

## Arquitectura

Reglas permanentes del repositorio en `CLAUDE.md`. Decisiones registradas en `docs/adr/`:

- `0001` — por qué `app/` está organizado por capa técnica (`agents/`, `contracts/`, `infrastructure/`, `orchestration/`, `api/`) y no por módulo de negocio.
- `0002` — política de errores, reintentos y presupuesto de llamadas LLM.
- `0003` — persistencia en PostgreSQL: tablas de negocio con SQLAlchemy/Alembic y checkpoints de LangGraph.
- `0004` — leases con fencing, recuperación automática, idempotencia de creación y sus limitaciones conocidas.

## Roadmap por fases

- **Fase 1 (completada)**: planificación y revisión arquitectónica con aprobación humana.
- **Fase 2 (completada)**: orquestador robusto — enrutamiento condicional, retroalimentación Planner ↔ Architect con límite, estados terminales explícitos, manejo de errores del LLM, presupuesto de llamadas, pruebas deterministas.
- **Fase 3 (completada, v0.3.0)**: PostgreSQL con SQLAlchemy y Alembic, checkpoints persistentes de LangGraph, proyección de negocio y auditoría, aprobaciones durables, leases con heartbeat y fencing, barrido de recuperación, `Idempotency-Key`, apagado ordenado, CI con PostgreSQL. Decisiones en los ADR 0003 y 0004.
- **Fase 4**: API de control con autenticación, aprobaciones y cancelación.
- **Fase 5**: Tool Gateway con sandbox real.
- **Fase 6**: Developer Agent trabajando en git worktrees aislados.
- **Fase 7**: agentes de QA, seguridad y revisión.
- **Fase 8**: paralelismo entre agentes.
- **Fase 9**: observabilidad y despliegue.

Las fases 4 a 9 no están iniciadas. Cada fase arranca solo con aprobación explícita.
