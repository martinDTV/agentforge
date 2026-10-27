# 0011. Publisher: rama de ejecución y Pull Request en borrador tras una aprobación humana de publicación (Fase 7, Paso 3)

- Status: accepted (aprobado el 2026-10-27 con las cinco decisiones de la sección final; etapas **3.1–3.3 implementadas y validadas con una API de GitHub simulada**; la etapa 3.4 fue autorizada el 2026-10-27 solo para `agentforge-sandbox-demo` y está detenida en sus comprobaciones previas: la instalación sigue en solo lectura y GitHub no expone protección de ramas en un repositorio privado de plan gratuito (`branch_rules_unavailable`), ver `docs/validation/fase7-paso3-criterios.md`; la GitHub App sigue con `contents:read` + `metadata:read` y `GITHUB_WRITE_ENABLED` permanece desactivado)
- Date: 2026-10-27
- Deciders: martin, Claude (Fase 7)

## Context

Con el Paso 2 (ADR 0010) una ejecución `development` parte de un espejo verificado de `martinDTV/agentforge-sandbox-demo`, el Scoper propone, un humano aprueba el alcance por digest, Developer escribe dentro de ese alcance y QA valida en el Runner. Al terminar, el worktree `WORKTREES_ROOT/<proyecto>/<run_id>/tree` queda en la rama local `run/<run_id>` del espejo, en el commit base `base_sha`, con los cambios **sin confirmar** (`git status` muestra `M` y `??`); nada sale del host. Ya existen las piezas que este paso reutiliza: `DecisionStage.PUBLISH` y la tabla `run_decisions`, la columna `runs.publication` (JSONB, reservada en la migración 0006), el permiso `repos:publish` sin rol, `publish.mode = manual` como único valor, `LocalGitAdapter.commit` (nunca concedido a agentes) y `LocalGitAdapter.push` deshabilitado (`push_disabled`), el `MirrorService` con su cerrojo por repositorio, el `InstallationTokenProvider` con lista cerrada de permisos y el ayudante `GIT_ASKPASS` efímero, el preflight y los escáneres de fuga.

El Paso 3 añade la **publicación**: crear una rama nueva en GitHub con exactamente el trabajo aprobado y abrir un Pull Request **en borrador**, únicamente después de una segunda decisión humana (`publish`), sin que ningún agente toque credenciales ni `push`, y sin fusionar nunca.

## Decision

### 1. Separación estructural: el Publisher es plataforma, no agente

| Componente | Paquete | Identidad | Puede |
| --- | --- | --- | --- |
| Scoper, Developer, QA | `app/agents`, gateway de herramientas | `agentforge:<agente>` (rol `agent`) | leer, escribir dentro del alcance aprobado, ejecutar en el sandbox; **ni Git de escritura, ni red, ni GitHub, ni tokens** (sin cambios en `DEFAULT_AGENT_GRANTS`, `ENABLED_RISK_CLASSES` ni `default_registry`) |
| Dominio de publicación | `app/publishing/` (nuevo) | — | plan de publicación, digest, escaneo previo, errores, puertos; importa solo `contracts`, `projects`, `git`, `security` |
| Publisher Git | `app/infrastructure/git/publish.py` (nuevo) | `agentforge:publisher` | confirmar el worktree en `run/<run_id>` con la identidad configurada y empujar **solo** `refs/heads/<prefijo><run_id>` con credencial efímera |
| Publisher GitHub | `app/infrastructure/github/publisher.py` (nuevo) | `agentforge:publisher` | buscar y crear un PR en borrador |
| Orquestación | `app/orchestration/publication.py` (nuevo), nodos en el grafo de desarrollo | — | preparar el plan, interrumpir en `publish`, ejecutar el Publisher tras la decisión |

