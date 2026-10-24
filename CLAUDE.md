# CLAUDE.md — AgentForge

Reglas permanentes de este repositorio. Mantener conciso; reflejar solo lo que existe, no funcionalidad imaginada.

## Arquitectura FastAPI

Antes de implementar o modificar cualquier componente de FastAPI (routers, schemas, dependencias, configuración, persistencia, testing, seguridad), consultar la skill `/fastapi-architect:fastapi-architect` si está disponible en la sesión. Es la referencia principal de estándares técnicos para este proyecto.

- No reorganizar carpetas ni reescribir componentes solo para igualar visualmente la skill.
- Toda desviación intencional respecto a la skill debe documentarse como ADR en `docs/adr/`.
- Ver `docs/adr/0001-layout-por-capa-tecnica.md` para la decisión sobre por qué `app/` está organizado por capa técnica (`agents/`, `contracts/`, `infrastructure/`, `orchestration/`) y no por módulo de negocio.

## Proveedor LLM

Claude vía Vertex AI (`AnthropicVertex`) es el proveedor principal. No cambiar de proveedor sin autorización explícita. Los agentes dependen de `LLMPort` (Protocol), nunca instancian `VertexClaudeAdapter` directamente — reciben el adaptador por inyección.

## Fases de desarrollo

El proyecto avanza fase por fase, con aprobación explícita antes de iniciar cada una. No implementar funcionalidad de una fase futura mientras se trabaja en la actual. Ver roadmap en `README.md`.

## Pruebas

- `pytest` solo escanea `tests/` (`testpaths` en `pyproject.toml`). Los scripts de demostración viven en `scripts/`, protegidos con `if __name__ == "__main__":`.
- Ningún test automatizado debe llamar a Vertex AI ni a ningún proveedor LLM real. Usar `FakeLLMAdapter` inyectado explícitamente.

## Verificación obligatoria antes de reportar una tarea como completa

```
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run pytest -q
```

## Git

No crear commits ni hacer push sin autorización explícita del usuario, incluso si el código y las pruebas están en verde.
