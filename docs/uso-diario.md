# AgentForge en el día a día

Objetivo: arrancar con un comando, asignar una tarea y recibir correos mientras los agentes
trabajan. Las dos decisiones humanas (alcance y publicación) siguen siendo obligatorias y se
toman desde la terminal o desde cualquier cliente HTTP.

## 1. Arrancar y parar

```bash
cd ~/Developer/personal/multiagent-starter
uv run python -m scripts.agentforge_dev up       # API + worker de notificaciones, en segundo plano
uv run python -m scripts.agentforge_dev status   # proceso, salud, migraciones, bandeja de correo
uv run python -m scripts.agentforge_dev logs -n 100
uv run python -m scripts.agentforge_dev down
```

`up` se niega si la base de datos no está en la última migración (ver §6) y no instala
servicios permanentes: tras reiniciar el Mac, vuelve a ejecutar `up`. Requisitos: PostgreSQL
local, `.env` (0600) y, para que Developer y QA ejecuten pruebas, el Runner alcanzable.

`status` resume todo lo que importa: `api.running`, `health` (`database`, `schema`,
`checkpoints` y `notifications`: `worker` cuando el worker de correo está activo, `disabled`
sin correo), `alembic` (`0010 (head)` esperado), `worker` y los recuentos de la bandeja de
salida por estado. `GET /health/ready` devuelve la misma información.

Valores predeterminados seguros: `GITHUB_WRITE_ENABLED=false` (nada sale del equipo),
`NOTIFICATIONS_LEVEL=priority`, `ROUTER_DEFAULT_MODE=auto`, sin `PUBLIC_APP_URL` (los correos
indican los comandos de la terminal en lugar de un enlace a la API local).

## 2. Crear una tarea

```bash
uv run python -m scripts.agentforge task \
  "Implementa validate_email(address) en src/mailcheck/validator.py con pruebas en tests/" \
  --project sandbox-github --repository sandbox-github --mode auto
```

Con `AGENTFORGE_PROJECT` y `AGENTFORGE_REPOSITORY` en el `.env` (hoy `sandbox-github`) basta
`uv run python -m scripts.agentforge task "…"`; `--mode` es opcional (`auto` por defecto). La identidad es `AGENTFORGE_SUBJECT` (por defecto tu usuario del sistema) con el rol
`project_operator`; el token lo emite el emisor local de desarrollo y nunca se imprime.

Modos de ruta:

| `--mode` | Qué hace |
| --- | --- |
| `auto` | Reglas obligatorias y, si no aplican, una clasificación del modelo. Tareas pequeñas van a `fast` |
| `fast` | Directo al Scoper, salvo regla obligatoria (migraciones, seguridad, arquitectura, varios subsistemas, ambigüedad) |
| `full` | Planner → Architect → aprobación del plan → Scoper → alcance → Developer → QA → publicación |

Equivalente HTTP: `POST /api/v1/runs` con `task`, `project_id`, `workflow_kind: development`,
`repository_id` y opcionalmente `route_mode`.

## 2b. Asignar un objetivo (Fase 8)

En lugar de crear cada tarea a mano, describe el objetivo y deja que el Supervisor lo
convierta en ejecuciones, una a la vez:

```bash
uv run python -m scripts.agentforge goal \
  "Añade validación de correo al registro de usuarios, con pruebas, y documenta el módulo" \
  --repository sandbox-github [--decomposition auto|single|plan] [--max-tasks 5] \
  [--max-usd 25] [--deadline-days 7]
uv run python -m scripts.agentforge goals [--status active|blocked|...]
uv run python -m scripts.agentforge goal-show <goal_id>     # tareas, ejecuciones hijas, qué espera
uv run python -m scripts.agentforge goal-events <goal_id>
uv run python -m scripts.agentforge goal-cancel <goal_id>
```

Qué ocurre: con `auto`, el Smart Router decide si el objetivo cabe en una sola ejecución
(`fast`) o necesita planificación; con `plan`, Planner y Architect proponen de 1 a 5 tareas
con dependencias y **tú apruebas el plan** (`approve <plan_run_id>`, etapa `plan`). Después el
Supervisor crea la primera tarea como una ejecución normal y tú apruebas su alcance y su
publicación como siempre. Una tarea que depende de otra solo empieza cuando **fusionas el
Pull Request** de la anterior: la plataforma lo comprueba en GitHub y en el espejo; un PR
abierto no basta. `goal-show` indica qué ejecución espera qué decisión y, si el objetivo está
`blocked`, por qué (`decision_pending`, `merge_pending`, `task_failed`, `pr_closed`, …).

Límites: una hija activa por objetivo, presupuesto agregado de llamadas y USD reservado antes
de crear cada hija, plazo de 7 días por defecto. Una hija fallida no se vuelve a crear sola:
revisa su código de error y decide. Repositorios sin remoto (sin PR) solo admiten
`--decomposition single`. El Supervisor nunca aprueba nada: no tiene `runs:approve`.

## 3. Aprobar el alcance

Recibirás "Alcance pendiente de aprobación". Revisa y decide:

```bash
uv run python -m scripts.agentforge pending <run_id>   # archivos, acciones, pruebas, digest
uv run python -m scripts.agentforge approve <run_id>   # pide los 8 primeros caracteres del digest
uv run python -m scripts.agentforge reject  <run_id> --reason "por qué"   # el motivo se registra y se envía por correo
```

