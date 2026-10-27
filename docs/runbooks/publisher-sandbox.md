# Runbook: primera publicación real del Publisher en `agentforge-sandbox-demo` (Fase 7, etapa 3.4)

Objetivo: que AgentForge cree **una** rama `agentforge/<run_id>` y **un** Pull Request en
borrador en `martinDTV/agentforge-sandbox-demo`, tras dos decisiones humanas reales (alcance
y publicación), con tokens de instalación acotados y revocados, sin tocar `main` y sin dejar
credenciales en ningún sitio. La autorización cubre solo ese repositorio. Referencia: ADR 0011.

## Condiciones previas (todas deben cumplirse; si una falla, no se publica)

| Condición | Cómo se comprueba | Quién la cumple |
| --- | --- | --- |
| La App concede exactamente `Contents: write`, `Pull requests: write`, `Metadata: read` y la instalación sigue en "Only select repositories" con solo el sandbox | `uv run python -m scripts.github_preflight --repository sandbox-github --project sandbox-github --expect publish` → `installation_restricted.ok` | El operador, en GitHub (paso 1) |
| `main` protegida por reglas que exigen Pull Request y bloquean *force push* y borrado, **sin** la App entre los actores con *bypass* | mismo preflight → `base_branch_protected.ok`; los actores con *bypass* se confirman en Settings porque la API no los expone | El operador, en GitHub (paso 2) |
| Escritura habilitada solo en la sesión de prueba y solo para el sandbox | `GITHUB_WRITE_ENABLED=true` y `PUBLISH_ALLOWED_REPOSITORIES='["sandbox-github"]'` como variables de entorno del proceso de la API, nunca en `.env` → `write_flag.ok` | El operador de la sesión (paso 3) |
| Los 18 commits de la rama intactos, árbol limpio | `git log --oneline main..HEAD | wc -l` = 18, `git status --short` vacío | — |
| Runner alcanzable | `RemoteSandboxAdapter.health()` → `verified`, `ready`, `rootless` | Tailscale activo en el Mac |

## 1. Permisos de la App (operador)

Settings → Developer settings → GitHub Apps → `agentforge-sandbox-martindtv` → *Permissions &
events*: `Contents: Read and write`, `Pull requests: Read and write`. Nada más (`Workflows`,
`Administration`, `Issues`, `Checks`, `Actions`, `Secrets` siguen sin conceder). Guardar y
después **aceptar la actualización en la instalación** (Settings → Applications → Installed
GitHub Apps → Configure). Hasta aceptarla, el canje de un token de escritura devuelve 422.

## 2. Protección de `main` (operador)

Settings del repositorio → *Rules* → *Rulesets* → *New branch ruleset*:

- Enforcement: **Active**. Target: `Default branch` (`main`).
- Reglas: *Require a pull request before merging*, *Block force pushes*, *Restrict deletions*.
- **Bypass list: vacía**. La App `agentforge-sandbox-martindtv` no debe figurar.

GitHub solo ofrece rulesets y protección clásica de ramas en repositorios privados de planes de
pago. En un repositorio privado de una cuenta gratuita la API responde `403 Upgrade to GitHub
Pro or make this repository public`, el preflight marca `base_branch_protected` con
`branch_rules_unavailable` y **la etapa se detiene**: hay que elegir entre activar GitHub Pro,
hacer público el sandbox (es desechable y no contiene secretos) o aceptar explícitamente, por
escrito, publicar sin protección de `main` confiando solo en los controles de la plataforma
(refspec fijo, *lease*, instalación exclusiva, bandera). AgentForge no toma esa decisión.

## 3. Sesión de prueba

```bash
# Solo en el proceso de la API; el .env no cambia.
GITHUB_WRITE_ENABLED=true PUBLISH_ALLOWED_REPOSITORIES='["sandbox-github"]' \
AUTHZ_SEPARATION_OF_DUTIES=true \
uv run uvicorn app.main:app --host 127.0.0.1 --port 8011 --log-level info > <log> 2>&1 &
uv run python -m scripts.github_preflight --repository sandbox-github --project sandbox-github --expect publish
```

## 4. Ejecución y decisiones humanas

1. Crear la ejecución `development` sobre `sandbox-github` con una tarea pequeña y reversible.
2. Leer la propuesta de alcance (`GET /runs/{id}/decisions`): archivos, acciones, `base.sha`,
   `hidden_paths`, `signals`. Aprobar con `stage=scope` y el `scope_digest` exacto, con un
   principal humano distinto del creador.
3. Developer y QA trabajan con Claude y el Runner. La plataforma prepara el plan: la ejecución
   queda en `waiting_approval` con `pending_stage=publish`.
4. Leer el plan (`GET /runs/{id}/decisions`): `remote_branch`, `base_sha`, `head_sha`,
   `patch_digest`, archivos con `sha256`, título, escaneo, comprobaciones de QA. Recalcular el
   `publish_digest` de forma independiente (`scripts.publication_digest`, ver abajo) y
   comprobar que coincide. Aprobar con `stage=publish` y ese digest, con un principal humano
   distinto del creador (y del aprobador del alcance donde aplique).
5. El Publisher recalcula el plan, empuja `agentforge/<run_id>` con `--force-with-lease`, abre
   el PR en borrador y revoca los tokens. Estado final `published`.

## 5. Verificación posterior

```bash
uv run python -m scripts.github_preflight --repository sandbox-github --project sandbox-github \
  --expect publish --scan-db --scan <log>          # no_token_leaks y remote_head de main sin cambios
psql "$DATABASE_URL" -c "select state, code, remote_branch, left(head_sha,12), pr_number, approved_by, actor_subject, recorded_at from publications where run_id='<run_id>' order by id"
psql "$DATABASE_URL" -c "select kind, actor_subject, payload from run_events where run_id='<run_id>' order by id"
```

En GitHub: un único PR en borrador cuyo `head` es `agentforge/<run_id>`, `main` en el mismo
commit que antes, sin ramas adicionales. No fusionar.

## 6. Cierre

Detener la API (las variables de la sesión desaparecen con ella), comprobar que
`Settings().github_write_enabled` es `False` sin las variables, repetir
`uv run pytest tests/unit/test_github_boundaries.py tests/unit/test_tool_policy.py -q`
(los agentes siguen sin permisos de publicación) y documentar identificadores y resultados en
`docs/validation/fase7-paso3-criterios.md`.

## Si la publicación falla después del push

El estado queda `failed/retryable` con `resume_from=pushed` y la ejecución `interrupted`.
Antes de reintentar, comprobar en GitHub si el PR ya existe para `agentforge/<run_id>`; el
reintento (`POST /runs/{id}/resume`, requiere `repos:publish`) busca el PR antes de crearlo y
nunca vuelve a empujar si la rama ya está en `head_sha`. No borrar la rama remota.

## Primera publicación real (2026-10-27)

Completada: <https://github.com/martinDTV/agentforge-sandbox-demo/pull/1> (borrador, no fusionado).
Si una aprobación de `publish` devuelve 500 con `UniqueViolation` sobre `uq_run_events_run_id_kind_ordinal`,
el código es anterior a `DecisionStage.event_ordinal`: la transacción se revierte sin efectos y basta
actualizar el código y repetir la aprobación con el mismo digest. Comprueba siempre antes de reintentar
que no exista ya la rama `agentforge/<run_id>` ni un PR (`gh api repos/<owner>/<repo>/pulls?state=all`).
