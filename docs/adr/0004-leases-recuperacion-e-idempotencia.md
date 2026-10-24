# 0004. Leases con fencing, recuperación automática e idempotencia de creación

- Status: accepted (Fase 3 cerrada el 2026-10-24; verificado con 197 pruebas unitarias y 55 de integración contra PostgreSQL 16, incluida la recuperación tras SIGKILL de un proceso real)
- Date: 2026-10-24
- Deciders: martin, Claude (Fase 3)

## Context

Tras el Paso 3 un run vive en dos sitios: el checkpoint de LangGraph (dónde va la ejecución) y la fila de `runs` (proyección, decisión humana, auditoría). Faltaba responder a tres preguntas que un sistema con procesos que mueren y peticiones que se repiten no puede evitar:

1. ¿Quién está ejecutando un run, y cómo sabe otro proceso que ya nadie lo hace?
2. ¿Qué pasa con un run cuyo proceso murió a mitad de camino, o con una decisión humana registrada que nunca se aplicó?
3. ¿Cómo evita un cliente crear dos ejecuciones al reintentar un `POST`?

Restricciones: un solo proceso en el despliegue inicial, pero ninguna garantía puede depender de la memoria de ese proceso; sin Redis ni workers; la skill `fastapi-architect` pide coordinación explícita en la base de datos y escrituras condicionales; las llamadas LLM duran más que cualquier intervalo razonable de heartbeat y corren en hilos del executor.

## Options considered

### Coordinación entre procesos

1. **Locks en memoria** (los `asyncio.Lock` por run del Paso 3): solo valen dentro de un proceso.
2. **`pg_advisory_lock` mientras dura la ejecución**: la sesión quedaría ocupada durante llamadas LLM de minutos, contra la regla de no mantener transacciones ni conexiones durante el grafo.
3. **Lease con vencimiento, renovado por heartbeat, en la propia fila** (elegida): `lease_owner`, `lease_expires_at`, `attempt` y un token de fencing monotónico `lease_generation`. Adquirir es un `UPDATE` condicional sobre "lease libre o vencido"; renovar y liberar están condicionados a `(owner, generation)`; perder la renovación cancela la ejecución local.

### Cómo evita un propietario anterior sobrescribir estados nuevos

1. **Solo comparar `lease_owner`**: un mismo proceso que readquiere no se distingue de sí mismo, y dos instancias con el mismo nombre de host se confunden.
2. **`lease_generation` monotónico** (elegida): cada adquisición incrementa el entero; toda escritura del propietario lleva `WHERE lease_owner = :owner AND lease_generation = :gen`. Además, toda proyección, con o sin lease, exige `last_checkpoint_id <= :nuevo`: los ids de checkpoint de LangGraph son uuid6 ordenados en el tiempo, así que una escritura tardía con un checkpoint más viejo se rechaza aunque la transición de estado fuese legal.

### Qué significa "en ejecución"

1. **Conjunto en memoria del proceso** (Paso 2): incorrecto con dos instancias.
2. **Lease activo según el reloj de PostgreSQL** (elegida): `lease_active = lease_expires_at > now()` calculado en la consulta. Un run con lease activo lo está conduciendo *alguien*; sin lease y con trabajo pendiente está `interrupted`.

### Política de recuperación

1. **Marcar `failed` todo lo que estuviera en `running` al arrancar**: destruye trabajo recuperable y miente sobre la causa.
2. **Clasificar desde el checkpoint, con lease** (elegida). El barrido reclama en lote con `FOR UPDATE SKIP LOCKED` las filas en `running` o `resume_pending` con lease libre o vencido, y decide por run:

