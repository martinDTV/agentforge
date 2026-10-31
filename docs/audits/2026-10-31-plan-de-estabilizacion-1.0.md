# Plan de estabilización AgentForge 1.0 (auditoría de 2026-10-31, solo lectura)

Base: código en `main` @ `v0.8.0` (`b84a0b4`), base de datos de desarrollo en `0011`, las 18
ejecuciones y 3 objetivos reales registrados hasta hoy, y los incidentes de la sesión de E2.
Sin cambios de código, base, servicios ni remotos. Archivo sin seguimiento en Git.

## 1. Evidencia real disponible

| Fuente | Dato |
|---|---|
| `runs` | 18 ejecuciones: 5 `completed`, 1 `published`, 2 `expired` (ambas en `scope`, TTL 1 h), 1 `failed/runner_unreachable` (10-26), 1 `cancelled`, 3 planes aprobados, 1 rechazado, 1 `correction_limit`, 2 `sandbox_smoke`, 1 plan `waiting_approval` |
| `goals` | `7bd42940` `failed/plan_rejected` (plan contradecía las pruebas de aceptación), `bac6eb0c` `failed/expired:plan` (en realidad `scope`), `88b23688` `planning` con un plan de 3 tareas frente a `max_tasks=2` |
| `tool_invocations` | 374: 361 ok, 13 `failed` (3 `exec.run` por `runner_unreachable`, 10 `workspace.read_file`), 0 `denied`, 0 `invalid_input` |
| `notification_outbox` | 10 enviados, 0 fallidos, 0 muertos; 5 con las plantillas nuevas |
| `publications` | 1 publicación completa (`prepared → approved → pushed → opened → published`) |
| Pruebas | 604 unitarias en 77 archivos; 89 de integración en 19 archivos; CI verde en `main` |
| Runner | `agentforge-runner-01` `ready`, 16/16 comprobaciones; 2 plazas |

## 2. Clasificación

**Capacidades verificadas (pruebas y ejecuciones reales).** Aprobación por digest con
`decision_stale`/`decision_expired`; ordinales de evento por etapa (`plan 0`, `scope 1`,
`publish 2`) tras la colisión scope/publish, cubiertos por `test_development_governance` y
`test_in_memory_repository`; publicación cerrada (`base_moved`, escaneo previo, refspec fijo,
`force-with-lease`); espejos con `flock` y credencial efímera; leases con heartbeat y fencing
(10 pruebas de integración); reserva atómica de hijas e idempotencia del Supervisor (23
escenarios unitarios, 5 de integración); bandeja de correo con reintentos y carta muerta; arranque
estricto ante esquema distinto; `agentforge_dev up` se niega con migraciones pendientes;
filtrado de alcance por política (`denied_globs`, `excluded_context_globs`), demostrado hoy
con `AGENTS.md` oculto y `tests/test_acceptance.py` denegado.

**Riesgos cubiertos con recuperación manual.** `base_moved` (falla cerrado; la tarea muere y
hace falta un objetivo nuevo); `runner_busy`/`runner_unreachable` (la ejecución falla con
`technical_checks_failed`); reinicio durante Developer/QA (`development_interrupted`, cierre
conservador); error de Vertex en una hija (falla sin reintento); PostgreSQL caído (503 con
`Retry-After`, tic del Supervisor tolerante). En todos, el objetivo queda `blocked`/`failed` y la
única salida hoy es crear otro objetivo y volver a planificar.

**Defectos confirmados.**

| # | Defecto | Evidencia | Frecuencia |
|---|---|---|---|
| D1 | El Planner y el Architect no reciben los hechos de la política (máximo de tareas, rutas escribibles y prohibidas) ni el árbol del repositorio; el límite `max_tasks` se comprueba **después** de la aprobación humana (`_materialise`) | `0903bb0c` (plan con supuestos falsos), `e2d68bf0` (3 tareas, `NOTES.md` fuera de `src/**`/`tests/**`, pasos imposibles para un Developer) | 2 de 3 planes de objetivo |
| D2 | Una decisión de alcance caducada mata la tarea y el objetivo; no hay re‑propuesta ni re‑creación; el reintento exige replanificar | `bac6eb0c` | 1 de 2 hijas |
| D3 | Código `expired:plan` al caducar `scope`: `expire_waiting_runs` pone `pending_stage=NULL` antes de que `failure_code_for_run` lea la etapa | `goal_tasks.last_error_code`, eventos y correo de `bac6eb0c` | 1 de 1 |
| D4 | El recordatorio se emite a la mitad de la ventana (`reminder_due`), inútil con 60 min y sin aviso final con 24 h | evento `needs_human` 00:32 de una ventana 00:02–01:02 | 1 de 1 |
| D5 | `runner_busy` y `runner_unreachable` no se reintentan dentro de `DevelopmentSession.checks`; cualquier fallo del Runner cierra la ejecución | `ed70edb8` (10-26) | 1 de 9 |
| D6 | `test_recovery_after_sigkill_of_the_driving_process` depende del reloj real con márgenes fijos (1,5 s) | fallo bajo carga el 10-30; pasa aislada | intermitente |
| D7 | Etiqueta de ruta `planned_upstream:none` en hijas de objetivo (cosmética; la ruta fue correcta) | `route_decisions` | 1 de 1 |
| D8 | No existe tope de gasto global (diario o mensual) ni alerta; solo por ejecución y por objetivo | revisión de `Settings` | — |
| D9 | La identidad de las personas es nominal: con `OIDC_JWKS_FILE` la única clave firmante es la de desarrollo en `.local/`, propiedad de la misma cuenta del sistema que ejecuta la API y la CLI; `scripts/agentforge.py`, la skill `/agentforge` y `scripts/dev_token.py` firman cualquier `sub` con cualquier rol; la API no autentica personas ni registra emisor o `jti` por decisión, así que `decide_publish` (creador ≠ publicador) solo compara etiquetas que la misma persona controla | `de8845f3` (creada como `agentforge-operator`, aprobada por `martin`, misma clave); `5a957712` bloqueada por `self_approval_forbidden` el 10-31 | estructural |

