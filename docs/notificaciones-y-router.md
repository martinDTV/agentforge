# Notificaciones por correo y Smart Router

Dos capacidades para usar AgentForge a diario sin vigilar la terminal: cada etapa de una
ejecución llega por correo, y la plataforma decide si una tarea necesita Planner y
Architect antes de tocar código. Ninguna de las dos cambia el modelo de seguridad: las
dos aprobaciones humanas (`scope` y `publish`) siguen siendo obligatorias, los agentes no
reciben credenciales SMTP, de GitHub ni del Runner, y el modelo nunca concede permisos.

## Arranque en un comando

```bash
uv run alembic upgrade head                          # paso de despliegue (0009)
uv run python -m scripts.agentforge_dev up           # API + worker de notificaciones, en segundo plano
uv run python -m scripts.agentforge_dev status       # procesos, salud, migraciones, bandeja de salida
uv run python -m scripts.agentforge_dev logs -n 100
uv run python -m scripts.agentforge_dev down
```

`up` arranca `uvicorn` desacoplado de la terminal (`setsid`/`nohup`) con pid y log en
`.local/run/`; cerrar la terminal no detiene la API ni el worker. No instala servicios
permanentes: tras un reinicio del Mac hay que volver a ejecutar `up`. Requisitos: PostgreSQL
local, `.env` configurado y, para Developer/QA, el Runner alcanzable por Tailscale.

## Notificaciones

### Configuración

En el `.env` local (ignorado por Git, `chmod 0600 .env`):

```dotenv
AGENTFORGE_EMAIL_ENABLED=true
AGENTFORGE_SMTP_HOST=smtp.gmail.com
AGENTFORGE_SMTP_PORT=587
AGENTFORGE_SMTP_STARTTLS=true
AGENTFORGE_SMTP_USERNAME=notificacione.agentforge@gmail.com
AGENTFORGE_SMTP_APP_PASSWORD=<contraseña de aplicación de 16 caracteres>
AGENTFORGE_EMAIL_FROM=notificacione.agentforge@gmail.com
AGENTFORGE_EMAIL_TO=vazquezmartin1240@gmail.com
# PUBLIC_APP_URL=https://…   # solo si existe un destino https accesible; sin él, instrucciones de CLI
NOTIFICATIONS_LEVEL=priority   # o verbose
```

Cómo colocar la contraseña sin que pase por el chat ni por la terminal visible:

1. En la cuenta remitente de Google, Seguridad → Verificación en dos pasos → Contraseñas de
   aplicación → crea una para "AgentForge" (16 caracteres).
2. Guárdala con el script de entrada oculta, que reescribe solo esa línea del `.env`,
   quita los espacios de los grupos de cuatro letras y deja el archivo en 0600:

   ```bash
   uv run python -m scripts.set_smtp_password
   ```

   Alternativa sin script: `nano .env` o `read -s PW && printf 'AGENTFORGE_SMTP_APP_PASSWORD=%s\n' "$PW" >> .env && unset PW && chmod 0600 .env`.

3. Comprueba la configuración sin enviar nada: `uv run python -m scripts.email_smoke`.
4. El primer envío real es una decisión explícita: `uv run python -m scripts.email_smoke --send`.

`Settings` rechaza arrancar si falta usuario, contraseña, remitente o destinatario, o si
STARTTLS está desactivado. La contraseña es un `SecretStr`: no aparece en logs, en la
bandeja de salida, en la auditoría ni en los checkpoints.

### Prueba real de SMTP (2026-10-27)

Primer y único envío real autorizado, ejecutado desde el worktree de la rama con
`AGENTFORGE_EMAIL_ENABLED=true` solo como variable del proceso (el `.env` conserva `false`,
así que ningún worker tocó ejecuciones reales):

```text
uv run python -m scripts.email_smoke          # validación: password_set=true, sin envío
uv run python -m scripts.email_smoke --send   # 08:19:14 UTC
sent=true code=smtp_accepted
detail="250 2.0.0 OK  1793089154 46e09a7af769-850fb64ce2asm11312459a34.18 - gsmtp"
```

STARTTLS con certificado verificado (`ssl.create_default_context`), autenticación con la
contraseña de aplicación, DATA aceptado por Gmail (250). La salida solo contiene
`password_set: true`; el valor nunca se leyó ni se imprimió. El adaptador conserva la
respuesta a DATA como evidencia (`code="smtp_accepted"`) y redacta el secreto si un
servidor lo devolviera.

### Cómo funciona

