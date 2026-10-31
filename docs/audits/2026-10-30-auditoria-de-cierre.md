# Auditoría de cierre — AgentForge hacia 1.0 (2026-10-30)

Solo lectura: no se modificó código, base de datos, servicios ni Git. Evidencia tomada de
Git local y remoto, la API de GitHub, la base `agentforge` (127.0.0.1:5434), el Runner por
Tailscale, la configuración (`.env` sin valores secretos) y los ADR. Archivo sin seguimiento
en Git; puede borrarse o incorporarse en la integración.

## 1. Ramas y worktrees

| Worktree / rama | HEAD | Estado real |
|---|---|---|
| `multiagent-starter` · `feat/phase-7-real-repositories` | `c1a1cb3` | Igual que `origin`. **Fusionada en `main` por el PR #9 (2026-10-27 09:16Z, merge `8863e7b`)**. Sin seguimiento: `docs/adr/0012-supervisor-de-objetivos.md` (borrador antiguo) y este informe |
| `multiagent-starter-phase8` · `feat/phase-8-goal-supervisor` | `f110eb7` | 8 commits sobre `c1a1cb3` (`fbd0fa4..f110eb7`), **sin push, sin PR**; limpio |
| `multiagent-starter-mail` · `feat/notification-emails-redesign` | `8826175` | 5 commits sobre `f110eb7`, **sin push, sin PR**; es el código en ejecución desde 2026-10-30 21:50Z |
| `agentforge-notify` · `feat/notifications-and-smart-router` | `64798aa` | Ancestro de `c1a1cb3`: todo su contenido está en `main`. Worktree y rama **obsoletos** |
| `main` local | `2e0e3e8` | Un merge por detrás de `origin/main` (`8863e7b`) |
| `docs/runner-proxmox-runbook` | `dfc0a3f` | 1 adelante / 15 detrás; `main` ya contiene una versión más nueva del runbook. **Obsoleta** |
| `origin/main` | `8863e7b` | 9 PR fusionados: Fases 1–7 completas, notificaciones, Smart Router, scripts de operación (migración 0009) |

Pila sin integrar: 13 commits lineales (phase-8 → mail) sobre el contenido ya fusionado; un
PR hacia `main` no debería producir conflictos.

## 2. Clasificación de capacidades

- **Integradas en `origin/main`**: planificación con aprobación humana, persistencia y
  checkpoints, API de control con OIDC, Tool Gateway, worktrees, Runner remoto, Developer +
  QA, desarrollo gobernado (alcance por digest), GitHub App y espejos, Publisher (PR en
  borrador), notificaciones por correo, Smart Router, `agentforge_dev` y CLI.
- **Implementadas, probadas y desplegadas en local, no integradas**: Supervisor de
  objetivos (ADR 0012, migración 0010), rediseño de correos y motivo de decisión
  (migración 0011).
- **Demostradas con ejecuciones reales (base de datos)**: 6 ejecuciones `planning`
  (2 aprobadas, 1 rechazada el 2026-10-30, 3 fallidas de smoke), 9 `development`
  (5 `completed`, 1 `published` con PR #1, 1 `expired` por caducidad de alcance, 1 `failed`
  por `runner_unreachable` el 10-26, 1 `cancelled`), 92 `exec.run` correctos y 31
  `workspace.write_file`, 1 decisión del router (`fast`), 4 correos enviados (plantillas
  antiguas), 1 objetivo (`plan`) que llegó al plan real y fue rechazado: **0 hijas creadas**.
- **No demostradas en real**: creación de hijas por el Supervisor, dependencia
  `after_merged`, evidencia de fusión, plantillas nuevas de correo (solo FakeEmailSender y
  vista previa), decisiones con `reason`.

## 3. Migraciones

`alembic current` = `0011 (head)` en la base de desarrollo. 0010 aplicada 2026-10-30
18:24Z; 0011 (`run_decisions.reason`, nula) 21:50Z con la API parada; 6 decisiones
históricas con `reason` nulo; recuentos intactos. Respaldos en
`~/.local/share/agentforge-backups/`: `20261030T182350Z` (antes de 0010),
`20261030T214944Z` (antes de 0011, restaurado en una base temporal con recuentos idénticos),
`20261027T083951Z`. Consecuencia: **el código de `main` (espera 0009) no arranca contra esta
base** hasta integrar las dos ramas.

## 4. PR #1 del sandbox

`martinDTV/agentforge-sandbox-demo#1`, rama `agentforge/de8845f3-…`, head `e4c101f` sobre
base `b04ba76`: **MERGED el 2026-10-30 21:42:11Z**, merge `442c0c7` = head actual de `main`
(protegida). AgentForge no lo registra porque la ejecución no pertenecía a un objetivo (la
evidencia de fusión solo se consulta para tareas del Supervisor); es lo esperado.