## 3. Matriz de fallos

| Fallo | Reproducción | Impacto | Prueba de regresión propuesta | Recuperación (hoy → objetivo) |
|---|---|---|---|---|
| Plan incompatible (D1) | Objetivo con `max_tasks=2` y texto que invite a "verificar" o "documentar"; FakeLLM que devuelve 3 tareas | Aprobación humana desperdiciada, 4 USD reservados, objetivo `failed` tras aprobar | Unitaria del grafo de planificación: 3 tareas con límite 2 → una revisión con retroalimentación determinista → si persiste, `plan_invalid` **antes** de `waiting_approval`; Planner recibe límites y globs en el texto | Hoy: rechazar y crear otro objetivo → objetivo: nunca llega a la persona un plan inválido |
| Caducidad de alcance (D2) | Hija en `waiting_approval` `scope`; avanzar el reloj más allá del TTL; barrido | Tarea `failed`, dependientes `skipped`, objetivo `failed`; replanificar | Coordinador con reloj controlado: `expired:scope` → tarea `ready` → nueva hija `attempt=1` desde la base actual, misma clave de plan, presupuesto re‑reservado; segunda caducidad → `failed` | Hoy: objetivo nuevo → objetivo: re‑creación acotada sin replanificar |
| Código de caducidad (D3) | Igual que D2 | Auditoría y correos engañosos | Integración: barrido de una hija en `scope` → `runs.error_code='expired:scope'`, evento `run_expired{stage}`, `goal_tasks.last_error_code='expired:scope'` | Corrección: registrar la etapa en el mismo `UPDATE` del barrido y en el evento |
| Recordatorios (D4) | Decisión con TTL 24 h y reloj controlado | Persona sin aviso útil; caducidades evitables | Unitaria de `reminder_due`: avisos a 50 % y a falta de 60 min, uno por tramo (ordinales distintos), cap `decision_reminder_seconds` | — |
| `runner_busy` / `runner_unreachable` (D5) | `FakeBackend` que responde `busy` dos veces y luego ejecuta | Ejecución perdida, trabajo del Developer descartado | Unitaria de `checks`: reintento con espera acotada dentro de `wall_seconds` para códigos `retryable`; misma `execution_id` (el Runner rechaza antes de reclamar) | Hoy: ejecución nueva → objetivo: espera y continúa |
| `base_moved` durante la ventana | Aprobar alcance, avanzar `main` del remoto `file://`, aprobar publicación | Publicación falla cerrada; tarea `failed` | Ya cubierto (`test_publication_flow`); añadir: el Supervisor re‑crea la hija una vez desde la base nueva y avisa del coste | Hoy: objetivo nuevo → objetivo: re‑creación acotada |
| Reinicio con hija en Developer/QA | Integración: matar el proceso que conduce una hija con FakeLLM/FakeBackend | `development_interrupted`; tarea `failed` | Tras reinicio: una sola hija por tarea (sin duplicados), tarea re‑creada una vez; nunca `approved` por accidente | Hoy: manual → objetivo: automático y acotado |
| Vertex caído o lento | `FakeLLMAdapter` que lanza `llm_unavailable`/`llm_timeout` | Hija `failed`; plan `plan_create_failed` | Hija re‑creada una vez; plan: objetivo `failed` con código claro y aviso (sin bucle) | Igual |
| SMTP caído | `FakeEmailSender` que falla N veces | Correos retrasados; estados intactos | Ya cubierto (`test_notifications_core`, integración); añadir prueba de que `run_events` no se bloquea por SMTP | Reintentos + carta muerta + `scripts.notifications requeue` |
| PostgreSQL caído | Parar el servidor de pruebas durante un tic y una petición | 503; tic pospuesto | Integración con contenedor pausado o `statement_timeout` forzado: readiness 503 → ok; ningún estado corrupto; el Supervisor retoma | Ya cubierto en parte (`test_health_db`, `test_database_engine`) |
| Duplicación tras caída del Supervisor | Caída simulada entre reservar `run_id` y `RunService.create` (prueba existente) | — | Mantener; añadir reinicio real en la campaña | Verificado |
| Presupuesto | Objetivo con `max_total_usd` justo | Tercera hija `ready` con `budget_exhausted` | Verificado (unitaria); añadir tope global (D8) si se adopta | — |
| Migraciones | Dump sintético en `0009` → `upgrade head` → `downgrade 0010` | Arranque abortado si hay desajuste | Ampliar `test_migrations` con datos sintéticos y ida/vuelta `0009↔0011`; script de respaldo+verificación | Respaldos manuales → `agentforge_dev migrate` con respaldo obligatorio |

## 4. Prioridad (severidad × frecuencia)

1. **D1** plan incompatible: 2/3 objetivos, cada uno con coste y una decisión humana perdida.
2. **D2 + D3 + D4** caducidad: 1/2 hijas; sin re‑creación cada caducidad es un objetivo entero.
3. **D5** Runner: 1/9 ejecuciones; con dos plazas y uso diario volverá a ocurrir.
4. Reinicios y Vertex: sin incidentes reales aún, pero el cierre conservador convierte cualquier reinicio en pérdida de tarea.
5. **D6** prueba intermitente: ruido en CI, no afecta al producto.
6. **D7, D8**: cosmético y preventivo.

## 5. Pruebas de resiliencia automatizadas

