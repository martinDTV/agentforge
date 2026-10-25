# 0005. Identidad delegada (OIDC), principal explícito y autorización por política

- Status: accepted (Fase 4 cerrada el 2026-10-24, versión 0.4.0; verificado con las suites unitaria e integración contra PostgreSQL 16)
- Date: 2026-10-24
- Deciders: martin, Claude (Fase 4)

## Context

Hasta la versión 0.3.0 cualquier cliente con acceso de red podía crear, leer y aprobar ejecuciones. La Fase 4 añade identidad y autorización sin convertir a AgentForge en un proveedor de identidad, y preparando el terreno para que los futuros agentes Developer, QA y Security actúen como principales de servicio con permisos acotados.

Restricciones: skill `fastapi-architect` (delegar la identidad a un proveedor OIDC, verificar tokens con algoritmos fijados y `aud`/`iss`/`exp` obligatorios, dependencias tipadas, comprobaciones finas en el servicio, 404 en lugar de 403 cuando revelar existencia es una fuga); nada en producción puede depender de claves locales; los tokens no deben aparecer en logs ni en respuestas.

## Options considered

### Quién emite credenciales

1. **Claves de API propias** guardadas hasheadas en PostgreSQL: autocontenido, pero obliga a implementar rotación, revocación y alta de usuarios, sin SSO.
2. **Proveedor OIDC externo; la API solo verifica** (elegida): identidad, MFA, rotación de claves y cuentas de servicio los resuelve el proveedor. En local y pruebas, un JWKS estático generado al vuelo, prohibido en `staging` y `production` por validación de Settings.

### Dónde vive la lógica de seguridad

1. **Dentro de los routers**: dispersa y difícil de probar.
2. **Paquete `app/security/` sin FastAPI** (elegida): `Principal`, verificación de tokens y, en el Paso 2, `AuthorizationPolicy`. `app/api/security.py` solo adapta a dependencias de FastAPI (`CurrentPrincipalDep`, y en el Paso 2 `require_permission`). Los servicios reciben el principal como argumento, nunca lo leen de un global.

### Cómo se comunica el fallo de autenticación

1. `HTTPException(401)` en la dependencia, como en la skill.
2. **`AuthenticationError` de dominio mapeada a Problem Details 401** (elegida) con `WWW-Authenticate: Bearer` y `error="invalid_token"` cuando había token: coherente con el resto de errores de la API, y con una razón cerrada (`missing_token`, `expired_token`, `invalid_token`) que nunca repite el token ni el mensaje de la librería.

## Decision (Paso 1)

- `AUTH_MODE=oidc` por defecto. Requiere `OIDC_ISSUER`, `OIDC_AUDIENCE` y exactamente una fuente de claves: `OIDC_JWKS_URL` o `OIDC_JWKS_FILE`. `AUTH_MODE=disabled` y `OIDC_JWKS_FILE` están prohibidos en `staging` y `production`; con `disabled` toda petición actúa como el administrador local `local-dev`.
- Verificación con PyJWT: solo algoritmos asimétricos (`RS*`, `ES*`, `PS*`; por defecto RS256 y ES256), `exp`, `iat`, `sub`, `iss` y `aud` obligatorios, tolerancia de reloj acotada a 300 s. Claves del JWKS del proveedor con caché, o de un archivo que se relee al cambiar.
- `Principal` inmutable: sujeto opaco, tipo `user` o `service` según una reclamación configurable, emisor, roles y proyectos leídos de reclamaciones configurables con rutas punteadas, expiración y `jti`. Sin correo ni nombre: lo que se atribuye en auditoría y logs es el sujeto.
- Toda ruta bajo `/api/v1` exige un principal autenticado mediante una dependencia a nivel de router; `/health/*` queda abierto. La resolución de claves puede bloquear, así que la dependencia es un `def` plano y FastAPI la ejecuta en el threadpool.
- Herramientas locales: `scripts/dev_keys.py` genera el par de claves en `.local/` (ignorado por git) y `scripts/dev_token.py` firma tokens de corta duración para `curl`.