La aprobación viaja con el digest exacto mostrado: si la propuesta cambia, la decisión no
aplica. En la ruta `full` la primera decisión es el plan (`stage: plan`), con el mismo mando.

## 4. Aprobar la publicación

**La escritura en GitHub se habilita antes de crear la tarea o el objetivo, no al aprobar la
publicación.** Una ejecución sobre un repositorio con remoto solo tiene un resultado posible, el
Pull Request, así que la API rechaza crearla (`409 publish_unavailable`) si esta instancia no puede
publicar en ese repositorio. Habilita la publicación solo como variables del proceso:

```bash
GITHUB_WRITE_ENABLED=true PUBLISH_ALLOWED_REPOSITORIES='["sandbox-github"]' \
  uv run python -m scripts.agentforge_dev up
```

Con la escritura habilitada los tokens de instalación se crean únicamente en el momento de publicar
y se revocan al terminar; tu aprobación `publish` sigue siendo obligatoria. Tras Developer y QA
llega "Plan de publicación listo para aprobar":

```bash
uv run python -m scripts.agentforge approve <run_id>   # etapa publish, digest de publicación
```

Resultado: una rama `agentforge/<run_id>` y un Pull Request en borrador; AgentForge nunca
fusiona ni toca `main`. Con `AUTHZ_SEPARATION_OF_DUTIES=true` quien aprueba no puede ser quien
creó la ejecución; en `ENVIRONMENT=local` la misma persona puede aprobar alcance y publicación.

Si la publicación deja de estar disponible mientras una ejecución ya está en marcha (reinicio sin
la bandera, repositorio retirado de la lista), la ejecución **no termina** `completed`: queda
`interrupted` con `publication.state = unavailable`, conserva su worktree durante la retención de
interrumpidas (7 días por defecto) y el barrido de recuperación no la toca. Habilita la publicación,
arranca de nuevo y reanúdala con `POST /api/v1/runs/<run_id>/resume` (requiere `repos:publish`):
preparará el plan de publicación y volverá a pedirte la decisión `publish`. En un objetivo, la tarea
aparece como `waiting_human` en etapa `publish` con el motivo `publication_unavailable`.

## 5. Consultar resultados

```bash
uv run python -m scripts.agentforge runs --status waiting_approval
uv run python -m scripts.agentforge show <run_id>      # estado, ruta, PR
uv run python -m scripts.agentforge events <run_id>    # auditoría de eventos
uv run python -m scripts.notifications status          # bandeja de correo por estado
```

Correos con `NOTIFICATIONS_LEVEL=priority` (predeterminado): plan, alcance y publicación
pendientes; ejecución completada; Pull Request creado; publicada; fallo; cancelación;
caducidad. Con `verbose` se añaden la ruta elegida, la planeación, Developer y QA paso a paso.
Todos los correos de una ejecución van en el mismo hilo.

## 6. Actualizar la base de datos (migraciones 0009 y 0010)

La migración 0010 (Fase 8) añade `goals`, `goal_tasks` y `goal_events`, y permite que una fila
de `notification_outbox` pertenezca a un objetivo en lugar de a una ejecución; no modifica
filas existentes. Se aplica con el mismo procedimiento que la 0009 (respaldo, API parada,
`uv run alembic upgrade head`, `alembic current` debe decir `0010 (head)`). El downgrade a 0009
elimina las tres tablas y se niega mientras exista alguna notificación de objetivo.


La migración 0009 añade `notification_outbox`, `route_decisions` y las columnas `route` y
`route_mode` de `runs`; no modifica ni borra filas existentes. Plan, con la API parada:

```bash
uv run python -m scripts.agentforge_dev down
pg_dump -Fc --no-owner --no-privileges -f ~/.local/share/agentforge-backups/agentforge-$(date -u +%Y%m%dT%H%M%SZ).dump "$DATABASE_URL_SIN_PREFIJO"
uv run alembic current                      # 0008
uv run alembic upgrade head                 # 0009
uv run alembic current                      # 0009 (head)
uv run python -m scripts.agentforge_dev up
```

Reversión: `uv run alembic downgrade 0008` elimina solo lo añadido por 0009; las ejecuciones,
decisiones, eventos y publicaciones no se tocan. El respaldo del 2026-10-27 está en
`~/.local/share/agentforge-backups/` con su manifiesto de recuentos.

## 7. Registros y auditoría

- `uv run python -m scripts.agentforge_dev logs -n 200`: log de la API (uvicorn); nunca
  contiene tokens ni la contraseña de correo.
- `uv run python -m scripts.agentforge events <run_id>`: eventos durables de la ejecución
  (ruta elegida, propuesta, aprobaciones, Developer, QA, publicación), con su ordinal.
- `uv run python -m scripts.notifications list --status sent`: correos entregados, con
  `Message-ID`; `--status failed --status dead` para los que requieren atención.
- En PostgreSQL: `runs` (estado, `route`, `route_mode`, `publication`), `run_decisions`,
  `route_decisions` (motivo, versión de política, confianza), `publications`,
  `notification_outbox`.

## 8. Si algo falla

- `agentforge_dev status` muestra `alembic` distinto de `(head)`: aplica §6.
- Un correo no llega: `scripts.notifications list --status failed --status dead` y
  `scripts.notifications requeue <id>`; el fallo del correo nunca afecta a la ejecución.
- Una aprobación devuelve 409: la propuesta cambió o caducó; vuelve a `pending`.
- Publicación `interrupted`: `POST /api/v1/runs/<id>/resume` o espera al barrido; nunca se
  repite la aprobación.
