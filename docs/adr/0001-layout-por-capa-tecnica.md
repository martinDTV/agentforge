# 0001. Mantener el layout por capa técnica en lugar de módulos de negocio tipo `modules/<dominio>/`

- Status: accepted
- Date: 2026-10-24
- Deciders: martin, Claude (fastapi-architect skill review)

## Context

AgentForge usa la skill `fastapi-architect` como referencia de arquitectura FastAPI. Esa skill recomienda, para su "Stage 1: modular monolith", organizar el backend por capacidad de negocio (`modules/customers/`, `modules/orders/`, cada uno con `router.py`, `schemas.py`, `service.py`, `repository.py`, `models.py`). También marca explícitamente como anti-patrón el "by-type folder layout" (`routers/`, `models/`, `services/` en la raíz).

AgentForge hoy está organizado así:

```
app/
├── agents/          # PlannerAgent, ArchitectAgent
├── contracts/       # DevelopmentPlan, ArchitectureReview, CreateRun, RunResponse
├── infrastructure/
│   └── llm/         # LLMPort, VertexClaudeAdapter, FakeLLMAdapter
├── orchestration/   # grafo LangGraph (planner -> architect -> approval)
└── main.py
```

A primera vista esto parece el anti-patrón que la skill señala (carpetas por "tipo"). La diferencia es que la skill diseña su convención para dominios CRUD con entidades de negocio independientes (clientes, pedidos), donde cada módulo encapsula su propia persistencia y reglas. AgentForge no tiene "entidades de negocio" en ese sentido: es un sistema multiagente con un único flujo (`planner → architect → approval`) sobre un mismo recurso (`Run`). Los ejes reales de variación del sistema son otros: qué agente razona, qué contrato valida la salida, qué adaptador de LLM se usa, cómo se orquesta el flujo — no "clientes vs. pedidos".

Restricciones relevantes: proyecto en Fase 1 (un solo desarrollador, sin base de datos, 3 endpoints), con roadmap aprobado hacia Fase 2 (orquestación más rica) y Fase 3 (PostgreSQL). No se debe reescribir código funcional solo para parecerse visualmente a una plantilla.

## Options considered

1. **Migrar a `modules/<agente>/` con router/schemas/service/repository por módulo**: alinea visualmente con la skill. Costo: fuerza una granularidad de "módulo de negocio" que no existe aún (no hay persistencia por agente, no hay entidades independientes); reescribe imports funcionando sin beneficio medible; previsiblemente se re-fragmentaría de nuevo en Fase 3 cuando aparezca persistencia real.
2. **Mantener el layout actual por capa técnica (`agents/`, `contracts/`, `infrastructure/`, `orchestration/`)**: refleja los ejes reales de cambio del sistema (agente, contrato, proveedor LLM, flujo de orquestación). Costo: diverge visualmente de la convención por defecto de la skill.
3. **Adoptar un híbrido ahora**: mover solo lo que ya tiene forma de "módulo de negocio" (ninguno existe hoy) y dejar el resto como está. Equivalente en la práctica a la opción 2 en esta fase.

## Decision

Se mantiene el layout por capa técnica (opción 2). La skill está diseñada para dominios CRUD con módulos de negocio independientes; AgentForge en Fase 1 no tiene esa forma, y forzarla introduciría reestructuración sin ganancia real, violando el principio de la propia skill de que "cada capa debe pagar su renta" y de no rediseñar visualmente sin una razón concreta.

Las demás recomendaciones de la skill que sí aplican sin fricción ya están adoptadas: `LLMPort` como `Protocol` (no ABC), `FakeLLMAdapter` para tests en vez de mocks, separación `XCreate`/`XUpdate`/`XRead` en los contratos de API, `get_settings()` cacheado y fallo rápido en settings requeridas.

## Consequences

- Se mantiene la claridad actual: cada carpeta representa un eje real de extensión del sistema (nuevo agente, nuevo contrato, nuevo proveedor LLM, nuevo nodo de orquestación).
- Se acepta que la skill no puede aplicarse literalmente a este tipo de sistema sin traducción; cada fase futura debe re-evaluar si ya apareció una "unidad de negocio" real que justifique un módulo propio.
- Qué nos haría revisar esta decisión: si en una fase futura (por ejemplo Fase 6 en adelante, con Developer/QA/Security agentes y persistencia por ejecución) aparecen *dominios de negocio independientes* con su propia persistencia, reglas y ciclo de vida (p. ej. "ejecuciones", "repositorios gestionados", "políticas de aprobación" como entidades con tablas propias) — en ese punto, cada uno de esos dominios sí debería adoptar el layout `modules/<dominio>/` de la skill, posiblemente conviviendo con las carpetas técnicas actuales (`agents/`, `infrastructure/`) que seguirán teniendo sentido como ejes transversales.
