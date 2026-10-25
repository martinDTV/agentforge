# AgentForge Runner

Servicio independiente que ejecuta programas permitidos (`ruff check`, `ruff format --check`, `pytest`) sobre una copia del worktree de una ejecución, dentro de contenedores Podman rootless, en una máquina Linux dedicada. Habla el protocolo v1 del ADR 0007 con el `RemoteSandboxAdapter` de la API y no importa nada de `app/`.

Es portable: la API solo necesita una URL, un token y el identificador esperado del runner. El mismo paquete sirve en una VM de Proxmox, en una VM local (UTM, Multipass, Lima) o en una VM de nube; cambia el transporte privado, no el runner.

## Estructura

```
runner/
├── pyproject.toml                 paquete agentforge-runner (FastAPI, uvicorn, pydantic, python-multipart)
├── agentforge_runner/
│   ├── protocol.py                contratos v1 (copia independiente de app/sandbox, con prueba de conformidad)
│   ├── programs.py                lista autoritativa de programas y validación de argv
│   ├── config.py                  RUNNER_*: imágenes fijadas por digest, bind privado, techos, credenciales
│   ├── auth.py                    bearer comparado por SHA-256 + allowlist de orígenes
│   ├── extract.py                 escaneo del tar.gz sin escribir en el host
│   ├── store.py                   SQLite: idempotencia, estados, reconciliación, resoluciones
│   ├── backend.py                 puerto del backend de aislamiento
│   ├── podman.py                  Podman rootless: preflight con sonda, forma fija del contenedor, kill, inspect
│   ├── fake_backend.py            backend guionizado para pruebas (no es evidencia de aislamiento)
│   ├── service.py                 /v1/health, /v1/executions, /v1/admin/executions
│   └── cli.py                     agentforge-runner serve | preflight | token
└── deploy/                        unidad systemd, runner.env.example, ACL de Tailscale, Containerfile
```

## Endpoints (protocolo v1)

| Método y ruta | Credencial | Respuesta |
|---|---|---|
| `GET /v1/health?refresh=` | API | identidad, versión, backend, rootless, perfiles, `ready` y comprobaciones (incluida la sonda) |
| `POST /v1/executions` multipart `request` + `workspace` | API | `200 SandboxResult`; `409 execution_in_progress / execution_conflict / execution_uncertain`; `422 argv_rejected / limits_rejected / profile_unknown / request_invalid / archive_mismatch / archive_rejected / path_rejected`; `413 archive_too_large`; `503 isolation_unavailable / runner_busy` |
| `GET /v1/executions/{id}` | API | estado, códigos, contenedor y resultado si existe |
| `DELETE /v1/executions/{id}` | API | `204` tras matar el contenedor; `404`; `409` si ya no está en curso |
| `GET /v1/admin/executions?state=uncertain` | admin | listado por estado |
| `GET /v1/admin/executions/{id}` | admin | detalle |
| `POST /v1/admin/executions/{id}/reconcile` | admin | cierra una ejecución incierta con la evidencia del motor (`podman inspect`); `409 no_evidence` si el contenedor ya no existe |
| `POST /v1/admin/executions/{id}/resolve` `{"outcome":"failed","reason":…}` | admin | la declara fallida; **no existe** la resolución manual como exitosa |

Orden del `POST`: token, origen, preflight, validación de la solicitud contra la lista y los techos, verificación de tamaño y digest, escaneo del archivo, rutas presentes en el archivo, plaza de concurrencia, reclamación idempotente, contenedor, resultado persistido. Lo que falla antes de la reclamación no deja rastro; lo reclamado queda registrado y la misma solicitud recibe después la misma respuesta.

## Forma del contenedor y cuotas

```
podman run --name agentforge-<id> --interactive --network none --no-hosts --read-only
  --mount type=tmpfs,dst=/work,tmpfs-size=<storage_bytes>,tmpfs-mode=1777,noexec,nosuid,nodev
  --mount type=tmpfs,dst=/tmp,tmpfs-size=<storage_bytes>,tmpfs-mode=1777,noexec,nosuid,nodev
  --workdir /work --userns=keep-id --user <uid>:<gid> --cap-drop ALL --security-opt no-new-privileges
  --pids-limit <pids> --memory <memory+storage> --memory-swap <memory+storage> --cpus 1.0
  --ulimit cpu=<s>:<s> --ulimit nofile=1024:1024 --timeout <wall_seconds> --log-driver none
  --env-host=false --pull never --env HOME=/tmp --env TMPDIR=/tmp --env K=V (lista permitida)
  <imagen@sha256:…> python -I -c <bootstrap> <argv validado>
```