`LocalGitAdapter.push` (el puerto que ven agentes y herramientas) **sigue deshabilitado**. La única función capaz de empujar vive en `publish.py`, recibe el `RemoteSpec`, el `run_id` y el `head_sha` desde código confiable y construye el refspec ella misma. La prueba estática de fronteras (`test_github_boundaries.py`) se amplía: `app/agents`, `app/tools`, `app/sandbox` y `app/workspaces` no importan `app/publishing` ni los módulos `publisher`/`publish`; ningún tool se llama `git.push`, `github.*` ni `publish.*`; la cadena `--force-with-lease` solo aparece en `publish.py`.

### 2. Dos decisiones humanas independientes

- `scope` (Paso 1) autoriza a **modificar archivos** y sigue igual.
- `publish` (nuevo) autoriza a **publicar**. Se solicita con una interrupción propia (`approve_publish`) después de que QA apruebe y de que la plataforma haya preparado el plan; la ejecución queda `waiting_approval` con `pending_stage = publish` y `approval_deadline = ahora + publish.approval_ttl_seconds` (política; por defecto 72 h).
- Permiso: `repos:publish` pasa a los roles `project_operator` y `admin` (humanos). Los agentes no lo tienen ni pueden tenerlo (`agent` solo `tools:execute`). `runs:approve` sigue siendo necesario además de `repos:publish`.
- Separación de funciones: el creador de la ejecución no puede aprobar su publicación (regla existente). En `staging`/`production` el aprobador de `publish` debe además ser distinto del aprobador de `scope` (`AUTHZ_PUBLISH_DISTINCT_APPROVER`, por defecto derivado del entorno como `separation_of_duties`).
- Denegar, cancelar y caducar en `publish` son terminales (`rejected`, `cancelled`, `expired`): el trabajo queda en el worktree local durante la retención configurada y nunca se publica. Una ejecución terminada antes de este paso (`completed`) no es publicable retroactivamente.

### 3. La aprobación se vincula al contenido exacto y al destino autorizado

`prepare_publication` construye un `PublicationPlan` inmutable y su `publish_digest = sha256(JSON canónico)` sobre:

| Campo | Origen | Si cambia |
| --- | --- | --- |
| `run_id`, `repository_id`, `project_id` | ejecución | otra ejecución, otro digest |
| `github_repository_id`, `owner/name`, `installation_id` | `RemoteSpec` de la política | destino distinto: obsoleta |
| `base_branch` (nombre) y `base_sha` | política y marcador del espejo / worktree | base distinta: obsoleta |
| `base_branch_head` | `HEAD` remoto de la rama base en el momento de preparar (tras `refresh`) | ver §9 |
| `remote_branch` = `<publish.branch_prefix><run_id>` | derivado, nunca configurable por petición | — |
| `head_sha` | commit creado por la plataforma en `run/<run_id>` (padre exacto `base_sha`) | cualquier re-commit: obsoleta |
| `patch_digest` | `sha256(git diff-tree -p --binary --full-index --no-color --no-ext-diff base_sha head_sha)` | cualquier cambio de contenido: obsoleta |
| `files[]` | ruta, acción, `sha256` del contenido final, bytes | — |
| `scope_digest` | decisión `scope` aplicada | — |
| `scan` | resultado del escaneo previo (§8), sin hallazgos bloqueantes | — |

La petición de aprobación muestra todo lo anterior más el `diffstat`, los resultados de QA y el resumen del modelo (escapado). `POST /api/v1/runs/{id}/approve` con `stage = publish` exige `publish_digest`; un digest distinto es `409 decision_stale`. Antes de empujar, el Publisher **recalcula** el plan desde el estado real del espejo (`rev-parse run/<run_id>`, árbol, diff) y lo compara con el aprobado: cualquier diferencia es `publish_digest_stale` y la ejecución termina `failed` sin tocar GitHub. La decisión `(run_id, publish)` es única: no hay "re-aprobación" sobre la misma ejecución; un contenido distinto exige una ejecución nueva.

### 4. Tokens de instalación mínimos por operación

