# 0012. Supervisor de objetivos: coordinador determinista sobre `RunService` y roadmap de concurrencia

- Status: **proposed** — dirección aprobada por el usuario el 2026-10-27 (coordinador determinista, sin agente LLM adicional); D1, D2, D3, D4, D5, D10 y D12 aprobadas el 2026-10-30 (D6/D7 aceptadas para el Paso 3; D8, D9 y D11 pendientes). **Paso 1 implementado el 2026-10-30** en `feat/phase-8-goal-supervisor` (ver «Notas de implementación»); Pasos 2–5 requieren autorización nueva.
- Date: 2026-10-27
- Deciders: martin, Claude

## Context

Hoy cada unidad de trabajo nace a mano: `scripts.agentforge task "…"` crea una ejecución `development` que recorre Smart Router → (Planner → Architect → aprobación del plan) → Scoper → aprobación del alcance → Developer → QA → aprobación de publicación → Publisher. El objetivo es entregar un **objetivo de alto nivel** y que la plataforma lo convierta en ejecuciones acotadas, las vigile, las integre y avise, sin perder ninguna decisión humana ni ampliar la autoridad de ningún agente. A medio plazo, el mismo mecanismo debe sostener varios Developers y QA trabajando a la vez con límites de capacidad explícitos.

### Decisiones del usuario (2026-10-27)

1. Supervisor **determinista**, basado en eventos durables y reconciliación, **sin modelo en el bucle de supervisión**. No se añade un agente LLM que duplique funciones existentes.
2. Las ejecuciones hijas **conservan la propiedad del creador del objetivo** mediante delegación limitada y auditable. El Supervisor **no puede aprobar** `scope` ni `publish`.
3. El Planner puede proponer **de 1 a 5 tareas**, con dependencias explícitas y **validación de ciclos**.
4. Concurrencia inicial: **una ejecución activa por objetivo**, con presupuestos agregados y límites por tarea.
5. Las tareas dependientes **no consumen cambios no integrados**: política `after_merged` desde el primer paso cuando corresponda; **no se simula integración entre ramas**.
6. Las aprobaciones humanas existentes (`plan`, `scope`, `publish`) siguen siendo obligatorias.
7. El Supervisor **reutiliza** `RunService`, Smart Router, Planner, Architect, Scoper, QA, Publisher y Notifications.

## 1. Inventario: qué existe y qué falta

| # | Capacidad | Ya existe | Falta |
|---|---|---|---|
| 1 | Recibir un objetivo | `POST /api/v1/runs` solo acepta una `task` (10–4000 caracteres) ligada a un repositorio | La entidad *objetivo* |
| 2 | Descomponer | Planner → `DevelopmentPlan.tasks[]` (`id`, `dependencies`, `acceptance_criteria`, sin ciclos validados); Architect con rondas acotadas; aprobación humana del plan; Smart Router decide por tarea si hace falta planificar | Convertir el plan aprobado en ejecuciones; validar ciclos; prompt del Planner ("3 a 5" → "1 a 5") |
| 3 | Crear hijas tipadas | `RunService.create(task, principal, project_id, run_id, idempotency_key, workflow_kind, repository_id, route_mode)`; autorización antes de escribir; idempotencia por `(proyecto, sujeto, clave)` | Un llamador que no sea la API y el vínculo padre→hija |
| 4 | Padre, dependencias, estados | `runs`, `run_decisions`, `run_events` `(run, kind, ordinal)`, `route_decisions`, `publications` (`head_sha`, `pr_number`) | Tablas de objetivo y tareas; `runs` no tiene `parent_id` ni `repository_id` |
| 5 | Supervisar por eventos | `run_events` transaccionales con la bandeja; `RunService(on_terminal=…)`; `RecoveryService.run_periodically`; proyección re‑derivada al leer | Consumidor distinto de Notifications; gancho terminal de una sola función |
| 6 | Bloqueadas, fallidas, duplicadas | `waiting_approval` + `approval_deadline` → `expired`; `failed` con `RunError.code` estable; `base_moved` cuando la base avanzó; `Idempotency-Key` | Reglas a nivel de objetivo; duplicado semántico; solapamiento de alcances |
| 7 | Evaluar sin repetir QA | `qa_reviewed` (`approved`, `checks`, `failed_checks`, `usage`), `runs.publication`, `publication_prepared` (`base_sha`, `head_sha`) | Agregación por objetivo; verificación del resultado **integrado** |
| 8 | Intervención humana | `interrupt()` por etapa, decisión por digest y caducidad, correos `*_pending` | Estado "necesita humano" del objetivo; recordatorios; "PR sin fusionar" |
| 9 | Notifications | Bandeja transaccional, deduplicación, worker, niveles | Tipos de objetivo; `notification_outbox.run_id` es `NOT NULL` |
| 10 | Límites | Por ejecución: `RunLimits`, `RepositoryPolicy`, `BudgetedLLM`, `MAX_RUN_ATTEMPTS`, `worktree_max_per_project`; runner: `RUNNER_MAX_CONCURRENT` (2 por defecto, `503 runner_busy` **antes** de reclamar) | Agregados por objetivo, por repositorio y globales; conciencia de las plazas del runner |

Hechos del código que condicionan todo lo que sigue:

- **Base Git de cada hija**: `WorktreeManager.ensure` corta el worktree desde la rama por defecto del espejo *después* de `MirrorService.refresh`; el `sha` queda en `approved_scope.base_sha`.
- **La base no puede moverse**: `prepare_publication` refresca el espejo y, si la rama base ya no apunta a `base_sha`, la publicación termina `failed/base_moved` (no transitorio; ADR 0011 §9: el rebase es manual). Todo el trabajo de Developer y QA de esa hija se pierde.
- **Nada integra**: AgentForge no fusiona, no rebasea y no empuja a ramas protegidas. "Integrado" significa exclusivamente "un humano fusionó el PR".
- **El runner no encola**: con las plazas ocupadas responde `503 runner_busy` antes de la reclamación idempotente; el adaptador lo marca `retryable`, pero `DevelopmentSession` hoy convierte cualquier fallo de herramienta en fin de la ejecución.
- **Una ejecución de desarrollo nunca se reconduce** (ADR 0008): el único "reintento" posible es crear otra hija.

## 2. Decisión: coordinador determinista, identidad `agentforge:supervisor`

Principios (confirmados por las decisiones del usuario):

1. Un **objetivo** es un agregado nuevo; sus **hijas** son ejecuciones ordinarias (`planning`, `development`, y más adelante `verification`) con los grafos actuales.
2. El Supervisor es **código de plataforma**, como el Publisher: identidad de auditoría `agentforge:supervisor`, nunca herramienta del gateway, nunca `Principal` con `runs:approve`.
3. La descomposición **reutiliza el flujo `planning`**; el Smart Router decide si descomponer.
4. Las hijas se crean **solo** con `RunService.create`, con principal delegado, clave de idempotencia y `run_id` preasignado.
5. La reconciliación la disparan **eventos reales** (gancho terminal, tic periódico, endpoint manual) y lee solo filas de `runs`, `run_events`, `publications` y, para la integración, el espejo y la API de GitHub desde la plataforma. **Cero llamadas al modelo en el bucle.**

### 2.1 Modelo de datos (migración 0010, aditiva)

