# Runbook H9.0 — Pocket ID con passkeys como proveedor de identidad humana

Estado: **preparación, nada desplegado** (2026-10-31). Este documento recoge la auditoría previa, la
topología propuesta y las instrucciones reproducibles. Instalar el servicio, habilitar certificados en
Tailscale, crear la ACL o cambiar cualquier otra infraestructura requiere autorización expresa del
titular. Ningún valor real (claves, URLs internas distintas de las plantillas, identificadores de
cliente) se guarda en Git. Diseño de referencia: ADR 0013.

## 1. Qué se comprobó antes de proponer

| Aspecto | Hallazgo (2026-10-31) |
|---|---|
| Proveedor | Pocket ID v2.19.1 (2026-10-22), BSD-2, OpenID Connect certificado, solo passkeys, un binario o un contenedor con SQLite; actualizaciones frecuentes (cinco versiones en seis semanas) |
| Ceremonia por operación | En el código fuente: `ReauthenticationRequired = prompt=login ∥ client.RequiresReauthentication ∥ max_age vencido`; ese paso **solo** se satisface con un `ReauthenticationToken` que emite el servicio WebAuthn tras una aserción de passkey (`amr = phr`) y que caduca al minuto; `auth_time` pasa a ser el instante de esa ceremonia. Es decir: un cliente marcado «Requires Re-Authentication» exige una passkey en **cada** autorización, aunque exista sesión viva. Pendiente de confirmar empíricamente en H9.0 (§6) |
| Flujos OIDC | Clientes públicos con PKCE (tipo de cliente «public»), código de autorización, PAR, autorización de dispositivo; `max_age`/`auth_time` conformes desde junio de 2026; `amr` emitido; `nonce` soportado |
| URLs de retorno | Coincidencia exacta; `http://127.0.0.1/<ruta>` sin puerto acepta cualquier puerto (apto para la API local en `127.0.0.1:8011`); `https` obligatorio fuera de loopback |
| Reclamaciones | `sub` permanente, `email`, `name`, `preferred_username`, `groups` (nombres de grupo), reclamaciones personalizadas por usuario o grupo; ID token RS256 de 1 h; tokens de refresco emitidos aunque no se pidan (**la API nunca los solicitará ni usará**) |
| Recuperación | Varias passkeys por usuario; código de acceso único desde el panel o desde la CLI del nodo (`pocket-id one-time-access-token <usuario>`, 1 h) |
| Respaldo | `pocket-id export --path -` genera un ZIP con base de datos y archivos; `import` lo restaura con la instancia parada; la `ENCRYPTION_KEY` es imprescindible para leer las claves de firma |
| Homelab | El host del homelab visible en el tailnet es un Proxmox VE (certificado «PVE Cluster Node», API en 8006 con 401 sin credenciales). **No fue posible inspeccionar su capacidad**: no hay clave SSH ni credencial de la API configuradas en este Mac. La VM del Runner (12 vCPU, 7,9 GB, 66 GB libres, carga 0) no se toca |
| Tailscale | MagicDNS activo (sufijo `*.<tailnet>.ts.net`); **certificados HTTPS no habilitados** (`CertDomains` vacío); ningún nodo iOS en el tailnet; cinco nodos Linux/macOS, entre ellos el host Proxmox y la VM del Runner |

Comandos de solo lectura que el titular puede ejecutar en el nodo Proxmox para completar la revisión de
capacidad (no modifican nada):

```bash
pvesh get /nodes/$(hostname)/status --output-format json | jq '{cpu, cores: .cpuinfo.cores, mem_total_gb: (.memory.total/1e9|floor), mem_free_gb: (.memory.free/1e9|floor), load: .loadavg}'
qm list; pct list; pvesm status
```

## 2. Arquitectura mínima recomendada

```
MacBook (navegador; Touch ID)                iPhone (Safari; Face ID)  [requiere Tailscale en el móvil]
        │ https, solo tailnet                         │
        ▼                                             ▼
pocket-id.<tailnet>.ts.net ── tailscale serve (TLS ts.net) ──► 127.0.0.1:1411 Pocket ID (LXC/VM dedicado en Proxmox)
        ▲                                                     datos: SQLite + uploads + encryption_key (0600)
        │ JWKS / descubrimiento (https, lectura)
AgentForge API (hoy en el Mac, 127.0.0.1:8011; Fase 9: nodo propio) ── verifica ID tokens, registro (iss, sub) → roles
        ▲
        │ clave de automatización (.local/, permisos fijos: leer y crear; nunca decidir)
CLI / skill / Claude Code
```