- Permisos de la App en el Paso 3: `contents: write`, `pull_requests: write`, `metadata: read`. **Nunca** `workflows`, `administration`, `issues`, `checks`, `actions`, `secrets` ni permisos de organización (sin `workflows`, GitHub rechaza cualquier push que toque `.github/workflows`, además del bloqueo local).
- Dos tokens distintos, acotados al repositorio (`repositories: [name]`) y a lo estrictamente necesario: `{contents: write, metadata: read}` para `ls-remote` y `push`; `{pull_requests: write, metadata: read}` para buscar y crear el PR. Se piden justo antes de usarse y se **revocan** al terminar (`DELETE /installation/token`), aunque caduquen en una hora.
- `InstallationTokenProvider` mantiene la lista cerrada: `READ_ONLY_PERMISSIONS` para espejos y `PUBLISH_PERMISSIONS` solo cuando `GITHUB_WRITE_ENABLED=true` (Settings; por defecto `false`; en `staging`/`production` requiere además `PUBLISH_ALLOWED_REPOSITORIES` explícita). Un canje de escritura con la bandera apagada falla en local (`permission_not_allowed`) sin llegar a GitHub, como hoy.
- Los agentes no cambian: el gateway no registra herramientas nuevas, el `SandboxRequest` sigue rechazando `GITHUB_*` y `GIT_*`, el Runner no tiene red, y los tokens viven solo en memoria del proceso de la API y en el entorno de un único `git push` a través del ayudante `GIT_ASKPASS` existente.

### 5. Protección de `main` y de cualquier rama base

Local, en `publish.py` (la única vía de `push`):

- Refspec fijo `<head_sha>:refs/heads/<prefijo><run_id>`; el prefijo (`publish.branch_prefix`, por defecto `agentforge/`) se valida (termina en `/`, no es `run/`, `release/`, `main`, `master` ni empieza por `-`), y el destino se comprueba contra `DEFAULT_PROTECTED_REFS` y los `protected_refs` del registro antes de invocar Git.
- Nunca refspecs que empiecen por `:` (borrado), nunca `--tags`/`--follow-tags`, nunca `--mirror`, nunca `--delete`; `--atomic`, `--no-verify`, `--force-with-lease=refs/heads/<rama>:<esperado>` donde `esperado` es vacío (la rama no debe existir) en el primer intento y `head_sha` en los reintentos; `protocol.allow=never` salvo `https`; `push.default=nothing`; timeout propio.
- `head_sha` debe ser exactamente un commit cuyo padre es `base_sha` (sin merges, sin historia adicional) y `base_sha` debe seguir siendo ancestro de la rama base remota (§9).

En GitHub, recomendado en el runbook del Paso 3 y verificado por el preflight donde la API lo permita (`GET /repos/{o}/{n}/rules/branches/{base}`): regla sobre `main` (y `release/*`) que exija PR antes de fusionar, prohíba *force push* y borrado, y **no** incluya la App en la lista de *bypass*. La App no puede fusionar: no tiene `administration` y la fusión queda, por diseño, en manos de un humano.

### 6. Pull Requests en borrador, nunca fusionados automáticamente

- `POST /repos/{o}/{n}/pulls` con `draft: true`, `head = <prefijo><run_id>`, `base = publish.base_branch`, `maintainer_can_modify: true`. La plataforma **no** llama a `merge`, `update-branch`, `auto-merge`, `reviews` ni a ningún endpoint de escritura distinto de crear el PR (y, en reintentos, buscarlo). `publish.draft` permanece `Literal[True]`.
- Título: `<publish.title_prefix> <resumen de la tarea truncado a 70 caracteres>` (por defecto `[AgentForge]`). Cuerpo desde plantilla fija: tarea, alcance aprobado (archivos con acción y `sha256`), base (`base_sha`, `base_branch_head`, retraso en commits), resultados de QA (programas, códigos de salida), trazabilidad (`run_id`, `scope` aprobado por sujeto opaco y fecha, `publish` aprobado por sujeto opaco y fecha, `publish_digest`), y una sección marcada "texto generado por el modelo" con el resumen **escapado**. Sin `@menciones` (se neutralizan), sin enlaces construidos por el modelo, sin correos, sin tokens. El commit lleva trailers `Run-Id`, `Base-Sha`, `Scope-Approved-By`, `Publish-Approved-By` con sujetos opacos.

