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

Valores predeterminados seguros: `GITHUB_WRITE_ENABLED=false` (nada sale del equipo),
`NOTIFICATIONS_LEVEL=priority`, `ROUTER_DEFAULT_MODE=auto`.

## 2. Crear una tarea

```bash
uv run python -m scripts.agentforge task \
  "Implementa validate_email(address) en src/mailcheck/validator.py con pruebas en tests/" \
  --project sandbox-github --repository sandbox-github --mode auto
```

Guarda `AGENTFORGE_PROJECT=…` y `AGENTFORGE_REPOSITORY=…` en el `.env` para omitir las
opciones. La identidad es `AGENTFORGE_SUBJECT` (por defecto tu usuario del sistema) con el rol
`project_operator`; el token lo emite el emisor local de desarrollo y nunca se imprime.

Modos de ruta:

| `--mode` | Qué hace |
| --- | --- |
| `auto` | Reglas obligatorias y, si no aplican, una clasificación del modelo. Tareas pequeñas van a `fast` |
| `fast` | Directo al Scoper, salvo regla obligatoria (migraciones, seguridad, arquitectura, varios subsistemas, ambigüedad) |
| `full` | Planner → Architect → aprobación del plan → Scoper → alcance → Developer → QA → publicación |

Equivalente HTTP: `POST /api/v1/runs` con `task`, `project_id`, `workflow_kind: development`,
`repository_id` y opcionalmente `route_mode`.

## 3. Aprobar el alcance

Recibirás "Alcance pendiente de aprobación". Revisa y decide:

```bash
uv run python -m scripts.agentforge pending <run_id>   # archivos, acciones, pruebas, digest
uv run python -m scripts.agentforge approve <run_id>   # pide los 8 primeros caracteres del digest
uv run python -m scripts.agentforge reject  <run_id>
```

La aprobación viaja con el digest exacto mostrado: si la propuesta cambia, la decisión no
aplica. En la ruta `full` la primera decisión es el plan (`stage: plan`), con el mismo mando.

## 4. Aprobar la publicación

Tras Developer y QA llega "Plan de publicación listo para aprobar". Publicar requiere que la
API haya arrancado con escritura habilitada, solo como variables del proceso:

```bash
GITHUB_WRITE_ENABLED=true PUBLISH_ALLOWED_REPOSITORIES='["sandbox-github"]' \
  uv run python -m scripts.agentforge_dev up
uv run python -m scripts.agentforge approve <run_id>   # etapa publish, digest de publicación
```

Resultado: una rama `agentforge/<run_id>` y un Pull Request en borrador; AgentForge nunca
fusiona ni toca `main`. Con `AUTHZ_SEPARATION_OF_DUTIES=true` quien aprueba no puede ser quien
creó la ejecución; en `ENVIRONMENT=local` la misma persona puede aprobar alcance y publicación.

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

## 6. Actualizar la base de datos (migración 0009)

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

## 7. Si algo falla

- `agentforge_dev status` muestra `alembic` distinto de `(head)`: aplica §6.
- Un correo no llega: `scripts.notifications list --status failed --status dead` y
  `scripts.notifications requeue <id>`; el fallo del correo nunca afecta a la ejecución.
- Una aprobación devuelve 409: la propuesta cambió o caducó; vuelve a `pending`.
- Publicación `interrupted`: `POST /api/v1/runs/<id>/resume` o espera al barrido; nunca se
  repite la aprobación.
