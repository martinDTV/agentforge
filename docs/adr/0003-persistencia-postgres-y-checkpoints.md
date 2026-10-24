# 0003. Persistencia en PostgreSQL: tablas de negocio con SQLAlchemy/Alembic y checkpoints de LangGraph

- Status: accepted (Fase 3 cerrada el 2026-10-24; la coordinación entre procesos, la recuperación y la idempotencia están en el ADR 0004)
- Date: 2026-10-24
- Deciders: martin, Claude (Fase 3)

## Context

Hasta Fase 2 todo el estado vive en memoria: el `InMemorySaver` de LangGraph guarda los checkpoints y la API deriva el estado del run leyendo ese checkpoint. Reiniciar el proceso pierde todas las ejecuciones, no hay auditoría, no hay idempotencia y una aprobación humana no sobrevive a una caída.

Fase 3 introduce PostgreSQL con dos responsabilidades distintas que conviene no mezclar:

- **A. Estado de ejecución del grafo** (checkpoints): lo necesita LangGraph para reanudar exactamente donde se quedó. Lo escribe `AsyncPostgresSaver` en sus propias tablas (`checkpoints`, `checkpoint_blobs`, `checkpoint_writes`, `checkpoint_migrations`) con un formato que es de la librería.
- **B. Estado de negocio**: lo que la API y la auditoría necesitan consultar sin abrir un checkpoint (estado, revisión, llamadas, errores), lo que el grafo no puede saber (quién aprobó y cuándo, qué proceso está ejecutando) y la traza de eventos.

Restricciones: skill `fastapi-architect` (SQLAlchemy 2.0 tipado, Alembic, convención de nombres, `pool_pre_ping`, secretos como `SecretStr`, readiness separado de liveness, migraciones fuera del arranque); tests sin Vertex AI; sin Docker en la máquina de desarrollo principal; un solo proceso en el despliegue inicial pero sin diseñar nada que impida varios.

## Options considered

### Driver de base de datos

1. **asyncpg para SQLAlchemy + psycopg para el saver**: es el default de la skill para SQLAlchemy asíncrono, pero `langgraph-checkpoint-postgres` exige psycopg 3. Dos drivers, dos configuraciones TLS, dos modos de fallo.
2. **psycopg 3 para ambos** (elegida): SQLAlchemy soporta `postgresql+psycopg` en modo asíncrono desde 2.0. Un solo driver, un solo DSN (`DATABASE_URL`, del que se deriva la forma libpq para el saver), una sola política de timeouts.

### Dónde se crean las tablas de checkpoints

1. **Envolver el DDL del saver en una migración de Alembic**: una sola herramienta, pero copia internals de la librería que cambian con cada versión.
2. **`setup()` del saver como paso de despliegue separado, junto a `alembic upgrade head`** (elegida): cada dueño versiona lo suyo. La app verifica al arrancar que ambos existen y no crea nada.

### Transacciones y ejecución del grafo

1. **Una sesión por petición con un solo commit al final** (patrón de la skill): obligaría a mantener una transacción abierta durante las llamadas al LLM, que duran segundos o minutos, bloqueando filas y agotando el pool.
2. **Unidades de trabajo cortas antes y después de ejecutar el grafo** (elegida): el repositorio gestiona transacciones breves por operación. Es una desviación consciente de la skill, justificada por la naturaleza del trabajo que hay entre ambas escrituras.

### Política de arranque frente a la base de datos

1. **Fallar siempre si la base no responde**: hace que una caída transitoria de PostgreSQL reinicie en bucle la aplicación.
2. **Conectividad blanda, esquema estricto** (elegida): si la base no responde al arrancar, la app arranca degradada y `/health/ready` devuelve 503 hasta que vuelva. Si la base responde pero su revisión de Alembic no es la cabeza del código, el arranque aborta: es un error de despliegue, no una condición transitoria.

### Pruebas de integración sin Docker

1. **testcontainers** (recomendado por la skill): requiere Docker, que no está disponible en la máquina principal de desarrollo.
2. **Servidor PostgreSQL real configurable, bases temporales por sesión** (elegida): `tests/integration` crea bases `agentforge_test_<hex>` en el servidor de `TEST_DATABASE_ADMIN_URL` (Homebrew local por defecto, contenedor de servicio en CI) y las borra al final. Si no hay servidor, la suite falla con mensaje explícito: nunca se salta. La suite unitaria (`tests/unit`, objetivo por defecto de `pytest`) no necesita ningún servicio.

## Decision