- **Cuota efectiva por ejecución.** `/work` es un tmpfs creado por el motor con el tamaño pedido; el workspace entra por `stdin` y lo extrae un bootstrap fijo con `tarfile` y el filtro `data`. El host nunca escribe el árbol no confiable; cada contenedor tiene su directorio propio; el kernel aplica la cuota (`ENOSPC`); todo desaparece con el contenedor. Las páginas de tmpfs se cargan al cgroup de memoria, por eso el límite del cgroup es `memory_bytes + storage_bytes`.
- **Memoria, procesos, CPU, tiempo.** cgroups v2 (`memory.max`, `pids.max`, `cpu.max`), `ulimit cpu` como techo de CPU y tiempo de pared impuesto por `--timeout` y por un temporizador del runner que mata el contenedor y, si hace falta, el cliente de podman.
- **Sonda de aislamiento.** El preflight lanza un contenedor con la misma forma y comprueba desde dentro: uid sin privilegios, raíz de solo lectura, `/work` escribible con el tamaño pedido, red bloqueada y solo `lo`, capacidades efectivas a cero, `no_new_privs`, `memory.max` y `pids.max` iguales a los pedidos. Si falla cualquier comprobación, `ready=false` y `503 isolation_unavailable`. No hay modo degradado.

## Ejecuciones inciertas

Una fila queda `uncertain` cuando el runner se reinicia con ejecuciones abiertas o cuando el motor ya no conserva el contenedor. Nunca se relanza sola: la API recibe `409 execution_uncertain` y su propia auditoría bloquea el paso. Un operador, con la credencial de administración:

1. `GET /v1/admin/executions?state=uncertain` para verlas.
2. `POST …/reconcile`: el runner consulta `podman inspect`; si el contenedor existe, cierra la fila con su código de salida y su estado OOM como evidencia (`resolution=engine:…`) y lo elimina.
3. `POST …/resolve {"outcome":"failed","reason":"…"}` cuando no hay evidencia. Declararla exitosa a mano no es posible.

Al arrancar, el runner hace lo mismo automáticamente con lo que el motor conserve: contenedores terminados se cierran con evidencia; los que siguen corriendo se matan y quedan `cancelled` con `runner_restarted`; los desaparecidos quedan `uncertain` con `container_missing`.

## Despliegue en Ubuntu Server 24.04 (no ejecutado todavía)

### 1. Preparación de la máquina

- VM dedicada: 4 vCPU, 8 GB de RAM, 60 GB de disco para `RUNNER_MAX_CONCURRENT=2` con los límites por defecto (1 GiB de memoria + 512 MiB de tmpfs por sandbox, todo en RAM); 16 GB para cuatro concurrentes. Sin agente con privilegios ni credenciales del host dentro.
- `apt install podman uidmap slirp4netns fuse-overlayfs unattended-upgrades ufw`; `ufw default deny incoming`; cgroups v2 ya es el valor por defecto.
- Reloj sincronizado (`systemd-timesyncd`), actualizaciones automáticas de seguridad.

### 2. Conexión privada y autenticación

- **Proxmox con Tailscale**: `tailscale up --advertise-tags=tag:agentforge-runner --ssh=false`; aplicar `deploy/tailscale-acl.example.json` (solo `tag:agentforge-api` → `tag:agentforge-runner:8787`); `ufw allow in on tailscale0 to any port 8787 proto tcp`; `RUNNER_BIND_HOST` = IP de Tailscale; en la API `SANDBOX_RUNNER_URL=http://<nombre>.ts.net:8787`.
- **VM local**: `RUNNER_BIND_HOST` = IP privada de la VM, `RUNNER_ALLOWED_CLIENT_CIDRS` = IP del host; en la API, `https://` con un certificado propio (`SANDBOX_RUNNER_CA_FILE`) o, solo en local, `SANDBOX_RUNNER_ALLOW_PRIVATE_HTTP=true`.
- **Nube**: subred privada, grupo de seguridad que solo admita la API en el puerto 8787, TLS terminado por un balanceador privado o por el propio runner detrás de un proxy; `RUNNER_ALLOWED_CLIENT_CIDRS` = CIDR de la API.
- En todos los casos el token es obligatorio: `agentforge-runner token` genera token y hash; el hash va a `RUNNER_TOKEN_SHA256` y el token a `SANDBOX_RUNNER_TOKEN`. Un segundo token para `RUNNER_ADMIN_TOKEN_SHA256`. Rotación: nuevo hash, reinicio del servicio, nuevo token en la API.

