# Fase 7, Paso 3 (etapas 3.1–3.3): criterios de aceptación y evidencia local

Rama `feat/phase-7-real-repositories`, 2026-10-27. Alcance: Publisher de plataforma, plan de
publicación con digest canónico, segunda decisión humana `publish`, push acotado a
`agentforge/<run_id>`, Pull Request en borrador, auditoría `publications` (migración 0008),
recuperación idempotente. **Validado únicamente con una API de GitHub simulada y remotos Git
locales sobre `file://`. Ninguna publicación real; la GitHub App sigue en solo lectura;
`GITHUB_WRITE_ENABLED` desactivado.**

Pruebas ejecutadas al cierre: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run mypy app`, `uv run mypy runner/agentforge_runner`, `uv run pytest -q`
(**766 passed**), `uv run pytest tests/integration -q` (**77 passed**,
PostgreSQL local), `git diff --check`.

| Evidencia pedida | Implementación | Prueba |
| --- | --- | --- |
| Denegación de publicación sin aprobación humana | `prepare_publication` congela el plan y el grafo se detiene en `approve_publish`; el Publisher solo corre tras una decisión `publish` reclamada en `run_decisions` | `test_publication_flow.py::test_nothing_leaves_the_host_without_a_second_human_decision` (sin rama remota ni PR mientras espera; viewer, `approver` sin `repos:publish`, creador y aprobador del alcance denegados; rechazo humano termina `rejected` sin tocar GitHub), `test_api_publication.py` (403 `permission_denied` / `self_approval_forbidden`) |
| Rechazo de una aprobación obsoleta | Digest canónico (`PublicationPlan.binding_fields`) exigido en la API y en el nodo; recálculo del plan desde el espejo antes del push | `test_publishing_plan.py::test_every_binding_field_changes_the_digest` (15 campos), `test_nothing_leaves…` (`decision_stale` sin digest, digest ajeno o campo equivocado), `test_a_plan_changed_after_preparation_is_refused_before_any_remote_operation` (commit enmendado → `publish_digest_stale`; rama base avanzada → `base_moved`; nada empujado) |
| Bloqueo de publicación hacia `main` | `GitPublisher.push_run_branch` rechaza antes de invocar Git cualquier rama fuera del prefijo reservado o protegida; `validate_branch_prefix` rechaza `main/`, `run/`, `release/` | `test_git_publish.py::test_pushes_outside_the_reserved_prefix_are_refused_before_git_runs` (8 casos, remoto intacto), `test_projects_policy.py::test_publish_policy_pins_a_reserved_branch_prefix…`, `main` sin cambios en todas las pruebas de flujo |
| Recuperación ante caída entre push y creación del PR | Estado `pushed` persistido por CAS antes de llamar a GitHub; fallo transitorio → `failed/retryable/resume_from=pushed` y ejecución `interrupted`; `resume` y el barrido reanudan desde `pushed` sin volver a empujar | `test_a_crash_between_push_and_pull_request_resumes_without_duplicates` (un solo `branch_pushed`, un PR, `attempts == 2`), `test_the_recovery_sweep_also_resumes_an_interrupted_publication` |
| Recuperación ante respuesta perdida después de crear el PR | `GitHubPublisher.open_draft` busca antes de crear y vuelve a buscar ante el 422 "ya existe" que provoca el reintento del cliente | `test_github_publisher.py::test_a_lost_response_after_creation_never_duplicates_the_pull_request`, `test_a_lost_response_after_pull_request_creation_is_recovered` |
| Prevención de PRs duplicados | Rama única por ejecución, búsqueda por `head` antes de crear, replay idempotente de la decisión | `test_open_draft_creates_once_then_finds_the_same_pull_request`, `test_an_approved_plan_is_pushed_once_as_one_draft_pull_request` (replay → un PR) |
| Bloqueo de secretos en el diff | Capa 1: `workspace.write_file` rechaza formatos de token conocidos (`content_denied`); capa 2: `scan_changes` con 9 patrones, alcance, política, symlinks, binarios, tamaños, CRLF; falla cerrado | `test_publishing_scan.py` (11 secretos, 16 reglas, `scan_error` cerrado), `test_secrets_in_the_diff_block_publication_before_any_plan` (ambas capas; sin plan, sin token de escritura) |
| Identidad y límites de permisos de la GitHub App | Tokens por operación (`PUSH_PERMISSIONS`, `PULLS_PERMISSIONS`) acotados al repositorio; `write_enabled` local; revocación; la instalación sin `write` es rechazada por GitHub (422 → `token_request_rejected`) | `test_github_publisher.py::test_write_tokens_need_the_flag_locally_and_the_grant_at_github`, `test_tokens_are_revoked_at_github_after_publication`, `test_without_the_write_flag_an_allowlist_or_the_grant_nothing_is_published` |
| Ausencia de credenciales en logs, checkpoints y auditoría | Tokens solo en memoria y en el entorno de un `git push` vía `GIT_ASKPASS`; cuerpo del PR y eventos sin secretos | `test_an_approved_plan_is_pushed_once…` (busca cada token emitido en el checkpoint, los eventos, `publications`, `runs.publication`, el cuerpo del PR, el disco y `caplog`), `test_github_boundaries.py::test_publication_capabilities_stay_in_the_platform_layer` |
| Separación estructural y agentes sin credenciales | `app/publishing` sin GitHub/HTTP/subproceso; único `push` en `publish.py`; `LocalGitAdapter.push` deshabilitado; sin herramientas nuevas | `test_github_boundaries.py` (6 pruebas estáticas), `test_tool_policy.py`, `test_scoped_development_mirrors.py::test_agents_still_have_no_github_tokens_or_push` |
| Dos decisiones humanas y separación de funciones | `repos:publish` en `project_operator`/`admin`; `decide_publish` (creador nunca; aprobador del alcance nunca con `publish_distinct_approver`) | `test_policy.py::test_publication_approval_needs_repos_publish_and_distinct_humans`, `test_api_publication.py` |
| Autorización de un solo uso, persistente y resistente a concurrencia | Decisión única por `(run_id, publish)` (reclamo atómico existente) + CAS en `runs.publication` | `test_publishing_state.py` (máquina de estados y CAS), integración `test_publications.py` (CAS con dos escritores sobre PostgreSQL), replay/contradicción en el flujo |
| Base válida y espejo actualizado | `refresh_mirror` antes de preparar y antes de empujar; `base_branch_head == base_sha` estricto; corrección del `fetch` a `refs/remotes/origin/*` para no podar `run/<run_id>` | `test_mirror_service.py::test_refresh_is_idempotent_tracks_the_remote_head_and_prunes_branches` (rama de ejecución sobrevive al refresco), flujo `base_moved` |
| Persistencia y migración | Migración 0008 (`published`, tabla `publications` con `CHECK`), guardia de downgrade, repositorios SQL y en memoria | `test_db_models.py`, integración `test_publications.py::test_0008_adds_published_and_the_publications_table_and_guards_the_downgrade`, `test_migrations.py` |

## Inventario de cambios (etapas 3.1–3.3)

- Nuevo: `app/publishing/{__init__,errors,plan,scan,state,ports,template}.py`, `app/infrastructure/git/publish.py`, `app/infrastructure/github/publisher.py`, `app/infrastructure/db/publications.py`, `app/orchestration/publication.py`, `migrations/versions/0008_publications_and_published_status.py`, pruebas `tests/unit/{test_publishing_plan,test_publishing_scan,test_publishing_state,test_git_publish,test_github_publisher,test_publication_flow,test_api_publication}.py`, `tests/integration/test_publications.py`, este documento.
- Modificado: `app/contracts/{lifecycle,execution}.py`, `app/projects/policy.py` (`PublishPolicy`), `app/security/policy.py` (`repos:publish`, `decide_publish`), `app/settings.py`, `app/infrastructure/github/{client,credentials,__init__}.py`, `app/infrastructure/git/{local,mirror}.py` (namespace `refs/remotes/origin/*`, `set_branch`), `app/infrastructure/db/{models,repository,fake,__init__}.py`, `app/orchestration/{graph,status,projection,events,scoped_development,runs,recovery,ports}.py`, `app/api/runs.py`, `app/main.py`, `.env.example`, README, CLAUDE.md, ADR 0010 (§5) y ADR 0011, pruebas existentes ajustadas (`test_policy`, `test_tool_policy`, `test_lifecycle`, `test_db_models`, `test_migrations`, `test_mirror_service`, `test_github_boundaries`, `test_development_mvp_script`, `tests/unit/github_fixtures.py`).

## Riesgos y límites antes de la etapa 3.4

1. Todo lo anterior se validó contra una simulación de GitHub y remotos `file://`; la primera publicación real puede revelar diferencias (422 por protección de rama, límites de tasa, formato de `expires_at`).
2. La protección de `main` en GitHub (regla sin *bypass* para la App) la configura el operador; el preflight con `--expect publish` se añadirá en la etapa 3.4.
3. `contents: write` concede más de lo que el Publisher usa; la defensa es la combinación de refspec fijo, lease, instalación exclusiva en el sandbox, bandera explícita y la regla de protección de `main`.
4. Un PR creado y una rama empujada no se borran nunca desde AgentForge; una publicación cancelada tras el push queda auditada como `cancelled_after_push` (no ejercitada en esta etapa).

## Etapa 3.4: comprobaciones previas a la primera publicación real (2026-10-27)

Autorización recibida, limitada a `martinDTV/agentforge-sandbox-demo`. Trabajo local
completado antes de tocar GitHub (sin confirmar en la rama por instrucción expresa):

- `scripts.github_preflight --expect publish`: exige `contents: write` + `pull_requests: write`
  + `metadata: read` en la instalación, `GITHUB_WRITE_ENABLED` con el repositorio en
  `PUBLISH_ALLOWED_REPOSITORIES`, y reglas en la rama base que exijan Pull Request y bloqueen
  *force push* y borrado (`GitHubClient.branch_rules`; `branch_rules_unavailable` cuando GitHub
  no expone reglas). Pruebas: `test_github_preflight.py::test_expect_publish_requires_write_grants_the_flag_and_a_protected_base`.
- `scripts.publication_digest`: recálculo independiente del `publish_digest` desde el espejo
  (cabeza de `run/<run_id>`, padre, archivos con hash, parche, escaneo) para que la persona que
  aprueba compruebe que el digest corresponde exactamente al contenido mostrado. Prueba:
  `test_publication_digest_script.py`.
- Runbook `docs/runbooks/publisher-sandbox.md` (condiciones, permisos, protección de `main`,
  sesión con las variables de escritura, decisiones humanas, verificación, cierre, reintento).
- Pruebas negativas contra la API simulada repetidas: 63 en `test_publication_flow`,
  `test_git_publish`, `test_github_publisher`, `test_publishing_scan`, `test_api_publication` y
  `test_github_boundaries` (sin aprobación, aprobación obsoleta, base movida, secreto
  detectado, push a `main`, fallo entre push y PR, respuesta perdida). Puertas: ruff, formato,
  `mypy app`, `mypy runner`, **768 unitarias**, **77 de integración**.
- Los 18 commits de la rama permanecen intactos; Runner `verified/ready/rootless`.

Preflight real `--expect publish` (solo lectura contra GitHub), resultado **`ok: false`**:

| Comprobación | Resultado | Causa |
| --- | --- | --- |
| `installation_restricted` | falla | la instalación sigue con `contents: read` + `metadata: read`; faltan `contents: write` y `pull_requests: write` |
| `base_branch_protected` | falla, `branch_rules_unavailable` | GitHub responde `403 Upgrade to GitHub Pro or make this repository public`: un repositorio privado de una cuenta gratuita no admite rulesets ni protección de ramas, así que la protección de `main` **no puede verificarse** |
| `write_flag` | falla (esperado) | `GITHUB_WRITE_ENABLED` sigue apagado hasta la sesión de prueba |
| `app_auth`, `installation_token`, `repository_identity`, `remote_head` (`f7e73dcf`), `mirror` en sincronía, `no_token_leaks` | ok | — |

**La etapa se detiene aquí** (condición 2 del plan: "si no se puede verificar, detente") hasta que
el operador conceda los permisos de escritura a la App y resuelva la protección de `main`
(GitHub Pro, repositorio público o renuncia explícita documentada). No se ha publicado nada;
ningún token de escritura se ha emitido.


### Resultado de la primera publicación real (2026-10-27)

Ejecución `de8845f3-bd98-48cc-bdd4-1fe67d55ea75`, repositorio `martinDTV/agentforge-sandbox-demo`
(id 1435009323), aprobada por `martin` en `scope` (digest `23567a52…`, 07:12 UTC) y en `publish`
(digest `6b66f7afe9d1eda3269eab8ad0e549d3604c2174668dae3e13d6c939a7bec833`, 08:33 UTC).

| Verificación previa | Resultado |
| --- | --- |
| Aprobación vigente | `waiting_approval`, etapa `publish`, caducidad 2026-10-30T07:13Z |
| Digest | igual en la API y recalculado desde el espejo con `scripts.publication_digest` (`matches: true`) |
| `main` | `b04ba76475f2881098d2189d28a4e6182caad32c` en GitHub, en el espejo y como `base_sha` del plan |
| Seguridad | escaneo previo sin hallazgos; preflight `--expect publish --scan-db` todo `ok`; solo la rama `main`; cero PR |
| Separación de funciones | creador `agentforge-operator` ≠ aprobador `martin`; `publish_distinct_approver=false` en `ENVIRONMENT=local` |

Resultado: PR en borrador <https://github.com/martinDTV/agentforge-sandbox-demo/pull/1>, abierto por
`agentforge-sandbox-martindtv[bot]`, base `main` en `b04ba764…`, rama
`agentforge/de8845f3-bd98-48cc-bdd4-1fe67d55ea75` en `e4c101fb…` (un commit, dos archivos, +165/−2),
`mergeable_state: blocked` por el ruleset, no fusionado. `main` sin cambios; el espejo recibió
`refs/remotes/origin/agentforge/<run_id>` y nada más. Auditoría: `publications` 1–5
(`prepared → approved → pushed → opened → published`, token de push con caducidad registrada),
`run_decisions` con ambas decisiones aplicadas, eventos 108–113 (`approval_recorded`,
`run_approved`, `branch_pushed`, `pull_request_opened`, `run_published`, `approval_applied`).
Sin cadenas de token en tablas, log de la API, PR ni archivos locales. La API con
`GITHUB_WRITE_ENABLED=true` se detuvo al terminar.

Incidente durante la operación: el primer `POST /approve` de `publish` devolvió 500 porque
`approval_recorded` usaba ordinal 0 en todas las etapas y `run_events` es único por
`(run_id, kind, ordinal)`; la transacción se revirtió sin dejar estado (sin lease, sin decisión,
sin push). Corrección: `DecisionStage.event_ordinal` (`plan` 0, `scope` 1, `publish` 2) aplicado a
`waiting_approval`, `approval_recorded`, `approval_applied` y `run_approved`; prueba de regresión
`test_two_decisions_on_one_run_keep_their_own_event_ordinals`; API reiniciada con las mismas
banderas y aprobación repetida con el mismo digest.