- PostgreSQL 16 con psycopg 3 para SQLAlchemy asíncrono y para el saver. `DATABASE_URL` es `SecretStr`, obligatoria, normalizada a `postgresql+psycopg://`; cualquier otro driver se rechaza al cargar la configuración.
- Modelos SQLAlchemy 2.0 con `Mapped`, `MetaData` con convención de nombres, timestamps con zona horaria y default de servidor, enums como `StrEnum` almacenados como texto (`values_callable`), con `CHECK` solo para el conjunto estable de estados del run. Claves primarias: UUID en `runs` porque es también el `thread_id` de LangGraph; `Identity` entera en el resto.
- Tablas de negocio: `runs` (proyección del estado, lease de ejecución, idempotencia), `run_approvals` (decisión humana durable con único por run y `applied_at`), `run_events` (auditoría append-only con clave natural única). Detalle y motivación de cada restricción en los docstrings de `app/infrastructure/db/models.py`.
- Alembic con plantilla asíncrona en `migrations/`, URL leída de Settings o inyectada por tests, `compare_type` activo, nombres de archivo `<rev>_<slug>`. Cada cambio de esquema es una migración revisada a mano; `Base.metadata.create_all()` no se usa fuera de pruebas.
- El engine se crea en `lifespan`, vive en `app.state.database`, se libera al apagar. `pool_pre_ping=True`, `connect_timeout` y `statement_timeout` configurables, `application_name` fijo para diagnóstico, `sslmode=require` fuera de `local`/`test`. Dos pools convivirán en Fase 3 (SQLAlchemy y el del saver); su suma por proceso debe quedar bajo el límite de conexiones del servidor y ambos tamaños son configurables.
- `/health/live` no toca la base. `/health/ready` hace `SELECT 1` y verifica una vez la revisión de Alembic; ninguno está versionado ni en el esquema OpenAPI. `/docs` y `/openapi.json` se deshabilitan en `staging` y `production`.
- Ni `alembic upgrade head` ni `setup()` del saver se ejecutan al arrancar la aplicación.

### Checkpointer (Paso 2)

- `AsyncPostgresSaver` sobre un `AsyncConnectionPool` de psycopg propio (`autocommit`, `prepare_threshold=0`, `dict_row`, como exige la librería), encapsulado en `PostgresCheckpointer` (`app/infrastructure/checkpoints/`). La orquestación solo conoce `BaseCheckpointSaver`: `build_graph(llm, limits, checkpointer)` compila contra lo que le inyecten y usa `InMemorySaver` por defecto, de modo que la suite unitaria no toca PostgreSQL.
- El grafo se conduce con `ainvoke`/`aget_state`. Los nodos y el puerto LLM siguen síncronos; LangGraph los ejecuta en su executor, verificado: un nodo bloqueante no detiene el event loop. Los endpoints pasan a `async def`.
- `thread_id == run_id` por construcción, en un único sitio (`RunService._config`).
- Serializador: `JsonPlusSerializer(pickle_fallback=False, allowed_msgpack_modules=None)`, es decir, solo la allowlist interna de tipos seguros de LangGraph (incluye `Interrupt` y `Command`, que la librería guarda en `checkpoint_writes`). Un tipo no serializable falla al escribir en el proceso que lo produce. Al leer, un tipo fuera de la allowlist no se instancia: LangGraph lo bloquea, lo registra en el log y devuelve su contenido como datos crudos (un `dict`), sin ejecutar ningún constructor. Por eso las pruebas de ida y vuelta de todos los estados del grafo son obligatorias: garantizan que ningún dato propio cae en ese caso. Cifrado opcional AES-EAX con `EncryptedSerializer`, clave en `SecretStr` validada a 16/24/32 bytes y pasada por parámetro, nunca vía variable de entorno de la librería.
- Ciclo de vida: el pool se abre en `lifespan` sin esperar (`open(wait=False)`); si la base está caída la app arranca degradada y readiness responde 503. En el apagado, `RunService.wait_until_idle()` espera hasta `SHUTDOWN_GRACE_SECONDS` a las ejecuciones en curso antes de cerrar el pool del saver y después el engine de SQLAlchemy. Los dos pools son independientes y configurables (`CHECKPOINT_POOL_SIZE`, `DATABASE_POOL_SIZE`).
- Verificación, no creación: al arrancar y en readiness se comprueba que existen las cuatro tablas y que `max(v)` de `checkpoint_migrations` coincide con la última migración de la librería instalada. `python -m scripts.setup_checkpoints` es el paso de despliegue que las crea o actualiza, idempotente, con `--check` para solo verificar.
- Estados derivados del checkpoint, en orden y solo con evidencia positiva: error registrado o tarea fallida → `failed`; `approved` escrito por el nodo de aprobación → `approved`/`rejected`; interrupción pendiente → `waiting_approval`; cualquier otra cosa → `running` si hay un lease vigente, `interrupted` si no. "Nada pendiente" no es evidencia de fin: entre dos supersteps LangGraph expone brevemente un snapshot con todas las tareas hechas y sin checkpoint siguiente, y un lector concurrente debe verlo como trabajo en curso (defecto encontrado y corregido en el Paso 4). `interrupted` es recuperable con `ainvoke(None, config)` desde el último checkpoint sin repetir nodos ya completados; quién lo hace, cuándo y bajo qué lease está en el ADR 0004.

### Persistencia de negocio y auditoría (Paso 3)

