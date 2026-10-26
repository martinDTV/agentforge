# Runbook: AgentForge Runner en el homelab Proxmox

Guía específica para desplegar el runner en una VM dedicada de Proxmox y ejecutar desde AgentForge la primera invocación real de `exec.run` con `ruff_check`. Todo lo que sigue es manual y reversible; nada de esto se ha ejecutado todavía. El runner nunca recibe credenciales de Proxmox, de GitHub, de la cuenta de Tailscale ni de ninguna nube: solo su propio token y el de administración.

Referencia de lo ya validado en Linux real (GitHub Actions, Ubuntu 24.04, Podman 4.9.3): rootless, cgroups v2 con `cpu`, `memory` y `pids`, red deshabilitada, raíz de solo lectura, cuota de `/work`, límite de memoria, timeout, cancelación, separación de ejecuciones y limpieza. La VM debe reproducir esas condiciones; el preflight del runner las comprueba antes de aceptar trabajo.

La guía distingue tres tipos de actividad. **Preparación** (Parte A): solo lectura y planificación; no crea ni modifica nada. **Verificación** (Parte C): comprobaciones reales dentro de la VM una vez exista; no expone servicios a la API. **Despliegue autorizado** (Partes B y D): cada paso requiere tu autorización explícita y queda registrado en el PR o en la bitácora de operación. Publicar este documento no autoriza ejecutarlo.

# Parte A. Preparación (sin autorización de despliegue)

## 1. Antes de crear nada: recursos libres del host

En el nodo Proxmox, como usuario con acceso a `pvesh` (no desde la VM del runner):

```
pvesh get /nodes/$(hostname)/status --output-format json | jq '{cpu: .cpu, cores: .cpuinfo.cores, mem_total_gb: (.memory.total/1e9|floor), mem_free_gb: (.memory.free/1e9|floor), load: .loadavg}'
pvesm status                      # espacio libre por almacenamiento
qm list                           # VMs existentes y su estado
cat /sys/fs/cgroup/cgroup.controllers   # el host también debe estar en cgroups v2
```

Requisito mínimo para la VM: 4 vCPU, 8 GB de RAM y 60 GB en un almacenamiento con margen. Si el host no tiene 8 GB libres de forma sostenida, reducir `RUNNER_MAX_CONCURRENT` a 1 y la VM a 4 GB; no reducir el aislamiento.

## 2. Topología de red segura

- La VM va en el puente habitual de la LAN, sin reenvío de puertos desde el router y sin IP pública.
- El único servicio expuesto es el runner, y solo en la interfaz de Tailscale (`RUNNER_BIND_HOST` = IP del tailnet). `ufw` deniega todo lo entrante salvo `tailscale0` al puerto 8787.
- Acceso de administración por SSH desde tu equipo solo por la LAN o por Tailscale; Tailscale SSH desactivado en el nodo del runner.
- Opcional, recomendable: VLAN o bridge separado para la VM del runner de modo que no alcance otros servicios del homelab aunque un sandbox escapara. Los sandboxes no tienen red; esta medida protege frente al propio runner.
- Ninguna ruta de la VM hacia la interfaz de gestión de Proxmox.


# Parte B. Despliegue autorizado, bloque 1: la máquina y el runner

Requiere tu autorización para crear la VM y modificar Tailscale. Hasta entonces, nada de esta parte se ejecuta.

## 3. VM Ubuntu Server 24.04 dedicada

- Plantilla cloud-init de Ubuntu Server 24.04 LTS; `qm create` con 4 cores, 8192 MB, disco de 60 GB en `virtio-scsi`, `qemu-guest-agent` instalado dentro de la VM.
- Sin discos ni carpetas compartidas con el host; sin passthrough.
- Usuario de administración con clave SSH tuya, distinto del usuario del runner.
- Dentro: `apt update && apt full-upgrade`, `unattended-upgrades`, `systemd-timesyncd` activo, `ufw` con `default deny incoming`.
- Snapshot de la VM al terminar la instalación base, antes de instalar el runner.

## 4. Podman rootless y cgroups v2

```
apt install -y podman uidmap slirp4netns fuse-overlayfs crun
adduser --disabled-password --gecos "" agentforge-runner
usermod --add-subuids 100000-165535 --add-subgids 100000-165535 agentforge-runner
loginctl enable-linger agentforge-runner
mkdir -p /etc/systemd/system/user@.service.d
printf '[Service]\nDelegate=cpu cpuset io memory pids\n' > /etc/systemd/system/user@.service.d/delegate.conf
systemctl daemon-reload
```

Comprobar como `agentforge-runner` (`machinectl shell agentforge-runner@` o `ssh`):

