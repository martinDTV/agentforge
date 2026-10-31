# 0013. Identidad verificable (H9): humano por OIDC externo con passkeys, automatizaciones con permisos fijos, decisiones confirmadas por ceremonia

- Status: **proposed** — dirección aprobada por el titular el 2026-10-31 («H9 como dirección
  arquitectónica»); solo H9.0 autorizado (preparación, sin despliegue). Revisado el mismo día con los
  ajustes pedidos (§2) y con la evidencia del código de Pocket ID (§3.1).
- Date: 2026-10-31
- Deciders: martin, Claude

## Context

La auditoría del 2026-10-31 (D9, `docs/audits/2026-10-31-plan-de-estabilizacion-1.0.md` §12) estableció
que la identidad de las personas en la instancia diaria es una etiqueta: la API acepta una sola clave
firmante, la de desarrollo de `.local/`, propiedad del mismo usuario del sistema que ejecuta la API, la
CLI, la skill y Claude Code; la CLI firma cualquier `sub` con rol `project_operator`; roles y proyectos se
leen del token; la auditoría guarda solo el sujeto; la regla «creador ≠ publicador» compara etiquetas. Las
publicaciones de E2 se aprobaron con excepciones de laboratorio y **no acreditan separación de funciones**.
La revocación de tokens de GitHub no deja evidencia.

Requisitos del titular: identidad humana de un proveedor OIDC confiable; Claude Code no puede acuñar ni
modificar tokens humanos; identidades de servicio con permisos limitados; permisos comprobados en el
servidor; nada autodeclarado por la CLI; cada aprobación registra identidad verificada, emisor y evidencia;
modelo explícito para un solo propietario humano; reutilizar contratos existentes; revocación de tokens de
GitHub con evidencia verificable o error observable; distinguir sesión, autenticación reciente, confirmación
de una decisión concreta y aprobación ligada al digest; **no asumir que `auth_time` reciente demuestra
presencia humana**; evitar complejidad innecesaria.

Se reutiliza: `Principal`, `JwtVerifier` (`JwksUrlKeys`, `JwksFileKeys`, `ClaimMapping`), `AuthorizationPolicy`,
el precedente de emisores de confianza por capacidad en `ToolAuthorizationPolicy`, la delegación del
Supervisor, los registros de datos del operador (`PROJECTS_FILE`), `run_decisions` con motivo (0011),
`publications`, la bandeja de correo, la skill y `scripts/agentforge.py`.

## 1. Modelo de amenazas

Activos: escritura en GitHub (tokens de la App), decisiones humanas (plan, alcance, publicación,
reanudación, cancelación), datos del operador (políticas, registro de identidades), auditoría.

| Actor | Capacidad | Amenaza | Control |
|---|---|---|---|
| A1 Claude Code (mismo usuario del sistema; sin permiso de Accesibilidad de macOS) | Lee archivos del usuario, ejecuta comandos, abre URLs | T1 acuñar token humano con una clave local. T2 reutilizar un token o sesión humana. T3 autodeclarar roles. T4 decidir por la API. T5 aprovechar una sesión viva del navegador. T6 mostrar una decisión y confirmar otra | T1: sin clave humana en el Mac. T2: sin tokens humanos persistidos; sesión opaca en memoria; passkey no exportable. T3: roles solo del registro del servidor. T4: decisiones exigen humano de emisor de confianza con ceremonia. T5: el cliente OIDC exige passkey en cada autorización. T6: la ceremonia se liga por `state`/`nonce` al digest que la API mostró |
| A2 Agentes y texto del modelo | Solo `ToolCall` por el gateway | Sin cambios | ADR 0006/0007 |
| A3 Persona con el Mac desbloqueado o copia del disco | Lee `.local/`, `.env`, `~/.config` | Clave de automatización | Solo crear y leer; rotación por archivo |
| A4 Atacante en el tailnet | Alcanza API o IdP | Suplantación, repetición | TLS; `aud`, `exp`, `nonce`, `state`; `iat` obligatorio; ACL por etiqueta |
| A5 IdP comprometido o caído | Emite tokens humanos / no emite | Suplantación total / sin decisiones | Autoalojado, solo passkeys, aislado; caída ⇒ decisiones fallan cerradas |
| A6 Error del operador | Edita registro o configuración | Roles de más | `extra="forbid"`, validación al arranque, huella en el log, respaldo |