`goals`: `id`, `project_id`, `repository_id`, `owner_subject`, `objective`, `decomposition` (`auto`/`single`/`plan`), `dependency_policy` (`after_merged`), `integration_mode` (`sequential` en el Paso 1; `parallel_disjoint` en el Paso 4), `verify_after_merge` (bool), `status`, `error_code`, `error_message`, `limits` (jsonb, instantánea), `route_decision` (jsonb), `plan_run_id`, `verification_run_id`, `deadline`, `next_reconcile_at`, `lease_owner`, `lease_until`, `llm_calls_total`, `reserved_usd_total`, `idempotency_key` (único por `(project_id, owner_subject, key)`), `created_at`, `updated_at`.

`goal_tasks`: `id`, `goal_id`, `task_key`, `title`, `task_text`, `dependencies` (jsonb), `ordinal`, `repository_id`, `fingerprint` (índice **único parcial** `(repository_id, fingerprint)` sobre estados no terminales), `status`, `attempt`, `last_error_code`, `run_id` (preasignado), `base_sha` (de `approved_scope`), `head_sha`, `pr_number`, `pr_url`, `merge_evidence` (jsonb: `merged_at`, `merge_commit_sha`, `verified_in_mirror`, `base_after`), `scope_digest`, `scope_paths` (jsonb), `created_at`, `updated_at`; unicidad `(goal_id, task_key)`.

`goal_events`: `id`, `goal_id`, `kind`, `ordinal`, `payload`, `actor_subject`, `created_at`; único `(goal_id, kind, ordinal)`.

`notification_outbox`: `run_id` nullable, `goal_id` nullable, `CHECK (run_id IS NOT NULL OR goal_id IS NOT NULL)`. Downgrade: elimina lo añadido y se niega si hay filas de bandeja con `run_id IS NULL` (mismo criterio que la 0006).

### 2.2 Estados

Objetivo: `planning` → `active` ⇄ `blocked` → `verifying` → `completed` | `partial` | `failed` | `cancelled` | `expired`. Terminales los cinco últimos. `verifying` existe solo con `verify_after_merge=true` (Paso 3).

Tarea: `pending` → `ready` → `creating` → `running` ⇄ `waiting_human` → `published` → `awaiting_merge` → `merged` | `failed` | `skipped` | `duplicate`. `published` y `awaiting_merge` se distinguen porque la primera es el estado terminal de la hija y la segunda la espera del objetivo. Sin remoto (repositorios locales `sandbox-*` sin `remote`), `completed` de la hija equivale a `merged` **solo** para tareas sin dependientes; un objetivo con dependencias sobre un repositorio sin remoto se rechaza al crearse (`goal_dependencies_unsupported`), porque no habría integración verificable.

Proyección desde la hija: `running`/`resume_pending`/`interrupted` → `running`; `waiting_approval` → `waiting_human`; `published` → `published`; `completed` (local) → `published` (sin PR); `approved` (planning) → fin de la fase `planning`; `failed`/`rejected`/`expired`/`cancelled` → `failed` con código.

### 2.3 Reconciliación

`GoalCoordinator.reconcile(goal_id)`, idempotente, bajo reclamación de la fila (`FOR UPDATE SKIP LOCKED`):

1. **Refrescar hijas** desde `RunRepository.get_run` y aplicar la tabla de §2.2; copiar `base_sha`, `head_sha`, `pr_number` de `runs.publication`.
2. **Fase `planning`**: plan `approved` → materializar tareas; validar DAG (ciclo → `failed/goal_dependency_cycle`; `> max_tasks` → `failed/goal_limit_reached`); plan rechazado/caducado/cancelado/fallido → cerrar con ese código.
3. **Integración de dependencias (`after_merged`)**: para cada tarea `published` con dependientes, comprobar fusión (§4.3). Si fusionada y verificada → `merged`, evento `dependency_merged`; si el PR se cerró sin fusionar → `failed/pr_closed`, dependientes `skipped`.
4. **Propagar fallos**: una tarea `failed` sin reintento marca `skipped` a sus dependientes (transitivamente).
5. **Programar**: tareas `ready` (dependencias `merged`) en orden topológico estable; antes de crear: duplicado, presupuesto (§4.5), capacidad (§4.7), plazo. Crear en segundo plano con `RunService.create(..., run_id=preasignado, idempotency_key="goal:<goal_id>:<task_key>:<attempt>", route_mode=…)`.
6. **Verificar la base de la hija** (§4.2): cuando la hija llega a `waiting_human` en `scope`, su `base_sha` debe contener los `merge_commit_sha` de sus dependencias; si no, cancelarla (`base_stale`, D3) y volver a `ready` tras refrescar el espejo.
7. **Bloqueos y recordatorios** (reglas de §2.6): decisiones a mitad de ventana, códigos no reintentables, PR sin fusionar más de `merge_reminder_seconds`.
8. **Reintentos** por re‑creación, solo `RETRYABLE_CHILD_CODES`, `attempt < max_retries_per_task`.
9. **Cierre**: con `verify_after_merge`, todas `merged` → `verifying`; sin verificación, todas `merged` o `published` (las hojas no necesitan fusión) → `completed`; alguna `failed`/`skipped` y nada activo → `partial` si alguna llegó a `published`, `failed` si ninguna; plazo → `expired`.
10. **Persistir** eventos y borradores de correo en la misma transacción; `next_reconcile_at` según actividad (60 s activo, 15 min esperando humanos o fusión).

Disparadores: gancho terminal compuesto en `app/main.py` (`release_worktree` ∘ `coordinator.on_run_terminal`), tic periódico, `POST /api/v1/goals/{id}/reconcile`.

### 2.4 Identidad y delegación

- Crear un objetivo exige `runs:create` + `repos:write`, pertenencia al proyecto y política del repositorio.
- Principal delegado: `Principal(subject=goal.owner_subject, kind="service", issuer="agentforge:delegation", roles={"goal_delegate"}, projects={goal.project_id}, expires_at=goal.deadline, token_id=str(goal.id))`.
- Rol interno `goal_delegate` → `{runs:create, runs:read, repos:write}` y, si se aprueba D3, `runs:cancel` limitado por construcción a las hijas del objetivo. **Nunca** `runs:approve`, `runs:resume`, `repos:publish`, `tools:execute`. Una prueba de política fija el conjunto; una prueba estática comprueba que `app/orchestration/goals.py` solo llama a `create`, `get`, `decisions`, `list_runs` (y `cancel` si D3).
- Separación de funciones intacta: el creador del objetivo no aprueba sus hijas en `staging`/`production`.
- Sin credenciales: el coordinador no importa `app/infrastructure/github` ni `app/infrastructure/git`; la comprobación de fusión la hace un componente de plataforma (`IntegrationProbe`, §4.3) que sí vive en infraestructura y expone solo un `MergeEvidence` tipado.

### 2.5 Descomposición

| `decomposition` | comportamiento | coste |
|---|---|---|
| `auto` | `SmartRouter.decide(objective)`: `fast` → una tarea (`route_mode=fast` en la hija; las reglas obligatorias y el escalado del Scoper siguen); `full` → ejecución `planning` | ≤ 1 clasificación |
| `single` | una tarea, sin router previo | 0 |
| `plan` | siempre `planning` (Planner ⇄ Architect, aprobación humana) | ≤ `max_llm_calls` de planificación |