```
podman info --format json | jq '{rootless: .host.security.rootless, cgroupVersion: .host.cgroupVersion, cgroupManager: .host.cgroupManager, controllers: .host.cgroupControllers, runtime: .host.ociRuntime.name, version: .version.Version}'
```

Debe mostrar `rootless: true`, `v2`, `systemd`, los controladores `cpu`, `memory`, `pids` y `crun`. Es exactamente lo que el runner exige en el preflight.

## 5. Tailscale y ACL

- `tailscale up --advertise-tags=tag:agentforge-runner --ssh=false` en la VM; el host de la API con `tag:agentforge-api`.
- ACL (panel de Tailscale), a partir de `runner/deploy/tailscale-acl.example.json`: la única regla de datos es `tag:agentforge-api` → `tag:agentforge-runner:8787`; el runner no inicia conexiones.
- `ufw allow in on tailscale0 to any port 8787 proto tcp`.
- Anotar el nombre MagicDNS del runner; será `SANDBOX_RUNNER_URL=http://<nombre>.<tailnet>.ts.net:8787`. El adaptador solo acepta `http://` hacia el tailnet o loopback; cualquier otra red requiere `https://`.

## 6. Instalación del runner

Como `agentforge-runner`:

```
git clone https://github.com/martinDTV/agentforge.git ~/agentforge   # clon de solo lectura, sin token
cd ~/agentforge && uv sync --package agentforge-runner --no-dev
mkdir -p ~/.config/agentforge-runner ~/.config/systemd/user
cp runner/deploy/runner.env.example ~/.config/agentforge-runner/runner.env && chmod 600 ~/.config/agentforge-runner/runner.env
cp runner/deploy/agentforge-runner.service ~/.config/systemd/user/
```

Rellenar `runner.env`: `RUNNER_RUNNER_ID` (por ejemplo `agentforge-runner-01`), `RUNNER_BIND_HOST` (IP de Tailscale), `RUNNER_ALLOWED_CLIENT_CIDRS=["100.64.0.0/10"]`, `RUNNER_STATE_DIR` bajo `~/.local/share/agentforge-runner`.

## 7. Imagen fijada por digest

```
podman build -t localhost/agentforge/python-3.13 -f runner/deploy/Containerfile.python-3.13 runner/deploy
podman image inspect --format '{{.Digest}}' localhost/agentforge/python-3.13
```

Antes de construir en la VM, fijar también la base del Containerfile por digest (`FROM docker.io/library/python:3.13-slim@sha256:…`, obtenido con `skopeo inspect` o `podman image inspect`). Poner el digest resultante en `RUNNER_IMAGES`. La construcción es la única fase con red. Para repositorios objetivo con dependencias, exportar `uv export --no-dev --frozen > requirements.txt` y activar las líneas del Containerfile; una imagen por perfil.

## 8. Credenciales y rotación

```
uv run agentforge-runner token    # token de la API: el hash va a RUNNER_TOKEN_SHA256
uv run agentforge-runner token    # token de administración: el hash va a RUNNER_ADMIN_TOKEN_SHA256
```

- Los tokens solo existen en la API (`SANDBOX_RUNNER_TOKEN`) y en tu gestor de contraseñas para el de administración; la VM guarda únicamente los hashes.
- Rotación: generar un token nuevo, sustituir el hash en `runner.env`, `systemctl --user restart agentforge-runner`, actualizar la API. Ventana de corte de segundos. `scripts/runner_token_rotation.py` lo hace de forma atómica a partir de un archivo privado con solo los digests.
- Actualización de módulos del paquete instalado: `uv run python -m scripts.stage_runner_deploy` prepara un directorio con los archivos, un manifiesto de digests (instalado y deseado), los módulos de compatibilidad y un self-test, e imprime los pasos; `deploy_runner_files.py install` se ejecuta una sola vez como el usuario del Runner, verifica que lo instalado coincide con el manifiesto, respalda, reemplaza de forma atómica, reinicia, ejecuta el self-test y revierte si algo falla; `rollback <respaldo>` restaura. La unidad y `runner.env` nunca se escriben.
- Rotar también la clave del nodo en Tailscale si la VM se considera comprometida; revocar el nodo desde el panel.

Arranque: `systemctl --user daemon-reload && systemctl --user enable --now agentforge-runner`; `journalctl --user -u agentforge-runner -f`.

# Parte C. Verificación en la VM

Sin exponer todavía el runner a la API: solo el preflight y la suite real, ejecutadas en la propia VM.

## 9. Pruebas reales en la VM

