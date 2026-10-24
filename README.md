# AgentForge

Plataforma multiagente para planificación y revisión arquitectónica de tareas de desarrollo, con FastAPI, LangGraph, Claude (Anthropic) vía Vertex AI, aprobación humana y contratos Pydantic.

**Estado actual: Fase 2.** Planifica, revisa arquitectura con un ciclo de retroalimentación acotado entre Planner y Architect, tolera fallos del LLM con estados terminales explícitos y solicita aprobación humana. Todavía no ejecuta código — ver [Importante](#importante) y [Roadmap](#roadmap-por-fases).

## Requisitos

- Python 3.11+ y `uv`
- Credenciales Application Default Credentials (ADC) de Google Cloud con acceso a Vertex AI
- Un proyecto de Google Cloud con el modelo Claude habilitado en Vertex AI Model Garden

## Inicio

```bash
cp .env.example .env
# editar .env y definir ANTHROPIC_VERTEX_PROJECT_ID, CLOUD_ML_REGION, CLAUDE_MODEL
uv sync
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

Abre http://127.0.0.1:8000/docs. La API está versionada bajo `/api/v1`.

## Flujo

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/runs \
  -H 'Content-Type: application/json' \
  -d '{"task":"Diseña el módulo de pacientes en FastAPI con pruebas unitarias"}'
```

Copia el `run_id` que devuelve la API (código `201`):

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
| `status` | `waiting_approval`, `approved`, `rejected` o `failed` |
| `revision` | Cuántas veces el Architect devolvió el plan al Planner (0 = primer plan) |
| `llm_calls` | Llamadas a agentes consumidas por el run |
| `architect_approved` | Veredicto del Architect sobre el plan actual; `null` si nunca llegó a ejecutarse |
| `plan`, `architecture` | Último plan y última revisión; `null` si el run falló antes de producirlos |
| `error` | Solo en `failed`: `{ "code", "message" }`, sin trazas ni payloads del proveedor |

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
| `500` | `internal_error` | Error inesperado |

## Sin gasto en modelo externo

Define `USE_FAKE_MODEL=true` en `.env` y reinicia el servidor para usar un `FakeLLMAdapter` con respuestas fijas (sin red, sin consumo de tokens). Luego ejecuta:

```bash
uv run pytest -q
```

Los tests siempre usan `FakeLLMAdapter` inyectado explícitamente (ver `tests/conftest.py`); nunca llaman a Vertex AI, independientemente del valor de `USE_FAKE_MODEL` en `.env`. El adaptador de Vertex se prueba contra un cliente stub (`tests/test_vertex_adapter.py`).

## Resiliencia frente al LLM

| Variable | Default | Efecto |
|---|---|---|
| `LLM_TIMEOUT_SECONDS` | `60` | Timeout por llamada en el cliente de Anthropic |
| `LLM_MAX_RETRIES` | `2` | Reintentos con backoff del SDK ante 429, 5xx y errores de conexión |
| `LLM_MAX_REPAIR_ATTEMPTS` | `1` | Veces que se le muestra al modelo su salida inválida y el error de validación para que la corrija |
| `MAX_PLAN_REVISIONS` | `2` | Rondas Planner ↔ Architect permitidas por run |
| `MAX_LLM_CALLS_PER_RUN` | `8` | Tope absoluto de llamadas a agentes por run |

La política completa está en `docs/adr/0002-politica-de-errores-reintentos-y-presupuesto-llm.md`.

## Salida estructurada

El adaptador `VertexClaudeAdapter` restringe la salida del modelo mediante instrucciones de prompt y valida la respuesta con Pydantic (`model_validate_json`), tolerando bloques de código Markdown alrededor del JSON. El mecanismo nativo del SDK (`output_config.format`) existe en `anthropic` 2.3.0, pero está bloqueado actualmente por una política de organización de Google Cloud (`constraints/vertexai.allowedPartnerModelFeatures`) para el modelo configurado — confirmado con una llamada real que devuelve `400 FAILED_PRECONDITION`. Si esa política se habilita, el cambio queda aislado a `app/infrastructure/llm/vertex_claude.py`.

## Scripts de demostración

`scripts/demo_vertex.py` y `scripts/demo_planner_architect.py` son scripts manuales (no se ejecutan en `pytest`, que solo escanea `tests/`). Hacen llamadas reales a Vertex AI:

```bash
uv run python scripts/demo_vertex.py
uv run python scripts/demo_planner_architect.py
```

## Importante

Esta es la **Fase 2**: planifica, revisa arquitectura con retroalimentación acotada y solicita aprobación humana. No existe todavía ejecución de código, herramientas ni aislamiento de repositorios. El checkpointer está en memoria: si reinicias el servidor pierdes ejecuciones. Ejecuta **un solo proceso** de servidor y usa únicamente `127.0.0.1`; la API todavía no tiene autenticación ni controles multiusuario.

## Arquitectura

Reglas permanentes del repositorio en `CLAUDE.md`. Decisiones registradas en `docs/adr/`:

- `0001` — por qué `app/` está organizado por capa técnica (`agents/`, `contracts/`, `infrastructure/`, `orchestration/`, `api/`) y no por módulo de negocio.
- `0002` — política de errores, reintentos y presupuesto de llamadas LLM.

## Roadmap por fases

- **Fase 1 (completada)**: planificación y revisión arquitectónica con aprobación humana.
- **Fase 2 (actual)**: orquestador robusto — enrutamiento condicional, retroalimentación Planner ↔ Architect con límite, estados terminales explícitos, manejo de errores del LLM, presupuesto de llamadas, pruebas deterministas.
- **Fase 3**: PostgreSQL con SQLAlchemy y Alembic, checkpoints persistentes, auditoría.
- **Fase 4**: API de control con autenticación, aprobaciones y cancelación.
- **Fase 5**: Tool Gateway con sandbox real.
- **Fase 6**: Developer Agent trabajando en git worktrees aislados.
- **Fase 7**: agentes de QA, seguridad y revisión.
- **Fase 8**: paralelismo entre agentes.
- **Fase 9**: observabilidad y despliegue.

Ninguna de las fases 3 a 9 está implementada todavía.