Texto de cada tarea (plantilla determinista): título, descripción, criterios de aceptación y "Parte k/n del objetivo: …" (recortado). Dependencias de `DevelopmentTask.dependencies`; orden topológico (Kahn); ciclo → fallo. Planner: prompt "de 1 a 5 tareas" (decisión 3).

Hijas de un plan aprobado: ruta `fast` con motivo `planned_upstream:<reglas>` (D4), porque la revisión arquitectónica ya ocurrió sobre el plan completo; las reglas obligatorias se evalúan y registran igual; el Scoper conserva el escalado como válvula de seguridad (si escala, esa hija pide su propia aprobación de plan y el objetivo lo notifica).

### 2.6 Reglas de detección (`supervisor-v1`, deterministas y versionadas como `router-v1`)

| situación | señal | acción |
|---|---|---|
| Decisión humana a mitad de ventana | `waiting_approval` y `now > proposed_at + ttl/2` | `blocked`, aviso `goal_needs_human` (ordinal = etapa) |
| Decisión caducada | hija `expired` | tarea `failed/expired:<stage>`, no reintentable |
| PR sin fusionar | `published` más de `merge_reminder_seconds` | `blocked`, aviso "fusiona #n o cancela" |
| PR cerrado sin fusionar | API: `state=closed`, `merged=false` | `failed/pr_closed`, dependientes `skipped` |
| Fallo transitorio | `RunError.code` ∈ `RETRYABLE_CHILD_CODES` | re‑crear si queda cupo; si no, `blocked` |
| Fallo de contenido o política | `correction_limit`, `scope_empty`, `scope_outside_policy`, `path_denied`, `change_too_large`, `publication_scan_failed`, `decision_*`, `rejected:*` | `failed`, dependientes `skipped`, `blocked` con el código |
| Hija colgada | `running` sin lease activo y `updated_at` anterior a `wall_seconds × 2` | anomalía en el aviso; la recuperación es quien la cierra |
| Duplicado exacto | mismo `fingerprint` activo en el repositorio | `duplicate` (enlaza la existente), sin crear hija |
| Solapamiento de alcances | `scope_paths` de la propuesta pendiente ∩ `scope_paths` aprobados de una hermana activa o publicada sin fusionar | aviso antes de que la persona apruebe; en `parallel_disjoint`, cancelación y re‑encolado (D3) |
| Base obsoleta | `base_sha` de la hija no contiene el `merge_commit_sha` de una dependencia | cancelación (`base_stale`, D3), refresco del espejo y vuelta a `ready` |
| Presupuesto, capacidad o plazo | reservas de §4.5 y §4.7 | deja de programar; `capacity_wait`/`budget_exhausted` una vez por tarea |

## 3. Dos formas de paralelismo: varias ejecuciones frente a varios agentes en una ejecución

Esta distinción decide el roadmap.

| aspecto | **Varias ejecuciones independientes** (N hijas, cada una con un Developer y un QA) | **Varios agentes del mismo rol dentro de una ejecución** (fan‑out con `Send` en un grafo) |
|---|---|---|
| Unidad de aislamiento | Una ejecución = un hilo de LangGraph, un lease, un worktree, un alcance aprobado, un presupuesto, sus tres decisiones humanas, su recuperación | Todo compartido: un checkpoint, un lease, un worktree, un presupuesto, una decisión de alcance que cubre a todos |
| Escritura | Cada Developer escribe en su worktree; nunca dos en el mismo | Dos Developers en el mismo worktree: conflictos de escritura o partición manual de rutas; `workspace.write_file` con hash esperado detectaría el pisado, pero la ejecución fallaría |
| Fallos | Uno falla, los demás siguen; el objetivo decide | Una rama falla → la ejecución falla o requiere recuperación parcial que `derive_status` no modela |
| Auditoría | `(run, kind, ordinal)` ya es único por ejecución | Eventos del mismo tipo y ordinal en paralelo chocan con la unicidad; habría que añadir `branch` al ordinal |
| Aprobaciones | Una por etapa y por ejecución (más trabajo humano, pero cada decisión es pequeña y exacta) | Una por etapa para todo el bloque (menos clics, decisión más grande y opaca) |
| Recuperación | ADR 0004/0008 sin cambios | Reanudar un fan‑out a medias no está contemplado; el estado parcial crece |
| Base Git | Cada hija parte de una base registrada y verificable | Una sola base; las ramas internas no existen en Git |
| Límites de capacidad | Externos al grafo: los aplica el coordinador contando ejecuciones activas | Internos al grafo: semáforos en memoria por proceso |
| Riesgo de cambio | Cero en los grafos actuales | Reescritura de `scoped_development`, `events`, `status`, `projection` |

**Decisión propuesta**: "3 Developers simultáneos" son **tres ejecuciones `development` activas a la vez**, cada una con su Developer; "2 QA simultáneos" son **dos ejecuciones en su fase de QA a la vez**. El único paralelismo intra‑ejecución que merece considerarse es de **solo lectura y con combinación determinista**: dos revisores QA independientes sobre el mismo diff (ambos deben aprobar; la evidencia técnica sigue mandando). Queda como opción del Paso 5, no como requisito. No se añaden seis agentes a un grafo.

## 4. Roadmap técnico de concurrencia

### 4.1 Modelo de capacidad

| recurso | límite hoy | cómo lo ve el coordinador |
|---|---|---|
| Ejecuciones activas por objetivo | — | `GoalLimits.max_concurrency` (1 en el Paso 1) |
| Ejecuciones activas por repositorio | — | `RepositoryPolicy.max_active_runs` (1 por defecto: la base no puede moverse; >1 solo con `integration_mode=parallel_disjoint`) |
| Ejecuciones activas globales (todos los objetivos) | — | `SUPERVISOR_MAX_ACTIVE_CHILDREN` (3) |
| Plazas del runner | `RUNNER_MAX_CONCURRENT=2` (VM de 8 GB; 16 GB para 4) | `SANDBOX_MAX_CONCURRENT_JOBS` declarado en Settings de la API. Cada ejecución ocupa **como mucho una plaza a la vez** (sus programas corren en secuencia), así que acotar las hijas activas acota las plazas; se reserva una para ejecuciones creadas a mano, que el coordinador no ve |
| Worktrees por proyecto | `worktree_max_per_project=20` | ya lo aplica `WorktreeManager` |
| Modelo | cuotas de Vertex | `BudgetedLLM` por hija; el coordinador reserva antes de crear |

"3 Developers + 2 QA simultáneos" exige, en infraestructura, `RUNNER_MAX_CONCURRENT ≥ 3` (idealmente 4–5) y una VM dimensionada; no es un problema de software de AgentForge sino de plazas. El coordinador nunca debe provocar `runner_busy` por diseño: cuenta hijas en fase `developer`/`apply_changes`/`qa` (visible por el último evento de la hija) y no supera las plazas.

### 4.2 Worktrees separados y bases Git verificables

Ya existe: un worktree por ejecución bajo `WORKTREES_ROOT/<proyecto>/<run_id>/tree`, rama `run/<run_id>`, `base_sha` registrado en el alcance y comprobado en la publicación (`base_moved`). Se añade:

