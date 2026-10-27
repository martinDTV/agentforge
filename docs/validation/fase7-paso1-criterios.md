# Fase 7, Paso 1: criterios de aceptación y evidencia

Rama `feat/phase-7-real-repositories`, 2026-10-26. Alcance del paso: políticas de
repositorio por proyecto, propuesta de alcance del Scoper, aprobación humana por
etapa ligada al digest exacto, autoridad de escritura efímera dentro del worktree
aprobado, límites, estados de espera, denegación, cancelación y caducidad, API
autenticada y autorizada, auditoría y pruebas de seguridad. Sin remotos, sin
`push`, sin repositorios reales, sin cambios en el Runner ni en la infraestructura.

Pruebas: `uv run pytest -q` (unitarias, fakes) y `uv run pytest tests/integration -q`
(PostgreSQL). Los nombres de prueba son los de los archivos indicados.

| Criterio | Implementación | Evidencia automatizada | Evidencia real |
| --- | --- | --- | --- |
| Política por proyecto con rutas permitidas y prohibidas, denegación por defecto | `app/projects/policy.py` (`RepositoryPolicy`, `DEFAULT_DENIED_GLOBS`, `ProjectPolicyRegistry.require`), `PROJECTS_FILE` | `test_projects_policy.py`: `test_policy_limits_and_publish_mode_are_bounded`, `test_registry_denies_by_default_and_checks_the_project` | Política local de `sandbox-lint` cargada por la API (`app.state.projects`) |
| Validación y normalización de rutas; globs anclados | `app/contracts/scope.py::is_canonical_relative_path`, `app/projects/globs.py` | `test_globs_are_anchored_and_component_aware`, `test_dangerous_glob_patterns_are_refused`, `test_canonical_relative_paths` | — |
| Scoper sin facultades de escritura ni ejecución | `app/agents/scoper.py`, `DevelopmentRuntime.read_gateway` (sin `exec.run` ni `write_file` registrados), `AgentKind.SCOPER` con concesión de lectura | `test_scoped_development.py::test_a_development_run_blocks_on_scope_approval_without_writing_anything` (solo filas `scoper/read`), `test_governed_run_through_the_api_on_postgres` (sin `scoper/write` ni `scoper/exec` en `tool_invocations`) | Demostración: ver abajo |
| Propuesta filtrada: fuera de política se rechaza con motivo; vacía termina `scope_empty` | `app/projects/scope.py::filter_scope` | `test_scope_filter_rejects_everything_the_policy_does_not_allow`, `test_scope_filter_caps_the_number_of_files_and_can_reject_everything`, `test_scoper_proposals_outside_policy_end_as_scope_empty` | README.md propuesto por Claude y rechazado por política en la demostración |
| Aprobación ligada a la ejecución y a la versión exacta del alcance | Digest canónico (`scope_digest`) en la interrupción, en la petición y en `run_decisions.scope_digest`; `RunService.approve` compara antes de reclamar; el nodo `approve_scope` rechaza un resume con otro digest | `test_scope_digest_depends_on_files_actions_and_tests_only`, `test_approval_must_name_the_exact_scope_and_needs_repos_write`, API: `test_development_run_waits_for_scope_and_is_approved_by_digest` (`decision_stale`, `decision_stage_mismatch`) | Pasos 8 y 9 de la demostración |
| Autoridad de escritura efímera solo dentro del worktree y la lista aprobada | `DevelopmentScope` construido en `session_for` desde `approved_scope`; `workspace.write_file` exige lista, worktree administrado y alcance vigente; revocación y caducidad | `test_approved_scope_becomes_developers_only_write_authority`, `test_developer_cannot_write_outside_the_approved_scope` (modelo y gateway), `test_development_mvp.py` (symlink, hardlink, conflicto, revocación, rutas peligrosas) | Archivos cambiados en la demostración = lista aprobada |
| Límites de archivos, tamaño, presupuesto, duración e iteraciones | `RepositoryPolicy` (`max_files_per_run`, `max_file_bytes`, `max_change_bytes`, `max_model_calls`, `max_reserved_usd`, `wall_seconds`, `max_corrections`); `apply_node` rechaza `change_too_large`; `BudgetedLLM` restaurado desde el checkpoint | `test_policy_limits_bound_the_run` (`change_too_large`, `development_budget_exceeded`, `correction_limit`), `test_scope_filter_caps_the_number_of_files...` | Límites visibles en la propuesta (`limits`) |
| Estados: espera, denegación, cancelación, caducidad | `waiting_approval` + `pending_stage` + `approval_deadline`; `rejected`; `cancelled` (`runs:cancel`); `expired` (al aprobar tarde y por barrido); migración 0006 | `test_rejection_cancellation_and_expiry_are_terminal_and_audited`, SQL: `test_stage_decisions_are_claimed_once_cancelled_and_expired_in_sql`, migración: `test_0006_adds_decisions_and_statuses_and_refuses_a_lossy_downgrade`, `test_lifecycle.py` | Demostración: cancelación de una ejecución en espera |
| API autenticada y autorizada para consultar y resolver aprobaciones | `GET /runs/{id}/decisions`, `POST /runs/{id}/approve` (`stage`, `scope_digest`), `POST /runs/{id}/cancel`; `repos:write` para crear y aprobar alcance; `runs:cancel`; separación de funciones | `test_api_development.py` (cuatro pruebas), `test_creation_is_denied_without_permission_policy_or_repository`, `test_separation_of_duties_applies_to_scope_approval`, `test_governed_run_through_the_api_on_postgres` (`self_approval_forbidden`) | Pasos 1, 2, 5, 6, 7 de la demostración |
| Auditoría de decisiones con actor y alcance aprobado | `run_decisions` (actor, digest, aplicado), eventos `scope_proposed`, `waiting_approval` (con etapa y digest), `approval_recorded` (actor, etapa, digest), `approval_applied`, `run_cancelled` (actor), `run_expired` | `test_approved_scope_becomes_developers_only_write_authority`, `test_governed_run_through_the_api_on_postgres` | Eventos de la demostración |
| Escalamiento de permisos | Agentes invisibles como principales (`RunNotFoundError`), `operator` sin `repos:write`, `approver` sin alcance, `viewer` sin aprobar ni cancelar, proyecto ajeno denegado, `tools:execute` solo del rol `agent` | `test_approval_must_name_the_exact_scope...`, `test_creation_is_denied_without_permission_policy_or_repository`, `test_policy.py`, `test_tool_policy.py` | Pasos 1, 5, 6 |
| Aprobación obsoleta, doble aprobación, cambio de alcance, carreras | Digest obligatorio; decisión única por `(run_id, stage)`; repetición idéntica idempotente; contradicción `409`; `claim_decision` con CAS de estado y lease | `test_concurrent_decisions_apply_exactly_one`, SQL concurrente en `test_stage_decisions_are_claimed_once...`, `test_approved_scope_becomes_developers_only_write_authority` (replay y contradicción) | Pasos 8, 13 y 14 |
| Reanudación de la decisión en otro proceso | Sesión reconstruida desde el checkpoint (`approved_scope`, `workspace_root`, `development_deadline`, `usage`) | `test_a_decision_in_another_process_resumes_the_same_run` | La API decide en un proceso distinto del que creó la ejecución (mismo proceso en la demostración; cubierto por la prueba) |
| Atribución: `initiated_by`, `approved_by`, `executed_by` | `events.py` (`EXECUTED_BY`, `attribute`), payloads de `run_created`, decisiones, cancelación y eventos de nodo; `RunEventRead` expone los tres campos | `test_events_distinguish_who_initiated_approved_and_executed`, expectativas de payload en `test_events_history.py`, `test_projection_events.py`, `test_persistence_service.py`, `test_business_persistence.py` | Eventos de la demostración (anteriores al cambio: nodos atribuidos al aprobador, ya corregido) |
| Política endurecida tras la aprobación | `check_against_policy` antes de cada ronda; `scope_outside_policy` | `test_policy_tightened_after_approval_blocks_the_writes`, `test_exclusions_take_precedence_over_writable_globs` | — |
| Globs: denegación por defecto, precedencia de exclusiones, rutas normalizadas, propuesta frente a alcance aprobado | `app/projects/globs.py`, `filter_scope`, `is_canonical_relative_path` (rechaza `\\`, `..`, `.`, dobles barras, absolutas) | `test_deny_by_default_nothing_matches_nothing_passes`, `test_exclusions_take_precedence_over_writable_globs`, `test_non_normalised_variants_of_a_writable_path_are_rejected_not_rewritten`, `test_approved_scope_covers_only_accepted_files_never_the_rejected_ones`, `test_a_path_rejected_in_the_proposal_never_becomes_writable`, `test_a_digest_of_a_different_file_set_is_stale` | README.md rechazado en la demostración y no escribible después |
| Sin publicación ni `push` | El grafo termina en `completed` tras QA; `git.push` sigue deshabilitado en el adaptador; `repos:publish` sin rol; `publish.mode` solo `manual` | `test_policy_limits_and_publish_mode_are_bounded`, `test_policy.py` (`repos:publish` para nadie) | — |
| Permisos globales de Developer y QA cerrados | `DEFAULT_AGENT_GRANTS` y `ENABLED_RISK_CLASSES` sin cambios | `test_tool_policy.py`, `test_development_mvp.py::test_write_is_scoped_audited_and_qa_cannot_write` | — |