Fuera de alcance: memoria de procesos vivos, `root` del Mac o del hipervisor, administrador del IdP. Riesgo
residual: una persona que confirma sin leer la decisión que la API le muestra.

## 2. Conceptos que el diseño distingue (y quién garantiza cada uno)

| Concepto | Qué es | Quién lo garantiza | Qué prueba y qué no |
|---|---|---|---|
| **Sesión OIDC válida** | El IdP recuerda al navegador (`SESSION_DURATION`, 15 min) y la API mantiene una sesión opaca en memoria (15 min inactiva, 60 min máximo) | Pocket ID; `app/api/ui` | Permite ver la lista de decisiones. **No** autoriza ninguna decisión |
| **Autenticación reciente** | `auth_time` del ID token dentro de una ventana (300 s) | `require_fresh` en `app/security` | Necesaria, **no suficiente**: una sesión reciente también la cumple. Por eso no es el control de presencia |
| **Confirmación humana de una decisión concreta** | Ceremonia de passkey exigida por el cliente OIDC (`requiresReauthentication`) **después** de que la API mostró ejecución, etapa y digest, con `state` y `nonce` derivados de ese digest | Pocket ID (ceremonia, `amr=phr`, `auth_time` = instante de la ceremonia) + la API (verifica `nonce`, `state` y `auth_time ≥ inicio de la petición`) | Prueba que una persona hizo un gesto físico para **esta** decisión. No prueba que leyó bien; por eso la decisión también va ligada al digest |
| **Aprobación ligada al digest y a la ejecución** | `claim_decision` con el digest exacto, caducidad y CAS | `RunService.approve` (existe) | La decisión no aplica a otra propuesta ni a otra ejecución |
| **Credenciales humanas inaccesibles a Claude Code** | Passkeys en Secure Enclave / iCloud Keychain o llave física; sin tokens de refresco; sin ID tokens en disco; sesión de la API opaca y en memoria | Diseño de la UI y del IdP | Claude Code puede abrir el navegador; no puede completar WebAuthn ni pulsar sin Accesibilidad |
| **Automatización sin autoasignación de privilegios** | Las reclamaciones del token de automatización se ignoran; rol fijo `AUTOMATION_CLI` en código; proyectos del registro | `MultiIssuerVerifier` + `AuthorizationPolicy` | Puede crear y leer; `human_required` al decidir |
| **Identidad verificada por emisor y `sub`** | El registro se indexa por `(iss, sub)`; `decided_by` guarda la etiqueta, la evidencia guarda `iss` y `sub` | Registro de identidades | Una etiqueta igual con otro emisor es otra identidad; etiquetas `automation:*`/`agentforge:*` prohibidas para humanos |
| **Revocación y expiración de sesiones humanas** | ID token de un solo uso; sesión de la API con cierre, caducidad y borrado al reiniciar; sesión del IdP corta; revocación de passkeys y sesiones en el IdP; baja en el registro ⇒ denegación inmediata | Pocket ID + `app/api/ui` + registro | Sin credencial humana de larga vida en ninguna parte |

## 3. Arquitectura mínima

### 3.1 Evidencia sobre Pocket ID (código fuente, 2026-10-31)