- `GitRepositoryPort.is_ancestor(repository_path, ancestor: str, descendant: str) -> bool` (argv fijo `merge-base --is-ancestor`, solo lectura, ejecutado sobre el espejo por la plataforma). Es la única primitiva nueva de Git.
- Cada tarea guarda `base_sha` y, al publicarse, `head_sha` y `pr_number`; `goal_events.task_base_recorded` deja constancia.
- Invariante por tarea dependiente: `is_ancestor(merge_commit_sha(dep), base_sha(tarea))` para toda dependencia. Se comprueba cuando la hija propone su alcance (antes de que una persona lo apruebe) y de nuevo la propia hija lo comprobará indirectamente en `prepare_publication` (`base_moved`).

### 4.3 Dependencias: protocolo `after_merged`

Para una tarea A `published` de la que depende B:

1. **Evidencia de fusión** (plataforma, `IntegrationProbe` en `app/infrastructure/github` + espejo): `GET /repos/{o}/{n}/pulls/{pr_number}` con un token de instalación **`pull_requests: read`** (nuevo conjunto `PULLS_READ_PERMISSIONS`, permitido aunque `GITHUB_WRITE_ENABLED=false`; la instalación ya concede `pull_requests` en el sandbox). Se leen `merged`, `merged_at`, `merge_commit_sha`, `state`. `PullRequestInfo` gana esos campos opcionales. Con "squash" o "rebase" el `head_sha` de A **no** aparece en la historia de la base, por eso la evidencia primaria es la API y no la ancestría de `head_sha`.
2. **Verificación en el espejo**: `MirrorService.refresh` y `is_ancestor(merge_commit_sha, refs/remotes/origin/<base>)` debe ser verdadero; si la API dice fusionado pero el espejo no lo contiene, la tarea queda `awaiting_merge` con anomalía `merge_unverified` y se reintenta en el siguiente tic.
3. **Registro**: `merge_evidence` en `goal_tasks`, evento `dependency_merged {task_key, pr_number, merge_commit_sha, base_after}`.
4. **Liberación**: B pasa a `ready`; al crearse, su Scoper refresca el espejo y registra `base_sha`; el coordinador exige `is_ancestor(merge_commit_sha(A), base_sha(B))`.
5. **PR cerrado sin fusionar** → A `failed/pr_closed`; B y sucesoras `skipped`; objetivo `blocked` → `partial`.
6. **PR sin decisión** más de `merge_reminder_seconds` (por defecto 24 h) → `goal_needs_human` ("fusiona #n o cancela el objetivo"); al vencer `deadline` → `expired`.

Sin API de GitHub disponible (sin App, o repositorio sin `remote`) no hay evidencia posible: los objetivos con dependencias se rechazan al crearse. No existe modo "confiar sin verificar".

### 4.4 Qué ocurre cuando dos tareas tocan el mismo archivo

| modo | qué pasa | quién lo detecta | resultado |
|---|---|---|---|
| `sequential` + `after_merged` (Paso 1) | B parte de una base que ya contiene A; el Developer de B lee la versión fusionada del archivo; QA valida el estado combinado | no hay conflicto que detectar | Correcto por construcción |
| `sequential` sin dependencia declarada entre A y B | Igual: con `max_concurrency=1` y `max_active_runs=1`, B no se crea hasta que A está `published`; pero B parte de la base **sin** A si A aún no se fusionó → B edita la versión antigua → al fusionar A, el PR de B entra en conflicto en GitHub o lo pisa silenciosamente | el coordinador compara `scope_paths` de B (de la propuesta pendiente) con los `scope_paths` **aprobados** de A, los únicos que A pudo escribir (`workspace.write_file` solo admite el alcance aprobado) | B se mantiene en `waiting_human` con aviso `scope_overlap`; recomendación: fusionar A y re‑crear B (o rechazar B). No se aprueba nada automáticamente |
| `parallel_disjoint` (Paso 4) | A y B simultáneas desde la misma base; con alcances aprobados disjuntos ambos PR son fusionables, pero fusionar A mueve la base y cualquier hermana que aún no haya publicado fallará con `base_moved` | regla dura: en este modo **toda hermana publica antes de que se fusione cualquiera**; el aviso de publicación del objetivo lo dice; el coordinador detecta la fusión prematura (la base del espejo avanzó) y marca las hermanas sin publicar como `base_moved_expected` | las no publicadas fallarán en `prepare_publication` con `base_moved` y se re‑crean desde la base nueva (cuenta como reintento); las publicadas se fusionan en orden; la verificación final (§4.6) comprueba la compatibilidad semántica |
| `parallel_disjoint` con alcances que se solapan | detectado al llegar la segunda propuesta de alcance | coordinador (`scope_paths` ∩ ≠ ∅) | la segunda no se deja aprobar: se cancela (`scope_overlap`, D3) y vuelve a `pending` con dependencia implícita de la primera |

Nunca se rebasea, nunca se fusiona, nunca se combina texto de dos ramas en la plataforma.

### 4.5 Presupuestos agregados

Reserva antes de crear, como `BudgetedLLM`: una hija nace solo si `llm_calls_total + policy.max_model_calls ≤ max_total_model_calls` y `reserved_usd_total + policy.max_reserved_usd ≤ max_total_usd`. Lo consumido se lee de `runs.llm_calls` y del `usage.reserved_usd` del último evento de cada hija (`scope_proposed`, `developer_completed`, `qa_reviewed` lo llevan). La planificación y la verificación cuentan contra el mismo presupuesto. Límites por defecto y topes: `max_tasks` 5/8, `max_concurrency` 1/3, `max_retries_per_task` 1/2, `max_total_model_calls` 8 + tareas × `policy.max_model_calls`, `max_total_usd` 25/100, `deadline` 7/14 días, `max_fix_tasks` 1/2.

### 4.6 Integración y pruebas finales sobre resultados compatibles

Nueva ejecución `verification` (`WorkflowKind.VERIFICATION`; la migración amplía `ck_runs_run_workflow_kind`), creada por el coordinador cuando **todas** las tareas están `merged` y `verify_after_merge=true`:

`refresh_mirror → worktree desde la base (registra base_sha; debe contener todos los merge_commit_sha del objetivo) → exec.run ruff_check, ruff_format_check, pytest (plataforma, sin modelo) → [opcional, Paso 5] revisión asesora del Architect sobre el diff base_antes..base_después → completed | failed`.

- Autoridad: `VerificationScope` (exec‑only, sin rutas escribibles, ligado a `run_id`, worktree y caducidad) y `VerificationToolPolicy`. Es una **segunda vía** de `exec.run` además de `DevelopmentToolPolicy` (ADR 0008 la declara única): requiere aprobación explícita (D6). Mantiene la restricción `sandbox-*` del MVP mientras exista.
- Sin decisión humana propia: no escribe ni publica; la política del repositorio debe tener `verification.enabled=true` y el objetivo `verify_after_merge=true` (D7).
- Resultado en `goal_events.verification_finished {programs: [{program, exit_code}], base_sha}`; nunca `stdout` en el evento (queda en el checkpoint de la ejecución, como en QA).
- Éxito → objetivo `completed`. Fallo técnico de la verificación (`runner_busy`, `runner_unreachable`, timeout) → re‑creación acotada. Fallo de las pruebas → `blocked/integration_failed` (§4.9).

### 4.7 Límites de concurrencia por objetivo y globales

Aplicados en la transacción de programación, con `pg_advisory_xact_lock('agentforge:supervisor:capacity')` para que dos coordinadores (dos procesos de la API) no superen el tope global por una carrera:

