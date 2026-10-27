# AgentForge

Plataforma multiagente para planificación y revisión arquitectónica de tareas de desarrollo, con FastAPI, LangGraph, Claude (Anthropic) vía Vertex AI, aprobación humana y contratos Pydantic.

**Versión 0.5.0 — Fase 5 completada.** AgentForge planifica una tarea de desarrollo, revisa la arquitectura del plan con un ciclo acotado de retroalimentación entre dos agentes y detiene el flujo hasta una aprobación humana. Todo el estado vive en PostgreSQL y sobrevive a reinicios y caídas. Existe un Tool Gateway con auditoría, repositorios Git administrados con un worktree por ejecución y un runner de ejecución aislada validado en Linux real, **pero ningún agente usa todavía herramientas, ninguna herramienta de escritura, ejecución o red está habilitada y ningún runner está desplegado**: todavía no es una plataforma de desarrollo autónomo — ver [Qué hace y qué no](#qué-hace-y-qué-no-hace-todavía), [Runner](#runner-paquete-independiente) y [Roadmap](#roadmap-por-fases).

## Qué hace y qué no hace todavía

Implementado y verificado (197 pruebas unitarias sin servicios externos, 55 de integración contra PostgreSQL 16, ninguna llamada real a Claude):

- Grafo Planner → Architect → aprobación humana, con hasta `MAX_PLAN_REVISIONS` rondas de corrección y un presupuesto de llamadas por ejecución.
- Errores del LLM tipados, reintentos acotados y reparación de salidas malformadas; una ejecución nunca termina en 500 por un fallo del proveedor.
- Checkpoints de LangGraph en PostgreSQL con serialización estricta y cifrado opcional; `thread_id` igual al `run_id`.
- Proyección de cada ejecución a `runs`, decisiones humanas durables en `run_approvals`, auditoría estructurada en `run_events`.
- Leases con heartbeat y token de fencing, barrido de recuperación al arrancar y periódico, apagado ordenado, `Idempotency-Key` en la creación, errores HTTP como Problem Details.
- Autenticación OIDC con verificación local de tokens, autorización denegada por defecto con permisos nominales, aislamiento por proyecto y propietario, separación de funciones en la aprobación humana, idempotencia por proyecto y principal.
- API de control: listado y auditoría con paginación keyset, reanudación manual de ejecuciones interrumpidas bajo lease y barrido administrativo síncrono acotado al ámbito del principal; cabeceras de seguridad.
- Flujo Developer + QA (Fase 6, MVP): autoridad efímera `DevelopmentScope` revocable y ligada a una ejecución, herramienta `workspace.write_file` acotada por lista y hash, grafo con hasta tres correcciones, presupuesto reservado antes de cada llamada, estado terminal `completed`, `workflow_kind` por fila y cierre conservador de ejecuciones interrumpidas; demostrado con Claude real, Runner real y auditoría en PostgreSQL sobre un repositorio desechable (`docs/validation/agentforge-dev-mvp-2026-10-26.md`). Solo invocable por el operador local.
- Tool Gateway (Fase 5): registro tipado de herramientas, contexto de ejecución confiable, identidad de automatización `agentforge:<agente>`, política por agente y proyecto con denegación por defecto, herramientas de solo lectura sobre el workspace y sobre Git, auditoría por paso lógico en `tool_invocations` con estados `pending`, `running`, `ok`, `failed`, `denied`, `invalid_input` y `uncertain`, repositorios Git administrados por configuración, un worktree por ejecución en la rama `run/<run_id>`, y la ejecución aislada: `SandboxPort`, `exec.run`, cliente del runner remoto por Tailscale y el servicio `runner/` (Podman rootless, protocolo v1), cuyo aislamiento se validó en Linux real: ocho pruebas en verde en Ubuntu 24.04 (`docs/validation/runner-podman-ubuntu-24.04.md`). Ningún agente lo usa todavía y ningún runner está desplegado.

Pendiente, por fase del roadmap:

- Cancelación de ejecuciones, caducidad de claves de idempotencia, rate limiting (delegado al gateway) y seguridad a nivel de fila.
- Escrituras Git por agentes, `push`, ejecución de comandos y red: `git.commit`, `git.push` y `exec.run` están registrados como contratos pero la puerta de riesgo los rechaza, y el adaptador no hace `push` en ningún caso. Sin runner configurado, `exec.run` responde `sandbox_unconfigured`: este host nunca ejecuta código no confiable.
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

## Autenticación

Desde la Fase 4 toda ruta bajo `/api/v1` exige un token de acceso OIDC en `Authorization: Bearer`. AgentForge no emite credenciales: verifica tokens JWT de tu proveedor con su JWKS, solo algoritmos asimétricos, y `iss`, `aud`, `exp`, `iat` y `sub` obligatorios. Los probes `/health/*` quedan abiertos.

| Variable | Significado |
|---|---|
| `AUTH_MODE` | `oidc` por defecto. `disabled` convierte cada petición en el administrador local; prohibido en `staging` y `production` |
| `OIDC_ISSUER`, `OIDC_AUDIENCE` | Emisor y audiencia que deben traer los tokens |
| `OIDC_JWKS_URL` o `OIDC_JWKS_FILE` | Exactamente una fuente de claves. El archivo estático solo se admite en `local` y `test` |
| `OIDC_ROLES_CLAIM`, `OIDC_PROJECTS_CLAIM`, `OIDC_KIND_CLAIM` | Dónde viven roles, proyectos y tipo de principal en el token; admiten rutas punteadas |

Sin proveedor a mano, en local:

```bash
uv run python -m scripts.dev_keys                      # genera .local/jwks.json y la clave privada (ignorados por git)
# añade a .env: AUTH_MODE=oidc, OIDC_ISSUER y OIDC_AUDIENCE que imprime, OIDC_JWKS_FILE=.local/jwks.json
TOKEN=$(uv run python -m scripts.dev_token --sub martin --roles admin)
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/runs/ID
```

Un fallo de autenticación responde `401` en Problem Details con código `unauthenticated` y cabecera `WWW-Authenticate: Bearer`; el token nunca se repite en la respuesta ni en los logs.

## Autorización

Denegado por defecto. El código comprueba **permisos**, nunca roles; los roles del token se traducen a permisos con esta tabla; el **ámbito** lo dan los proyectos del token (`projects`) y la propiedad de cada ejecución.

| Rol | `runs:create` | `runs:read` | `runs:approve` | `runs:resume` | `runs:audit:read` | `recovery:sweep` | `projects:all` |
|---|---|---|---|---|---|---|---|
| `viewer` | | ✓ | | | ✓ | | |
| `operator` | ✓ | ✓ | | ✓ | ✓ | | |
| `approver` | | ✓ | ✓ | | ✓ | | |
| `admin` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | |
| `platform_admin` | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |

Reglas:

- Una ejecución pertenece al proyecto en que se creó y al principal que la creó (`owner`, `project_id` en la respuesta). Solo la ven los miembros de ese proyecto con `runs:read`, su propietario mientras siga perteneciendo al proyecto, o quien tenga `projects:all`. Para cualquier otro **no existe**: `404`, nunca `403`, para no revelar recursos ajenos. Perder la pertenencia al proyecto retira también la visibilidad de las ejecuciones propias.
- Sobre una ejecución visible, una operación sin permiso responde `403 permission_denied`. Un `admin` actúa solo dentro de los proyectos de su token; cruzar proyectos exige el permiso explícito `projects:all` del rol `platform_admin`, que además debe indicar `project_id` al crear.
- Crear sin `project_id` usa el único proyecto del token; con varios, `400 project_required`; sin proyectos, `403`.
- **Aprobación humana**: exige `runs:approve` y queda registrada con el sujeto que decidió (`decided_by`, y `actor_subject` en los eventos `approval_recorded`, `run_approved`/`run_rejected`, `approval_applied`). Es distinta del veredicto del Architect (`architect_approved`), que es un agente, y del permiso para aprobar, que es política. **Separación de funciones**: por defecto el creador de un run no puede aprobarlo en `staging` y `production` (`403 self_approval_forbidden`); en `local` y `test` sí, salvo que `AUTHZ_SEPARATION_OF_DUTIES` lo fuerce.
- `Idempotency-Key` tiene ámbito por proyecto y principal: la misma clave en otro proyecto o de otro usuario es otra ejecución, y nunca permite consultar una ajena.
- Ejecuciones anteriores a la autenticación: la migración `0003` las deja en el proyecto reservado `legacy` con propietario `legacy:unowned`. Ningún token puede reclamar ese proyecto; solo `projects:all` las ve. No se asignan a usuarios nuevos.
- Permisos reservados para los agentes de fases posteriores: `tools:execute`, `repos:write`. Ningún rol por defecto los concede.

## API de control

| Endpoint | Permiso | Qué hace |
|---|---|---|
| `GET /api/v1/runs` | cualquier principal autenticado, dentro de su ámbito | Lista ejecuciones visibles, más recientes primero. Filtros `status`, `project_id`, `owner`, `mine`. Quien no tiene `runs:read` ve solo las suyas |
| `GET /api/v1/runs/{id}/events` | ejecución visible y `runs:audit:read` | Traza de auditoría, más antigua primero, con `actor` por evento |
| `POST /api/v1/runs/{id}/resume` | ejecución visible y `runs:resume` | Reanuda una ejecución `interrupted` desde su último checkpoint, bajo lease y contando intento. Síncrono: responde cuando alcanza su siguiente parada. `409 run_not_resumable` si no está interrumpida, otro proceso la tiene o agotó `MAX_RUN_ATTEMPTS` |
| `POST /api/v1/admin/recovery/sweeps` | `recovery:sweep` | Ejecuta un barrido de recuperación **síncrono** y devuelve el informe real: reclamados, resultados y errores. Limitado a los proyectos del principal salvo `projects:all`; `project_id` lo acota más |

Paginación keyset: `limit` entre 1 y 100 y un `cursor` opaco en `next_cursor`. El cursor es una posición, no una capacidad: el ámbito del principal se reaplica en cada página, va ligado a los filtros y al sujeto de la consulta y se valida estrictamente; un cursor ajeno o manipulado responde `400 invalid_cursor`. Las listas ordenan por `(created_at, id)` descendente y los eventos por `id` ascendente, estables bajo escrituras concurrentes.

El barrido administrativo se ejecuta dentro de la petición, acotado por `RECOVERY_BATCH_SIZE`, y puede incluir llamadas al LLM si `RECOVERY_AUTO_RESUME` está activo; concede un tiempo de espera generoso al cliente. No se responde `202` con una tarea en memoria porque esa promesa no sería durable. Cada instancia ejecuta un solo barrido a la vez: uno manual mientras otro corre responde `409 sweep_in_progress`, y el periódico salta ese tick.

## Cabeceras de seguridad y rate limiting

Todas las respuestas llevan `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY` y una `Permissions-Policy` restrictiva; las de `/api/*` añaden `Cache-Control: no-store`; en `staging` y `production` se añade `Strict-Transport-Security`, porque ahí se asume TLS delante. No hay CSP para no romper `/docs`, que en producción está apagado.

**El rate limiting debe vivir en el gateway o ingress**, no en la aplicación: un límite por proceso no protege con varias instancias. Mientras no exista, cualquier token válido puede provocar llamadas al LLM a voluntad; `MAX_LLM_CALLS_PER_RUN` acota cada ejecución, no cada cliente.

## Tool Gateway (Fase 5, Paso 1)

Toda herramienta pasa por `ToolGateway.invoke(context, call)`. El `ToolCall` es lo único que produce el modelo: un nombre y argumentos que se validan contra el esquema cerrado de la herramienta. El `ToolContext` lo construye la orquestación con la ejecución, el intento, el proyecto, el agente, el principal de automatización y la raíz del workspace; nunca se deriva de texto del modelo.

- **Identidad.** Los agentes actúan como `agentforge:<agente>`, un principal de servicio con el rol `agent` (solo `tools:execute`) y un único proyecto. No heredan credenciales ni variables del entorno; ningún rol humano puede invocar herramientas.
- **Política.** Denegación por defecto: emisor confiable, permiso, proyecto, concesión del agente y clase de riesgo, en ese orden. Matriz por defecto: Planner sin herramientas; Architect, Developer, QA y Security con `workspace.*` en clase `read`. Solo `read` es ejecutable en este paso.
- **Herramientas.** `workspace.list_dir`, `workspace.read_file` y `workspace.find`: rutas relativas sin `..`, enlaces simbólicos resueltos dentro de la raíz, `.git` y archivos de credenciales rechazados, lecturas acotadas (`TOOL_MAX_READ_BYTES`, `TOOL_MAX_ENTRIES`), binarios detectados y secretos redactados.
- **Auditoría e idempotencia.** Una fila en `tool_invocations` por `(run_id, step, attempt)`, reclamada antes de ejecutar y cerrada después: estado, código, digests de entrada y salida, duración; nunca argumentos ni contenidos. El paso lógico lo elige la orquestación y sobrevive a reinicios: un reintento con los mismos argumentos vuelve a ejecutar las lecturas; con otros argumentos es `invocation_conflict`; el mismo paso dos veces en un intento es `duplicate_invocation`. Un intento que murió ejecutando deja el paso `uncertain`, y las herramientas con efectos no lo repiten (`uncertain_effects`, `already_completed`). Sin auditoría no se ejecuta nada.
- **Git administrado.** Los repositorios se registran por configuración (`GIT_REPOSITORIES_FILE`, todos bajo `GIT_REPOSITORIES_ROOT`); el modelo solo los nombra por identificador. El adaptador ejecuta el binario `git` sin shell, con argumentos construidos por la aplicación, entorno saneado (sin `~/.gitconfig`, sin agente SSH, sin credenciales, sin hooks) y timeout. `git.log`, `git.diff` y `git.status` leen con argumentos nominales; `git.commit` y `git.push` existen como contratos bloqueados. Las ramas `main`, `master`, `release/*`, la rama por defecto y las etiquetas están protegidas; solo `run/<run_id>` es escribible, con la identidad de servicio `AgentForge`.
- **Ejecución aislada.** `exec.run` (clase `exec`) acepta un programa permitido (`ruff_check`, `ruff_format_check`, `pytest`) y opciones tipadas; la aplicación construye `argv`, empaqueta el worktree sin `.git` ni credenciales y lo envía a un runner Linux remoto (Ubuntu 24.04, Podman rootless, Tailscale) con límites explícitos de CPU, memoria, procesos, tiempo, almacenamiento y salida, y red deshabilitada. `pytest` es código no confiable y solo corre allí. Sin `SANDBOX_RUNNER_URL`, `SANDBOX_RUNNER_TOKEN` y `SANDBOX_RUNNER_ID` no hay ejecución; nunca hay ejecución local de reserva. El cliente verifica identidad y backend del runner antes de ejecutar y cancela en el runner cuando expira el timeout. Protocolo, frontera y plan de despliegue en el ADR 0007.
- **Worktrees.** Uno por ejecución en `<WORKTREES_ROOT>/<proyecto>/<run_id>/tree`, con un marcador fuera del checkout. Creación y recuperación idempotentes, retención tras terminar (`WORKTREE_RETENTION_SECONDS`) y más larga para `interrupted` (`WORKTREE_INTERRUPTED_RETENTION_SECONDS`), cuotas por proyecto y espacio libre mínimo. Al eliminar no se siguen enlaces simbólicos y la rama se conserva.
- **Resultados.** `ToolResult` con `outcome` `ok`, `denied`, `invalid_input` o `failed` y un `ToolError` con código estable; los fallos de la herramienta nunca llegan como excepción al llamador.

Límites: `TOOL_MAX_OUTPUT_BYTES` acota la salida serializada, `TOOL_TIMEOUT_SECONDS` el tiempo por invocación y `GIT_TIMEOUT_SECONDS` cada llamada a `git`. Decisiones y limitaciones en el ADR 0006.

## Repositorios de GitHub mediante espejos locales (Fase 7, Paso 2)

Un repositorio real se declara en la política (`PROJECTS_FILE`) con un bloque `remote`: `owner`, `name`, el **id numérico de GitHub** (`github_repository_id`), el `installation_id` de la GitHub App dedicada y la rama por defecto. AgentForge mantiene un espejo `--bare` bajo `GIT_REPOSITORIES_ROOT/<proyecto>/<name>.git` y, antes de que el Scoper lea nada, la plataforma verifica en GitHub que `owner/name` sigue siendo ese id y pertenece a la instalación autorizada, comprueba que el remoto del espejo es la URL derivada, trae solo `refs/heads/*` y registra el `base_sha` del que parte el worktree. Una transferencia, una sustitución del repositorio, un remoto manipulado o un marcador alterado terminan la ejecución con su código (`repository_substituted`, `repository_transferred`, `mirror_remote_mismatch`, `mirror_identity_mismatch`) sin crear worktree. Las credenciales (`GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY_FILE` con modo `0600`) viven solo en el proceso de la API: tokens de instalación de una hora, limitados a `contents:read` y `metadata:read`, entregados a un único `git fetch` mediante un ayudante `GIT_ASKPASS` temporal; nunca llegan a argv, URLs, logs, auditoría, checkpoints ni agentes. El contenido remoto es dato no confiable: los archivos de instrucciones para agentes (`AGENTS.md`, `CLAUDE.md`, `.cursorrules`, …) se ocultan al Scoper y no pueden entrar en el alcance; la propuesta muestra `base`, `hidden_paths` y `signals` de posible inyección. Auditoría en `mirror_syncs` (migración 0007) y evento `mirror_refreshed`. Operación: `scripts/github_preflight.py` (verificación de solo lectura de App, instalación restringida, identidad, commit remoto, espejo y ausencia de tokens) y `scripts/mirror_repository.py --create|--refresh|--check`; alta de la App e instalación solo en `agentforge-sandbox-demo` en `docs/runbooks/github-app.md`. Sin `push` ni publicación todavía (ADR 0010).

## Desarrollo gobernado por la API (Fase 7, Paso 1)

Una ejecución `development` creada por la API queda **bloqueada hasta que un humano autorizado aprueba un alcance concreto**. El agente Scoper, de solo lectura, propone los archivos; la plataforma los filtra con la política del repositorio (globs escribibles y prohibidos, límites) y publica la propuesta filtrada con su digest; `POST /api/v1/runs/{id}/approve` con `stage=scope` y ese `scope_digest` la autoriza. Solo entonces Developer recibe una autoridad efímera de escritura limitada a esa lista y a ese worktree; QA valida en el Runner como en la Fase 6. Decisiones por etapa en `run_decisions` (migración 0006), consulta en `GET /runs/{id}/decisions`, retirada con `POST /runs/{id}/cancel`, caducidad automática. Requisitos: `PROJECTS_FILE` con la política por repositorio, rol `project_operator` (o `admin`) para crear y aprobar, separación de funciones en entornos productivos. Sin remotos ni `push`: la publicación llega en pasos posteriores de la Fase 7 (ADR 0009).

```bash
curl -s -X POST $API/api/v1/runs -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  -d '{"task": "...", "project_id": "sandbox-lint", "workflow_kind": "development", "repository_id": "sandbox-lint"}'
curl -s $API/api/v1/runs/$RUN/decisions -H "Authorization: Bearer $TOKEN"       # propuesta filtrada + digest
curl -s -X POST $API/api/v1/runs/$RUN/approve -H "Authorization: Bearer $TOKEN2" -H 'Content-Type: application/json' \
  -d '{"approved": true, "stage": "scope", "scope_digest": "<digest>"}'
```

## Flujo Developer + QA (Fase 6, MVP)

El primer flujo multiagente real: Developer edita una lista explícita de archivos en el worktree de la ejecución, QA ejecuta `ruff check`, `ruff format --check` y `pytest` en el Runner remoto y devuelve observaciones, y Developer corrige hasta tres veces. Todo pasa por el Tool Gateway con auditoría por paso; la escritura es la herramienta `workspace.write_file` (archivo completo, ruta en lista, hash esperado, sin enlaces ni secretos). La autoridad es un `DevelopmentScope` efímero ligado a un `run_id`, un proyecto `sandbox-*`, un worktree y una caducidad: las concesiones y la puerta de riesgo del servicio no cambian y la API no concede escritura ni ejecución. Se invoca desde la máquina del operador:

```bash
uv run python -m scripts.development_mvp --seed-demo --yes            # crea y registra sandbox-dev
uv run python -m scripts.development_mvp --fake-model --yes            # fontanería sin modelo (informe model=fake)
uv run python -m scripts.development_mvp --yes --patch-out run.patch   # Claude vía Vertex + Runner real
uv run python -m scripts.development_mvp --profile lint --seed-demo --ruff-diff --yes   # trampa I001 + diff de Ruff
```

Condiciones de fin: `completed` (QA aprueba con los tres programas en cero; `approved` sigue reservado a la decisión humana), `correction_limit`, el código de la herramienta o del Runner que falló, `development_budget_exceeded`, `development_timeout` o cancelación. La fila lleva `workflow_kind=development` (migración 0005) y una ejecución de desarrollo interrumpida nunca se reanuda: el barrido la cierra como `failed/development_interrupted`. Cuando el Runner conoce `ruff_check_diff`, `--ruff-diff` hace que QA adjunte la corrección exacta de Ruff a sus observaciones. Decisiones y limitaciones en el ADR 0008; evidencia de las ejecuciones reales en `docs/validation/agentforge-dev-mvp-2026-10-26.md`.

## Flujo

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/runs \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"task":"Diseña el módulo de pacientes en FastAPI con pruebas unitarias"}'
```

Copia el `run_id` que devuelve la API (código `201`). Opcionalmente envía `Idempotency-Key: <clave>` (hasta 128 caracteres): repetir la misma clave con la misma tarea devuelve la ejecución existente con `200` y `Idempotency-Replayed: true`, sin ejecutar nada; la misma clave con otra tarea responde `409 idempotency_key_reused`. La clave es global al despliegue y queda ligada al run aunque este falle.

```bash
curl -s -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/runs/TU_RUN_ID