Suite `tests/resilience/` (integración, fuera de `pytest` por defecto, trabajo `resilience` en CI
manual y nocturno): bases desechables `agentforge_test_*` (fixture existente), remotos `file://`
y espejos bajo `tmp_path`, `FakeLLMAdapter` guionizado, `FakeGitHub`, `FakeEmailSender`,
`FakeBackend` del Runner con `busy`, `unreachable`, `timeout` y `ok` programables; nunca el
Runner real ni Vertex. Escenarios: los once de la matriz, con reloj controlado donde haya
TTL. Métrica: 0 fallos en 20 ejecuciones consecutivas de la suite antes de declarar 1.0.
El Runner real solo entra en la campaña del §8 y en `tests/runner_integration/` (Podman).

## 6. Política de aprobaciones caducadas

1. Una autorización caducada no se reutiliza jamás: el digest muere con la decisión
   (`decision_expired`, `decision_stale` ya lo imponen) y el cliente nunca lo reenvía.
2. Caducidad en `scope` (nada escrito): la tarea vuelve a `ready`; el Supervisor crea una hija
   nueva (`attempt+1`) desde la base actual con la **misma** tarea del plan aprobado; la
   persona recibe una propuesta nueva con digest nuevo. No se replanifica: el plan aprobado
   es estructura, no autorización de escritura. Tope: un reintento por tarea; el presupuesto se
   reserva de nuevo y cuenta contra el objetivo.
3. Caducidad en `publish` (código escrito y QA verde): no se descarta el trabajo. La ejecución
   vuelve a `prepare_publication` (ya idempotente) y produce un plan de publicación nuevo; si
   la base avanzó, falla con `base_moved` y aplica el punto 2.
4. Caducidad en `plan`: no existe TTL; se mantiene (un plan pendiente no consume nada).
5. Recordatorios ligados a la ventana real: a la mitad y a falta de una hora, por correo, con
   ordinal propio; `decision_reminder_seconds` actúa como tope.
6. Toda caducidad deja evidencia: código con la etapa correcta, evento con `stage` y
   `deadline`, correo con "qué sigue" (re‑creación automática o decisión humana).

## 7. Criterios medibles de salida para 1.0

- **Diez ejecuciones completas consecutivas** en `agentforge-sandbox-demo` (alcance aprobado →
  Developer → QA verde → publicación aprobada → PR en borrador), sin cambios de código del
  motor entre la primera y la décima, sin `technical_checks_failed` de origen técnico, sin
  `interrupted`, con coste medio documentado.
- **Dos objetivos de dos tareas dependientes completados** de principio a fin (fusión humana de
  T1, verificación de dependencia, T2 creada desde la base fusionada, T2 publicada).
- **Cero violaciones de seguridad**, definidas y comprobadas por consulta: ninguna escritura
  fuera del alcance aprobado (`tool_invocations`), ningún `push` fuera de `agentforge/<run_id>`,
  ninguna lectura de rutas excluidas, ningún token, correo ni contraseña en logs, eventos,
  checkpoints o correos (escaneo automático), 16/16 comprobaciones del Runner en cada arranque.
- Suite de resiliencia: 20 pasadas consecutivas en verde; CI de `main` verde 20 veces seguidas
  sin pruebas intermitentes (o en cuarentena con marcador y seguimiento).
- Recuperación demostrada una vez en real: reinicio de la API con una hija en QA → sin
  duplicados, hija re‑creada; caída de SMTP simulada → correos entregados después.
- Operación: respaldo antes de cada migración y una restauración probada; `agentforge_dev
  status` sin avisos; runbook de caducidades y fallos técnicos.
- **Identidad**: ninguna ejecución autenticada con el emisor de desarrollo, E2 incluida, cuenta
  como evidencia de separación de funciones real. El criterio "cero violaciones de seguridad"
  solo se declara cumplido con H9 desplegado y la campaña H7 hecha con credenciales humanas
  verificables.

## 8. Incrementos (cada uno con sus criterios)

| # | Incremento | Criterios de aceptación |
|---|---|---|
| H1 | **Compuerta de plan y hechos de política al Planner** (D1): el Supervisor antepone al texto de planificación los límites (`max_tasks`), rutas escribibles y prohibidas y el listado de rutas del espejo filtrado por `excluded_context_globs`; `RunLimits` gana `max_tasks`; una compuerta determinista entre Planner y Architect pide una revisión con retroalimentación fija (tareas de más, dependencias desconocidas, rutas prohibidas) y, agotadas las revisiones, termina `plan_invalid` sin llegar a la persona | Unitarias con FakeLLM: 3 tareas → revisión → 2 tareas → aprobación; 3 tareas persistentes → `plan_invalid` antes de `waiting_approval`; el texto del Planner contiene límites y rutas; ningún nuevo agente; `max_plan_revisions` respetado |
| H2 | **Caducidad correcta y recordatorios** (D3, D4): el barrido escribe `error_code='expired:<etapa>'` y el evento `run_expired{stage, deadline}`; `failure_code_for_run` usa ese código; avisos a 50 % y a falta de 60 min | Integración del barrido; unitaria de `reminder_due`; correo de caducidad con etapa correcta |
| H3 | **Re‑creación acotada por fallo técnico** (D2, D5, `base_moved`, reinicio, Vertex): `RETRYABLE_CHILD_CODES = {expired:scope, base_moved, base_stale, development_interrupted, runner_busy, runner_unreachable, llm_timeout, llm_unavailable}`, `max_retries_per_task=1`, re‑reserva de presupuesto, evento `task_retried`; espera acotada de `runner_busy`/`runner_unreachable` en `checks` | 23 escenarios existentes intactos + 6 nuevos (uno por código) con reloj controlado; dos coordinadores no duplican la re‑creación; un `runner_busy` simulado dos veces no cierra la ejecución |
| H4 | **Operación segura**: `agentforge_dev down` se niega con ejecuciones `running` salvo `--force`; `agentforge_dev migrate` = respaldo `pg_dump -Fc` + `alembic upgrade head` + verificación + recuentos; `restore-drill` en base desechable | Pruebas unitarias de los scripts con subprocess falso; un simulacro real documentado |
| H5 | **Suite de resiliencia** (§5) y trabajo de CI `resilience` | Once escenarios en verde, 20 pasadas consecutivas |
| H6 | **Prueba SIGKILL** (D6): márgenes relativos al TTL y marcador `timing` ejecutado en serie | 20 pasadas en verde con las tres suites en paralelo |
| H7 | **Campaña de validación 1.0** en el sandbox: 10 ejecuciones + 2 objetivos, evidencia en `docs/validation/agentforge-1.0-campaña.md`, correo y skill observados | Criterios del §7 cumplidos y firmados por el operador |
| H8 (opcional) | Tope de gasto diario con aviso al 80 % (D8) | Unitaria; aviso por correo; creación rechazada con `budget_daily_exceeded` |
| H9 (prerrequisito de H7) | **Identidad real y separación de funciones sostenible** (D9): emisor OIDC externo con credencial por persona vía `OIDC_JWKS_URL` (Pocket ID o Authentik en Proxmox, o Google); roles y proyectos asignados por un registro de sujetos de la plataforma (dato del operador, como `PROJECTS_FILE`), no por reclamaciones del token; `runs:approve` y `repos:publish` solo para principales `user` de emisores humanos de confianza, nunca el emisor de desarrollo fuera de `test`; CLI y skill sin `--as` ni `AGENTFORGE_SUBJECT` (inicio de sesión del proveedor, token en caché 0600, nunca firman); emisor, `jti` y tipo de principal persistidos en `run_decisions`, `publications` y `run_events` (migración); instancia diaria con perfil de producción (`OIDC_JWKS_FILE` y `AUTH_MODE=disabled` prohibidos); ADR que defina la separación como humano frente a automatización (ningún agente, delegado ni plataforma aprueba jamás) y la regla de dos humanos como ajuste explícito, obligatorio con más de un sujeto humano registrado | Un token del emisor de desarrollo no puede aprobar `publish` fuera de `test`; ningún módulo fuera de `tests/` acuña tokens; la CLI no contiene firma; cada decisión registra emisor y `jti`; una publicación real aprobada con credencial del proveedor externo |