```
activas_objetivo  = tareas del objetivo en {creating, running, waiting_human}
activas_repo      = tareas de cualquier objetivo en ese repositorio en esos estados
activas_global    = tareas de cualquier objetivo en esos estados
tope_global       = min(SUPERVISOR_MAX_ACTIVE_CHILDREN, SANDBOX_MAX_CONCURRENT_JOBS - 1)   # Paso 1
                  = SUPERVISOR_MAX_ACTIVE_CHILDREN                                           # Paso 2, con D10
crear si  activas_objetivo < max_concurrency
       y  activas_repo     < policy.max_active_runs
       y  activas_global   < tope_global
```

El coordinador solo controla la **creación**; una hija aprobada horas después entra en su fase de sandbox cuando la persona decide, no cuando el coordinador quiere. Por eso en el Paso 1 el tope global se ata a las plazas del runner (cada ejecución usa como mucho una plaza a la vez) y en el Paso 2, con el reintento acotado de `runner_busy` dentro de `DevelopmentSession.checks` (D10), el tope puede superar las plazas y la presión la absorbe la espera. Ese reintento es seguro: el runner rechaza `runner_busy` **antes** de la reclamación idempotente, así que repetir la misma `execution_id` no deja rastro ni duplica efectos. Cuando no hay plaza, la tarea queda `ready` y el objetivo registra `capacity_wait` una vez por tarea; ninguna hija falla por falta de capacidad.

### 4.8 Architect como rol especializado

Tres facetas, ninguna de las cuales añade un agente nuevo:

1. **Una revisión arquitectónica por objetivo, no por hija** (Paso 1, D4): el Architect revisa el plan completo en la ejecución `planning` del objetivo; las hijas enrutan `fast` con motivo `planned_upstream` y no repiten Planner ni Architect salvo que el Scoper escale.
2. **Architect con contexto del repositorio** (Paso 4): la plataforma construye el listado de rutas y los archivos clave con los mismos filtros del Scoper (globs excluidos, señales de inyección) y los pasa como dato a Planner y Architect en la planificación del objetivo. `DEFAULT_AGENT_GRANTS` ya concede lectura al Architect; el Planner sigue sin herramientas.
3. **Architect asesor sobre el resultado integrado** (Paso 5, opcional): `ArchitectureReview` sobre el diff `base_antes..base_después` dentro de la verificación; informativo, `approved=false` lleva a `needs_human`, nunca a una corrección automática.

### 4.9 Qué ocurre cuando una ejecución falla durante la integración

| caso | señal | acción del coordinador | acción humana posible |
|---|---|---|---|
| Fallo técnico de la verificación | `failed` con código transitorio | re‑crear la verificación (`attempt+1`) hasta `max_retries_per_task` | ninguna |
| Pruebas rojas sobre la base integrada | `verification_finished` con `exit_code ≠ 0` | objetivo `blocked/integration_failed`, aviso con programas y códigos, enlace a la ejecución | `POST /goals/{id}/tasks` (D8): una **tarea de corrección** escrita por la persona (texto humano, nunca generado desde la salida de las pruebas), con dependencia implícita de todas las fusionadas; cuenta en presupuesto y en `max_fix_tasks`; al fusionarse, se verifica de nuevo. O cerrar como `partial` |
| Base de la verificación no contiene todas las fusiones | `is_ancestor` falso tras refrescar | `awaiting_merge` + anomalía | fusionar lo que falte |
| Fusión prematura en `parallel_disjoint` | base del espejo avanzó con hermanas sin publicar | marca `base_moved_expected`; las afectadas fallan en `prepare_publication` y se re‑crean desde la base nueva | nada; se avisa de que publicar antes de fusionar evita el coste |
| La plataforma cae a mitad de la verificación | hija `interrupted` → la recuperación la cierra como `development_interrupted` (misma política que desarrollo) | re‑crear | ninguna |

La plataforma **nunca revierte una fusión** ni empuja correcciones: toda corrección es una tarea nueva con sus aprobaciones.

## 5. Contratos propuestos

Esquemas Pydantic (`extra="forbid"` en entradas, `frozen` en registros). Nombres y campos para discusión.

```python
# app/contracts/goals.py
class GoalDecomposition(StrEnum):
    AUTO = "auto"
    SINGLE = "single"
    PLAN = "plan"


class DependencyPolicy(StrEnum):
    AFTER_MERGED = "after_merged"  # única en el Paso 1


class IntegrationMode(StrEnum):
    SEQUENTIAL = "sequential"
    PARALLEL_DISJOINT = "parallel_disjoint"  # Paso 4


class GoalStatus(StrEnum):
    PLANNING, ACTIVE, BLOCKED, VERIFYING, COMPLETED, PARTIAL, FAILED, CANCELLED, EXPIRED


class GoalTaskStatus(StrEnum):
    (
        PENDING,
        READY,
        CREATING,
        RUNNING,
        WAITING_HUMAN,
        PUBLISHED,
        AWAITING_MERGE,
    )
    MERGED, FAILED, SKIPPED, DUPLICATE


class GoalLimits(BaseModel):  # instantánea en goals.limits
    max_tasks: int = Field(5, ge=1, le=8)
    max_concurrency: int = Field(1, ge=1, le=3)
    max_retries_per_task: int = Field(1, ge=0, le=2)
    max_fix_tasks: int = Field(1, ge=0, le=2)
    max_total_model_calls: int = Field(..., ge=1)
    max_total_usd: float = Field(25.0, gt=0, le=100)
    deadline_seconds: float = Field(7 * 86_400, gt=0, le=14 * 86_400)
    merge_reminder_seconds: float = Field(86_400, gt=0)


class CreateGoal(BaseModel):
    objective: str = Field(min_length=20, max_length=4000)
    project_id: str | None = Field(None, pattern=PROJECT_ID_PATTERN)
    repository_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    decomposition: GoalDecomposition = GoalDecomposition.AUTO
    dependency_policy: DependencyPolicy = DependencyPolicy.AFTER_MERGED
    integration_mode: IntegrationMode = IntegrationMode.SEQUENTIAL
    verify_after_merge: bool = False  # True exige verification.enabled en la política
    limits: GoalLimits | None = None  # None = valores de Settings


class GoalTaskRead(BaseModel):
    task_key: str
    title: str
    status: GoalTaskStatus
    dependencies: list[str]
    run_id: str | None
    run_status: RunStatus | None
    pending_stage: DecisionStage | None
    approval_deadline: datetime | None
    pending_digest: str | None
    base_sha: str | None
    head_sha: str | None
    pr_number: int | None
    pr_url: str | None
    merge_evidence: MergeEvidence | None
    attempt: int
    last_error_code: str | None


class GoalResponse(BaseModel):
    goal_id: str
    status: GoalStatus
    owner: str
    project_id: str
    repository_id: str
    decomposition: GoalDecomposition
    integration_mode: IntegrationMode
    plan_run_id: str | None
    verification_run_id: str | None
    tasks: list[GoalTaskRead]
    limits: GoalLimits
    usage: GoalUsage  # llm_calls_total, reserved_usd_total, tasks_created, retries
    deadline: datetime
    error: RunError | None


class GoalEventKind(StrEnum):
    (
        GOAL_CREATED,
        ROUTE_SELECTED,
        PLAN_REQUESTED,
        PLAN_APPROVED,
        PLAN_REJECTED,
    )
    (
        TASKS_MATERIALIZED,
        TASK_READY,
        CHILD_CREATED,
        TASK_BASE_RECORDED,
        TASK_WAITING_HUMAN,
    )
    (
        SCOPE_OVERLAP,
        TASK_PUBLISHED,
        DEPENDENCY_MERGED,
        PR_CLOSED,
        TASK_FAILED,
        TASK_RETRIED,
    )
    (
        TASK_SKIPPED,
        TASK_DUPLICATE,
        CAPACITY_WAIT,
        BUDGET_EXHAUSTED,
        NEEDS_HUMAN,
    )
    (
        VERIFICATION_REQUESTED,
        VERIFICATION_FINISHED,
        INTEGRATION_FAILED,
        FIX_TASK_ADDED,
    )
    GOAL_COMPLETED, GOAL_PARTIAL, GOAL_FAILED, GOAL_CANCELLED, GOAL_EXPIRED
```