## Decision (Paso 2): autorización, propiedad y aislamiento

- **`AuthorizationPolicy` en `app/security/policy.py`**, sin FastAPI ni SQL. Tres cosas separadas: permisos nominales (lo único que el código comprueba), roles del token traducidos por una tabla, y ámbito dado por los proyectos del token más la propiedad del run. Denegado por defecto: un rol desconocido o ausente no concede nada; nada supone que un autenticado sea administrador.
- **Decisiones de tres valores**: `allowed`, `hidden` (el recurso no debe revelarse: la API responde 404) y `forbidden` (visible pero no permitido: 403). El servicio evalúa la decisión **antes** de cualquier escritura; los routers solo pasan el principal.
- **Ámbito administrativo acotado**: `admin` tiene todos los permisos de runs pero solo dentro de los proyectos de su token; cruzar proyectos exige `projects:all`, concedido únicamente por `platform_admin`, que además debe indicar proyecto al crear.
- **Propiedad persistida** (migración 0003): `runs.owner_subject` y `runs.project_id` obligatorios con índices por proyecto y por propietario; `run_approvals.decided_by`; `run_events.actor_subject`, nulo para eventos de agentes y de sistema. Las filas anteriores a la autenticación reciben los centinelas `legacy:unowned` / `legacy`; `legacy` es un proyecto reservado que ningún token puede reclamar, de modo que solo `projects:all` alcanza esas filas y nunca se asignan a usuarios nuevos. El downgrade conserva una fila por clave de idempotencia antes de restaurar el índice global.
- **Aprobación humana vs. veredicto del agente vs. permiso**: `architect_approved` es la opinión del Architect; `run_approvals` con `decided_by` es la decisión humana durable; `runs:approve` es el permiso. **Separación de funciones**: el creador no puede aprobar su run cuando `separation_of_duties` está activo, por defecto en `staging`/`production` y desactivado en `local`/`test` (`AUTHZ_SEPARATION_OF_DUTIES` lo fuerza).
- **Idempotencia por proyecto y principal**: el índice único pasa a `(project_id, owner_subject, idempotency_key)`; la misma clave de otro usuario u otro proyecto es otro run y jamás devuelve el ajeno.
- **Sin privilegios implícitos**: la única identidad sin token es `local-dev` de `AUTH_MODE=disabled`, que Settings rechaza fuera de `local`/`test`. En producción la política se construye desde Settings.

## Decision (Paso 3): API de control

- **Listado con ámbito en la consulta**: `AuthorizationPolicy.listing_scope` traduce la regla de visibilidad a un filtro (`AccessScope`: proyectos o todos, y propietario único cuando falta `runs:read`) que el repositorio aplica en el `WHERE` de cada página. Un filtro `project_id` del cliente solo puede estrechar ese ámbito, nunca ampliarlo.
- **Paginación keyset** por `(created_at, id)` descendente para runs y por `id` ascendente para eventos. Los cursores son opacos, llevan versión, tipo y una huella de los filtros y del sujeto, y se validan estrictamente; no son capacidades: el ámbito se reaplica siempre, así que un cursor forjado solo mueve la ventana dentro de lo que el llamante ya ve. Se descartó firmarlos con un secreto porque no protegen nada que el `WHERE` no proteja ya.
- **Auditoría** (`runs:audit:read`): visible primero (404), permiso después (403). Expone `actor` por evento, nulo para agentes y sistema.
- **Reanudación manual** (`runs:resume`): misma maquinaria que el barrido, bajo el mismo lease y con el mismo tope `MAX_RUN_ATTEMPTS`; solo para `interrupted`. Síncrona, como la creación. Dos reanudaciones concurrentes: una gana el lease, la otra recibe `409 run_not_resumable`.
- **Barrido administrativo síncrono** (`recovery:sweep`): se ejecuta dentro de la petición y devuelve el informe real; se descartó `202` porque una tarea en memoria no ofrece garantía durable. Alcance limitado a los proyectos del principal salvo `projects:all`. Los eventos que produce quedan atribuidos al principal que lo disparó.