Fuera de 1.0 (se mantienen en sus ADR): capacidad global y `max_active_runs>1` (Fase 8 Paso 2),
verificación de integración (Paso 3), hermanas disjuntas y Architect con contexto completo
(Paso 4), panel web, despliegue permanente en Proxmox (Fase 9). La identidad de producción
deja de estar fuera de 1.0: es H9 (D9) y precede a la campaña H7.
H1 toma de Paso 4 solo el listado de rutas como dato; H3 toma de Paso 2 solo la re‑creación
acotada; ninguno añade concurrencia.

## 9. Clivana: necesidades mínimas para incorporarlo con seguridad

**Bloqueo obligatorio (2026-10-31):** H9, identidad real, debe estar desplegado y validado antes de
registrar Clivana o cualquier repositorio de producción en `PROJECTS_FILE`. Mientras tanto AgentForge
solo opera sobre `sandbox-github`, y ninguna aprobación emitida con el emisor de desarrollo acredita
separación de funciones.

1. Confirmar la pila: el Runner solo ejecuta `ruff_check`, `ruff_format_check`, `ruff_check_diff`
   y `pytest` en el perfil `python-3.13`. Si Clivana no es un proyecto Python con ruff y pytest,
   QA no tiene comprobaciones ejecutables y **no es seguro** delegarle escritura hasta añadir
   un perfil y programas nuevos al Runner (cambio de infraestructura, fuera de 1.0).
2. GitHub App instalada en el repositorio de Clivana (misma App, `installation_id` propio;
   permisos actuales: `contents`, `metadata`, `pull_requests`); rama por defecto protegida con
   revisión obligatoria para que ningún PR en borrador pueda fusionarse sin una persona.
3. Política en `PROJECTS_FILE`: `repository_id` y `project_id` propios; `remote` con
   `github_repository_id`; `writable_globs` acotados al módulo (p. ej. `src/agenda/**`,
   `tests/agenda/**`); `denied_globs` para migraciones, configuración, datos, pruebas de
   aceptación y cualquier ruta con información clínica; `test_paths`/`lint_paths` reales;
   `max_files_per_run` bajo; TTL de alcance 86400; `publish.mode=manual`.
4. Espejo creado con `scripts.mirror_repository --create`; comprobación de que el espejo no
   contiene datos reales (solo código y pruebas sintéticas).
5. `PUBLISH_ALLOWED_REPOSITORIES` solo durante ventanas de publicación autorizadas;
   `GITHUB_WRITE_ENABLED=false` por defecto.
6. Primera tarea pequeña y verificable; primer objetivo solo tras H1–H3 y la campaña del §7.
7. Operador y aprobador: tu identidad con `project_operator` sobre el proyecto de Clivana;
   la skill detecta el repositorio por remoto y nunca asume autorización.

## 10. Primer incremento recomendado

**H1 — compuerta de plan y hechos de política al Planner.** Es el fallo más frecuente (dos de
tres objetivos reales murieron en el plan), cada caso cuesta una planificación y una decisión
humana, y la corrección es pequeña y determinista: un preámbulo construido por la plataforma,
`max_tasks` en `RunLimits` y una compuerta que reutiliza el bucle de revisión existente.
No añade agentes, tablas ni concurrencia. H2 (dos funciones y una prueba) puede acompañarlo
en la misma entrega sin ampliar el alcance.

## 11. Incidente E2 del 2026-10-31 07:52Z (tercer objetivo) y defectos P1/P2