### 7. Idempotencia y recuperación entre `push` y PR

`runs.publication` guarda una máquina de estados escrita tras cada paso con *compare-and-set* sobre el estado anterior (misma técnica que `claim_decision`):

```
prepared ─(decisión publish aprobada)─▶ approved ─▶ pushed ─▶ opened ─▶ published
                                            │           │          │
                                            └───────────┴──────────┴──▶ failed(code, retryable)
```

| Caída después de… | Al reanudar |
| --- | --- |
| aprobar, antes de `push` | `git ls-remote origin refs/heads/<rama>`: ausente → `push` con lease vacío; presente con `head_sha` → pasar a `pushed`; presente con otro SHA → `remote_branch_conflict` (nunca se sobrescribe) |
| `push`, antes de crear el PR | `GET /pulls?head=<owner>:<rama>&state=all`: existe → `opened` con su número; no existe → crear |
| crear el PR, antes de persistir | el mismo `GET` encuentra el PR: no se crea otro |
| cualquier paso, por caída del proceso | el servicio de recuperación re-dirige ejecuciones `running`/`interrupted` con `publication.state ∈ {approved, pushed, opened}` por el mismo camino idempotente |

La rama remota es única por ejecución (`<prefijo><run_id>`), así que un duplicado es imposible por construcción; el PR se busca siempre por `head` antes de crearse. Un `approve` idéntico sobre una publicación `failed` **recuperable** (autenticación, 5xx, timeout, red) vuelve a ejecutar el Publisher desde el estado persistido; sobre una fallida por motivos de contenido (`publish_digest_stale`, `publication_scan_failed`, `base_invalid`, `remote_branch_conflict`) responde `409` y la ejecución queda `failed`.

### 8. Escaneo previo a la publicación (`app/publishing/scan.py`)

Se ejecuta al preparar el plan y **otra vez** inmediatamente antes de `push`; cualquier hallazgo bloqueante impide publicar (`publication_scan_failed`) y se muestra al humano:

1. **Alcance**: el conjunto de rutas cambiadas entre `base_sha` y `head_sha` debe ser subconjunto de los archivos del alcance aprobado, con acciones coherentes (`create` → el archivo no existía en `base_sha`; `edit` → existía); ninguna ruta fuera del alcance, ninguna ruta no canónica.
2. **Política**: ninguna ruta en `DEFAULT_DENIED_GLOBS`, `denied_globs`, `all_excluded_context_globs` ni `.github/**` (aunque el alcance la contuviera); respeto a `max_file_bytes` y `max_change_bytes`.
3. **Forma**: sin symlinks, sin submódulos (modo `160000`), sin cambios de bit ejecutable salvo que ya lo fuera, sin archivos binarios, sin finales CRLF introducidos, sin BOM nuevos.
4. **Secretos** en líneas añadidas: tokens de GitHub (`gh[pousr]_`, `github_pat_`), claves AWS (`AKIA…`), Google (`AIza…`), Slack (`xox[baprs]-`), bloques `PRIVATE KEY`, JWT, URLs con credenciales, `password|secret|token|api[_-]?key` seguidos de un literal; nombres de archivo sensibles (`is_sensitive_name`).
5. **Commit**: exactamente un commit nuevo, padre `base_sha`, autor y committer iguales a la identidad configurada del repositorio, mensaje generado por la plataforma, sin firma GPG, sin `Co-Authored-By` ni menciones.

Las reglas del escaneo son de plataforma y no dependen del modelo; el texto del modelo nunca participa en decidir qué se publica.

### 9. Espejo actualizado y commit base válido