Nivel (`NOTIFICATIONS_LEVEL`): `priority`, el predeterminado, envía solo lo que requiere a
una persona o cierra una ejecución (plan, alcance y publicación pendientes; completada; PR
creado; publicada; fallo; cancelación; caducidad). `verbose` añade la ruta elegida o escalada,
la planeación, Developer iniciado y completado, QA iniciado, correcciones y QA aprobado.


- Cada evento durable de `run_events` que merece aviso (creación, ruta elegida, plan,
  alcance pendiente, Developer iniciado y completado, QA iniciado, correcciones, QA
  aprobado, plan de publicación listo, PR creado, fin por completado, publicado, fallo,
  cancelación o caducidad) genera una fila en `notification_outbox` **en la misma
  transacción** que el evento. Un correo por evento y destinatario, con clave de
  deduplicación `(run, tipo, ronda, destinatario)`: repetir un evento o una transacción no
  duplica correos, y las llamadas al modelo no generan avisos.
- Un worker (dentro de la API o `scripts.notification_worker`) reclama las filas vencidas
  con `FOR UPDATE SKIP LOCKED`, envía por SMTP con STARTTLS verificado y registra el
  resultado: `sent`; `failed` con reintento (30 s, 2 min, 10 min, 30 min, 1 h); `dead`
  tras agotar intentos o ante un error no recuperable (credenciales rechazadas,
  destinatario rechazado). Una respuesta incierta del servidor (conexión perdida tras
  enviar los datos) se reintenta con el **mismo `Message-ID`**, así el cliente de correo
  puede reconocer el duplicado; todos los correos de una ejecución van en el mismo hilo.
- Un fallo del correo nunca afecta a la ejecución: el grafo no espera al envío y el worker
  nunca toca `runs`.
- Contenido: cada correo dice qué ocurrió, en qué estado queda y qué sigue (ver «Diseño de
  los correos»). Nunca secretos, código ni archivos del repositorio.

### Avisos de objetivo (Fase 8)

Los objetivos usan la misma bandeja (`notification_outbox.goal_id`) y los mismos niveles:
`priority` envía `goal_needs_human` (decisión o fusión pendientes, tarea fallida, PR cerrado,
presupuesto agotado), `goal_completed`, `goal_partial`, `goal_failed`, `goal_cancelled` y
`goal_expired`; `verbose` añade creación, planificación solicitada, tarea lista, iniciada,
publicada y dependencia fusionada. Cada objetivo tiene su propio hilo
(`<agentforge.goal.<id>.thread@…>`); los correos de sus ejecuciones hijas siguen llegando como
hasta ahora.

### Caducidades y último aviso (H2)

Cuando una decisión caduca, el correo nombra la etapa real (`alcance`, `publicación` o `plan`)
tomada del evento `run_expired` o del código `expired:<etapa>` de la ejecución. En los objetivos,
además del recordatorio a mitad de la ventana, llega un correo "Última llamada" a
`final_notice_seconds` del cierre (una hora por defecto, configurable por objetivo) cuando la
ventana es lo bastante larga para distinguirlo del primero. Cada aviso se envía una sola vez,
también tras reinicios, porque su ordinal en `goal_events` es único por tarea, motivo y etapa.
Un plan descartado por la compuerta determinista (H1) produce el correo de objetivo fallido con
el motivo "plan inválido según las restricciones de la plataforma".

### Diseño de los correos

Asunto: `[AgentForge] <qué ocurrió> — <proyecto>`, sin hashes ni identificadores largos
(`Acción requerida: aprobar alcance — sandbox-github`, `QA necesita correcciones (ronda 2) —
sandbox-github`, `PR #12 creado — sandbox-github`, `Plan rechazado — sandbox-github`,
`Objetivo cerrado: plan rechazado — sandbox-github`). Cuerpo HTML de 600 px con tablas y CSS
en línea, sin imágenes, scripts ni fuentes remotas, legible en Gmail y móvil; estado con
palabra y símbolo (pendiente ●, en curso ◐, completado ✓, rechazado o fallido ✕), resumen,
hechos clave, listas (tareas, archivos, pruebas, riesgos), bloque «Qué sigue» con los comandos
de la terminal, enlaces identificados y detalles técnicos al final. La alternativa `text/plain`
contiene toda la información del HTML.