Objetivo `4c03b45d`: el plan pasó la compuerta a la primera (H1 sin falsos positivos), el alcance de
T1 se aprobó con 24 h de ventana, Developer creó `src/mailcheck/normalize.py` y
`tests/test_normalize.py` y QA aprobó (ruff, formato y pytest en verde) en 21 segundos y 1,73 USD.
Pero la API corría con `GITHUB_WRITE_ENABLED=false`: `prepare_publication` no produce plan cuando el
Publisher no existe (`runs_set_publication` ni siquiera persiste `publish_unavailable`), el grafo
termina y la ejecución queda `completed`, estado terminal que `resume` no acepta. El trabajo quedó
en el worktree sin publicar. El Supervisor proyectó `completed` como tarea `published`, intentó
verificar una fusión sin `pr_number` y bloqueó el objetivo con `merge_unverified`, enviando además
un correo engañoso ("Fusión declarada por GitHub pero ausente en el espejo").

- **P1 (plataforma).** Con repositorio remoto y Publisher deshabilitado, la ejecución debe negarse
  a aprobar el alcance (`409 publish_unavailable`, antes de gastar nada) o detenerse en un estado
  reanudable tras QA, nunca terminar `completed` en silencio. La guía diaria debe decir que la
  bandera de escritura se habilita **antes de aprobar el alcance**, no al aprobar la publicación.
- **P2 (Supervisor).** `completed` en un repositorio con remoto no es `published`: debe proyectarse
  como `awaiting_publication` (o fallo técnico re-creable en H3), sin pasar por la verificación de
  fusión ni por su correo.

Ambos entran en H3 junto con la re-creación acotada; no se cambia nada durante esta ejecución.

### Estado de P1 y P2 (2026-10-31, rama `fix/publication-states`)

Corregidos en código con pruebas de regresión: compuerta `publish_unavailable` en la creación de
ejecuciones y objetivos; publicación no disponible ⇒ `interrupted` reanudable (nunca `completed`),
barrido sin reintentos, `resume` condicionado a la disponibilidad; Supervisor con `remote` en la
proyección (`completed` ≠ `published`), `publication_missing` en lugar de `merge_unverified`,
`publication_unavailable` como bloqueo explícito. El trabajo de `75192c74` no es recuperable por
mecanismo oficial (ejecución terminal); su evidencia está en
`~/.local/share/agentforge-backups/e2-t1-75192c74/`.

## 12. Brecha de identidad (comprobación de solo lectura, 2026-10-31 09:00–09:30Z)

Origen: la aprobación `publish` de T1 del objetivo `3946864c` (ejecución `5a957712`) fue rechazada
con `403 self_approval_forbidden` porque las hijas de un objetivo heredan como `owner_subject` al
creador del objetivo (`martin`) y la skill aprobaba como `martin`. Al investigar la alternativa de
aprobar con otro sujeto se comprobó lo siguiente, sin modificar código, políticas, PostgreSQL ni
configuración:

- La API verifica correctamente los tokens (RS256/ES256, `iss`, `aud`, `exp`, `iat`, `sub`, `kid`),
  pero con `OIDC_JWKS_FILE=.local/jwks.json` solo existe una clave pública; **todo token aceptado
  por esta instancia fue firmado con `.local/jwk_private.pem`** (0600, generado el 25 de octubre,
  propiedad del mismo usuario que ejecuta la API, la CLI, la skill y las sesiones de Claude Code).
- El sujeto es un identificador controlado por el cliente: `scripts/agentforge.py::headers_for`
  firma `sub=AGENTFORGE_SUBJECT` (o `--as`) con rol fijo `project_operator`; la skill hace lo mismo;
  `scripts/dev_token.py` firma cualquier `--sub` con cualquier `--roles`. Nadie autentica a la persona.
- Los roles (`project_operator` ⇒ `runs:approve` + `repos:publish`) y los proyectos se leen de las
  reclamaciones del token y se traducen por una tabla estática; no hay registro de qué sujetos pueden
  tener qué rol.
- Cualquier proceso que corra como `martin` en este Mac puede suplantar `agentforge-operator` o
  cualquier otra etiqueta cambiando una variable de entorno; la API no puede distinguirlo. La base
  de datos acumula seis etiquetas "humanas" distintas, todas firmadas con la misma clave.
- La publicación del 2026-10-27 (`de8845f3`) pasó la regla porque la ejecución se creó con la
  etiqueta `agentforge-operator` (token acuñado a mano antes de que existiera `scripts/agentforge.py`)
  y `martin` aprobó alcance y publicación. Las garantías reales fueron estructurales (nada sale del
  host sin una fila `publish` reclamada con el digest exacto; Publisher como código de plataforma;
  agentes sin tokens; tokens de GitHub por operación y revocados). La fila "Separación de funciones"
  de `docs/validation/fase7-paso3-criterios.md` debe reformularse: fue nominal.
- La auditoría guarda solo la etiqueta (`run_decisions.decided_by`, `publications.approved_by`,
  `run_events.actor_subject`); no guarda emisor, `jti` ni tipo de principal.
- No existe una vía oficial de excepción: la regla creador ≠ publicador en `decide_publish` es
  incondicional (ningún ajuste `AUTHZ_*` la desactiva), el cuerpo de `POST /runs/{id}/decisions`
  solo admite `approved`, `stage`, digests y `reason`, el único endpoint administrativo es el barrido
  de recuperación, y `AUTH_MODE=disabled` (`local-dev`) pasaría la regla a costa de eliminar toda
  autenticación. La única forma de continuar es firmar un token con otro `sub` controlado por la
  misma persona.

Consecuencias registradas:

1. **E2 valida exclusivamente la orquestación técnica del sandbox** (Supervisor, Scoper, Developer,
   QA, Publisher, dependencias `after_merged`). No cuenta, ni contará, como evidencia de
   autenticación ni de separación de funciones real, se apruebe T1 como se apruebe.
2. El operador autorizó el 2026-10-31 una excepción local, explícita y limitada a la publicación de
   T1 del objetivo `3946864c`, con la condición de que Claude Code no cambie `AGENTFORGE_SUBJECT`
   por su cuenta para simular un segundo aprobador. Al no existir vía formal con la identidad real
   `martin`, la aprobación **queda pendiente de una instrucción humana que nombre el mecanismo**;
   no se registró ninguna decisión `publish` (ejecución `waiting_approval`, digest `8c1cff44…`,
   caducidad 2026-11-03T09:03Z, `publications` 6 en `prepared`, nada en GitHub).