### 3. Podman rootless

- `adduser --disabled-password agentforge-runner`; `usermod --add-subuids 100000-165535 --add-subgids 100000-165535 agentforge-runner`; `loginctl enable-linger agentforge-runner`; sin `sudo`.
- Como ese usuario: `podman info --format '{{.Host.Security.Rootless}} {{.Host.CgroupVersion}} {{.Host.CgroupControllers}}'` debe mostrar `true v2` y los controladores `cpu`, `memory`, `pids`. Si falta alguno, habilitar la delegación en `/etc/systemd/system/user@.service.d/delegate.conf` (`Delegate=cpu cpuset io memory pids`).

### 4. Instalación y configuración del runner

- Clonar el repositorio en `~/agentforge` y `uv sync --package agentforge-runner --no-dev`.
- Copiar `deploy/runner.env.example` a `~/.config/agentforge-runner/runner.env` (modo 0600) y completar.
- Copiar `deploy/agentforge-runner.service` a `~/.config/systemd/user/`, `systemctl --user daemon-reload`, `systemctl --user enable --now agentforge-runner`.

### 5. Construcción de imágenes verificadas

- `podman build -t localhost/agentforge/python-3.13 -f runner/deploy/Containerfile.python-3.13 runner/deploy`; para el proyecto objetivo, exportar `uv export --no-dev --frozen > requirements.txt` y activar las líneas del Containerfile.
- Fijar la base también por digest en el Containerfile antes de construir en producción; anotar `podman image inspect --format '{{.Digest}}'` y ponerlo en `RUNNER_IMAGES`. La única fase con red es `podman build`.
- Reconstruir y volver a fijar al actualizar dependencias; el preflight rechaza una imagen ausente.

### 6. Pruebas de aislamiento reales

- `agentforge-runner preflight` debe imprimir todas las comprobaciones en `OK`, incluidas las `probe:*`, y terminar en `ready`.
- `RUNNER_TEST_IMAGE=localhost/agentforge/python-3.13@sha256:… uv run pytest tests/runner_integration -q` en la máquina: `ruff check` real, `pytest` que comprueba ausencia de red y raíz de solo lectura, límite de memoria, timeout de pared y `kill`. Fallan explícitamente si falta algo. También existe el workflow manual `runner-podman` en GitHub Actions.
- Hasta que estas pruebas pasen en una máquina real, el aislamiento no está demostrado; las pruebas unitarias usan un backend simulado.

### 7. Integración con AgentForge

- En la API: `SANDBOX_RUNNER_URL`, `SANDBOX_RUNNER_TOKEN`, `SANDBOX_RUNNER_ID` (y `SANDBOX_RUNNER_CA_FILE` si procede). `GET /v1/health` desde el adaptador debe devolver `verified=true`.
- `exec.run` sigue sin estar concedido a ningún agente y la puerta de riesgo sigue en `read`; la primera ejecución real se hace con una concesión explícita a un agente de prueba.

## Garantías y límites del aislamiento

Rootless convierte un escape del contenedor en el usuario `agentforge-runner` de la máquina, no en root. `--network none` elimina la red del sandbox. cgroups v2 hacen efectivos memoria, procesos y CPU; el tmpfs hace efectiva la cuota de disco por ejecución. No protege frente a un exploit del kernel, a canales laterales entre ejecuciones concurrentes ni a una imagen envenenada; por eso la máquina es dedicada y sin credenciales. La sonda prueba la forma en el host concreto, no la ausencia de vulnerabilidades.
