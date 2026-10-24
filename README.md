# AgentForge

Plataforma multiagente para planificación y revisión arquitectónica de tareas de desarrollo, con FastAPI, LangGraph, Claude (Anthropic) vía Vertex AI, aprobación humana y contratos Pydantic.

**Estado actual: Fase 1.** Solo planifica, revisa arquitectura y solicita aprobación humana — ver [Importante](#importante) y [Roadmap](#roadmap-por-fases) más abajo.

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

Abre http://127.0.0.1:8000/docs.

## Flujo

```bash
curl -s -X POST http://127.0.0.1:8000/runs \
  -H 'Content-Type: application/json' \
  -d '{"task":"Diseña el módulo de pacientes en FastAPI con pruebas unitarias"}'
```

Copia el `run_id` que devuelve la API:

```bash
curl -s -X POST http://127.0.0.1:8000/runs/TU_RUN_ID/approve \
 -H 'Content-Type: application/json' \
 -d '{"approved":true}'
```

El grafo ejecuta `planner -> architect -> approval`: Planner genera un `DevelopmentPlan`, Architect lo revisa y produce un `ArchitectureReview`, y el flujo queda en pausa (`waiting_approval`) hasta recibir la aprobación humana.

## Sin gasto en modelo externo

Define `USE_FAKE_MODEL=true` en `.env` y reinicia el servidor para usar un `FakeLLMAdapter` con respuestas fijas (sin red, sin consumo de tokens). Luego ejecuta:

```bash
uv run pytest -q
```

Los tests siempre usan `FakeLLMAdapter` inyectado explícitamente (ver `tests/conftest.py`); nunca llaman a Vertex AI, independientemente del valor de `USE_FAKE_MODEL` en `.env`.

## Salida estructurada

El adaptador `VertexClaudeAdapter` restringe la salida del modelo mediante instrucciones de prompt y valida la respuesta con Pydantic (`model_validate_json`). El mecanismo nativo del SDK (`output_config.format`) existe en `anthropic` 2.3.0, pero está bloqueado actualmente por una política de organización de Google Cloud (`constraints/vertexai.allowedPartnerModelFeatures`) para el modelo configurado — confirmado con una llamada real que devuelve `400 FAILED_PRECONDITION`. Si esa política se habilita, el cambio queda aislado a `app/infrastructure/llm/vertex_claude.py`.

## Scripts de demostración

`scripts/demo_vertex.py` y `scripts/demo_planner_architect.py` son scripts manuales (no se ejecutan en `pytest`, que solo escanea `tests/`). Hacen llamadas reales a Vertex AI:

```bash
uv run python scripts/demo_vertex.py
uv run python scripts/demo_planner_architect.py
```

## Importante

Esta es la **Fase 1**: planifica, revisa arquitectura y solicita aprobación humana. No existe todavía ejecución de código, herramientas ni aislamiento de repositorios. El checkpointer está en memoria: si reinicias el servidor pierdes ejecuciones. Ejecuta **un solo proceso** de servidor y usa únicamente `127.0.0.1`; la API todavía no tiene autenticación ni controles multiusuario.

## Roadmap por fases

- **Fase 1 (actual)**: planificación y revisión arquitectónica con aprobación humana. Sin ejecución de código.
- **Fase 2**: checkpointer persistente (PostgreSQL), autenticación, idempotencia y bloqueo de ejecuciones concurrentes por `run_id`.
- **Fase 3**: Tool Gateway con lista de herramientas permitidas y ejecución controlada de acciones.
- **Fase 4**: worktrees aislados por ejecución para trabajar sobre repositorios reales de forma segura.
- **Fase 5**: agentes de QA y seguridad, y soporte para workers que ejecuten tareas de forma asíncrona.

Ninguna de las fases 2 a 5 está implementada todavía.