3. D9 entra en el plan y H9 lo corrige antes de la campaña H7 (§8). Hasta entonces, cualquier
   aprobación `publish` en local se documenta como excepción de laboratorio con la etiqueta usada,
   nunca como separación de funciones, y no se convierte en procedimiento normal de operación.
4. Correcciones de documentación pendientes: `docs/uso-diario.md` §4 omite que el creador nunca
   publica; ADR 0005 §"Separación de funciones" y la tabla de validación de la Fase 7 deben decir
   qué garantiza y qué no la configuración local.

### Resultado de la excepción de laboratorio (2026-10-31 09:26Z)

El titular autorizó por escrito una excepción única: etiqueta operativa `martin:publisher`, declarada
como la misma persona que `martin`, exclusiva para el repositorio `sandbox-github`, el objetivo
`3946864c`, la ejecución `5a957712`, la etapa `publish`, el digest `8c1cff44…aeacd3e` y el commit
`d75b852` sobre la base `442c0c7`, con un motivo literal que la califica de excepción y niega que
acredite separación de funciones. **E2 queda marcada como prueba de orquestación exclusivamente.**

- Verificaciones previas repetidas a las 09:25Z: `waiting_approval`/`publish`, digest idéntico, 71,6 h de
  vigencia, repositorio y rama remota esperados, `head d75b852` con único padre `442c0c7`, solo los dos
  archivos autorizados añadidos con los sha256 de la propuesta, QA 0/0/0, escaneo limpio, `main` en
  `442c0c7` en GitHub y en el espejo, cero decisiones `publish`, ramas remotas sin cambios, escritura
  habilitada solo en el entorno del proceso de la API. El digest se recalculó fuera del Publisher con
  `scripts.publication_digest` (`matches: true`).
- Mecanismo: `AGENTFORGE_SUBJECT=martin:publisher` únicamente en el entorno del proceso `af decide`;
  no se escribió en `.env`, en la configuración de la skill ni en ningún otro lugar; no se reutilizará
  en otras decisiones. Sin cambios en `AUTH_MODE`, políticas, código ni PostgreSQL.
- Auditoría: `run_decisions` `publish` aprobado por `martin:publisher` a las 09:26:24Z con el motivo
  literal; `publications` 7–10 `approved → pushed → opened → published`, intento 1, actor
  `agentforge:publisher`; eventos `approval_recorded`/`run_approved` (ordinal 2, actor
  `martin:publisher`), `branch_pushed` 09:26:27Z, `pull_request_opened` 09:26:29Z, `run_published`
  09:26:30Z, `approval_applied`; correo `pull_request_opened` enviado.
- GitHub: PR #2 en borrador <https://github.com/martinDTV/agentforge-sandbox-demo/pull/2>, abierto por
  `agentforge-sandbox-martindtv[bot]`, rama `agentforge/5a957712-c917-40d7-8349-96fe729a3abc` en
  `d75b852`, base `main@442c0c7`, 1 commit, 2 archivos, +59/−0, `mergeable_state: blocked`, no
  fusionado; `main` intacto; el espejo recibió `refs/remotes/origin/agentforge/5a957712…`.
- Credenciales: dos tokens de instalación (push y PR) emitidos solo durante la publicación; el nodo
  `publish` los revoca en su `finally` con `DELETE /installation/token`; el flujo terminó sin excepción y
  el token de push tiene `token_expires_at` 10:26:26Z como tope duro. **Observación:** el resultado de la
  revocación no queda en auditoría ni en el log (`revoke_installation_token` devuelve `False` en silencio
  ante un error de transporte); añadir a H9 la persistencia del resultado de revocación. Ningún patrón de
  token en `run_events`, `publications`, `runs`, `run_decisions` ni en el log de la API.
- Supervisor: `task_published` 09:26:31Z; T1 `published` con PR #2; T2 `pending` hasta la fusión humana
  de #2 y la evidencia `after_merged`; objetivo `active`, 5 llamadas de modelo usadas de 28 reservadas.

La fila "Separación de funciones" de cualquier informe de E2 debe decir: **no evaluada; excepción de
laboratorio con una sola persona**.

### Fusión humana de T1 y materialización de T2 (2026-10-31 09:43–09:58Z)

- Antes de la fusión se comprobó: PR #2 abierto, head `d75b852`, base `main@442c0c7`, dos archivos +59/−0,
  `mergeable: true`, sin check runs ni status checks configurados, ruleset `protect-main` activo sin
  bypass (sin borrado, sin forzado, Pull Request con una aprobación). El `blocked` se debía al borrador y
  a la revisión pendiente. Claude Code marcó el PR como listo para revisión (09:43:21Z) y **no** emitió la
  revisión: la aprobación la registró el titular (`martinDTV`, 09:46:07Z, sobre `d75b852`) y fusionó él
  mismo con merge commit `6d01b1313edbcf9cdb238df4502e915649d065cb` (09:46:13Z, padres `442c0c7` y
  `d75b852`). Sin bypass ni cambios en las reglas. La rama `agentforge/5a957712…` se conserva.
- Supervisor, reconciliación de las 09:57:23Z: `dependency_merged` para T1 con evidencia de GitHub
  (`merged_at` 09:46:13Z, `merge_commit_sha 6d01b13`) verificada en el espejo
  (`verified_in_mirror: true`, `origin/main = 6d01b13`); T1 pasa a `merged`; `task_ready` T2;
  `child_created` una sola vez (`22d23149-f16e-4bc8-bfee-a095d4533032`, intento 1, clave
  `goal:3946864c:T2:1`, `delegated_for martin`, `planned_upstream`); `task_base_recorded` con
  `base_sha 6d01b13`. Ruta `fast` por regla `planned_upstream` (etiqueta `planned_upstream:none`, D7).