- Antes de preparar y antes de empujar, la plataforma ejecuta `MirrorService.refresh` (identidad por id, remoto derivado, `fetch` de ramas) y lee `base_branch_head`.
- `base_sha` debe ser **ancestro** de `base_branch_head` (`git merge-base --is-ancestor`); si no lo es (historia reescrita), `base_invalid` y la ejecución termina `failed`.
- Retraso permitido: `behind = rev-list --count base_sha..base_branch_head`. Por defecto `publish.max_base_behind = 0`: si la rama base avanzó desde la preparación, la aprobación queda obsoleta (`base_moved`) y la publicación no se realiza; el operador inicia una ejecución nueva sobre la base actual. Un valor mayor permite publicar un PR cuya base avanzó hasta ese número de commits y lo deja constancia en el cuerpo del PR. No hay *rebase* automático en este paso.
- La rama remota de destino no debe existir previamente (lease vacío); si existe con otro contenido, `remote_branch_conflict`.

### 10. Auditoría sin credenciales

- Migración **0008** (aditiva): estado terminal `published` en `runs.status`; tabla `publications` con `run_id`, `repository_id`, `project_id`, `remote_branch`, `base_branch`, `base_sha`, `base_branch_head`, `head_sha`, `patch_digest`, `publish_digest`, `pr_number`, `pr_url`, `pr_node_id`, `state`, `code`, `approved_by` (sujeto opaco), `pushed_at`, `opened_at`, `token_expires_at`, `actor_subject = agentforge:publisher`; índice por `run_id` y restricción única `(repository_id, remote_branch)`.
- Eventos nuevos: `publication_prepared` (digest, archivos, retraso de base, resumen del escaneo), `publication_scan_failed`, `branch_pushed` (rama, `head_sha`, `executed_by: agentforge:publisher`, `approved_by`), `pull_request_opened` (número, URL, borrador), `publication_failed` (código, reintentable), `run_published`. Ningún payload contiene tokens, URLs con credenciales ni el cuerpo del PR.
- `GET /runs/{id}` expone `publication` (estado, rama, `head_sha`, PR); `GET /runs/{id}/decisions` muestra el plan pendiente con su digest; `RunEventRead` ya expone `approved_by`/`executed_by`.
- Logs: igual que el espejo (`installation_id`, repositorio, caducidad), nunca el token; el log del `push` se limita al código de salida y a `stderr` redactado.

### 11. Revocación, expiración, concurrencia, timeouts y recuperación

- **Revocación/expiración de tokens**: `token_invalid` → invalidar, canjear una vez más y reintentar el paso; App suspendida o desinstalada (`installation_forbidden`, `installation_not_found`) → `failed(publication_unavailable)` reintentable cuando el operador restaure la instalación. Tokens revocados por la plataforma al terminar.
- **Expiración de la decisión**: `publish.approval_ttl_seconds` con el barrido existente (`expire_waiting`) → `expired`.
- **Concurrencia**: decisión única por `(run_id, publish)` (reclamo atómico existente); un solo Publisher por ejecución mediante CAS en `publication.state` y el *lease* de la ejecución; el `push` toma el mismo `flock` por repositorio que el espejo, así nunca coincide con un `refresh`; dos ejecuciones distintas sobre el mismo repositorio publican ramas distintas y se serializan.
- **Timeouts**: `PUBLISH_PUSH_TIMEOUT_SECONDS` (por defecto 120), cliente HTTP 15 s con un reintento en 5xx/red, `publish.wall_seconds` (por defecto 300) para el paso completo; al agotarse, `failed(publication_timeout)` reintentable.
- **Recuperación**: el servicio de recuperación trata `publication.state ∈ {approved, pushed, opened}` como trabajo pendiente idempotente (§7) y nunca vuelve a pedir una decisión; las ejecuciones `waiting_approval` en `publish` se conservan hasta decisión, cancelación o caducidad.
- **Cancelación**: `runs:cancel` sigue válida mientras se espera `publish`; tras `pushed` ya no cancela la rama remota (no hay borrado remoto en este paso), pero impide crear el PR y deja `publication.state = cancelled_after_push` auditado.

### 12. `publish.mode = manual` por defecto

Sigue siendo el único valor aceptado; `after_qa` continúa reservado y no se implementa. Toda publicación pasa por la decisión humana `publish`.

## Notas de implementación (etapas 3.1–3.3, 2026-10-27)