`authorization_service.go`: `ReauthenticationRequired = prompt=login ∥ client.RequiresReauthentication ∥
requiresReauthenticationForMaxAge(max_age, auth_time, now)`. El paso de reautenticación solo se satisface
con un `ReauthenticationToken` que el servicio WebAuthn emite tras una aserción de passkey
(`authenticationMethod == phr`), consumible una vez y en menos de un minuto; al consumirlo,
`authenticationTime = reauthenticatedAt`, que es el `auth_time` del token. Clientes públicos con PKCE;
`http://127.0.0.1/<ruta>` sin puerto acepta cualquier puerto; `amr`, `nonce` y `auth_time` presentes. Lo
anterior se confirma empíricamente en H9.0 (runbook §6) antes de construir nada encima.

### 3.2 Componentes

1. **Dos emisores** en `MultiIssuerVerifier` (`app/security/tokens.py`), enrutando por `iss` sin confiar en
   nada más del token hasta verificar la firma:
   - humano: `OIDC_ISSUER` de Pocket ID, `JwksUrlKeys`, `aud` = `agentforge-ui`; `kind=user`; roles y proyectos
     del registro por `(iss, sub)`; sujeto desconocido ⇒ sin roles;
   - automatización: clave actual de `.local/` como `AUTOMATION_JWKS_FILE` con `AUTOMATION_ISSUER`;
     `kind=service`; sujeto `automation:<nombre>`; rol fijo `AUTOMATION_CLI` = {`runs:read`, `runs:audit:read`,
     `runs:create`, `repos:write`}; proyectos del registro de servicios;
   - otro emisor ⇒ `invalid_token`; `OIDC_JWKS_FILE` humano solo en `ENVIRONMENT=test`.
2. **Registro de identidades** `IDENTITY_FILE` (`app/security/registry.py`): `humans[]` {`issuer`, `subject`,
   `label`, `roles`, `projects`}, `services[]` {`name`, `projects`}; `extra="forbid"`, relectura por `mtime`,
   huella en el log.
3. **Política**: `runs:approve`, `runs:resume`, `runs:cancel`, `repos:publish` exigen `kind=user` de un emisor
   humano (`human_required`) y `AuthEvidence` con ceremonia (`amr` contiene `phr` cuando el emisor lo emite y
   `auth_time ≥ inicio de la confirmación`); `require_fresh` como segunda barrera. Delegación y agentes sin
   cambios.
4. **Confirmación en navegador servida por la API** (`app/api/ui.py`, HTML escapado sin plantillas externas,
   `python-multipart` ya presente): `GET /ui/decisions` (sesión o inicio de sesión OIDC con PKCE),
   `POST /ui/decisions/{run}/{stage}` (digest, motivo, CSRF ⇒ crea una confirmación pendiente con `state`
   aleatorio y `nonce = H(state ‖ digest)` y redirige al IdP), `GET /ui/callback` (verifica ID token, `nonce`,
   `state`, `auth_time`, registro; llama a `RunService.approve` con el principal humano y la evidencia),
   `POST /ui/logout`. Sesiones y confirmaciones pendientes en memoria con caducidad; nada firmado con
   secretos que Claude Code pueda leer.
5. **Evidencia** (`AuthEvidence` en `app/contracts`): `issuer`, `subject`, `kind`, `iat`, `auth_time`, `amr`,
   `jti` si existe, `token_fingerprint`; migración 0012 aditiva: `run_decisions`/`run_approvals` ganan
   `decided_issuer` y `auth_evidence JSONB`; `publications` gana `approved_issuer`; eventos con `actor_issuer`.
6. **CLI y skill**: `af decide`/`cancel`/`resume` dejan de enviar decisiones; muestran la decisión y abren
   `http://127.0.0.1:8011/ui/decisions/<run>`; el resto usa la clave de automatización; desaparecen `--as` y
   `AGENTFORGE_SUBJECT`; D10 se corrige en el mismo cambio.

Sin cambios en Runner, Proxmox, Tool Gateway, Publisher, GitHub App, esquema de permisos existente,
delegación del Supervisor.