## 5. Fase 8: pendiente y dependencias

| Paso | Contenido | Depende de |
|---|---|---|
| 1 (cierre) | Criterio 8: objetivo real de dos tareas dependientes en el sandbox; hallazgo documentado: Planner/Architect sin contexto del repositorio | PR #1 fusionado (hecho); integración recomendada |
| 2 | `SANDBOX_MAX_CONCURRENT_JOBS`, `CapacityLedger`, `capacity_wait`, re-creación acotada (`RETRYABLE_CHILD_CODES`), reintento de `runner_busy` (D10), `max_active_runs` por repositorio | Paso 1 cerrado |
| 3 | `WorkflowKind.VERIFICATION`, `VerificationScope` (D6, ADR 0008), `POST /goals/{id}/tasks` (D8), `completed` condicionado (D9) | Paso 2 |
| 4 | `parallel_disjoint`, solapamiento de alcances, fusión prematura, Architect con contexto | Pasos 2 y 3; Runner con ≥ 4 plazas |
| 5 (opcional) | Doble QA, Architect asesor, resumen narrativo (D11) | Paso 4 |

## 6. Runner

`agentforge-runner-01`, `0.1.0+v1`, `podman-rootless`, `ready`, 16 comprobaciones de
aislamiento en verde, perfil `python-3.13`, alcanzable en la dirección privada del tailnet configurada en `SANDBOX_RUNNER_URL`.
Concurrencia: `RUNNER_MAX_CONCURRENT` por defecto **2** (rango 1–16); la VM se dimensionó
para 2 (4 vCPU, 8 GB, 60 GB; 16 GB para 4). El valor efectivo no se expone en `/v1/health`
(no hay `in_flight` ni `max_concurrent`): confirmar en la VM. Cada ejecución ocupa como
mucho una plaza a la vez; la API no conoce las plazas (`SANDBOX_MAX_CONCURRENT_JOBS` llega en
el Paso 2) y una tercera ejecución simultánea recibiría `runner_busy` y fallaría. Hoy el
Supervisor limita a 1 hija por repositorio y 3 globales, así que no hay riesgo real.
Para 4 Developers + 2 QA: hasta 6 ejecuciones en fase de sandbox → `RUNNER_MAX_CONCURRENT`
4–6, VM de 16 GB, más Paso 2 y Paso 4.

## 7. Vertex, presupuestos, leases, Proxmox

- Vertex: proyecto el proyecto de GCP configurado en `ANTHROPIC_VERTEX_PROJECT_ID`, región `global`, `claude-sonnet-4-6`; credenciales
  ADC de tipo **`authorized_user`** (sesión del usuario en el Mac): no sirven en una VM.
  Cuotas no verificadas desde aquí (consola → Quotas → `aiplatform.googleapis.com`).
  `llm_max_retries=2`, `llm_timeout_seconds=60` (política de desarrollo: 400 s).
- Presupuestos: `max_llm_calls_per_run=8`; política por repositorio `max_model_calls=10`,
  `max_reserved_usd=8`, `wall_seconds=1500`, `scope_approval_ttl_seconds=3600`; objetivo:
  `max_total_usd` 25 (tope 100), `max_tasks` 5 (tope 8), plazo 7 días. Coste real observado:
  ejecución publicada 3.63 USD reservados (5 llamadas, 17.9k tokens de entrada, 3.4k de
  salida); planificación del objetivo 4.0 USD reservados.
- Leases y tiempos: `run_lease_ttl_seconds=60` (heartbeat + fencing),
  `supervisor_lease_seconds=60`, `supervisor_tick_seconds=60`, `recovery_sweep_seconds=60`,
  `supervisor_creating_timeout_seconds=1800`, `max_run_attempts=3`, `statement_timeout`
  30 s, `worktree_max_per_project=20`. Sin leases activos en la base.
- Proxmox: solo el Runner está desplegado (VM dedicada). Capacidad libre del host y
  configuración efectiva de la VM no comprobables desde el Mac (`pvesh get
  /nodes/<nodo>/status`, `qm config <vmid>`).

## 8. Qué falta para funcionar sin el MacBook

Hoy la API, el worker de correo y el Supervisor corren en un solo proceso `uvicorn` lanzado
por `scripts/agentforge_dev.py` en el Mac, desde un worktree de rama. Falta:

1. VM o LXC `agentforge-api` en Proxmox con PostgreSQL, `uv`, checkout etiquetado,
   unidad `systemd` (una sola: API + worker + Supervisor viven en el mismo proceso),
   `alembic upgrade head` y `setup_checkpoints` como pasos de despliegue.