curl -s -X POST http://127.0.0.1:8000/api/v1/runs/TU_RUN_ID/approve \
  -H "Authorization: Bearer $TOKEN" \
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
| `status` | `running`, `waiting_approval`, `resume_pending`, `interrupted`, `approved`, `rejected`, `completed`, `cancelled`, `expired` o `failed` (ver tabla siguiente) |
| `pending_stage` | Etapa de decisión humana pendiente: `plan`, `scope` o `publish` (ADR 0009) |
| `approval_deadline` | Cuándo caduca la decisión pendiente, si caduca |
| `workflow_kind` | `planning` (Planner → Architect → aprobación humana) o `development` (Developer → QA, Fase 6) |
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
| `completed` | Un flujo sin decisión humana final (`development`) cumplió su criterio de éxito: QA aprobó con `ruff check`, `ruff format --check` y `pytest` en cero. Terminal |
| `cancelled` | Un humano con `runs:cancel` retiró una ejecución que esperaba una decisión o estaba interrumpida. Terminal |
| `expired` | Nadie decidió dentro de la ventana de la etapa pendiente. Terminal |
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
| `401` | `unauthenticated` | Sin token, token expirado o inválido. Lleva `WWW-Authenticate: Bearer` |
| `403` | `permission_denied` | Autenticado pero sin permiso sobre un recurso visible, o proyecto no autorizado al crear |
| `403` | `self_approval_forbidden` | El creador de la ejecución intenta aprobarla con separación de funciones activa |
| `400` | `project_required` | El token pertenece a varios proyectos y la petición no indica `project_id` |
| `400` | `invalid_cursor` | Cursor de paginación ajeno, manipulado o de otra consulta |
| `409` | `run_not_resumable` | Reanudación manual de una ejecución que no está `interrupted`, que otro proceso tiene o que agotó sus intentos |
| `409` | `sweep_in_progress` | Esta instancia ya está ejecutando un barrido de recuperación |
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