## Decision (cierre): endurecimiento

- **Cabeceras de seguridad** en todas las respuestas (`X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, `Permissions-Policy` restrictiva) y `Cache-Control: no-store` en `/api/*`; `Strict-Transport-Security` solo en `staging`/`production`, donde se asume TLS. Sin CSP para no romper la documentación interactiva, que de todos modos está apagada en producción.
- **Rate limiting en el gateway**, no en la aplicación: un límite por proceso no protege con varias instancias y la skill lo sitúa en el ingress. Mientras no haya gateway, un token válido puede saturar llamadas al LLM; el presupuesto por ejecución (`MAX_LLM_CALLS_PER_RUN`) acota el daño por run, no por cliente.
- **Barridos sin solapamiento por instancia**: `RecoveryService` serializa los barridos con un lock de proceso; el manual falla rápido con `409 sweep_in_progress` si ya hay uno, y el periódico salta el tick. Entre instancias siguen protegiendo `FOR UPDATE SKIP LOCKED` y los leases. Un cliente que corte la petición no cancela el barrido: el trabajo reclamado se completa y queda persistido.

## Limitaciones conocidas al cerrar la Fase 4

**Autorización**

- El ámbito nace de la reclamación `projects` del token. Si el proveedor no la emite, nadie crea sin `platform_admin`; cada proveedor exige configurar `OIDC_PROJECTS_CLAIM` y probar el mapeo.
- **La propiedad no sobrevive a la revocación del proyecto**: un propietario que pierde la pertenencia deja de ver sus propias ejecuciones (404), incluso para aprobarlas. Es deliberado, y hay prueba de ello; recuperar acceso pasa por el proyecto o por `projects:all`.
- La visibilidad por propiedad compara el sujeto exacto; un cambio de proveedor que cambie los sujetos deja ejecuciones alcanzables solo con `projects:all`.
- El control de acceso vive en la API y los servicios: quien acceda directamente a la base lee todo. Sin seguridad a nivel de fila.
- Los cursores son posiciones opacas y decodificables, ligados al sujeto y a los filtros; no son secretos ni capacidades. Un cambio de permisos entre páginas se aplica en la página siguiente (hay prueba).
- `AUTH_MODE=disabled` da a `local-dev` ámbito global; imposible en producción por Settings, pero relevante en entornos locales compartidos.

**Concurrencia y operación**

- El barrido manual es síncrono y acotado por `RECOVERY_BATCH_SIZE`; con reanudación automática activa incluye llamadas al LLM y puede superar el timeout de un gateway, que cortaría la respuesta pero no el trabajo.
- La reanudación manual respeta `MAX_RUN_ATTEMPTS`; no hay permiso para forzar más intentos.
- No hay cancelación de ejecuciones, ni caducidad ni ámbito por tenant de las claves de idempotencia más allá de proyecto y principal.
- Validado con una instancia; con varias, el riesgo residual de checkpoints tardíos del ADR 0004 sigue vigente.

## Pending for the next steps

- Cancelación de ejecuciones, caducidad de claves de idempotencia, seguridad a nivel de fila y cercado de checkpoints con varias instancias quedan para fases posteriores.

## Consequences

- Cambio incompatible asumido: desde esta versión todo cliente necesita un token. En local basta con `AUTH_MODE=disabled` o con las claves de desarrollo.
- Si el JWKS del proveedor no responde, la API falla cerrada con 401; la caché de claves amortigua cortes breves.
- Las diferencias entre proveedores se absorben con las rutas de reclamaciones configurables; cada proveedor nuevo exige una prueba de mapeo.
- Qué nos haría revisar esta decisión: necesidad de emitir credenciales propias para clientes sin proveedor OIDC, o varios emisores de confianza a la vez.