2. Secretos fuera del Mac: `.env` 0600, clave privada de la GitHub App, contraseña SMTP,
   token del Runner; **cuenta de servicio de Vertex** (`roles/aiplatform.user`) en lugar de
   ADC de usuario.
3. Datos: `PROJECTS_FILE`, `GIT_REPOSITORIES_FILE`, `MIRRORS_ROOT`, `WORKTREES_ROOT` en el
   disco de la VM; copia de seguridad diaria (`pg_dump -Fc`, temporizador) con prueba de
   restauración.
4. Red e identidad: nodo Tailscale con `tag:agentforge-api` (la ACL del Runner ya lo
   espera); `tailscale serve` da HTTPS sin servicios comerciales → `PUBLIC_APP_URL`
   válida para los enlaces del correo. El emisor OIDC local solo es válido con
   `ENVIRONMENT=local`: suficiente para uso diario dentro de la tailnet; la "producción
   privada" exige identidad real (modo Tailscale identity por cabeceras, o IdP propio).
5. Cliente: la skill y el CLI apuntan a la URL de la tailnet con `AGENTFORGE_TOKEN` o el
   mismo emisor local en la VM.

## 9. Panel privado mínimo (propuesta)

Páginas estáticas (HTML + JS sin framework ni build, sin CDN) servidas por la misma API bajo
`/panel`, fuera del esquema OpenAPI, consumiendo los contratos existentes:
`/health/ready`, `GET /runs` (filtros `status`, `project_id`), `GET /runs/{id}`,
`/decisions`, `/events`, `GET /goals`, `/goals/{id}`, `/goals/{id}/events`,
`POST /runs/{id}/approve` y `/cancel`, `POST /goals/{id}/cancel`. Vistas: Resumen
(pendientes, en curso, objetivos activos, estado del worker y del Runner), Ejecución
(estado, ruta, propuesta con digest, eventos, PR, coste de los eventos), Objetivo (tareas,
dependencias, presupuesto), Decidir (8 caracteres del digest + motivo). Token pegado una
vez (`scripts.dev_token`) en `sessionStorage`; más adelante identidad Tailscale. Código
nuevo de servidor: montaje estático con CSP estricta, `GET /api/v1/repositories` (registro
de solo lectura, que también usará la skill) y, opcional, `GET /api/v1/admin/notifications`
(recuentos de la bandeja). Sin servicios comerciales.

## 10. Criterios de aceptación

**Listo para uso diario (1.0):** `main` integrado y etiquetado; stack permanente en
Proxmox (systemd, reinicio automático, migraciones aplicadas, copias diarias verificadas)
alcanzable por Tailscale con el Mac apagado; credenciales de servicio (Vertex, App, SMTP)
fuera del Mac; al menos un repositorio real registrado y una tarea real completada hasta
PR fusionado; un objetivo real de ≥ 2 tareas dependientes completado (criterio 8);
correos nuevos recibidos con enlaces válidos; skill `/agentforge` operativa contra la API
permanente; runbooks de despliegue, restauración y rotación; CI verde en la etiqueta.

**Listo para producción privada (1.x):** identidad real sin emisor local y separación de
funciones decidida; Pasos 2–4 de la Fase 8 con Runner de ≥ 4 plazas y prueba de carga de 4
Developers + 2 QA sin `runner_busy` ni `base_moved` inesperados; tope de gasto mensual con
alerta y cuotas de Vertex documentadas; observabilidad (métricas, logs centralizados,
auditoría en el panel); copias cifradas fuera del host y recuperación probada en una VM
limpia; revisión de permisos de la App y rotación periódica; panel con decisiones por digest.

## 11. Entregas pequeñas, por dependencias

| # | Entrega | Depende de |
|---|---|---|
| E1 | Cierre de integración: PR `feat/notification-emails-redesign` (incluye phase-8) → `main`, CI, etiqueta `v0.8.0`, stack desde `main`, borrar worktrees y ramas obsoletos y el ADR duplicado | — |
| E2 | Validación real del Supervisor: objetivo T1/T2 en el sandbox, `docs/validation/fase8-paso1-criterios.md` | PR #1 fusionado (hecho); E1 recomendada |
| E3 | `GET /api/v1/repositories` (solo lectura) y la skill lo consume | E1 |
| E4 | Permanencia: VM `agentforge-api` (PostgreSQL, systemd, cuenta de servicio Vertex, `tailscale serve`, copias), runbook | E1 |
| E5 | Primer repositorio real (Clivana): App instalada, política, espejo, tarea pequeña | E2; E4 para no depender del Mac |
| E6 | Fase 8 Paso 2 + Runner a 16 GB / `RUNNER_MAX_CONCURRENT=4` + `in_flight` en health | E2 |
| E7 | Panel mínimo | E3, E4 |
| E8 | Fase 8 Paso 3 (verificación) | E6 |
| E9 | Fase 8 Paso 4 (hermanas disjuntas, Architect con contexto): meta 4 Dev / 2 QA / 1 Architect | E8 |
| E10 | Identidad de producción privada (`ENVIRONMENT=production`) | E4 |
| E11 | Paso 5 opcional | E9 |