### 3.3 Alternativa considerada y descartada para el mínimo

Inicio de sesión PKCE desde la CLI con `nonce` ligado al digest: menos código, pero la decisión la muestra la
terminal que la automatización controla y el token lo maneja un proceso que Claude Code puede lanzar. Queda
como opción futura para uso sin navegador, nunca como vía principal.

## 4. Flujos

**Decisión humana.** 1) La persona abre `/ui/decisions` (o la skill le imprime el enlace). 2) Sin sesión,
la API inicia OIDC (PKCE, `state`, `nonce`); Pocket ID exige la passkey; la API verifica el ID token y crea la
sesión opaca. 3) La página muestra cada decisión pendiente con ejecución, etapa, digest, resumen y
caducidad; la persona escribe el motivo y pulsa aprobar o rechazar. 4) La API registra la confirmación
pendiente y redirige al IdP con `prompt=login`, `max_age=0`, `state` y `nonce` ligados al digest; Pocket ID
exige **otra** passkey. 5) En el callback la API verifica `nonce`, `state`, `auth_time ≥ t₄`, `amr`, registro;
llama a `RunService.approve` (digest, caducidad, política, CAS, como hoy) con `AuthEvidence`; muestra el
resultado. Dos gestos como máximo por decisión; uno si la sesión sigue viva.

**Automatización.** Token de 15 min firmado con la clave de `.local/`, `sub` = nombre del servicio; la API
lo normaliza y le asigna `AUTOMATION_CLI`; `human_required` ante cualquier decisión.

**Supervisor.** Sin cambios: hijas del objetivo con el dueño del objetivo. Como el trabajo lo crea la
automatización, la persona aprueba todas las hijas sin excepciones (creador ≠ aprobador por construcción).
Si una persona crea trabajo con su identidad, la regla actual le impide publicarlo; la UI lo advierte.

## 5. Roles: asignación y comprobación

| Principal | Roles y proyectos | Puede decidir |
|---|---|---|
| Humano registrado (`kind=user`, emisor Pocket ID) | del registro (`project_operator`, `admin`) | sí, con ceremonia y evidencia |
| Humano no registrado | ninguno (403 y evento) | no |
| Automatización (`kind=service`, emisor de automatización) | `AUTOMATION_CLI` fijo + proyectos del registro | no |
| Delegado del Supervisor | `goal_delegate` fijo | no |
| Agentes | `agent` fijo | no |
| `local-dev` (`AUTH_MODE=disabled`) | `platform_admin` | solo `local`/`test`; prohibido en la instancia diaria |

La separación de funciones es **humano frente a automatización**: la automatización crea y ejecuta, la
persona decide. «Creador ≠ publicador» se mantiene sin excepciones. «Aprobador de alcance ≠ publicador»
(`AUTHZ_PUBLISH_DISTINCT_APPROVER`) se declara `false` de forma explícita mientras el registro tenga un
solo humano con `repos:publish`, y pasa a `true` cuando haya más de uno.

## 6. Sesiones y credenciales

- Humanas: passkeys (dos por persona: plataforma y llave física o móvil); ID tokens de un solo uso; sin
  tokens de refresco (la API no los pide ni los acepta); sesión del IdP 15 min; sesión de la API opaca, en
  memoria, 15 min inactiva, 60 min absoluta, cierre explícito, borrada al reiniciar.
- Automatización: par RSA en `.local/` (0600), `AutomationIssuer` (el `DevIssuer` actual renombrado), tokens
  de 15 min; rotación por archivo (la API relee el JWKS por `mtime`); daño máximo: crear trabajo dentro de
  presupuestos que una persona debe aprobar, y leer.
- Registro: archivo del operador 0600 fuera del repositorio, respaldado con `projects.json`.

## 7. Auditoría de aprobaciones y de revocación