Lo construido coincide con la decisión salvo en estos puntos, que la precisan:

- **Poda del espejo.** El `fetch` del Paso 2 traía las ramas remotas a `refs/heads/*` con `--prune`; al refrescar el espejo después de crear un worktree, Git podaba las ramas locales `run/<run_id>` (incluidas las de otras ejecuciones concurrentes). Las pruebas de este paso lo destaparon. Ahora el espejo trae las ramas a `refs/remotes/origin/*` (donde la poda es inocua) y la plataforma copia explícitamente la rama por defecto a `refs/heads/<default>` tras cada `fetch` (`LocalGitAdapter.set_branch`, que rechaza ramas `run/*`). ADR 0010 §5 queda corregido en ese punto.
- **Decisión única por etapa, digest en la misma columna.** El digest de publicación se guarda en `run_decisions.scope_digest` (la columna es "el digest de lo decidido"); la API lo expone como `publish_digest` para la etapa `publish` y deja `scope_digest` nulo. La resolución `resume_pending → waiting_approval` se permite solo cuando la etapa pendiente es distinta de la decidida (`scope → publish`); volver a la misma etapa sigue bloqueado por la proyección.
- **Evidencia positiva en el checkpoint.** `publication_plan`, `publish_approved`, `published` y `publication_interrupted` son claves de `RunState`; `derive_status` las lee antes que `completed`, de modo que una ejecución con publicación pendiente nunca se proyecta como `completed`, una publicación aprobada y no terminada es `interrupted` (recuperable) y `published` es terminal. Un fallo transitorio deja `publication_interrupted` y el estado `failed/retryable` con `resume_from` en `runs.publication`; `RunService.resume` (que para publicaciones exige `repos:publish`) y el barrido de recuperación (`claim_interrupted_publications`) reanudan desde el último paso duradero con `aupdate_state(as_node="approve_publish")`.
- **Dos tokens y revocación.** `InstallationTokenProvider(write_enabled=...)` admite `contents:write` y `pull_requests:write` solo con la bandera; el Publisher pide uno por operación y los revoca (`DELETE /installation/token`) en `finally`, también cuando la publicación falla.
- **Rechazo persistido.** `approve_publish` registra `rejected` en `runs.publication` y en `publications` antes de terminar la ejecución como `rejected`.
- **Mensaje de commit y cuerpo del PR** se renderizan desde campos explícitos del plan y de las decisiones; el texto del modelo va entre vallas `text`, sin menciones ni enlaces.
- **Tabla `publications`** es un registro de transiciones (una fila por paso), no una fila por ejecución; `runs.publication` guarda el estado actual.

## Permisos adicionales de la GitHub App (análisis; no se solicitan todavía)

| Permiso | Nivel | Para qué | Riesgo que introduce | Mitigación |
| --- | --- | --- | --- | --- |
| `contents` | **write** (hoy read) | `git push` de `refs/heads/agentforge/<run_id>` | permite empujar a cualquier rama del repositorio instalado, incluida `main` si no hay reglas | refspec y lease locales, `push.default=nothing`, prohibición local de ramas protegidas, regla de protección de `main` en GitHub sin *bypass* para la App, instalación solo en el sandbox, `GITHUB_WRITE_ENABLED` explícito |
| `pull_requests` | **write** (hoy ninguno) | crear el PR en borrador y buscarlo | permite editar PRs y comentar | token separado solo para ese paso, sin llamadas a `merge`/reviews, PR siempre `draft` |
| `metadata` | read (ya) | identidad, instalación, reglas de rama | — | — |
| `workflows` | **nunca** | — | permitiría cambiar CI | no concedido: GitHub rechaza pushes que toquen `.github/workflows` |
| `issues`, `checks`, `administration`, `actions`, `secrets` | **nunca** en este paso | etiquetas, *check runs*, reglas | ampliarían la superficie | fuera de alcance; si algún día hicieran falta, ADR propio |