| Lo que encuentra | Qué hace | Resultado |
|---|---|---|
| Checkpoint terminado o detenido en aprobación, fila atrasada | Reproyecta y reconstruye los eventos faltantes desde el historial de checkpoints | `reprojected` |
| `resume_pending` con decisión sin `applied_at` | Reanuda si el checkpoint sigue interrumpido, proyecta, marca aplicada | `approval_applied` |
| Trabajo pendiente, nadie al volante, reanudación automática apagada | Proyecta `interrupted`, libera el lease, no toca `attempt` | `interrupted` |
| Trabajo pendiente, reanudación automática encendida, `attempt < max` | `attempt + 1`, evento `run_resumed`, `ainvoke(None)` desde el último checkpoint bajo el lease | `resumed` |
| Trabajo pendiente, `attempt >= max` | `failed` con código `abandoned` | `abandoned` |

   La reanudación automática está apagada por defecto porque cuesta llamadas LLM y porque un run `interrupted` es recuperable sin pérdida. `interrupted` solo entra en el lote cuando está encendida.

### Idempotencia de creación

1. **Comprobar si existe la clave y luego insertar**: dos peticiones simultáneas pasan la comprobación.
2. **`INSERT ... ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL DO NOTHING`** (elegida) sobre el índice único parcial existente. Si no inserta, se lee la fila existente y se compara la tarea: igual, se reutiliza (200 con `Idempotency-Replayed: true`, aunque el run siga en marcha o haya fallado); distinta, 409 `idempotency_key_reused`. Ámbito: global al despliegue, porque todavía no hay principal ni tenant; cuando haya autenticación el ámbito pasará a ser por principal. Las claves no caducan en esta fase.

## Decision

- Leases en la fila de `runs` con heartbeat cada tercio del TTL (`RUN_LEASE_TTL_SECONDS`, `RUN_HEARTBEAT_SECONDS`), token `lease_generation` (migración `0002`), escrituras del propietario cercadas por `(owner, generation)` y proyecciones cercadas por `last_checkpoint_id` monotónico. Perder una renovación, o no poder renovar durante todo el TTL, cancela la tarea que conduce el grafo y la petición responde 409 `run_lease_lost`.
- `RecoveryService` barre al arrancar y cada `RECOVERY_SWEEP_SECONDS` con lotes de `RECOVERY_BATCH_SIZE`, `RECOVERY_AUTO_RESUME=false` por defecto, `MAX_RUN_ATTEMPTS` como tope. Dos barridos concurrentes nunca comparten un run.
- `apply_pending_approval` adquiere el lease de un `resume_pending` libre o vencido y aplica la decisión; la misma decisión repetida es idempotente y la contraria se rechaza.
- Apagado ordenado: dejar de aceptar (`503 shutting_down`), parar el barrido, esperar `SHUTDOWN_GRACE_SECONDS`, cancelar lo que siga, liberar **solo** los leases de este propietario sin cambiar estados, cerrar pool del saver y engine. Nada se marca terminal por apagarse.
- Los `asyncio.Lock` por run se conservan únicamente para serializar llamadas dentro del proceso; no son una garantía de corrección.

## Zombis y escrituras tardías: qué protege el fencing y qué no

El fencing cubre la tabla de negocio: un propietario anterior no puede proyectar, renovar ni liberar. **No cubre los checkpoints de LangGraph**: la librería escribe en sus tablas sin conocer nuestro lease. Un proceso zombi cuyo hilo de LLM termina después de perder el lease podría hacer que su loop de LangGraph escribiese un checkpoint tardío en el mismo `thread_id`.

Medidas concretas:

1. **Cancelación inmediata**: en cuanto el heartbeat no renueva, se cancela la tarea que consume `astream`; LangGraph no ejecuta más supersteps ni escribe más checkpoints desde ese loop. El hilo del LLM en curso termina solo y su resultado se descarta sin checkpoint.
2. **Toma de control solo tras vencimiento**: el barrido no quita leases vivos; con heartbeat a un tercio del TTL hacen falta dos renovaciones fallidas seguidas antes de que otro pueda reclamar.
3. **Evidencia antes de reanudar**: cualquier reanudación o re-conducción se hace bajo un lease recién adquirido y la proyección final exige que ese lease siga vigente; si no, la petición termina en `run_lease_lost` y el checkpoint queda para quien sí lo tenga.
4. **Guarda monotónica de checkpoint**: aunque un zombi lograse escribir, su proyección nunca retrocede la fila.
5. **Reconstrucción de auditoría solo con evidencia**: los eventos recuperados salen de checkpoints persistidos, deduplicados por clave natural; nunca se inventan.