Cada decisión guarda `decided_by` (etiqueta), `decided_issuer` y `auth_evidence` (`sub`, `iat`,
`auth_time`, `amr`, `jti`, `token_fingerprint`). Los correos dicen «aprobado por `<label>`, identidad
verificada por `<emisor>` con passkey». Una decisión de automatización no puede existir.

Revocación de tokens de instalación de GitHub (H9.2): tras `DELETE /installation/token`, el Publisher
verifica con una llamada autenticada por el token revocado, que debe devolver 401; emite `tokens_revoked`
`{push, pulls, verified}`; si no puede verificar, la publicación queda `published` con
`publication.code = token_revocation_unverified`, advertencia en el log y aviso en readiness hasta la
caducidad del token.

## 8. Plan de migración desde la identidad local

1. **H9.0** (operador; runbook `docs/runbooks/identidad-pocket-id.md`): nodo aislado, `tailscale serve`,
   Pocket ID fijado, dos passkeys, cliente `agentforge-ui` con reautenticación, prueba de ceremonia por
   autorización, respaldo y restauración ensayados.
2. **Configuración**: `OIDC_ISSUER`, `OIDC_AUDIENCE`, `OIDC_JWKS_URL`; `AUTOMATION_JWKS_FILE=.local/jwks.json`;
   `IDENTITY_FILE`; `UI_BASE_URL`; retirar `OIDC_JWKS_FILE`.
3. **H9.1** con migración 0012 (respaldo previo), reinicio, verificación en real.
4. **Limpieza**: `--as`/`AGENTFORGE_SUBJECT` fuera de CLI, skill y documentación; corregir
   `docs/uso-diario.md` §4, ADR 0005 y la fila de separación de funciones de la validación de Fase 7;
   `scripts/dev_token.py` solo para pruebas.
5. **H9.3**: instancia diaria en `ENVIRONMENT=staging` con `AUTHZ_PUBLISH_DISTINCT_APPROVER=false` explícito.

Reversión: la suite sigue usando el emisor estático en `test`; en la instancia diaria no hay vuelta a la
identidad nominal sin decisión expresa del titular.

## 9. Pruebas de seguridad y recuperación

Unitarias: enrutamiento por `iss` y rechazo de emisores desconocidos; roles del registro frente a
reclamaciones; automatización con `platform_admin` en el token ⇒ `AUTOMATION_CLI`; sujeto normalizado;
`human_required` para decisiones con `kind=service` y con el delegado; `require_fresh`; `nonce`/`state`
incorrectos o reutilizados ⇒ rechazo; `auth_time` anterior al inicio de la confirmación ⇒ rechazo; CSRF;
sesión caducada ⇒ nuevo inicio de sesión; evidencia persistida y en el correo; estáticas: ningún módulo
fuera de `tests/` y `scripts/dev_keys.py` firma tokens; la CLI no envía decisiones ni lee
`AGENTFORGE_SUBJECT`; `app/security` sin FastAPI. Settings: `OIDC_JWKS_FILE` fuera de `test` rechazado.

Integración: migración 0012 arriba y abajo; decisión con evidencia leída; `approved_issuer`.

Recuperación (simulacros): IdP caído ⇒ decisiones fallan cerradas y lecturas siguen; rotación de claves del
IdP ⇒ JWKS refrescado; pérdida de passkeys ⇒ segunda passkey o código de acceso único desde el nodo; clave
de automatización comprometida ⇒ rotación; registro manipulado ⇒ huella distinta; revocación de GitHub
fallida ⇒ `token_revocation_unverified` visible; restauración de Pocket ID desde export con `kid` intacto.

## 10. Costos y dependencias