Al cambiar los permisos, GitHub exige **aceptar la actualización en la instalación**; el preflight (`--expect publish`) comprobará `permissions == {contents: write, pull_requests: write, metadata: read}`, `repository_selection: selected`, y reportará las reglas de la rama base. Ese cambio lo hace el operador, solo en `agentforge-sandbox-demo`, al llegar a la etapa 3.4.

## Fuera de alcance del Paso 3

Fusionar PRs, `rebase` o actualización automática de la rama, borrado de ramas remotas, comentarios en PRs, lectura de revisiones o issues en prompts, etiquetas, *check runs*, webhooks, `publish.mode = after_qa`, varios commits por ejecución, repositorios distintos del sandbox.

## Plan de implementación por etapas

Cada etapa termina con `ruff`, `mypy`, `pytest` y la suite de integración en verde, commits temáticos locales sin `push`, y sin cambios en la App ni en GitHub hasta la 3.4.

### 3.1 Dominio, política, permisos y persistencia

- `app/publishing/`: `PublicationPlan`, `publish_digest`, `PublicationState`, `ScanFinding`/`ScanReport`, errores con código estable, puerto `PublicationRepository`.
- Política: `PublishPolicy` gana `branch_prefix` (`agentforge/`), `approval_ttl_seconds` (259 200), `max_base_behind` (0), `wall_seconds` (300), `title_prefix` (`[AgentForge]`); validadores (prefijo reservado, límites).
- Seguridad: `repos:publish` para `project_operator` y `admin`; `AUTHZ_PUBLISH_DISTINCT_APPROVER`; `AuthorizationPolicy.can_publish(principal, run, scope_approver)`.
- Settings: `GITHUB_WRITE_ENABLED` (false), `PUBLISH_ALLOWED_REPOSITORIES` (lista; obligatoria en producción cuando la bandera está activa), `PUBLISH_PUSH_TIMEOUT_SECONDS`.
- Migración 0008: `published` + tabla `publications`; repositorios SQL y en memoria; `RunLifecycleStatus.PUBLISHED` terminal; proyección y `RunState` con `publication`.
- Pruebas: digest canónico y sus invalidaciones (cada campo), validación de prefijos, roles (agentes nunca `repos:publish`; separación de funciones), migración arriba/abajo con la guardia de `published`, repositorio SQL.
- Criterio de aceptación: una política con `remote` y `publish` por defecto carga; el digest cambia ante cualquier variación de contenido, base o destino; ningún rol de agente tiene `repos:publish`.

### 3.2 Capacidades de plataforma: commit, push acotado y PR en borrador

- `app/infrastructure/git/publish.py`: `commit_run_branch` (identidad del repositorio, mensaje y trailers de plantilla, exactamente un commit sobre `base_sha`, idempotente si el árbol ya está confirmado), `remote_branch_state` (`ls-remote` con credencial), `push_run_branch` (refspec fijo, `--force-with-lease`, `--atomic`, protocolo `https`, timeout, credencial por `GIT_ASKPASS`), `ancestry` (`merge-base --is-ancestor`, `rev-list --count`), `patch_digest`.
- `app/infrastructure/github/publisher.py`: `find_pull_request(head)`, `create_draft_pull_request`, `revoke_token`; `InstallationTokenProvider` con `PUBLISH_PERMISSIONS` tras la bandera; `GitHubClient.branch_rules` para el preflight.
- `app/publishing/scan.py` con las reglas de §8 sobre `git diff-tree`/`cat-file`.
- Pruebas (sin red): "GitHub" Git local sobre `file://` con `receive.denyNonFastForwards` y una rama protegida simulada; push a `main` rechazado **en local antes de invocar Git**; refspec de borrado rechazado; lease vacío sobre rama existente → `remote_branch_conflict`; reintento con lease `head_sha`; token revocado a mitad; API simulada: PR existente encontrado antes de crear, 5xx en creación seguido de búsqueda sin duplicado, 422 por rama inexistente; escaneo: cada regla con un caso positivo y uno negativo; fuga de tokens buscada en archivos, logs y argv del subproceso.
- Criterio de aceptación: ningún camino de código puede producir un `push` fuera de `refs/heads/<prefijo><run_id>`; con `GITHUB_WRITE_ENABLED=false` no se emite ningún token de escritura.

