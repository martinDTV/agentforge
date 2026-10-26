# Fase 6: auditoría de capacidades y alcance del MVP

Rama de trabajo: `chore/phase-5-close` (los cambios de la validación del Runner del
2026-10-26 se conservan sin commit). El MVP de desarrollo se invoca desde un
comando de operador local y reutiliza los contratos, el Gateway, los repositorios
PostgreSQL y el checkpointer del servicio. La API pública no concede permisos de
escritura ni de ejecución a partir de parámetros enviados por clientes.

## Matriz de capacidades

| Capacidad | Antes de Fase 6 | Implementado en Fase 6 | Verificación |
| --- | --- | --- | --- |
| LangGraph | Planner → Architect → aprobación humana, revisiones acotadas | `app/orchestration/development.py`: Developer → apply → QA, `revise_development`, máximo tres correcciones, fin explícito | Unit: éxito, límite, fallo del Runner, presupuesto, timeout con cancelación, archivo nuevo entre revisiones. Integración: fila, eventos, auditoría, checkpoints, recuperación conservadora |
| Modelos | Claude por Vertex, JSON validado, reintentos y reparación | Prompt del adaptador permite código dentro del JSON; `BudgetedLLM` con reserva previa por cada turno posible (reparaciones incluidas), cota de llamadas, tokens, coste y tamaño; `max_tokens` explícito; `repeated_failures` para el Developer | Unit (fake y adaptador que acepta `max_tokens`); ejecuciones reales en `docs/validation/agentforge-dev-mvp-2026-10-26.md` |
| Developer / QA | Solo enumerados en `AgentKind` | `app/agents/development.py`: contratos `DeveloperChanges`, `QAReview`, prompts con límites de rol | Unit y ejecución real |
| Gateway | Registro cerrado, política, idempotencia, timeout, auditoría | Cancelación externa cancela en el Runner y audita `failed/cancelled` (lectura) o `uncertain/cancelled` (escritura y ejecución); herramientas `write` no se abandonan al cancelar | Unit (`test_cancellation_reaches_remote_sandbox_and_closes_audit`, timeout del grafo) |
| Archivos | list/read/find con redacción; sin edición | `workspace.write_file`: lista explícita, forma canónica, hash esperado, sin enlaces, atómica, sin secretos ni binarios | Unit: rutas peligrosas, `.git`, `.env`, test de aceptación, symlink, hardlink, conflicto, duplicado, QA sin escritura |
| Git y worktrees | Registro por proyecto, worktree por ejecución, diff/status | Reutilizados; el parche final se toma del worktree; `git.commit`/`git.push` siguen cerrados | Unit y ejecución real (worktree conservado para inspección) |
| Sandbox | `ruff_check`, `ruff_format_check`, `pytest` autorizados | Reutilizado: Developer ejecuta ruff; QA ejecuta los tres; `pytest` confinado a `authorized_test_paths`; nuevo `ruff_check_diff` (solo lectura) en ambas listas, desplegado en el Runner el 2026-10-26 con respaldo y self-test | Runner real en Proxmox: ejecuciones `finished/completed` confirmadas por su API; ocho pruebas de aislamiento repetidas como UID 110 el 2026-10-26 |
| Persistencia | runs, eventos, auditoría, checkpoints | Migración 0005: `runs.workflow_kind` y estado `completed`; eventos `developer_completed`, `developer_applied`, `qa_reviewed`, `run_completed` reconstruibles desde el historial; recuperación cierra como `development_interrupted` y proyecta las terminadas | Integración (`test_development_persistence.py`, `test_migrations.py`: upgrade, downgrade protegido, checkpoint de desarrollo leído por el grafo de la API) y consulta directa a PostgreSQL |
| Seguridad | write/exec cerrados globalmente | `DevelopmentScope` revocable, rutas escribibles canónicas y acotadas, `DevelopmentToolPolicy` solo `sandbox-*`; concesiones globales intactas | Unit: `development_scope_denied` por run, caducidad y revocación, rutas no canónicas rechazadas al construir, `tool_not_granted` para QA en escritura, `DEFAULT_AGENT_GRANTS` sin `exec.run` |

## Pendiente tras el MVP

- Exposición del flujo por la API bajo configuración por proyecto (repositorios y rutas escribibles) y aprobación humana previa.
- Reanudación de ejecuciones de desarrollo interrumpidas (hoy se cierran como fallidas).
- Edición por parches o por rangos para archivos grandes; hoy archivos completos de hasta 32 KiB, ocho por revisión.
- Prueba de concurrencia de varias ejecuciones de desarrollo contra el mismo Runner.