Veracidad: el correo distingue tres fuentes y nunca mezcla ni inventa. Los hechos de la
plataforma (estado, etapa, archivos, comprobaciones, PR). El **motivo registrado** por la
persona que decidió (`reason`, opcional en `POST /runs/{id}/approve` y `--reason` en la CLI,
migración 0011); si no lo hay, el correo dice «Sin motivo registrado». El texto del modelo
(resúmenes del Planner, Scoper, Developer o QA) va marcado como «del modelo», escapado y nunca
convertido en enlace. Los códigos de error se explican con una frase neutral de una tabla fija;
un código desconocido se muestra tal cual. Un rechazo nombra su etapa (plan, alcance o
publicación), sus consecuencias y el trabajo que se conserva; el cierre de un objetivo por una
decisión humana se presenta como consecuencia de esa decisión.

Enlaces: el Pull Request enlaza a GitHub (solo `https://github.com/...`). Los enlaces a la
propia plataforma solo aparecen con `PUBLIC_APP_URL` configurado (https, sin credenciales, sin
loopback); sin él, el correo indica los comandos de `scripts.agentforge`. Ningún enlace aprueba
nada ni lleva tokens. Previsualización local sin enviar nada:

```bash
uv run python -m scripts.notifications_preview               # .local/notifications-preview.html
uv run python -m scripts.notifications_preview --public-url https://agentforge.example
```

Un solo correo por resultado: la aprobación de QA en un repositorio con remoto no anuncia
«completada» (sigue la publicación) y `run_published` comparte clave con el correo del PR.

### Ver y recuperar notificaciones

```bash
uv run python -m scripts.notifications status                 # recuento por estado
uv run python -m scripts.notifications list --status failed --status dead
uv run python -m scripts.notifications requeue <id>           # vuelve a encolar una muerta
uv run python -m scripts.notifications deliver                # una pasada del worker ahora
uv run python -m scripts.notification_worker                  # worker independiente (bucle)
uv run python -m scripts.notifications_demo                   # demostración con FakeEmailSender
```

## Smart Router

Cada ejecución `development` pasa primero por el nodo `router`, que elige la ruta y la deja
registrada en `route_decisions` (ruta, modo pedido, motivo, versión de la política,
confianza, si se usó el clasificador y su coste) y en `runs.route` / `runs.route_mode`.

| Modo (`route_mode`) | Comportamiento |
| --- | --- |
| `auto` (por defecto) | Reglas deterministas; si ninguna obliga a `full`, una clasificación con el `LLMPort` (una llamada, descontada del presupuesto de la ejecución). `fast` solo con riesgo bajo, alcance de pocos archivos, sin ambigüedad y confianza ≥ `ROUTER_CONFIDENCE_THRESHOLD` (0,7). Clasificador caído o no configurado → `full` |
| `fast` | Scoper directo, **salvo** que una regla obligatoria exija `full` o la política del repositorio tenga `allow_fast_route: false` |
| `full` | Planner → Architect → aprobación humana del plan → Scoper → aprobación del alcance → Developer → QA → aprobación de publicación → Publisher |

Reglas obligatorias (`router-v1`): arquitectura o infraestructura, migraciones de datos o
esquema, autenticación/autorización/seguridad, cambios incompatibles en contratos públicos,
varios subsistemas o alto impacto, requisitos ambiguos (tarea muy corta, signos de duda).

Escalado: si el Scoper marca complejidad alta, pide planificación o la propuesta alcanza el
máximo de archivos, una ruta `fast` se escala a `full` **antes de cualquier escritura**; el
plan pasa por Planner, Architect y aprobación humana y después se aprueba el alcance ya
preparado (el Scoper no se repite).

```bash
curl -s -X POST http://127.0.0.1:8011/api/v1/runs -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"task": "Añade greet(name) en src/app.py y pruebas", "project_id": "sandbox-github",
       "workflow_kind": "development", "repository_id": "sandbox-github", "route_mode": "auto"}'
```

`GET /api/v1/runs/{id}` devuelve `route` y `route_mode`; `GET /api/v1/runs/{id}/decisions`
muestra la decisión pendiente (`plan`, `scope` o `publish`). La publicación sigue siendo
manual y nunca despliega: un PR en borrador es el final del flujo.

## Limitaciones

- La API de Gmail limita el volumen diario de una cuenta normal; la bandeja de salida
  absorbe ráfagas y reintenta, pero no hay cola externa ni varios remitentes.
- No hay notificaciones de despliegue: el Publisher termina en un PR en borrador. Un aviso
  de *deploy* futuro sería un evento más del mismo mecanismo, sin ejecutar despliegues.
- El clasificador es orientativo y en español/inglés; una tarea escrita de forma inusual
  puede ir a `full` sin necesidad (coste: una ronda de planificación), nunca a `fast` con
  una regla obligatoria.