```
uv run agentforge-runner preflight            # todas las comprobaciones en OK, incluidas probe:*
RUNNER_TEST_IMAGE=localhost/agentforge/python-3.13@sha256:<digest> uv run pytest tests/runner_integration -v -p no:cacheprovider
podman ps -a                                   # vacío al terminar
```

Son las ocho pruebas que pasaron en GitHub Actions. Repetirlas aquí demuestra la VM concreta, no el modelo. Si el preflight falla, no continuar: el runner no acepta trabajo y no hay modo degradado.

Prueba de concurrencia mínima que todavía falta: lanzar dos `pytest` con `time.sleep` simultáneos desde dos `curl` con tokens válidos y comprobar que ambos corren, que un tercero recibe `503 runner_busy` con `RUNNER_MAX_CONCURRENT=2`, y que `podman stats` muestra los límites.

# Parte D. Despliegue autorizado, bloque 2: conexión desde AgentForge y primera invocación

Requiere que la Parte C esté en verde y tu autorización para configurar la API.

## 10. Conexión desde AgentForge

En el `.env` de la API, en local o donde corra:

```
SANDBOX_RUNNER_URL=http://<nombre>.<tailnet>.ts.net:8787
SANDBOX_RUNNER_TOKEN=<token de la API>
SANDBOX_RUNNER_ID=agentforge-runner-01
```

Comprobación sin agentes ni ejecución, desde la API (Parte C ya en verde):

```
uv run python -c "from app.settings import get_settings; from app.infrastructure.sandbox import RemoteSandboxAdapter; print(RemoteSandboxAdapter.from_settings(get_settings()).health())"
```

Debe devolver `verified=True`, `backend='podman-rootless'`, `rootless=True` y `ready=True`.

## 11. Primera invocación `ruff_check` desde AgentForge

Condiciones: los agentes siguen sin herramientas y la puerta global de riesgo sigue en `read`. La primera ejecución la hace un operador, con un gateway construido a propósito, sobre un repositorio administrado de prueba, nunca sobre este repositorio ni los tuyos.

1. Repositorio de prueba: bajo `GIT_REPOSITORIES_ROOT`, un `bare` sembrado con un archivo Python que tenga un hallazgo de `ruff` (por ejemplo `import os` sin usar) y registrado en `GIT_REPOSITORIES_FILE` con `id=demo`, `project_id=sandbox-demo` y `authorized_test_paths=["tests"]`.
2. Worktree: `WorktreeManager.ensure(run_id, "sandbox-demo", "demo")` sobre un `run_id` nuevo; será `workspace_root`.
3. Política de operador: `ToolAuthorizationPolicy(AuthorizationPolicy(), {AgentKind.DEVELOPER: AgentGrant(tools={"exec.run"}, risks={RiskClass.READ, RiskClass.EXEC})}, enabled_risks={RiskClass.READ, RiskClass.EXEC})`. Es la única apertura de `exec`, local al script, con un agente de prueba y sin tocar la política por defecto de la aplicación.
4. Gateway: `ToolGateway(default_registry(limits, GitTools(adapter, manager, limits), ExecRunTool(RemoteSandboxAdapter.from_settings(settings), manager, git=adapter, policy=ExecutionPolicy.from_settings(settings))), policy, SqlAlchemyToolInvocationRepository(db.session_factory))`, con la auditoría en PostgreSQL para que la invocación quede registrada.
5. Invocación: `gateway.invoke(context, ToolCall(name="exec.run", arguments={"program": "ruff_check", "paths": ["src"]}), step="smoke:ruff")` con un `ToolContext` del agente de prueba y el principal de `AutomationIdentity`.
6. Resultado esperado: `outcome=ok`, `status=completed`, `exit_code=1` por el hallazgo, `stdout` con la línea de `ruff`, `runner_id` igual al configurado; en `tool_invocations` una fila `ok` con `risk=exec`; en el runner `GET /v1/executions/<id>` en `finished`; `podman ps -a` vacío.
7. Repetir la misma llamada con el mismo `step`: `denied` con `duplicate_invocation` y sin segundo contenedor. Con `step` nuevo y `paths=["../etc"]`: `failed` con `path_invalid` y sin llegar al runner.
8. Al terminar, cerrar la política de operador; nada queda habilitado para los agentes.

Propuesta: materializar los pasos 3 a 7 como `scripts/sandbox_smoke.py`, protegido con `if __name__ == "__main__":`, que lea la configuración del runner de `.env`, exija `ENVIRONMENT=local` y se niegue a ejecutarse contra la política por defecto. Forma parte del alcance de cierre a aprobar.

## Qué no hace esta guía

No crea la VM, no modifica Proxmox, Tailscale ni la red, no habilita `exec` para agentes reales ni toca Developer, QA o Security. Cada paso requiere tu autorización.