## Demostración real por la API

API local (`uvicorn`, puerto 8011) con `PROJECTS_FILE` apuntando a la política de
`sandbox-lint`, `AUTHZ_SEPARATION_OF_DUTIES=true`, OIDC local con el emisor de
desarrollo, Claude `claude-sonnet-4-6` vía Vertex y el Runner real de Proxmox.
Principales: `olivia` y `omar` (`project_operator`), `alice` (`approver`),
`victor` (`viewer`), `oscar` (`operator`). Los tokens nunca se imprimieron.

| Paso | Petición | Resultado |
| --- | --- | --- |
| 1 | `oscar` crea una ejecución `development` | `403 permission_denied` (sin `repos:write`) |
| 2 | `olivia` la crea sobre un repositorio sin política | `403 repository_not_authorized` |
| 3 | `olivia` la crea sobre `sandbox-lint` | `201`, `waiting_approval`, `pending_stage=scope`, caducidad a una hora, una llamada al modelo (Scoper, 12 s) |
| 4 | `victor` consulta las decisiones | `200`: propuesta filtrada de tres archivos (`src/textkit/slug.py` editar, `src/textkit/__init__.py` editar, `tests/test_slug_many.py` crear), tres rutas de prueba, sin rechazos, digest `5f38afb9…` |
| 5 | `victor` aprueba | `403 permission_denied` |
| 6 | `alice` aprueba (puede aprobar planes, no alcances) | `403 permission_denied` |
| 7 | `olivia` aprueba su propia ejecución | `403 self_approval_forbidden` |
| 8 | `omar` aprueba con un digest obsoleto | `409 decision_stale` |
| 9 | `omar` aprueba la etapa `publish` | `409 decision_stage_mismatch` |
| 10 | Estado | sigue `waiting_approval`; nada escrito |
| 11 | `omar` aprueba `scope` con el digest exacto | `200`, `completed` tras 32 s: Developer y QA con Claude y el Runner, una corrección (revisión 1), cinco llamadas al modelo en total |
| 12 | Decisiones | `scope`, aprobada por `omar`, aplicada |
| 13 | Repetir la misma decisión | `200` idempotente |
| 14 | Decisión contraria | `409 run_already_decided` |
| 15 | Cancelar la ejecución terminada | `409 run_not_cancellable` |
| 16 | `olivia` crea una segunda ejecución | `201`, `waiting_approval` |
| 17 | `victor` la cancela | `403 permission_denied` |
| 18 | `olivia` la cancela | `200`, `cancelled` |

