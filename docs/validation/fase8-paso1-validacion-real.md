# Fase 8, Paso 1 — Validación real del Supervisor de objetivos (en curso)

Registro de la primera validación sobre la infraestructura real (API local, PostgreSQL de
desarrollo, Claude vía Vertex AI, GitHub App de solo lectura, Runner `agentforge-runner-01`).
Ningún PR se ha fusionado ni publicado durante esta validación; `GITHUB_WRITE_ENABLED` sigue
desactivado.

## 2026-10-30 — Puesta en marcha de la migración 0010

| paso | evidencia |
|---|---|
| Worktree `../multiagent-starter-phase8`, rama `feat/phase-8-goal-supervisor` | limpio; 7 commits sobre `c1a1cb3` (`fbd0fa4`…`040bc43`) |
| Base de desarrollo antes | `alembic current` = `0009`; API antigua (pid 29219, checkout principal) detenida antes de migrar; ninguna ejecución en vuelo |
| Respaldo | `~/.local/share/agentforge-backups/agentforge-dev-20261030T182350Z.dump` (`pg_dump -Fc`, 162 504 bytes, 84 entradas en `pg_restore --list`, 14 tablas) + `.counts.txt`; el respaldo del 2026-10-27 se conserva |
| Cadena | `0009 -> 0010 (head)`, una sola cabeza; 0 filas de `notification_outbox` con `run_id` nulo |
| Aplicación | `uv run alembic upgrade 0010` desde el worktree (`.env` y `.local` enlazados al checkout principal) |
| Recuentos antes = después | runs 14, run_events 111, run_decisions 5, publications 5, notification_outbox 1, route_decisions 1, mirror_syncs 27, tool_invocations 374, checkpoints 99 / 419 writes / 202 blobs; `goals`, `goal_tasks`, `goal_events` creadas vacías |
| Pila | `agentforge_dev up` desde el worktree: `/health/ready` `ok` con `notifications: worker`; `/api/v1/goals` responde 200; Runner `verified=True ready=True backend=podman-rootless profiles=[python-3.13]`; `email_smoke` sin `--send`: configuración válida, nada enviado |

## 2026-10-30 — Objetivo `7bd42940-419c-4c55-8e1d-a6ebb5f4c650`

- Creación: `scripts.agentforge goal "…" --decomposition plan --max-tasks 2 --deadline-days 7` en `sandbox-github`. Respuesta inmediata `planning`, planificación `0903bb0c-e766-47ce-a2b8-6ee968d3ffb0` reservada antes de crearla; reserva 8 llamadas / 4,0 USD (tope 28 / 25).
- Planificación real: Planner y Architect (2 llamadas, revisión 0, Architect aprobó) en 26 s. Plan: T1 `normalize_email` en `src/mailcheck/normalize.py` con pruebas; T2 integra `normalize_email` en `validate_email`, dependiente de T1. Huella SHA-256 del plan canónico: `a48ee3b50373c5ba36525e37b2b5e2ef772a0578cf596b9ee7a68bb300b14b9c`.
- Correos: `plan_pending` de la ejecución entregado por Gmail en un intento; `goal_created` no se envía con `NOTIFICATIONS_LEVEL=priority`.
- Revisión humana: el plan contradecía `tests/test_acceptance.py` (recortar espacios haría válida `"user@example.com "`, que la suite exige rechazar) y asumía un `validate_email` que en `main` (`b04ba76`) sigue siendo un *stub* porque el PR #1 no está fusionado. **Decisión humana: rechazar** (`scripts.agentforge reject 0903bb0c… --yes`, etapa `plan`, sin digest en esta etapa).
- Resultado: ejecución `rejected`; objetivo `failed` con `plan_rejected:plan`, 0 tareas, 0 hijas (runs totales 15 = 14 previas + la planificación), lease liberado; eventos `goal_created`, `plan_requested`, `plan_refused`, `goal_failed`; correos `run_completed` (decisión rechazada) y `goal_failed` entregados. Claude no aprobó ni rechazó nada por su cuenta.
- Hallazgo y mejora futura registrados en el ADR 0012 («Hallazgo de la validación real»).

## 2026-10-30 — Revisión de solo lectura del PR #1 (`de8845f3`)

- Rama `agentforge/de8845f3-bd98-48cc-bdd4-1fe67d55ea75`, head `e4c101f`, base `b04ba76` = `main` actual en GitHub (comprobado con token de solo lectura, revocado después). `merge-base --is-ancestor` verdadero: fusión *fast-forward*, sin conflictos posibles.
- Cambios: `src/mailcheck/validator.py` (+58/−2) y `tests/test_validator.py` (nuevo, 25 casos). `src/mailcheck/__init__.py`, `tests/test_acceptance.py`, `README.md` y `pyproject.toml` intactos; firma pública `validate_email(address: str) -> bool` y docstring conservadas.
- Reglas del README comprobadas una a una en el código (un `@`, longitudes 254/64, caracteres y puntos de la parte local, etiquetas 1–63 sin guion inicial o final, TLD alfabético de ≥ 2, sin espacios, `TypeError` si no es `str`); cada entrada de `VALID`/`INVALID` de la suite de aceptación coincide con el veredicto del código.
- Evidencia de ejecución en el Runner durante la ejecución `de8845f3` sobre ese mismo head: ronda QA 1 con `ruff_check`, `ruff_format_check` y `pytest` en `ok`, `qa_reviewed` `approved=True checks=3 failed_checks=0` (la ronda 0 tuvo 1 comprobación fallida y 2 observaciones, corregidas por Developer); escaneo previo a publicación sin hallazgos; aprobaciones `scope` y `publish` por `martin`. No se ejecutó código del repositorio en el host.
- Pendiente de autorización humana: fusionar el PR #1 en GitHub.
