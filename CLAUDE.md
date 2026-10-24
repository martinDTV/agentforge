# CLAUDE.md — AgentForge

Reglas permanentes de este repositorio. Mantener conciso; reflejar solo lo que existe, no funcionalidad imaginada.

## Arquitectura FastAPI

Antes de implementar o modificar cualquier componente de FastAPI (routers, schemas, dependencias, configuración, persistencia, testing, seguridad), consultar la skill `/fastapi-architect:fastapi-architect` si está disponible en la sesión. Es la referencia principal de estándares técnicos para este proyecto, y sus estándares se aplican siempre, no se difieren como deuda: códigos HTTP correctos, excepciones de dominio mapeadas a Problem Details en `app/api/errors.py`, prefijo `/api/v1`, dependencias tipadas con `Annotated`, recursos creados en `lifespan` y `def` plano en endpoints bloqueantes.

- No reorganizar carpetas ni reescribir componentes solo para igualar visualmente la skill.
- Toda desviación intencional respecto a la skill debe documentarse como ADR en `docs/adr/`.
- Ver `docs/adr/0001-layout-por-capa-tecnica.md` para la decisión sobre por qué `app/` está organizado por capa técnica (`agents/`, `contracts/`, `infrastructure/`, `orchestration/`, `api/`) y no por módulo de negocio. Ver `docs/adr/0002-politica-de-errores-reintentos-y-presupuesto-llm.md` para la política de errores LLM, reintentos y presupuesto por run.

## Proveedor LLM

Claude vía Vertex AI (`AnthropicVertex`) es el proveedor principal. No cambiar de proveedor sin autorización explícita. Los agentes dependen de `LLMPort` (Protocol), nunca instancian `VertexClaudeAdapter` directamente — reciben el adaptador por inyección.

## Fases de desarrollo

El proyecto avanza fase por fase, con aprobación explícita antes de iniciar cada una. No implementar funcionalidad de una fase futura mientras se trabaja en la actual. Ver roadmap en `README.md`.

## Pruebas

- `pytest` sin argumentos ejecuta solo `tests/unit` (`testpaths` en `pyproject.toml`): esa suite no puede depender de ningún servicio externo. Las pruebas que necesitan PostgreSQL viven en `tests/integration`, se ejecutan explícitamente y deben **fallar** con mensaje claro si no hay servidor, nunca saltarse. Los scripts de demostración viven en `scripts/`, protegidos con `if __name__ == "__main__":`.
- Ningún test automatizado debe llamar a Vertex AI ni a ningún proveedor LLM real. Usar `FakeLLMAdapter` inyectado explícitamente; `tests/conftest.py` bloquea la construcción del cliente real durante todo pytest.
- Migraciones de Alembic y `setup()` del checkpointer son pasos de despliegue: nunca se ejecutan en el arranque de FastAPI.

## Verificación obligatoria antes de reportar una tarea como completa

```
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -q
uv run pytest tests/integration -q   # requiere PostgreSQL accesible
```

La CI (`.github/workflows/ci.yml`) ejecuta exactamente estos comandos contra un servicio PostgreSQL; un cambio que no pase localmente no debe publicarse.

## Git

No crear commits ni hacer push sin autorización explícita del usuario, incluso si el código y las pruebas están en verde.