- T2 propuso su alcance a las 09:57:36Z y espera decisión humana (`waiting_approval`/`scope`, caducidad
  2026-11-01T09:57:36Z): editar `src/mailcheck/validator.py` y `tests/test_validator.py`; pruebas
  previstas `tests/test_validator.py`, `tests/test_acceptance.py`, `tests/test_normalize.py`; digest
  `a58a500ff02e59f259d6972a3c32aa4d076f3b224c86c757926d60a01532bc1d`; base `main@6d01b13` sin cambios
  desde la sincronización; worktree y rama `run/22d23149…` creados en `6d01b13`; una ruta oculta
  (`AGENTS.md`). Coste del Scoper: 1 llamada, 13 500 tokens de entrada y 4 487 de salida (0,57 USD
  reservados). Objetivo: 2 de 2 tareas creadas, 28 de 28 llamadas reservadas, 20 de 25 USD reservados,
  6 llamadas usadas. No se aprobó nada.

### T2: alcance aprobado, Developer y QA, plan de publicación (2026-10-31 10:19–10:21Z)

- Aprobación `scope` de `22d23149` registrada por `martin` (identidad normal, sin etiqueta alternativa) a las
  10:19:57Z con el motivo literal del titular y el digest `a58a500f…`, tras verificar estado, digest,
  caducidad, `main` y espejo en `6d01b13`, dos archivos autorizados, límites de la política sin cambios
  (`projects.json` sin modificar desde el 2026-10-31T03:46Z) y Publisher habilitado solo para
  `sandbox-github` en el entorno del proceso. **Hallazgo de la skill (D10, no del motor):** `af decide`
  devolvió `api_timeout` porque su cliente HTTP espera 30 s y `POST /approve` responde cuando termina la
  primera pasada de Developer y QA (26 s en T1, 52 s en T2); la decisión quedó registrada una sola vez
  (CAS) y no se repitió. Corregir en la skill: tiempo de espera mayor para `decide` o, mejor, releer la
  decisión antes de declarar fallo.
- Developer: 2 archivos modificados en el worktree (`src/mailcheck/validator.py` +5, `tests/test_validator.py`
  +20); commit `902fae9a9f2ce56cf8d3d17ff175f023c194239f` sobre `6d01b13`; `tests/test_acceptance.py` con
  el mismo blob (`476c6bd0`) en base y head. QA: `ruff_check`, `ruff_format_check` y `pytest` con salida 0,
  0 incidencias, sin correcciones. `publication_prepared` 10:20:50Z; `waiting_approval`/`publish`,
  digest `4a20be1c4a537edfcecbd41b0d840674270f0bb2ad4db2f0bb55eeb3b5419132`, caducidad
  2026-11-03T10:20:50Z, rama `agentforge/22d23149-f16e-4bc8-bfee-a095d4533032`, escaneo limpio; digest
  recalculado de forma independiente (`matches: true`). Nada empujado ni PR creado.
- Consumo de la ejecución: 3 llamadas de modelo de 10 (Scoper 1, Developer 1, QA 1), 30 181 tokens de
  entrada y 8 779 de salida acumulados, 2,36 USD reservados de 8. Objetivo: 28 llamadas reservadas de
  28, 20 USD reservados de 25.
- Pendiente de decisión humana: `publish` de T2. Si se aprueba, aplica la misma excepción de identidad
  que T1 y debe autorizarse de nuevo de forma expresa (la etiqueta `martin:publisher` no se reutiliza
  sin instrucción).

### Publicación de T2 bajo la segunda excepción de laboratorio (2026-10-31 10:25Z)

- Autorización escrita del titular para la ejecución `22d23149`, etapa `publish`, digest `4a20be1c…9132`,
  commit `902fae9` sobre `6d01b13`, repositorio `sandbox-github`, con motivo literal que declara la
  etiqueta `martin:publisher` como la misma persona y niega separación de funciones. Comprobaciones previas
  repetidas a las 10:24Z (dieciocho condiciones: estado, digest, 71,9 h de vigencia, head, padre, base,
  diff de dos archivos modificados, `tests/test_acceptance.py` con el mismo blob `476c6bd0`, QA 0/0/0,
  escaneo limpio, `main` y espejo en `6d01b13`, ramas y PR remotos sin cambios, Publisher solo para
  `sandbox-github`, cero decisiones `publish`); todas coincidieron.
- Mecanismo idéntico al de T1: `AGENTFORGE_SUBJECT=martin:publisher` solo en el entorno del proceso
  `af decide`; nada persistido en `.env` ni en la configuración de la skill; sin cambios en `AUTH_MODE`,
  políticas, código, credenciales ni PostgreSQL.
- Auditoría: `run_decisions` `publish` aprobado por `martin:publisher` 10:25:30Z con el motivo literal;
  `publications` 12–15 `approved → pushed → opened → published`, intento 1, actor `agentforge:publisher`;
  eventos `branch_pushed` 10:25:34Z, `pull_request_opened` 10:25:35Z, `run_published` 10:25:36Z; correos
  `publish_pending`, `pull_request_opened` y `goal_completed` enviados.
- GitHub: PR #3 en borrador <https://github.com/martinDTV/agentforge-sandbox-demo/pull/3>, abierto por
  `agentforge-sandbox-martindtv[bot]`, rama `agentforge/22d23149-f16e-4bc8-bfee-a095d4533032` en
  `902fae9`, base `main@6d01b13`, 1 commit, 2 archivos modificados, +25/−0, `mergeable_state: blocked`
  (borrador y revisión requerida), no fusionado; `main` intacto.
- Credenciales: token de push con `token_expires_at` 11:25:32Z y token de PR, ambos emitidos solo durante
  la publicación y revocados por el `finally` del nodo `publish`; el flujo terminó sin excepción. Sigue
  sin auditarse el resultado de la revocación (anotado en H9). Ningún patrón de token en tablas ni log.