- **Puerto y adaptadores**: `RunRepository` es un `Protocol` en `app/orchestration/ports.py` con registros tipados (`PersistedRun`, `RunProjection`, `RunEvent`, `ApprovalDecision`, `ApprovalClaim`). `SqlAlchemyRunRepository` lo implementa sobre PostgreSQL y `InMemoryRunRepository` sobre diccionarios con las mismas reglas, de modo que la suite unitaria ejerce la lógica real sin base de datos. Cada método del adaptador es exactamente una transacción corta.
- **Una sola definición de estados**: `RunLifecycleStatus` en `app/contracts/lifecycle.py` es a la vez el valor de `runs.status`, el `status` de `RunResponse` y la vara de medir de la orquestación. `ALLOWED_TRANSITIONS` define las transiciones legales; `sources_for(nuevo)` alimenta la cláusula `WHERE status IN (...)` de la actualización condicional.
- **Fila antes que grafo**: `create` inserta la fila en `running` (con `attempt=1` y el evento `run_created`) y solo después conduce el grafo. Una caída posterior deja una fila que reconciliar; nunca un checkpoint sin dueño.
- **Proyección sin transacción compartida**: el checkpoint lo escribe LangGraph y la fila la escribimos nosotros, en transacciones distintas. La proyección se deriva del snapshot (`build_projection`), se compara con la fila (`projection_differs`, incluido `last_checkpoint_id`) y se escribe condicionalmente. Las filas terminales no se tocan; `resume_pending` no retrocede a `waiting_approval`. Una proyección rechazada no es un error: se relee la fila, que ya refleja una escritura más reciente.
- **Reconciliación en lectura**: `get` de un run no terminal compara la fila con el checkpoint y reproyecta si hay deriva. Cubre la caída entre checkpoint y proyección para los runs que alguien lee; los que nadie lee los repara el barrido del ADR 0004.
- **Auditoría derivada del stream**: el servicio consume `astream(stream_mode="updates")` y convierte cada actualización de nodo en eventos acotados (`app/orchestration/events.py`); los agentes no conocen la auditoría. La clave única `(run_id, kind, ordinal)` con `ON CONFLICT DO NOTHING` hace idempotentes los reintentos. Si la base no está disponible, la auditoría se registra en el log y la ejecución continúa: el checkpoint es la verdad y su siguiente escritura fallará de forma visible si la base sigue caída.
- **Decisión humana durable**: `claim_approval` ejecuta en una transacción la transición condicional `waiting_approval → resume_pending`, la inserción única en `run_approvals` y el evento `approval_recorded`. Solo tras confirmar se envía `Command(resume=...)`; `applied_at` se marca cuando la proyección muestra `approved` o `rejected`. La misma decisión repetida es una repetición idempotente (200); la contraria, 409. `apply_pending_approval` reaplica una decisión registrada y no aplicada, también si la caída fue después de reanudar pero antes de proyectar; el ADR 0004 define bajo qué lease y desde qué barrido se invoca.
- **Errores de base de datos**: `as_unavailable` traduce solo fallos de conexión (`OperationalError`, `InterfaceError`, agotamiento del pool, errores de psycopg y `PoolTimeout` del checkpointer) a `DatabaseUnavailableError`, que la API devuelve como 503 Problem Details con código `database_unavailable` y `Retry-After`. `ProgrammingError`, `DataError` e `IntegrityError` siguen siendo 500: son bugs, no caídas. Ningún mensaje incluye DSN ni credenciales.

## Seguridad (restricciones que el resto de la fase debe respetar)

- **Deserialización de checkpoints**: el saver se construirá con `JsonPlusSerializer` sin fallback a pickle y con el modo estricto de msgpack. El estado del grafo debe seguir siendo JSON nativo (dicts, listas, str, int, bool, None); una prueba de ida y vuelta lo garantiza. Si alguien puede escribir en las tablas de checkpoints, puede influir en lo que el proceso deserializa: por eso la base no se expone y el rol de la app es el único con permisos sobre ellas.
- **Cifrado en reposo**: opcional mediante `EncryptedSerializer` AES de LangGraph, clave en `SecretStr`, apagado por defecto. Con cifrado activo los blobs dejan de ser inspeccionables en la base.
- **Credenciales**: nunca en el código ni en logs; `DATABASE_URL` sale del gestor de secretos en despliegue; `display_url` oculta la contraseña; los mensajes de error de conexión se recortan y no incluyen el DSN completo.
- **Exposición**: compose liga el puerto a `127.0.0.1`; en despliegue la base vive en red privada con TLS obligatorio. Rol de base de datos dedicado, sin superusuario.
- **Ciclo de vida de conexiones**: una conexión por operación corta, devuelta al pool; nunca una transacción abierta durante una llamada al LLM; `pg_terminate_backend` de un operador se tolera gracias a `pool_pre_ping`.

## Consequences

- Reiniciar la aplicación no pierde runs: el checkpoint reanuda la ejecución y la fila de negocio se reconcilia desde él.
- Cada operador necesita ahora un PostgreSQL alcanzable y dos comandos de despliegue antes de arrancar: `uv run alembic upgrade head` y el `setup()` del saver.
- Añadir una columna o un estado exige una migración; la convención de nombres garantiza que se pueda deshacer.
- Qué nos haría revisar esta decisión: varias réplicas de la aplicación (presupuesto de conexiones, PgBouncer en modo transacción, cercado de checkpoints), o que `langgraph-checkpoint-postgres` deje de exigir psycopg.