Auditoría (`run_events`, clave única por run, tipo y ordinal, así los reintentos no duplican): `run_created`, `planner_completed`, `architect_reviewed`, `revision_requested`, `waiting_approval`, `approval_recorded`, `run_approved` / `run_rejected`, `approval_applied`, `run_failed`, `run_interrupted`, `run_resumed` y, en el flujo de desarrollo, `scope_proposed`, `developer_completed`, `developer_applied`, `qa_reviewed`, `run_completed`, `run_cancelled` y `run_expired`. Atribución (ADR 0009): `actor` es el humano que causó el evento y es nulo para agentes y sistema; el payload añade `initiated_by`, `approved_by`/`rejected_by`, `cancelled_by` y `executed_by` (`agentforge:<agente>`), expuestos también como campos de `RunEventRead`. Los payloads contienen contadores, códigos y banderas; nunca prompts, planes, credenciales ni datos sensibles. Si el proceso murió entre un checkpoint y su evento, la recuperación reconstruye los eventos de nodo desde el historial de checkpoints, solo con lo que hay evidencia.

Leases y recuperación (`docs/adr/0004`): cada run se conduce bajo un lease en su propia fila (`lease_owner`, `lease_expires_at`, `lease_generation`), renovado por un heartbeat cada tercio de `RUN_LEASE_TTL_SECONDS`. Toda escritura del propietario está cercada por `(owner, generation)` y toda proyección por un `last_checkpoint_id` monotónico. Perder el lease cancela la ejecución local. Un barrido al arrancar y cada `RECOVERY_SWEEP_SECONDS` reclama con `FOR UPDATE SKIP LOCKED` los runs con lease libre o vencido y decide desde el checkpoint: reproyectar, aplicar una decisión pendiente, dejar `interrupted`, reanudar si `RECOVERY_AUTO_RESUME=true`, o `failed`/`abandoned` al agotar `MAX_RUN_ATTEMPTS`. El apagado deja de aceptar trabajo, espera `SHUTDOWN_GRACE_SECONDS`, cancela lo que siga, libera solo sus leases y cierra los pools; nunca marca nada como terminado.