- Supervisor: `task_published` T2 y `goal_completed` (2 tareas, 0 fallidas) a las 10:25:36Z. El objetivo
  termina `completed` con la última tarea publicada: la fusión del PR #3 es un paso humano fuera del
  ciclo del objetivo, por diseño (ADR 0012). T1 `merged`, T2 `published`.
- Presupuesto final del objetivo: 8 llamadas de modelo usadas de 28 reservadas (planificación 2, T1 3, T2 3),
  20 USD reservados de 25; T2 reservó 2,36 USD reales de 8.

**Cierre de E2 pendiente:** fusión humana del PR #3, verificación final y reinicio de la API sin
`GITHUB_WRITE_ENABLED` ni `PUBLISH_ALLOWED_REPOSITORIES`. E2 se registra como validación de orquestación
(Supervisor, Scoper, Developer, QA, Publisher, dependencia `after_merged`, dos publicaciones reales).
La separación real de identidades no se validó y H9 sigue siendo bloqueo obligatorio para producción.

### Cierre de E2 (2026-10-31 10:29–10:36Z)

- PR #3: listo para revisión a las 10:29:16Z (Claude Code, tras verificar head `902fae9`, base `main@6d01b13`,
  dos archivos +25/−0, sin conflictos, ruleset activo sin bypass); revisión aprobatoria del titular
  (`martinDTV`) a las 10:33:13Z sobre `902fae9`; fusión por el titular a las 10:33:19Z con merge commit
  **`3117f6ff186d34d629ad47b3696e6d2226d892f3`** (padres `6d01b13` y `902fae9`). Claude Code no emitió
  revisiones ni ejecutó este merge; sus intentos de fusión se detuvieron por diseño al no constar aún la
  revisión. Las tres ramas `agentforge/<run_id>` se conservan.
- `main` en GitHub y el espejo (refrescado con `scripts.mirror_repository --refresh`, `6d01b13 → 3117f6f`)
  contienen los commits de T1 (`d75b852`) y T2 (`902fae9`); blobs de `normalize.py`, `validator.py`,
  `test_normalize.py` y `test_validator.py` idénticos en GitHub y espejo; `tests/test_acceptance.py`
  conserva el blob `476c6bd0` desde el commit semilla `b04ba76`.
- Plataforma en reposo antes del reinicio: objetivo `completed` (T1 `merged`, T2 `published`), ninguna
  ejecución `running`/`waiting_approval`/`interrupted`/`resume_pending`, sin leases, todas las
  publicaciones en estado terminal, bandeja de correo con 23 enviados y 0 pendientes.
- Reinicio controlado: `agentforge_dev down` (pid 98686, 10:35:06Z) y `up` (pid 85504, 10:35:08Z) **sin**
  `GITHUB_WRITE_ENABLED` ni `PUBLISH_ALLOWED_REPOSITORIES` (ausentes del entorno del nuevo proceso y del
  `.env`); `AUTH_MODE=oidc`, `ENVIRONMENT=local`, resto de configuración intacta. Readiness `ok` en base de
  datos, esquema, checkpoints y `notifications: worker`; Alembic `0011 (head)`; Runner
  `agentforge-runner-01` (podman rootless) `ready` con todas sus comprobaciones en verde; la skill ve 0
  decisiones pendientes, 0 ejecuciones activas y 0 objetivos activos. La escritura remota queda
  deshabilitada: cualquier trabajo nuevo sobre `sandbox-github` será rechazado con `publish_unavailable`
  hasta una nueva ventana autorizada.
- Contabilidad final (modelo `claude-sonnet-4-6` en Vertex): llamadas 8 de 28 reservadas (planificación 2,
  T1 3, T2 3). Tokens reales registrados: T1 11 431 entrada / 3 845 salida; T2 30 181 / 8 779; la
  ejecución de planificación no registra tokens en eventos (brecha menor de observabilidad). Reservas: 20
  USD a nivel de objetivo (de 25) y 1,72 + 2,36 USD a nivel de ejecución (a 20 USD/millón, tope
  conservador de `BudgetedLLM`). Gasto efectivo **estimado** a tarifa de lista de Anthropic para Sonnet 4.6
  (3 / 15 USD por millón): T1 ≈ 0,09 USD, T2 ≈ 0,22 USD, total ≈ 0,31 USD sin la planificación; la
  facturación real de Vertex puede diferir y la plataforma no contabiliza gasto efectivo (añadir a H8/D8).
- Decisiones humanas: 5 (plan, 2 alcances, 2 publicaciones). Excepciones de identidad de laboratorio: 2,
  ambas `martin:publisher`, autorizadas por escrito, no persistidas, no reutilizables. Revisiones y
  fusiones en GitHub: 2, ambas del titular.
- Motor sin cambios durante E2: árbol de trabajo limpio sobre `8d4daba`; ninguna política, migración ni
  configuración modificada; `projects.json` intacto desde el 2026-10-31T03:46Z.

**Veredicto de E2:** validación de orquestación superada (Supervisor con plan aprobado, dos hijas
secuenciales con dependencia `after_merged` verificada en GitHub y espejo, Scoper, Developer, QA en Runner
aislado, Publisher con dos PR reales, correos). **No** valida autenticación ni separación de funciones.
Pendientes para 1.0: H3 (re-creación acotada por fallo técnico), H9 (identidad real, bloqueo obligatorio
para Clivana/producción), D10 (tiempo de espera de `af decide` en la skill), auditoría del resultado de la
revocación de tokens de instalación, tokens de la ejecución de planificación en eventos, etiqueta D7.

**Documentos permanentes derivados (2026-10-31):** informe de cierre de E2 en `docs/validation/e2-supervisor-orquestacion-2026-10-31.md`; diseño de H9 en `docs/adr/0013-identidad-verificable-h9.md` (propuesto, pendiente de revisión del titular).
