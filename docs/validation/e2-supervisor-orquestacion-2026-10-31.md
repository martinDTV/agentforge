# E2 — Validación real del Supervisor de objetivos (orquestación), 2026-10-31

**Clasificación: prueba de orquestación técnica.** E2 demuestra que AgentForge convierte un objetivo
aprobado en dos tareas dependientes, las desarrolla, las verifica, las publica como Pull Requests
en borrador y encadena la segunda solo tras comprobar la fusión de la primera. **E2 no valida
autenticación ni separación real de funciones**: las dos aprobaciones de publicación se registraron
bajo una excepción de laboratorio con una sola persona (ver §6 y el ADR 0013). Los datos privados
(`.local/`, respaldos, worktrees) quedan fuera de este documento y del repositorio.

## 1. Alcance y entorno

| Elemento | Valor |
|---|---|
| Motor | `main` en `8d4daba` (PR #11 H1/H2, PR #12 compuerta de plan, PR #13 P1/P2 integrados); sin cambios de código, políticas ni configuración durante E2 |
| Base de datos | PostgreSQL local de desarrollo, Alembic `0011 (head)` |
| Modelo | `claude-sonnet-4-6` vía Vertex AI, `LLMPort` con `BudgetedLLM` |
| Ejecución aislada | Runner `agentforge-runner-01`, Podman rootless, 16/16 comprobaciones, perfil `python-3.13` |
| Repositorio | `martinDTV/agentforge-sandbox-demo` (id 1435009323), política `sandbox-github`: escribible `src/**`, `tests/**`; prohibido `tests/test_acceptance.py`; 6 archivos, 10 llamadas, 8 USD y 1500 s por ejecución; TTL de alcance 24 h; `publish.mode = manual` |
| Publicación | `GITHUB_WRITE_ENABLED=true` y `PUBLISH_ALLOWED_REPOSITORIES=["sandbox-github"]` **solo en el entorno del proceso** de la API (pid 98686, 08:53Z–10:35Z); `.env` sin cambios; deshabilitada de nuevo al cierre |
| Objetivo | Descomposición `plan`, máximo 2 tareas, 25 USD, 7 días; T1 `normalize_email` + pruebas; T2 integrarla en `validate_email` sin tocar las 7 reglas ni los veredictos de aceptación; `after_merged` |

## 2. Intentos previos (contexto)

| Objetivo | Resultado | Causa | Dónde está el detalle |
|---|---|---|---|
| `7bd42940` (10-30) | `plan_rejected` | Plan contrario a `test_acceptance.py` y a un `validate_email` aún no fusionado | `docs/validation/fase8-paso1-validacion-real.md` |
| `bac6eb0c` (10-30/31) | `expired:plan` (código erróneo: la etapa caducada fue `scope`) | TTL de alcance de 60 min; defectos D2/D3/D4 | `docs/audits/2026-10-31-plan-de-estabilizacion-1.0.md` §2 |
| `88b23688` (10-31) | `plan_rejected` | Plan de 3 tareas fuera de política (origen de H1) | ídem, §2 y §8 |
| `4c03b45d` (10-31) | `cancelled` | T1 terminó `completed` sin publicación con la escritura apagada y el Supervisor la tomó por publicada (P1/P2, PR #13) | ídem, §11 |
| **`3946864c` (10-31)** | **`completed`** | Este documento | — |

## 3. Cronología del objetivo `3946864c-4337-42ba-a509-a01bbae4db8f` (UTC)

| Hora | Hecho | Actor |
|---|---|---|
| 08:53:45 | Objetivo creado; planificación `3ed0afae` reservada y lanzada | `martin` (skill) / Supervisor |
| 08:54:12 | Planner y Architect proponen 2 tareas (2 llamadas, revisión 0, compuerta de plan en verde) | plataforma |
| 08:57:07 | **Plan aprobado** (revisión 0); T1 materializada y creada (`5a957712`), base `442c0c7` | `martin` / Supervisor |
| 08:57:19 | Ruta `fast` (regla `planned_upstream`); Scoper propone alcance, digest `d44f1c10…` | plataforma |
| 09:02:37 | **Alcance de T1 aprobado** | `martin` |
| 09:03:01 | Developer (2 archivos) y QA (`ruff_check`, `ruff_format_check`, `pytest` = 0) | agentes en Runner |
| 09:03:03 | Plan de publicación: head `d75b852`, digest `8c1cff44…`, caducidad 11-03 09:03 | Publisher |
| 09:14 | Intento de aprobación `publish` como `martin` rechazado: `403 self_approval_forbidden` | plataforma |
| 09:26:24 | **Publicación de T1 aprobada** bajo excepción de laboratorio (`martin:publisher`) | titular |
| 09:26:29 | Rama `agentforge/5a957712…` empujada; **PR #2** en borrador | Publisher |
| 09:43:21 | PR #2 listo para revisión | Claude Code |
| 09:46:07 / 09:46:13 | Revisión aprobatoria y **fusión del PR #2**, merge commit `6d01b13` | `martinDTV` |
| 09:57:23 | `dependency_merged`: evidencia GitHub (`merged_at`, `merge_commit_sha`) verificada en el espejo; T1 `merged`; T2 lista | Supervisor |
| 09:57:36 | T2 creada una sola vez (`22d23149`, clave `goal:3946864c:T2:1`), base `6d01b13`; alcance propuesto, digest `a58a500f…` | Supervisor / Scoper |
| 10:19:57 | **Alcance de T2 aprobado** | `martin` |
| 10:20:48 | Developer (2 archivos) y QA (0/0/0, sin correcciones) | agentes en Runner |
| 10:20:50 | Plan de publicación: head `902fae9`, digest `4a20be1c…`, caducidad 11-03 10:20 | Publisher |
| 10:25:30 | **Publicación de T2 aprobada** bajo la segunda excepción de laboratorio | titular |
| 10:25:35 | Rama `agentforge/22d23149…` empujada; **PR #3** en borrador; objetivo `completed` | Publisher / Supervisor |
| 10:29:16 | PR #3 listo para revisión | Claude Code |
| 10:33:13 / 10:33:19 | Revisión aprobatoria y **fusión del PR #3**, merge commit `3117f6f` | `martinDTV` |
| 10:35:06–10:35:08 | Reinicio de API y worker **sin** variables de escritura (pid 85504) | operador |
| 10:35:50 | Espejo refrescado `6d01b13 → 3117f6f` (`scripts.mirror_repository --refresh`) | operador |

Tiempos útiles: plan en 27 s; T1 de alcance aprobado a plan de publicación en 26 s; T2 en 53 s; el
resto fue espera humana (aprobaciones y fusiones) y el intervalo de reconciliación del Supervisor
(11 min entre la fusión de T1 y la creación de T2).

## 4. Resultados

| | T1 | T2 |
|---|---|---|
| Ejecución | `5a957712-c917-40d7-8349-96fe729a3abc` | `22d23149-f16e-4bc8-bfee-a095d4533032` |
| Base Git | `442c0c7` | `6d01b1313edbcf9cdb238df4502e915649d065cb` |
| Commit del agente | `d75b85283bfdfe40f2a4f1cfeb70efb1c93cc6eb` | `902fae9a9f2ce56cf8d3d17ff175f023c194239f` |
| Archivos | `src/mailcheck/normalize.py` (+14), `tests/test_normalize.py` (+45), ambos nuevos | `src/mailcheck/validator.py` (+5), `tests/test_validator.py` (+20), ambos editados |
| QA (Runner) | `ruff_check` 0, `ruff_format_check` 0, `pytest` 0 | ídem; QA aprobó a la primera |
| Escaneo previo a publicar | 0 hallazgos | 0 hallazgos |
| Digest de publicación | `8c1cff4439115d654d7b1719d42b45109cf97ca0d7f904b8bfa4ad4d4aeacd3e` | `4a20be1c4a537edfcecbd41b0d840674270f0bb2ad4db2f0bb55eeb3b5419132` |
| Digest recalculado fuera del Publisher | coincide | coincide |
| Pull Request | #2, borrador, abierto por la App | #3, borrador, abierto por la App |
| Fusión | `martinDTV`, merge commit `6d01b13` | `martinDTV`, merge commit `3117f6f` |
| `tests/test_acceptance.py` | blob `476c6bd0` intacto | blob `476c6bd0` intacto (igual que el commit semilla `b04ba76`) |

`main` en GitHub y el espejo local contienen ambos commits; los blobs de los cuatro archivos son
idénticos en los dos lugares. Las ramas `agentforge/<run_id>` se conservan; AgentForge no borra ramas
ni fusiona.

## 5. Decisiones humanas

| # | Ejecución | Etapa | Sujeto registrado | Hora | Observación |
|---|---|---|---|---|---|
| 1 | `3ed0afae` | plan | `martin` | 08:57:07 | revisión 0, motivo literal del titular |
| 2 | `5a957712` | scope | `martin` | 09:02:37 | digest `d44f1c10…` |
| 3 | `5a957712` | publish | `martin:publisher` | 09:26:24 | **excepción de laboratorio 1** |
| 4 | `22d23149` | scope | `martin` | 10:19:57 | digest `a58a500f…` |
| 5 | `22d23149` | publish | `martin:publisher` | 10:25:30 | **excepción de laboratorio 2** |

Más dos revisiones aprobatorias y dos fusiones en GitHub, todas de `martinDTV`. Claude Code no
emitió revisiones ni ejecutó fusiones; marcó los dos PR como listos para revisión.

## 6. Excepciones de identidad (lo que E2 no demuestra)

La regla `decide_publish` prohíbe que el `owner_subject` apruebe la publicación, y las hijas de un
objetivo heredan como dueño al creador del objetivo (`martin`). Con una sola persona, la única
salida sin cambiar el motor fue aprobar con una etiqueta local distinta. El titular autorizó por
escrito, para cada publicación, la etiqueta `martin:publisher` declarándola **la misma persona**, con
un motivo que niega expresamente la separación de funciones. La etiqueta viajó solo en el entorno
del proceso de la skill y no quedó en ninguna configuración.

Lo que la auditoría de identidad encontró (D9): la instancia acepta únicamente tokens firmados con la
clave de desarrollo de `.local/`, propiedad del mismo usuario del sistema que ejecuta API, CLI, skill
y Claude Code; la CLI firma cualquier `sub` con rol `project_operator`; los roles se leen del token;
la auditoría guarda solo la etiqueta. Por tanto **ninguna aprobación de E2, ni la del 27 de octubre,
acredita dos identidades humanas**. La corrección es H9 (ADR 0013), bloqueo obligatorio antes de
Clivana o cualquier repositorio de producción.

## 7. Costos: reservado, real y no disponible

| Categoría | Dato | Fuente |
|---|---|---|
| **Presupuesto reservado** (tope, no gasto) | Objetivo: 28 llamadas y 20 USD reservados de 28 / 25. Ejecuciones: T1 1,72 USD, T2 2,36 USD, valoradas a 20 USD por millón de tokens reservados (`BudgetedLLM`, `CONSERVATIVE_USD_PER_MILLION`) | `goals`, eventos `*_completed` |
| **Llamadas reales** | 8: planificación 2, T1 3 (Scoper, Developer, QA), T2 3 | `runs.llm_calls`, `goals.llm_calls_used` |
| **Tokens reales** | T1: 11 431 entrada / 3 845 salida. T2: 30 181 / 8 779. Total 41 612 / 12 624 | eventos `scope_proposed`, `developer_completed`, `qa_reviewed` |
| **Gasto efectivo estimado** | T1 ≈ 0,09 USD; T2 ≈ 0,22 USD; **≈ 0,31 USD** a tarifa de lista de Anthropic para Sonnet 4.6 (3 / 15 USD por millón) | cálculo sobre los tokens anteriores |
| **No disponible** | Tokens de la planificación (2 llamadas, sin `usage` en eventos); facturación real de Vertex AI (tarifa propia, no consultada); la plataforma no contabiliza gasto efectivo, solo reservas (D8) | — |

## 8. Seguridad observada

- Escrituras de los agentes: solo `workspace.write_file` sobre los archivos del alcance aprobado
  (`tool_invocations`); `exec.run` únicamente con los programas de QA permitidos.
- Push: exclusivamente a `agentforge/<run_id>` con `--force-with-lease`; `main` solo cambió por las
  fusiones humanas. Ninguna rama borrada.
- Tokens de GitHub: emitidos por operación durante la publicación, con caducidad de una hora
  registrada en `publications.token_expires_at`; revocación ejecutada por código en el `finally` del
  nodo `publish`. **Sin evidencia verificable del resultado de la revocación** (pendiente).
- Sin patrones de token en `run_events`, `publications`, `runs`, `run_decisions` ni en el log de la API.
- Archivos de instrucciones para agentes ocultos al Scoper (`hidden_paths: 1`).
- Hallazgo operativo de la skill (D10): `af decide` espera 30 s y `POST /approve` responde al terminar
  la primera pasada de Developer y QA (52 s en T2); la decisión se registró una sola vez.

## 9. Criterios de AgentForge 1.0

Demostrados hoy:

- Objetivo con plan aprobado y **dos tareas dependientes completadas** de extremo a extremo, con
  fusión humana de T1, verificación `after_merged` en GitHub y espejo, T2 creada una sola vez desde la
  base fusionada y publicada.
- Dos ejecuciones completas consecutivas (alcance → Developer → QA → publicación → PR) sin cambios de
  motor entre ellas; sin `technical_checks_failed`, sin `interrupted`.
- Cero violaciones de seguridad observables en el alcance de E2 (escrituras, push, lecturas excluidas,
  tokens en logs), Runner 16/16 en cada arranque.
- Reinicio controlado con la plataforma en reposo y escritura remota deshabilitada al cierre.

Pendientes:

- Diez ejecuciones completas consecutivas y un segundo objetivo de dos tareas (campaña H7).
- Suite de resiliencia (20 pasadas) y CI estable (H5, H6); recuperación demostrada en real; simulacro
  de restauración (H4).
- **Aprobaciones con credenciales humanas verificables (H9)**: hasta entonces, ninguna publicación
  cuenta como separación de funciones.
- Re-creación acotada por fallo técnico (H3); evidencia de revocación de tokens; D10; D7; D8.

## 10. Referencias

- `docs/audits/2026-10-31-plan-de-estabilizacion-1.0.md` (diario de la estabilización, §11–§12 con la
  evidencia completa de E2 y de la brecha de identidad) y `docs/audits/2026-10-30-auditoria-de-cierre.md`.
- ADR 0011 (Publisher), ADR 0012 (Supervisor), **ADR 0013 (identidad verificable, propuesto)**.
- Pull Requests del sandbox: #2 (`6d01b13`), #3 (`3117f6f`).