```python
# app/goals/ports.py (dominio; implementaciones en app/infrastructure)
class MergeEvidence(BaseModel):          # frozen
    pr_number: int; merged_at: datetime; merge_commit_sha: str
    verified_in_mirror: bool; base_after: str | None

class IntegrationProbe(Protocol):        # plataforma; nunca lo ve un agente
    async def merge_evidence(self, spec: RemoteSpec, pr_number: int) -> MergeEvidence | None: ...
    async def base_contains(self, repository_id: str, project_id: str, sha: str) -> bool: ...
    async def base_head(self, repository_id: str, project_id: str) -> str: ...

class CapacityLedger(Protocol):          # cuenta tareas activas bajo bloqueo consultivo
    async def active(self, *, goal_id: UUID, repository_id: str) -> CapacitySnapshot: ...

class GoalRepository(Protocol):
    async def create_goal(...) -> PersistedGoal | None   # idempotente por clave
    async def claim_due(self, *, owner: str, now: datetime, limit: int, lease_seconds: float) -> list[PersistedGoal]
    async def update_goal(self, goal_id: UUID, patch: GoalPatch, *, lease: str) -> PersistedGoal | None  # CAS por estado
    async def upsert_tasks(...); async def update_task(...)     # transiciones condicionales
    async def append_events(self, goal_id: UUID, events: Sequence[GoalEvent]) -> int  # + insert_outbox
    async def list_goals(...); async def get_goal(...); async def list_events(...)
```

Adiciones a puertos existentes (todas de lectura, todas de plataforma):

- `GitRepositoryPort.is_ancestor(repository_path: Path, ancestor: str, descendant: str) -> bool`.
- `PullRequestInfo` + `merged_at: datetime | None`, `merge_commit_sha: str | None`; `GitHubClient.get_pull_request(token, owner, name, number)`; `credentials.PULLS_READ_PERMISSIONS = {"pull_requests": "read", "metadata": "read"}` admitido sin `GITHUB_WRITE_ENABLED`.
- `RepositoryPolicy` + `max_active_runs: int = 1` y `verification: VerificationPolicy(enabled=False, programs=("ruff_check","ruff_format_check","pytest"), wall_seconds=600)`.
- Entrada opcional del grafo de desarrollo `planned_upstream: {goal_id, plan_digest}`: el `router` registra `reason="planned_upstream:<reglas>"` y elige `fast` salvo `allow_fast_route=false` (D4).
- `Settings`: `SUPERVISOR_ENABLED`, `SUPERVISOR_TICK_SECONDS` (60), `SUPERVISOR_MAX_ACTIVE_CHILDREN` (3), `SANDBOX_MAX_CONCURRENT_JOBS` (2), `GOAL_DEFAULT_*` para `GoalLimits`.

API (`/api/v1/goals`): `POST` (201, `Idempotency-Key`), `GET` (lista paginada), `GET /{id}`, `GET /{id}/events`, `POST /{id}/cancel`, `POST /{id}/reconcile` (`recovery:sweep`), `POST /{id}/tasks` (tarea de corrección, `repos:write`, D8). Errores Problem Details: `goal_not_found` 404, `goal_not_cancellable` 409, `goal_dependency_cycle` 400, `goal_dependencies_unsupported` 400, `goal_limit_reached` 409, `goal_idempotency_key_reused` 409, `goal_verification_unavailable` 400. Las aprobaciones siguen en `POST /runs/{run_id}/approve`.

## 6. Ubicación en el código y cambios en lo existente

Nuevo: `app/contracts/goals.py`; `app/goals/` (dominio puro: `dag.py`, `state.py`, `limits.py`, `rules.py` con `POLICY_VERSION="supervisor-v1"`, `tasks.py`, `report.py`, `ports.py`); `app/orchestration/goals.py` (`GoalService`, `GoalCoordinator`); `app/orchestration/verification.py` (grafo `verification`, Paso 3); `app/infrastructure/db/goals.py` + modelos + `migrations/versions/0010_*`; `app/infrastructure/github/integration.py` (`IntegrationProbe`); `app/api/goals.py`; `scripts/agentforge.py` (`goal`, `goals`, `goal-show`, `goal-cancel`, `goal-fix`); pruebas unitarias e integración.

Aditivo en lo existente (cada punto enumerable y probado por separado): `app/agents/planner.py` (prompt 1–5), `app/contracts/planning.py` (validación de ciclos), `app/git/ports.py` + `app/infrastructure/git/local.py` (`is_ancestor`), `app/infrastructure/github/client.py`/`credentials.py` (PR fusionado, permiso de lectura), `app/projects/policy.py` (`max_active_runs`, `verification`), `app/security/policy.py` (rol `goal_delegate`), `app/security/development.py` (`VerificationScope`, Paso 3), `app/orchestration/scoped_development.py` (entrada `planned_upstream`, Paso 1; reintento de `runner_busy` en `development.py`, Paso 2), `app/contracts/lifecycle.py` (`WorkflowKind.VERIFICATION`, Paso 3), `app/notifications/*` (tipos y `GoalContext`), modelo y migración de `notification_outbox`, `app/main.py` (cableado, gancho compuesto, tic), `tests/unit/test_tool_layer_boundaries.py`.

No se tocan: `RunService`, `RecoveryService`, el Tool Gateway, el Publisher, los agentes salvo el prompt del Planner.

## 7. Opciones consideradas (resumen)

Supervisor LLM con bucle propio: duplica Planner y Router, cuesta una llamada por tic durante días, convierte textos de tareas y errores en canal de inyección hacia la creación de trabajo y necesitaría una herramienta `runs.create` con autoridad para un modelo, lo que rompe "un `ToolCall` nunca aporta autoridad" y "los agentes solo tienen `tools:execute`". **Descartado por el usuario.** El modelo sigue donde ya está (Planner, Architect, Scoper, Developer, QA, clasificador) y, como asesor opcional, en el resumen final y en la revisión de integración del Paso 5.

Fan‑out de agentes dentro de un grafo: ver §3. **Descartado** salvo la doble revisión QA de solo lectura (Paso 5, opcional).

## 8. Decisiones arquitectónicas

### Aprobadas por el usuario el 2026-10-27 (segunda ronda), con su redacción