### 3.3 Orquestación, API y recuperación

- Nodos `prepare_publication` (tras QA aprobado), `approve_publish` (interrupción con el plan completo) y `publish` (máquina de estados §7) en `scoped_development`; eventos de §10; `RunService.approve` con `stage=publish` y `publish_digest`; `GET /runs/{id}` con `publication`; expiración y cancelación; recuperación de `approved/pushed/opened`.
- Pruebas (fakes): flujo completo hasta `published`; rechazo, cancelación y caducidad en `publish`; digest obsoleto por re-commit, por cambio de política de destino y por avance de la base; aprobador sin `repos:publish`, agente invisible, creador o aprobador de `scope` rechazados según la separación de funciones; caída simulada entre `push` y PR sin duplicados; dos aprobaciones concurrentes → una; `GITHUB_WRITE_ENABLED=false` → `publish_unavailable` antes de tocar nada; integración en PostgreSQL de decisiones `publish`, `publications` y recuperación.
- Criterio de aceptación: con fakes, una ejecución termina `published` con un solo `push` y un solo PR en borrador; cualquier alteración entre la aprobación y la publicación la detiene con su código.

### 3.4 Preflight, runbook y primera publicación real en el sandbox

- `scripts.github_preflight --expect publish`; runbook del Paso 3: cambiar permisos de la App (`contents: write`, `pull_requests: write`), aceptar en la instalación, regla de protección de `main` sin *bypass*, `GITHUB_WRITE_ENABLED=true` solo en local y solo para `sandbox-github`.
- Prueba real por el operador: ejecución gobernada completa → aprobación `publish` → rama `agentforge/<run_id>` y PR en borrador en `agentforge-sandbox-demo`; pruebas negativas reales: intento de empujar a `main` (rechazado en local), lease sobre rama existente, digest obsoleto, token revocado desde GitHub durante la espera, bandera apagada; barrido de credenciales con `--scan-db` y el log de la API.
- Criterio de aceptación: PR en borrador abierto por la App con el cuerpo de plantilla, `main` intacta, un único `push`, cero tokens en disco, base de datos y logs, y la App sin `workflows`.

### 3.5 Cierre

Matriz de aceptación del Paso 3 con evidencia real, ADR 0011 a `accepted`, README y CLAUDE.md, decisión explícita sobre si `GITHUB_WRITE_ENABLED` permanece activo en local tras la prueba (recomendación: apagarlo entre pruebas).

## Riesgos

- `contents: write` es un permiso amplio dentro del repositorio instalado: la defensa real es la combinación de refspec fijo, lease, instalación exclusiva en el sandbox, bandera de escritura y la regla de protección de `main` en GitHub; cualquiera de ellas sola no basta.
- El cuerpo del PR contiene texto del modelo: va escapado y marcado, pero GitHub renderiza Markdown; se evitan menciones y enlaces del modelo.
- Un `push` exitoso seguido de un fallo persistente al crear el PR deja una rama remota sin PR: visible en `publications` y recuperable; no se borra automáticamente.
- `max_base_behind = 0` puede hacer fracasar publicaciones en repositorios activos; es una elección deliberada de seguridad y se relaja por política, no por defecto.

## Decisiones que se piden al aprobar este ADR

1. Prefijo reservado de ramas remotas: `agentforge/<run_id>` (propuesta) u otro.
2. Separación de funciones en `publish`: creador ≠ publicador siempre; además publicador ≠ aprobador de `scope` en `staging`/`production` (propuesta).
3. Política por defecto ante avance de la rama base: estricta (`max_base_behind = 0`, propuesta) o tolerante.
4. Resultado tras `push` exitoso y PR imposible: dejar la rama y marcar reintentable (propuesta) o exigir borrado manual documentado.
5. Idioma y prefijo del título del PR (`[AgentForge]`, cuerpo en español, propuesta).