| Opción | Garantía de presencia | Operación | Costo | Veredicto |
|---|---|---|---|---|
| **Pocket ID autoalojado** (passkeys, reautenticación por cliente) en LXC de Proxmox detrás de `tailscale serve` | Ceremonia de passkey exigible en cada autorización (código verificado; confirmar en H9.0) | Un servicio pequeño más; export diario; versión fijada | 0 licencia; 1 vCPU / 512 MB | **Recomendado** |
| Google OIDC | Sesión viva del navegador completa el flujo sin nuevo factor; `max_age` no fiable; clics automatizables | Cero hospedaje | 0 | Alternativa débil; solo si el homelab no puede sostener el IdP |
| Authentik | Equivalente con más superficie | Postgres + Redis + workers | 0; ~2 GB RAM | Innecesario |
| Cabeceras de Tailscale | No distingue humano de automatización en el mismo Mac | mínima | 0 | Descartado |
| Clave local actual | Identidad nominal (D9) | ninguna | 0 | Descartado |

Dependencias: Tailscale con certificados HTTPS habilitados (hoy no), Proxmox encendido para decidir,
navegador en el Mac; Tailscale en el iPhone para usarlo. Código: `registry.py`, verificador multiemisor,
`app/api/ui.py` con cuatro rutas, migración aditiva, cambios acotados en `policy.py`, `settings.py`,
`api/errors.py`, `scripts/agentforge.py` y la skill; sin dependencias nuevas.

## 11. Incrementos y criterios verificables

| # | Incremento | Criterios |
|---|---|---|
| H9.0 | Proveedor desplegado en nodo aislado, cliente `agentforge-ui`, dos passkeys, respaldo (operador; runbook) | Descubrimiento y JWKS por Tailscale; **dos autorizaciones consecutivas exigen passkey** con `auth_time` creciente y `amr=phr`; reutilización pasada un minuto rechazada; export restaurado en un contenedor desechable; IdP inalcanzable fuera del tailnet; procedimiento de recuperación ensayado |
| H9.1 | Núcleo: multiemisor, registro, política `human_required`, UI de confirmación, evidencia (0012), CLI sin decisiones, D10 | Pruebas del §9; en real: automatización ⇒ `human_required`; decisión `scope` real con `decided_issuer` de Pocket ID y `amr=phr`; `.local` sin tokens humanos; una publicación real sin excepción de laboratorio |
| H9.2 | Revocación verificada de tokens de GitHub | `tokens_revoked{verified:true}` en una publicación real; `FakeGitHub` que ignora la revocación ⇒ `token_revocation_unverified` |
| H9.3 | Perfil `staging` para la instancia diaria, documentación corregida, acceso desde el móvil cuando la API esté en el tailnet | `agentforge_dev up` se niega con `AUTH_MODE=disabled` o JWKS humano en archivo; publicación real completa con una sola persona y sin excepciones |

Orden: H9.0 → H9.1 → H9.2 → H9.3. H3 espera la revisión de este diseño; la campaña H7 solo cuenta
aprobaciones posteriores a H9.1.

## Propuesta mínima

H9.0 + H9.1: Pocket ID aislado con passkeys y reautenticación por cliente; la clave local degradada a
automatización con rol fijo; roles del registro `(iss, sub)`; decisiones solo desde una página servida por
la API que muestra la decisión y exige una ceremonia de passkey ligada al digest; evidencia del emisor en
cada decisión. Claude Code conserva lectura y creación, y no puede aprobar: no tiene passkey, no puede
pulsar en el navegador y la API rechaza cualquier decisión sin ceremonia.

## Consequences

- Más fácil: aprobar con identidad real desde el navegador; auditoría con emisor y evidencia; la skill
  sigue siendo útil sin poder decidir; base para el panel privado.
- Más difícil: cada decisión cuesta un gesto; decidir depende del homelab; un servicio más que respaldar.
- Revisar si aparece un segundo humano con `repos:publish`, si la API sale del tailnet, o si la prueba de
  H9.0 muestra que Pocket ID no exige la ceremonia por autorización (entonces WebAuthn en la propia API o
  otro proveedor, nunca Google como vía principal).