- **Un nodo nuevo y aislado** para Pocket ID: LXC sin privilegios (Debian 12 u Ubuntu 24.04) con 1 vCPU,
  512 MB de RAM y 4 GB de disco, o una VM mínima si se prefiere Podman. Sin código de AgentForge, sin
  acceso al Runner, sin claves de GitHub ni de Vertex.
- **Solo un puerto expuesto, 443, solo en el tailnet**, mediante `tailscale serve --bg 1411` en el nodo
  (requiere habilitar «HTTPS Certificates» en el panel de Tailscale; MagicDNS ya está activo). Pocket ID
  escucha únicamente en `127.0.0.1:1411`. Sin DNS público, sin puertos en el router, sin Funnel.
- **ACL de Tailscale**: `tag:agentforge-human-devices` (Mac, iPhone) y `tag:agentforge-api` → `tag:agentforge-idp:443`;
  nada más llega al IdP. Tailscale SSH desactivado en el nodo; administración por SSH con clave del titular.
- **Dos clientes OIDC** en Pocket ID, ambos públicos con PKCE, grupo permitido `agentforge-operadores`
  (solo el titular), «Skip consent» activado, **«Requires Re-Authentication» activado**:
  - `agentforge-ui`: URL de retorno `http://127.0.0.1/ui/callback` mientras la API viva en el Mac;
    `https://api.<tailnet>.ts.net/ui/callback` cuando se mueva (H9.3/Fase 9).
  - `agentforge-cli` (opcional, reservado): `http://127.0.0.1/callback`. No se usa en el diseño mínimo.
- **Separación de credenciales**: las credenciales humanas son passkeys (Secure Enclave / iCloud Keychain o
  llave física); el IdP solo guarda sus claves públicas; la API solo guarda un registro `(iss, sub) → label,
  roles, proyectos`. La credencial de automatización es la clave RSA de `.local/` del Mac, con permisos
  fijos del lado del servidor. Claude Code puede leer esta última y ninguna de las primeras.

## 3. Requisitos exactos de despliegue (para cuando se autorice)

1. Proxmox: crear el LXC/VM dedicado (sin nesting salvo que se use Podman; sin dispositivos compartidos).
2. Tailscale: habilitar HTTPS Certificates en el panel; `tailscale up --advertise-tags=tag:agentforge-idp --ssh=false`
   en el nodo; añadir la regla de ACL anterior; etiquetar Mac e iPhone (o usar sus usuarios) en la ACL.
3. Pocket ID: copiar `deploy/identity/pocket-id/{compose.yaml,env.example}` (o el `.service.example` para el
   binario), fijar `APP_URL=https://pocket-id.<tailnet>.ts.net`, generar `secrets/encryption_key` con
   `openssl rand -base64 32` (0600, copia en el gestor de contraseñas), `docker compose up -d` o `systemctl enable --now pocket-id`.
4. `tailscale serve --bg 1411` en el nodo; comprobar `https://pocket-id.<tailnet>.ts.net/.well-known/openid-configuration` desde el Mac.
5. `/setup`: crear el único usuario administrador del titular y registrar **dos passkeys** (plataforma con
   iCloud Keychain y una llave física o la del iPhone). Crear el grupo `agentforge-operadores` y asignarlo.
6. Crear el cliente `agentforge-ui` (público, PKCE, re-autenticación, grupo permitido, sin consentimiento).
   Anotar el `client_id` fuera de Git.
7. Primer respaldo: `pocket-id export` y prueba de `import` en un contenedor desechable (§7).

## 4. Inicio de sesión desde MacBook y móvil

- **MacBook**: Safari o Chrome → Pocket ID pide la passkey → Touch ID. La passkey de plataforma se sincroniza
  por iCloud Keychain; si solo existiera en el iPhone, el navegador ofrece el flujo entre dispositivos por
  código QR.
- **iPhone**: Safari → Face ID con la misma passkey sincronizada. Requiere la app de Tailscale en el móvil
  (hoy no hay nodo iOS) para alcanzar el IdP; para aprobar también desde el móvil la API tendría que ser
  alcanzable en el tailnet (Fase 9). Hasta entonces, el móvil sirve de segunda passkey y de recuperación.
- Política recomendada: `WEBAUTHN_USER_VERIFICATION=required` (siempre biometría o PIN), passkeys sincronizadas
  permitidas, sin signups, sin correo, sin acceso único por correo.

## 5. Separación humano / automatización y fallo del proveedor