Confirmación independiente en PostgreSQL para la ejecución
`6c3f44e5-78c1-463a-9ae6-7c5ab4d6c0d6`: fila `completed/development`, sin
`pending_stage` ni lease; `run_decisions` con una única fila `scope`, aprobada
por `omar`, con el digest y aplicada; `tool_invocations`: Scoper solo tres
lecturas, Developer cuatro escrituras (`developer:0:write:0..2` y
`developer:1:write:0`, exactamente los archivos aprobados), QA sin escrituras;
eventos `run_created` (olivia), `scope_proposed`, `waiting_approval`,
`approval_recorded` (omar, etapa y digest), nodos de Developer y QA,
`run_completed` y `approval_applied`. La API del Runner confirma las
ejecuciones `finished/completed` (Ruff en 1 en la ronda 0, todo en 0 en la
ronda 1) y ninguna ejecución abierta al terminar. En el worktree solo cambiaron
los tres archivos del alcance aprobado y el bloque de imports quedó correcto. La
segunda ejecución quedó `cancelled` sin decisiones, con `run_cancelled`
atribuido a `olivia` y su worktree marcado como liberado con ese estado.

Observación: el Scoper propuso editar también `src/textkit/__init__.py` para
exportar la función; la política lo permitía y el humano lo aprobó a la vista.
El modelo escribió "AgentForce" en una docstring del repositorio desechable: un
error de calidad sin efecto de seguridad que un revisor humano del PR vería.

## Comprobaciones finales

`ruff check .`, `ruff format --check .`, `mypy app`, `mypy runner/agentforge_runner`,
`mypy` de los scripts de operador, `pytest -q` (632 unitarias) y
`pytest tests/integration -q` (73 contra PostgreSQL) en verde; `git diff --check`
limpio; concesiones globales de Scoper, Developer y QA sin `exec.run` ni
escritura y puerta de riesgo global en `read`. Base de datos local de desarrollo
migrada a `0006` (aditiva). Runner, Proxmox y Tailscale sin cambios (el código del
Runner es idéntico a `main`; la comprobación en vivo del servicio quedó pendiente
en el cierre porque Tailscale estaba detenido en la Mac); ningún repositorio real
tocado. Downgrade de 0006 verificado: rechazado con `cancelled` y con `expired`,
por separado y juntos, y aplicado tras resolverlas.