Riesgo residual documentado: una ventana de como mucho un intervalo de heartbeat más la duración de la llamada LLM en curso, durante la cual un zombi puede dejar un checkpoint huérfano en el hilo. Con una sola instancia la ventana no existe. Cuando haya varias instancias se evaluará un cercado a nivel de checkpoint (por ejemplo, `checkpoint_ns` por generación de lease o un saver envolvente que rechace escrituras con lease vencido). No se promete semántica de exactamente una vez para efectos externos; lo que se garantiza es idempotencia de las escrituras propias y verificabilidad de las decisiones.

## Limitaciones conocidas al cerrar la Fase 3

**Idempotencia**

- El ámbito de `Idempotency-Key` es global al despliegue: dos clientes distintos que usen la misma clave comparten el run. Pasará a ser por principal cuando exista autenticación (Fase 4).
- Las claves no caducan ni se limpian. La reutilización se decide comparando el texto exacto de la tarea; un cambio de un carácter es una solicitud distinta (409).
- La clave queda ligada al run aunque este haya fallado; repetirla no vuelve a ejecutar nada. La repetición devuelve el estado *actual*, que durante una petición concurrente puede ser `running`.
- Solo cubre `POST /runs`. La aprobación es idempotente por su propia regla (misma decisión 200, contraria 409), sin cabecera.

**Recuperación manual**

- No existe endpoint para reconducir un run `interrupted` ni para reintentar uno `failed`/`abandoned`. Hoy la única reanudación automática es `RECOVERY_AUTO_RESUME=true`; la API de control de Fase 4 añadirá la manual.
- `interrupted` solo se vuelve a clasificar cuando entra en el lote del barrido con reanudación automática activa; con ella apagada queda a la espera de intervención.
- No hay endpoint que exponga `run_events`; la auditoría se consulta en la base de datos.

**Concurrencia**

- Validado con una instancia de la aplicación y con varios procesos sobre la misma base en pruebas; no se ha operado todavía con varias instancias en producción.
- El fencing cubre las tablas de negocio, no los checkpoints de LangGraph (sección anterior). La ventana de un checkpoint tardío tras perder un lease es de como mucho un intervalo de heartbeat más la llamada LLM en curso.
- Un hilo de Python no se puede interrumpir: el apagado deja de aceptar, cancela y libera en segundos, pero la salida del proceso espera a que termine la llamada LLM en curso, acotada por `LLM_TIMEOUT_SECONDS` y sus reintentos. Mientras tanto el lease ya está liberado y nada se marca terminal.
- Durante una caída de PostgreSQL el heartbeat tolera fallos hasta agotar el TTL; si la base vuelve antes, el run continúa; si no, se cancela localmente y lo retoma la recuperación.
- El barrido corre dentro del proceso web. Con mucho volumen convendría un proceso dedicado (Fase 8).

## Consequences

- Reiniciar, matar o escalar el proceso deja de perder decisiones ni ejecuciones: todo lo que estaba a medias se clasifica desde el checkpoint en el siguiente barrido.
- Cada run en marcha cuesta una pequeña escritura por heartbeat; con lotes acotados y `SKIP LOCKED` el barrido es seguro en concurrencia.
- Un cliente puede reintentar `POST /runs` con `Idempotency-Key` sin crear duplicados, y reintentar una aprobación sin reanudar dos veces.
- Qué nos haría revisar esta decisión: varias instancias en producción (cercado de checkpoints), claves de idempotencia por principal con caducidad (Fase 4), o un volumen que pida un planificador de recuperación separado del proceso web (Fase 8).