Checkpointer:

- Serialización estricta: msgpack con la allowlist interna de tipos seguros de LangGraph y **sin** fallback a pickle. El estado del grafo es JSON nativo; un tipo desconocido falla al escribir, nunca al leer de la base.
- Cifrado en reposo opcional (`CHECKPOINT_ENCRYPTION_KEY`, AES-EAX). La clave vive en el gestor de secretos, nunca en el repositorio ni en logs.
- Pool propio (`CHECKPOINT_POOL_SIZE`) abierto en `lifespan` sin esperar: si PostgreSQL está caído al arrancar la app queda degradada y `/health/ready` responde `503`. El apagado espera hasta `SHUTDOWN_GRACE_SECONDS` a las ejecuciones activas antes de cerrar los pools.
- Los pasos de despliegue (`alembic upgrade head` y `scripts.setup_checkpoints`) leen solo la configuración de base de datos: no necesitan variables de Vertex AI ni de autenticación.
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

## Runner (paquete independiente)

`runner/` contiene `agentforge-runner`, el servicio que ejecuta los programas permitidos dentro de Podman rootless en una máquina Linux dedicada: VM de Proxmox, VM local o nube, con el mismo paquete. Es un miembro del workspace de `uv` instalado aquí solo para sus pruebas unitarias, que usan un backend simulado y no demuestran aislamiento; `app/` nunca lo importa. Las pruebas reales viven en `tests/runner_integration/` y en el workflow `runner-podman`, que corre en Linux con Podman; su primera ejecución en verde está documentada en `docs/validation/runner-podman-ubuntu-24.04.md`. Estructura, endpoints, cuotas, ejecuciones inciertas y rotación de secretos en `runner/README.md`; despliegue paso a paso en el homelab en `docs/runbooks/runner-proxmox.md`; primera invocación real con `scripts/sandbox_smoke.py`.