- **D3** — `runs:cancel` autorizado **exclusivamente sobre ejecuciones hijas propias**, con comprobación de propiedad (la hija debe constar en `goal_tasks` del objetivo y tener `owner_subject` igual al del objetivo) y auditoría (`goal_events.child_cancelled` con motivo). Nunca sobre ejecuciones ajenas.
- **D4** — Las hijas de un plan general aprobado usan `fast` con motivo `planned_upstream` cuando la política del repositorio permite omitir Planner y Architect (`allow_fast_route=true`). **No omite** el Scoper ni las aprobaciones humanas de `scope` y `publish`.
- **D5** — `after_merged` como política inicial de dependencias: una dependiente no comienza hasta verificar la fusión del PR anterior **en GitHub** y confirmar la base correspondiente **en el espejo**. No se presupone que el `head_sha` sea ancestro (squash merge). Si la integración no se puede verificar, el objetivo se detiene.
- **D6 y D7** — Aceptada la vía futura de verificación de integración (herramientas en sandbox de solo lectura, políticas estrictas, sin autoridad de publicación). **No se implementa en el Paso 1**; pertenece al Paso 3.
- **D10** — Reintentos limitados y seguros ante `runner_busy`, respetando idempotencia, cancelaciones, leases y presupuestos; nunca ejecuciones duplicadas. Ubicación (Paso 1 o Paso 2) por confirmar.
- **D12** — Nombre: **Fase 8 — Coordinación de ejecuciones**.

### Pendientes (texto exacto y valor recomendado)

| # | decisión (texto exacto) | recomendación | afecta al Paso 1 |
|---|---|---|---|
| D1 | Objetivo de un solo repositorio en los Pasos 1–4 | Sí: `goals.repository_id` obligatorio, `CreateGoal.repository_id` obligatorio, todas las tareas del plan se ejecutan en ese repositorio; multi‑repositorio en un ADR posterior | **Sí** (modelo de datos, contrato de creación, índice de duplicados, `IntegrationProbe`) |
| D2 | Tareas dependientes sobre repositorios sin remoto | Rechazar al crear: en un repositorio sin `remote` solo se admite `decomposition=single` (una hija, sin dependencias posibles); `auto` y `plan` responden `400 goal_dependencies_unsupported`. Alternativa descartada: permitir `plan` y fallar al materializar si el plan aprobado trae dependencias (gasta una planificación y una aprobación humana para fallar) | **Sí** (validación en `GoalService.create`) |
| D8 | Tareas de corrección escritas por una persona (`POST /goals/{id}/tasks`), nunca generadas desde la salida de las pruebas | Sí, con `max_fix_tasks` (1 por defecto, tope 2) | No (Paso 3; `GoalLimits` del Paso 1 no incluye `max_fix_tasks`) |
| D9 | `completed` exige verificación superada cuando está habilitada; si no, `partial` | Sí | No (Paso 3; en el Paso 1 `completed` = todas las tareas en `published`/`merged`, o `completed` de la hija en repositorios locales) |
| D11 | Doble QA intra‑ejecución y Architect asesor en la verificación | Posponer al Paso 5; no bloquean nada | No |

## 9. Plan incremental con criterios de aceptación

Cada paso arranca con aprobación explícita, termina con las verificaciones obligatorias del repositorio en verde y, cuando toca el sandbox real, con una validación documentada en `docs/validation/`.

### Paso 1 — Objetivos secuenciales con `after_merged`

Alcance: migración 0010; `GoalService`, `GoalCoordinator`; `decomposition` `auto`/`single`/`plan`; `max_concurrency=1`, `max_active_runs=1`; `IntegrationProbe` (PR fusionado + `is_ancestor`); duplicado exacto; recordatorios; cancelación con propagación (D3); avisos `priority`; API y CLI; cambios aditivos: Planner 1–5 y ciclos, `is_ancestor`, PR fusionado y `PULLS_READ_PERMISSIONS`, rol `goal_delegate`, `planned_upstream`, bandeja con `goal_id`, gancho compuesto. Sin re‑creación automática de hijas (`max_retries_per_task=0`): una hija fallida deja la tarea `failed` y el objetivo `blocked` con el código; la re‑creación acotada llega en el Paso 2. La resiliencia del propio coordinador (reinicios, base de datos o GitHub no disponibles durante un tic) sí forma parte del Paso 1.

Criterios de aceptación:

1. **Unitario** (FakeLLM, `InMemoryRunRepository`, `FakeGitHub`, remotos `file://`): un objetivo `plan` con tres tareas A→B→C crea A; A `published`; el PR de A fusionado (simulado por `FakeGitHub` + commit en el remoto `file://`) libera B; B se crea con `base_sha` que contiene el `merge_commit_sha` de A (prueba de `is_ancestor`); C igual; objetivo `completed`.
2. Un plan con ciclo termina `failed/goal_dependency_cycle` sin crear hijas; un objetivo con dependencias sobre un repositorio sin remoto responde `400 goal_dependencies_unsupported`.
3. El PR de A cerrado sin fusionar → A `failed/pr_closed`, B y C `skipped`, objetivo `partial`, un solo aviso `goal_needs_human`.
4. Dos reconciliaciones concurrentes (dos coordinadores) sobre el mismo objetivo crean **una** hija (clave de idempotencia y `run_id` preasignado; prueba con la repetición tras una caída simulada entre escribir `run_id` y crear).
5. El principal delegado no puede aprobar, reanudar ni publicar (prueba de `AuthorizationPolicy`); la prueba estática de fronteras pasa; `app/goals` importa solo `app/contracts` y `app/security`.
6. Presupuesto: con `max_total_model_calls` justo para dos hijas, la tercera queda `ready` con `budget_exhausted` y el objetivo termina `partial`.
7. **Integración** (PostgreSQL): migración 0010 arriba y abajo con el rechazo del downgrade; `claim_due` con `SKIP LOCKED` bajo dos sesiones; avisos de objetivo en la bandeja con `run_id` nulo entregados por el worker.
8. **Validación real** (autorización explícita, sandbox `agentforge-sandbox-demo`): un objetivo de dos tareas dependientes recorre plan, alcances, publicaciones, fusión manual de la primera y creación de la segunda desde la base fusionada; evidencia en `docs/validation/fase8-paso1-criterios.md`.

### Paso 2 — Capacidad y límites globales

Alcance: `SUPERVISOR_MAX_ACTIVE_CHILDREN`, `SANDBOX_MAX_CONCURRENT_JOBS`, `CapacityLedger` con bloqueo consultivo, `capacity_wait`; reintentos por re‑creación con `RETRYABLE_CHILD_CODES` (incluye `base_moved`, `development_interrupted`, `runner_busy`, `runner_unreachable`, `mirror_sync_failed`, `mirror_locked`, `llm_timeout`, `llm_unavailable`, `abandoned`); reintento acotado de `runner_busy` en `DevelopmentSession.checks` (D10); `max_active_runs` por repositorio.

Criterios: con tope global 2 y tres objetivos listos, solo dos hijas existen a la vez y la tercera arranca al terminar una (prueba con reloj controlado); dos procesos no superan el tope (prueba de integración con dos sesiones y el bloqueo consultivo); un `runner_busy` simulado por `FakeBackend` se reintenta dentro de `wall_seconds` y la ejecución continúa; `base_moved` provoca una re‑creación con base nueva y `attempt=1`, y una segunda `base_moved` agota el reintento y bloquea el objetivo.

### Paso 3 — Verificación de integración

Alcance: `WorkflowKind.VERIFICATION` y grafo; `VerificationScope`/`VerificationToolPolicy` (D6, ADR 0008 actualizado); `verification` en `RepositoryPolicy`; `verifying`, `integration_failed`; `POST /goals/{id}/tasks` (D8); `completed` condicionado (D9).

