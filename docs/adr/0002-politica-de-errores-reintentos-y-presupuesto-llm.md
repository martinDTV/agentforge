# 0002. Política de errores, reintentos y presupuesto de llamadas LLM

- Status: accepted
- Date: 2026-10-24
- Deciders: martin, Claude (Fase 2)

## Context

En Fase 1 el grafo era lineal (`planner -> architect -> approval`) y cualquier fallo del proveedor LLM (timeout, 429, salida no parseable) se propagaba como excepción hasta FastAPI, que respondía `500` sin dejar rastro útil en el run. El veredicto del Architect (`approved=false`) no tenía efecto sobre el flujo. No existía ningún tope al número de llamadas por ejecución.

Fase 2 introduce retroalimentación Planner ↔ Architect (un bucle) y, con ello, la necesidad de decidir dónde viven los reintentos, cómo termina un run cuando algo falla, quién decide cuando el Architect nunca aprueba, y cómo se acota el gasto. Son decisiones transversales de las que dependerán las fases siguientes (persistencia de runs, más agentes, paralelismo), por eso se registran aquí.

Restricciones: la skill `fastapi-architect` exige excepciones de dominio sin conocimiento HTTP, mapeadas en un solo lugar a Problem Details; los tests no pueden tocar Vertex AI; el SDK de Anthropic ya implementa reintentos con backoff exponencial y respeta `Retry-After`.

## Options considered

### Dónde viven los reintentos

1. **Solo en el SDK de Anthropic** (`max_retries`, `timeout`): cubre 429, 5xx y errores de conexión con backoff probado. No cubre salidas malformadas.
2. **`RetryPolicy` de LangGraph en los nodos**: reintenta el nodo completo. Duplicaría los reintentos del SDK para errores transitorios y haría que el contador de llamadas del run no reflejara las llamadas reales.
3. **SDK para lo transitorio + bucle de reparación en el adaptador para lo malformado** (elegida): cada capa reintenta lo que entiende. El adaptador reenvía al modelo su respuesta inválida y el error de validación, acotado por `LLM_MAX_REPAIR_ATTEMPTS`.

### Qué hace el grafo cuando un agente falla

1. **Dejar propagar la excepción**: `500`, run en estado indefinido, sin información para el cliente.
2. **Capturar `LLMError` en el nodo y escribir `error` en el estado** (elegida): el run termina en `failed` con un código estable y un mensaje propio (nunca el payload del proveedor). Las excepciones que no son `LLMError` sí se propagan: son bugs y no deben disfrazarse. Aun así, LangGraph conserva la tarea fallida en el checkpoint, y el run se lee como `failed` con código `internal_error`.

### Qué pasa cuando el Architect nunca aprueba

1. **Terminar en `failed` por revisiones agotadas**: pierde trabajo útil (hay plan y revisión) y quita a la persona la posibilidad de decidir.
2. **Pasar a aprobación humana con `architect_approved=false` y los `issues` visibles** (elegida): la aprobación humana sigue siendo la puerta final; el límite solo acota el gasto automático.

### Semántica del presupuesto de llamadas

1. **Solo el límite de revisiones**: acota implícitamente las llamadas (2 por ronda), pero deja de ser cierto en cuanto un nodo haga más de una llamada o se añadan agentes.
2. **Límite de revisiones + presupuesto explícito `MAX_LLM_CALLS_PER_RUN`** (elegida), con dos comprobaciones:
   - el router no inicia una ronda de revisión que no quepa en el presupuesto restante (va a aprobación humana);
   - cada nodo que llama al LLM se niega a ejecutarse con el presupuesto agotado y escribe `llm_budget_exceeded`. Esta segunda comprobación convierte el tope en un invariante (`llm_calls <= max_llm_calls`) y no en una heurística de enrutamiento.

### Respuesta HTTP de un run que falla al crearse

1. **`502`/`503`**: el cliente no obtiene el `run_id` de forma natural y el recurso ya existe en el checkpointer.
2. **`201` con `status: "failed"` y `error` estructurado** (elegida): el recurso se creó, se puede consultar con `GET`, y `approve` responde `409 run_already_decided`.

## Decision

- Errores del proveedor se traducen en `app/infrastructure/llm/errors.py` (`LLMTimeoutError`, `LLMUnavailableError`, `LLMRequestRejectedError`, `LLMMalformedOutputError`); ninguna excepción del SDK de Anthropic sale de `VertexClaudeAdapter`.
- Reintentos transitorios: SDK (`LLM_MAX_RETRIES`, `LLM_TIMEOUT_SECONDS`). Reparación de salida malformada: adaptador (`LLM_MAX_REPAIR_ATTEMPTS`). El grafo no reintenta.
- Los nodos capturan solo `LLMError`, escriben `error` en el estado y el router los lleva a `END`. El estado del run se deriva en un único sitio (`app/orchestration/status.py`).
- Límites por run en `RunLimits` (inmutable), inyectados en `build_graph`; la orquestación no lee `Settings`.
- Revisiones agotadas o presupuesto insuficiente para otra ronda → aprobación humana, nunca `failed`.
- Errores de dominio (`RunNotFoundError`, `RunAlreadyDecidedError`) sin conocimiento HTTP, mapeados en `app/api/errors.py` a Problem Details, como indica la skill.

## Consequences

- Un cliente puede distinguir con `error.code` entre un problema de configuración (`llm_request_rejected`), uno transitorio (`llm_unavailable`, `llm_timeout`) y uno de calidad del modelo (`llm_malformed_output`), sin leer logs.
- El contador `llm_calls` cuenta invocaciones de agentes, no peticiones HTTP: los reintentos del SDK y los intentos de reparación quedan dentro de una misma llamada de agente, acotados por sus propias variables. La contabilidad de tokens y costo queda para Fase 3 (auditoría con persistencia).
- Añadir un agente nuevo exige: capturar `LLMError` en su nodo, respetar `budget_exhausted`, y revisar si `max_llm_calls` por defecto sigue siendo coherente con `2 * (1 + max_revisions)` más sus llamadas.
- Qué nos haría revisar esta decisión: ejecución asíncrona de runs (el `201` con `failed` dejaría de ser inmediato y haría falta un estado `running`), o un checkpointer persistente donde los reintentos a nivel de nodo (`RetryPolicy`) empiecen a aportar reanudación real tras caídas del proceso.