## Arquitectura

Reglas permanentes del repositorio en `CLAUDE.md`. Decisiones registradas en `docs/adr/`:

- `0001` — por qué `app/` está organizado por capa técnica (`agents/`, `contracts/`, `infrastructure/`, `orchestration/`, `api/`) y no por módulo de negocio.
- `0002` — política de errores, reintentos y presupuesto de llamadas LLM.
- `0003` — persistencia en PostgreSQL: tablas de negocio con SQLAlchemy/Alembic y checkpoints de LangGraph.
- `0004` — leases con fencing, recuperación automática, idempotencia de creación y sus limitaciones conocidas.
- `0005` — identidad delegada (OIDC), principal explícito, autorización por política, API de control y sus limitaciones conocidas.
- `0006` — Tool Gateway: registro tipado, contexto confiable, identidad de automatización, política por agente, auditoría por paso lógico, adaptador Git administrado y worktrees por ejecución, con sus limitaciones al cierre.
- `0007` — SandboxPort y ejecución aislada en un runner Linux remoto: contratos, frontera de seguridad, transporte privado, evidencia de la validación real y limitaciones al cierre.

## Roadmap por fases

- **Fase 1 (completada)**: planificación y revisión arquitectónica con aprobación humana.
- **Fase 2 (completada)**: orquestador robusto — enrutamiento condicional, retroalimentación Planner ↔ Architect con límite, estados terminales explícitos, manejo de errores del LLM, presupuesto de llamadas, pruebas deterministas.
- **Fase 4 (completada, v0.4.0)**: autenticación OIDC, autorización por propietario y proyecto, idempotencia por principal, listado y auditoría paginados, reanudación manual, barrido administrativo, cabeceras de seguridad. Decisiones y limitaciones en el ADR 0005.
- **Fase 3 (completada, v0.3.0)**: PostgreSQL con SQLAlchemy y Alembic, checkpoints persistentes de LangGraph, proyección de negocio y auditoría, aprobaciones durables, leases con heartbeat y fencing, barrido de recuperación, `Idempotency-Key`, apagado ordenado, CI con PostgreSQL. Decisiones en los ADR 0003 y 0004.
- **Fase 5 (completada, v0.5.0)**: Tool Gateway con auditoría por paso lógico, identidad de automatización y política por agente; adaptador Git administrado y worktrees por ejecución; `SandboxPort`, programas permitidos y `exec.run`; servicio `runner/` con Podman rootless validado en Linux real (ocho pruebas de aislamiento). Decisiones y limitaciones en los ADR 0006 y 0007; despliegue en `docs/runbooks/runner-proxmox.md`. Pendiente: despliegue en una VM persistente, prueba de concurrencia, integración de herramientas con los agentes bajo aprobación humana.
- **Fase 6 (en curso)**: MVP Developer + QA sobre worktrees aislados con escritura acotada, validación en el Runner y hasta tres correcciones, invocado por el operador sobre repositorios `sandbox-*` (ADR 0008). Pendiente: exposición por API con aprobación humana, estado terminal propio, repositorios reales.
- **Fase 7 (en curso)**: repositorios reales con política por proyecto, aprobación humana por etapas, worktrees aislados y Pull Requests revisables (ADR 0009). Paso 1 entregado: alcance propuesto por el Scoper, aprobación por digest, autoridad de escritura efímera, estados `cancelled` y `expired`, migración 0006. Paso 2 implementado y validado contra GitHub real el 2026-10-26 sobre el sandbox privado `agentforge-sandbox-demo` (espejo, identidad por id, pruebas negativas, barrido de credenciales y una ejecución gobernada completa con Claude y el Runner, sin `push`): GitHub App dedicada, identidad por id de repositorio, espejos validados, `GIT_ASKPASS` efímero, contenido remoto como dato, `mirror_syncs` (migración 0007), runbook de alta (ADR 0010). Pendientes: primera integración real con `agentforge-sandbox-demo`, Publisher y PR en borrador (Paso 3), primer repositorio real (Paso 4).
- **Fase 8**: paralelismo entre agentes.
- **Fase 9**: observabilidad y despliegue.

Las fases 7 a 9 no están iniciadas. Cada fase arranca solo con aprobación explícita.