Criterios: con `verify_after_merge=true`, un objetivo cuyas tareas están todas `merged` crea exactamente una verificación cuyo `base_sha` contiene todos los `merge_commit_sha`; pruebas verdes → `completed`; pruebas rojas (fixture con un test que falla en la base fusionada) → `blocked/integration_failed`, aviso con programas y códigos, y una tarea de corrección humana que, al fusionarse, dispara una nueva verificación verde → `completed`; la verificación no tiene decisión pendiente en ningún momento (`GET /runs/{id}/decisions` vacío); `exec.run` fuera del `VerificationScope` es denegado (prueba del gateway); la migración amplía la restricción de `workflow_kind` y el downgrade se niega si existen ejecuciones `verification`.

### Paso 4 — Hermanas disjuntas en paralelo y Architect con contexto

Alcance: `integration_mode=parallel_disjoint` (`max_concurrency` hasta 3, `max_active_runs` hasta 3); detección de solapamiento de `scope_paths` con cancelación y re‑encolado (D3); regla "publicar todas antes de fusionar" con detección de fusión prematura; verificación obligatoria en este modo; Architect de objetivo con contexto del repositorio (listado y archivos clave construidos por la plataforma con los mismos filtros del Scoper: `excluded_context_globs`, señales de inyección) en la ejecución `planning` del objetivo; Planner recibe el listado como dato.

Criterios: tres tareas independientes con alcances disjuntos avanzan a la vez sin superar las plazas del runner (prueba con `FakeBackend` contando ejecuciones simultáneas); dos propuestas de alcance que comparten un archivo → la segunda se cancela con `scope_overlap` y vuelve a `pending` dependiendo de la primera; fusionar una hermana antes de que las otras publiquen produce `base_moved_expected`, re‑creación y verificación final verde; el plan de un objetivo con contexto nombra solo rutas existentes (prueba con FakeLLM guionizado y comprobación determinista de las rutas).

### Paso 5 (opcional) — Asesores acotados

Doble QA de solo lectura dentro de una ejecución (ambos deben aprobar; evidencia técnica manda), Architect asesor sobre el diff integrado en la verificación (`ArchitectureReview` informativo; `approved=false` → `needs_human`, nunca corrección automática), resumen narrativo del objetivo (`GoalSummary`, ≤ 1200 caracteres, presupuestado, informativo). Criterios: ninguna de las tres llamadas cambia un estado; cada una está desactivada por defecto y acotada por `BudgetedLLM`.

## Notas de implementación del Paso 1 (2026-10-30)

Entregado en la rama `feat/phase-8-goal-supervisor` (worktree `../multiagent-starter-phase8`), sin tocar la base de desarrollo ni el Runner ni la GitHub App:

- **Decisiones aplicadas**: D1 (un repositorio por objetivo, `repository_id` obligatorio), D2 (sin remoto solo `single`; `auto`/`plan` → `400 goal_dependencies_unsupported`), D3 (`goal_delegate` con `runs:cancel` solo sobre hijas propias, con comprobación de propiedad y evento `child_cancelled`), D4 (`planned_upstream` en `SmartRouter.decide` y en el nodo `router`; el Scoper y las aprobaciones humanas siguen), D5 (`GitHubIntegrationProbe`: `GET /pulls/{n}` con token `pull_requests: read`, `merge_commit_sha` y `merge-base --is-ancestor` en el espejo refrescado; sin evidencia, la dependiente espera), D10 pospuesta al Paso 2, sin re‑creación automática de hijas fallidas.
- **Ajustes respecto al diseño**: `goal_tasks.run_id` y `goals.plan_run_id` **no llevan clave foránea** a `runs`, porque el id se reserva antes de que exista la fila (la prueba de integración lo demostró); `app/goals` importa `contracts` y `security` (como `app/tools`), no solo `contracts`; los motivos de bloqueo se derivan del estado durable en cada tic (`task_failed`, `pr_closed`, `budget_exhausted`) además de las señales del tic (`decision_pending`, `merge_pending`, `merge_unverified`, `plan_pending`), y un objetivo solo pasa a `blocked` cuando ninguna hija está `creating`/`running`; una base obsoleta (`base_stale`) cancela a la hija y re‑encola la tarea para el tic siguiente, con un máximo de dos intentos; los leases de objetivo usan el reloj del coordinador, no el de PostgreSQL.
- **Estados de tarea**: `pending → ready → creating → running ⇄ waiting_human → published → merged`, más `failed`, `skipped`, `duplicate`; `awaiting_merge` del diseño se expresa como `published` con dependientes sin fusionar.
- **Cambios en código existente, todos aditivos**: prompt del Planner (1–5 tareas) y validación de ciclos en `DevelopmentPlan`; `RunState.planned_upstream`, `RunService.create(planned_upstream=…)`; rol `goal_delegate` y `app/security/delegation.py`; `LocalGitAdapter.is_ancestor`; `PullRequestInfo.merged_at/merge_commit_sha`, `GitHubClient.get_pull_request`, `PULLS_READ_PERMISSIONS` admitido sin `GITHUB_WRITE_ENABLED`; `notification_outbox.run_id` nullable + `goal_id`; `GoalContext`/`draft_goal` en Notifications; cableado en `app/main.py` (gancho terminal compuesto, tic `SUPERVISOR_TICK_SECONDS`); subcomandos `goal*` en `scripts/agentforge.py`. `RunService`, los grafos, el Tool Gateway, el Publisher y `RecoveryService` no cambian.
- **Pruebas**: `tests/unit/test_goal_coordinator.py` (23 escenarios con `FakeLauncher`/`FakeProbe`), `test_goal_flow.py` (RunService real, FakeGitHub, remoto `file://`, *squash merge* y verificación real en el espejo), `test_goals_domain.py`, `test_api_goals.py`, ampliaciones de router, Git, GitHub, notificaciones, CLI y fronteras; `tests/integration/test_goals_persistence.py` (migración 0010 arriba y abajo con rechazo del downgrade, leases con `SKIP LOCKED`, reservas concurrentes contra presupuesto y capacidad, índice de huella activa, avisos de objetivo entregados).
- **Pendiente de validación real** (autorización explícita): un objetivo de dos tareas dependientes sobre `agentforge-sandbox-demo` con fusión manual del primer PR, evidencia en `docs/validation/fase8-paso1-criterios.md`.

## 10. Riesgos

- **Carga de aprobaciones humanas**: hasta `n` alcances y `n` publicaciones más el plan; mitigación: secuencial por defecto, avisos con digest, vista del objetivo con todo lo pendiente.
- **Dependencia de la fusión humana**: un PR olvidado detiene la cadena; mitigación: recordatorios y `deadline`; sin atajos (decisión 5).
- **`base_moved` por commits externos**: cualquier commit en la rama base durante una hija la invalida; mitigación: re‑creación acotada y aviso del coste.
- **Capacidad del runner**: 2 plazas hoy; "3 Developers + 2 QA" exige redimensionar la VM; el coordinador nunca sobrepasa las plazas declaradas.
- **Semántica de "merged"** con squash/rebase: cubierta por la API + `merge_commit_sha`; sin API no hay dependencias.
- **Más estados y tablas**: complejidad real; mitigación: tablas de transición únicas, reglas versionadas, pruebas exhaustivas con dobles existentes.