## 12. Siguiente incremento único

**E1 — Cierre de integración `v0.8.0`.** Todo lo demás se apoya en un `main` único y el
stack actual depende de un worktree de rama cuyo esquema (`0011`) `main` no reconoce. No
requiere código nuevo: PR, CI, merge, etiqueta, reinicio desde `main` y limpieza. Inmediatamente
después, E2.

## 13. Cierre de E1 (2026-10-30) y deuda técnica registrada

- PR #10 (`chore/v0.8.0-integration`, cabeza `52f8b0b`, 14 commits) fusionado en `main` por
  martinDTV a las 23:09:44Z; commit de merge `b84a0b4` (padres `8863e7b` y `52f8b0b`; árbol
  idéntico al merge en seco, sin cambios inesperados). Etiqueta anotada `v0.8.0` sobre
  `b84a0b4`, publicada sin sobrescribir referencias.
- Copia antigua sin seguimiento del ADR 0012 preservada en
  `~/.local/share/agentforge-backups/untracked-20261030/`; sus 25 líneas exclusivas son el
  pseudocódigo de contratos del borrador inicial (nombres como `integration_mode`,
  `verification_run_id`, `AWAITING_MERGE`, `TASK_RETRIED`) superado por los contratos
  implementados; no contiene decisiones ausentes en la versión rastreada.

**Deuda técnica — prueba intermitente de recuperación tras SIGKILL.**
`tests/integration/test_recovery.py::test_recovery_after_sigkill_of_the_driving_process`
usa `RecoveryPolicy(lease_ttl_seconds=1.5, heartbeat_seconds=0.4)` y mide ventanas reales de
reloj. Falló una vez el 2026-10-30 al ejecutar simultáneamente las suites unitaria, de
integración y los analizadores en la misma máquina; pasó en todas las ejecuciones aisladas
(local y CI). Causa probable: inanición de CPU que retrasa el heartbeat o el barrido más
allá del margen de 1,5 s. Opciones cuando se aborde: márgenes relativos al TTL en lugar de
constantes, marcador `timing` para ejecutarla en serie, o reintento único documentado. No
se cambia nada en 0.8.0; la CI la ejecuta en serie y está en verde.

## 14. E2, primer intento (2026-10-30/31): caducidad del alcance y deuda registrada

Objetivo `bac6eb0c` (plan `9ecb5338` aprobado con motivo a las 00:01:46Z; hija T1 `d28ee5e3`
propuso alcance a las 00:01:59Z con `base_sha 442c0c7`). La decisión caducó a la hora
(política `scope_approval_ttl_seconds=3600`): el barrido marcó la ejecución `expired` a las
01:02:26Z, el Supervisor pasó T1 a `failed`, T2 a `skipped` y el objetivo a `failed`. Cinco
correos reales salieron con las plantillas nuevas (plan, alcance, recordatorio, tarea fallida,
objetivo fallido). Corrección operativa autorizada: TTL de alcance a 86400 s en la política del
sandbox (respaldo conservado). No existe renovación de una decisión caducada ni re-creación de
hijas en el Paso 1: el reintento es un objetivo nuevo con el mismo texto.

**Deuda técnica 2 — etiqueta `expired:plan` en una caducidad de `scope`.**
`app/goals/rules.py::failure_code_for_run` deriva la etapa de `pending_stage` de la fila, pero
el barrido la limpia al expirar, así que cae al valor por defecto `plan`. El código correcto
debería salir de la última decisión pendiente registrada (`waiting_approval` o `run_decisions`)
o conservarse en el evento `run_expired`. Afecta a `goal_tasks.last_error_code`, a los eventos
`task_failed`/`needs_human`/`goal_failed` y al correo.

**Deuda técnica 3 — recordatorios no adaptados a ventanas de 24 h.**
El recordatorio `decision_pending` se emitió a los 30 minutos de una ventana de 60 (sin margen
útil); con 86400 s la cadencia debería ligarse al TTL (p. ej., avisos a mitad de ventana y a
falta de una hora) y respetar `decision_reminder_seconds` del objetivo. Pendiente de diseño;
no se cambia nada en 0.8.0.
