# I0.1 — Conducción separada de la petición HTTP: informe de cierre (2026-10-31)

Incremento I0 del plan maestro 1.0 (estabilidad antes que funcionalidad). Objetivo: que `POST /runs`,
`/approve` y `/resume` respondan cuando el trabajo es durable y el grafo se conduzca separado de la
petición, sin perder ejecuciones ni decisiones y sin reescribir la orquestación. Autorizado como
cambio pequeño y verificable; integrado localmente en `main` con revisión humana previa.

## Integración

| Elemento | Valor |
|---|---|
| `main` antes de integrar | `5703382` |
| Rama integrada | `feat/i0-1-detached-drive` → `44ee51f` |
| Merge (sin squash, historia conservada) | `88e265d` |
| Commits integrados | `acfcd7e` motor y pruebas · `8663a31` CLI `--wait` · `cc1a2fd` documentación · `44ee51f` prueba de integración |
| Ajuste posterior en `main` | `d236ef6` (`tests/conftest.py::settle` espera a la tarea de fondo, no solo al estado) |
| `origin/main` (último conocido, sin `fetch`) | `8d4daba` — **no se ha hecho push, PR ni etiqueta** |

El merge no toca `app/security`, `app/publishing`, `app/tools`, `app/goals`, `app/sandbox`,
`app/projects`, `app/git`, `app/workspaces`, `app/infrastructure`, `app/contracts`, `migrations` ni
`runner` (diff vacío). Archivos de aplicación cambiados: `app/orchestration/{runs,status,projection,
exceptions,recovery,goals}.py`, `app/main.py`, `app/api/runs.py` (docstrings), `scripts/agentforge.py`,
`scripts/development_mvp.py`.

## Verificación sobre `main`

Primera ronda, sobre `88e265d`: ruff, format, mypy (app y runner), unit 904 en verde; integración
88/89 — falló `test_control_api.py::test_event_pages_are_persisted_with_actors` por una carrera del
arnés: `settle` devolvía al ver el estado proyectado y el evento `approval_applied` lo escribe la
tarea de fondo justo después. Corrección solo en pruebas (`d236ef6`): cuando el cliente expone la
app, `settle` espera además a que la ejecución salga de `active_run_ids`.

Segunda ronda, sobre `d236ef6`:

| Comprobación | Resultado |
|---|---|
| `uv run ruff check .` / `ruff format --check .` | limpio (356 archivos) |
| `uv run mypy app` / `mypy runner/agentforge_runner` | sin errores (160 / 14 archivos) |
| `uv run pytest -q` | 904 passed |
| `uv run pytest tests/integration -q` (PostgreSQL local, bases desechables) | 89 passed |
| `git diff --check 5703382..HEAD` | sin problemas |
| Árbol | limpio salvo archivos sin seguimiento ajenos a I0.1 (ver estado operativo) |

Contratos: autorización y separación de funciones (`test_api_authz`, `test_authorization`),
publicación idempotente y PR en borrador (`test_publication_flow`, incluida la nueva prueba de
aprobaciones `publish` concurrentes en modo detached), presupuesto y decisiones (`test_api_development`,
`test_scoped_development`, `test_decision_reason*`) pasan sin cambios de código en esos módulos.

## Skill `/agentforge`

Otro proceso modificó `~/.claude/skills/agentforge` entre las 17:38 y las 18:09 (timeouts HTTP
explícitos y configurables; clave de idempotencia por solicitud con `--request-id`, aleatoria por
defecto). Esos cambios son compatibles con I0.1 y se conservaron íntegros. El parche mínimo de
compatibilidad (seguimiento acotado tras `task` y `decide`, relectura de la decisión registrada ante
`api_timeout`, un timeout nunca se interpreta como rechazo ni se repite el POST, `--wait`, flujos de
`SKILL.md`) se aplicó a las 18:34 solo tras comprobar que los hashes coincidían con la instantánea
revisada y que el `dry-run` era limpio. Resultado: 56 pruebas de la skill en verde; los 16 avisos
E501 que quedan son anteriores al parche.

## Riesgos residuales aceptados

- El barrido periódico reconduce dentro de su propia tarea y el barrido manual sigue siendo
  síncrono (I0.2).
- Una ejecución nunca iniciada cuya primera llamada al modelo estaba en vuelo al morir el proceso se
  reconduce: esa llamada puede facturarse dos veces, acotado por `MAX_RUN_ATTEMPTS`.
- Si `create_run` escribe la fila y falla el checkpoint de entrada, el cliente recibe `503` y la fila
  queda para el barrido como `interrupted` (ventana preexistente, ahora documentada).
- Los fallos de nodo ya no producen `500`: la fila queda `failed` con `error.code` y se lee por `GET`.

## Estado operativo y pendientes

- API en el checkout de `main` (pid 8592, puerto 8011, sin `--reload`) sigue ejecutando el código
  anterior al merge; no se reinició nada. El nuevo comportamiento entra en vigor en el siguiente
  arranque autorizado. Publisher deshabilitado; base de datos en la migración 0011; sin cambios en
  Runner ni Proxmox.
- Archivos sin seguimiento en `main` ajenos a I0.1: `docs/audits/2026-10-31-desglose-tecnico-i0-i1.md`,
  `docs/audits/2026-10-31-plan-maestro-agentforge-1.0.md`, `runner/deploy/Containerfile.pulso-mx`,
  `runner/deploy/pulso-mx-requirements.txt`.
- Worktree `../multiagent-starter-i0-1` (rama ya integrada) con enlaces simbólicos `.env` y `.local`
  al checkout principal: sin trabajo pendiente; retirar cuando se autorice la limpieza.
- Siguiente incremento: I0.2 (barrido y reconducción fuera de la tarea del barrido), no iniciado.