| Situación | Comportamiento esperado |
|---|---|
| Claude Code ejecuta la skill | Solo credencial de automatización: estados, creación de trabajo; `human_required` si intenta decidir |
| Claude Code abre el navegador | Puede abrir la página; no puede completar la ceremonia de passkey ni pulsar en el navegador (requiere permiso de Accesibilidad de macOS, que Terminal no tiene) |
| Token humano en disco | No existe: la API nunca pide `offline_access`, la UI usa sesión opaca en memoria y el ID token se usa una vez |
| Pocket ID caído | Las decisiones fallan cerradas (`identity_provider_unavailable`); lecturas y creación siguen con la automatización; el Supervisor espera; caducidades según H2. **Ninguna aprobación privilegiada salta la autenticación**; el emisor de desarrollo no vuelve a aceptarse para humanos |
| Pocket ID comprometido | Revocar passkeys y sesiones desde el panel, rotar `ENCRYPTION_KEY`, reinstalar desde respaldo limpio, retirar el nodo de la ACL; mientras tanto no hay decisiones posibles |
| Mac robado | Sin tokens humanos ni passkeys en archivos; la clave de automatización solo crea y lee; rotar `.local/` |

## 6. Evidencia de passkey por aprobación: prueba de aceptación de H9.0

Objetivo: demostrar, sin código de AgentForge, que cada autorización del cliente `agentforge-ui` exige una
ceremonia de passkey y deja evidencia en el token.

1. Con sesión abierta en Pocket ID, iniciar una autorización del cliente (p. ej. con el «OIDC Data Preview»
   del panel o un cliente de prueba local) → debe pedir la passkey aunque la sesión esté viva.
2. Repetir inmediatamente una segunda autorización → debe pedir la passkey de nuevo.
3. Decodificar ambos ID tokens: `auth_time` del segundo mayor que el del primero y posterior al inicio de la
   segunda petición; `amr` contiene `phr`; `nonce` coincide con el enviado.
4. Intentar reutilizar la pantalla de reautenticación pasado un minuto sin nueva passkey → rechazo
   (`reauthentication_required`).
5. Documentar versión exacta de Pocket ID, capturas de las dos ceremonias y los dos tokens (sin secretos) en
   `docs/validation/`.

Si el paso 1 o 2 no exige la passkey, Pocket ID no sirve como está: se descarta o se escala (WebAuthn en la
propia API), y no se continúa con H9.1 sobre una garantía falsa. `auth_time` reciente por sí solo **no** es
prueba de presencia: la prueba es la ceremonia exigida por el cliente, con `amr = phr` y `auth_time`
posterior a la petición concreta.

## 7. Respaldo y recuperación

- **Diario**: `docker compose exec -T pocket-id ./pocket-id export --path - > export-$(date -u +%F).zip` (o
  el binario), copiado por el titular al Mac mediante `scp` por Tailscale y guardado junto a los respaldos
  de PostgreSQL; retención 30 días. Además, `vzdump` del LXC en Proxmox si ya existe esa rutina.
- **Clave**: `ENCRYPTION_KEY` en el gestor de contraseñas del titular; sin ella el respaldo no sirve.
- **Restauración**: parar la instancia, `pocket-id import --yes --path export.zip`, arrancar, comprobar
  descubrimiento, JWKS (el `kid` debe coincidir con el anterior) e inicio de sesión con passkey. Ensayo
  trimestral en un contenedor desechable.
- **Pérdida de passkeys**: segunda passkey; si se pierden todas, `pocket-id one-time-access-token <usuario>`
  desde el nodo (solo el titular con SSH), enlace válido 1 h, registrar nueva passkey y revocar las antiguas.
- **Rotación**: `ENCRYPTION_KEY` con `pocket-id encryption-key-rotate`; claves de firma solo si hay
  compromiso (la API refresca el JWKS en ≤ 5 min).

## 8. Riesgos y controles

| Riesgo | Control |
|---|---|
| Sesión del navegador reutilizada por automatización local | Cliente con reautenticación obligatoria: cada decisión exige passkey; sesión del IdP de 15 min; la API exige `auth_time` posterior a la petición concreta |
| Confirmar una decisión distinta de la mostrada | La API muestra la decisión y liga la ceremonia al digest mediante `state`/`nonce`; la decisión vuelve a comprobarse por digest y caducidad en el servidor |
| Pocket ID expuesto fuera del tailnet | Solo `tailscale serve`, sin Funnel, ACL por etiqueta, puerto 1411 en loopback |
| Pérdida del nodo o del disco | Export diario + `ENCRYPTION_KEY` en el gestor; ensayo de restauración |
| Actualización que cambie la semántica de reautenticación | Versión fijada; prueba del §6 repetida tras cada actualización antes de usarla para decidir |
| Dependencia del homelab para decidir | Aceptada (ya lo es para QA); documentada en ADR 0013 |

## 9. Lo que este runbook no autoriza

Crear el LXC, habilitar certificados o ACL en Tailscale, instalar Pocket ID, cambiar la autenticación de la
API, ejecutar migraciones o reactivar el Publisher. Cada uno requiere una autorización posterior con la
topología de §2 aprobada.
