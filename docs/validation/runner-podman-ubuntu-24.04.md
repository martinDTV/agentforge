# Validación real del AgentForge Runner con Podman rootless (Ubuntu 24.04)

Evidencia de la primera validación del aislamiento en Linux real, ejecutada el 2026-10-25 en GitHub Actions (`ubuntu-24.04`) por el workflow `runner-podman` sobre el commit `349cc8d` (idéntico, en `runner/`, a lo fusionado en `main` por el PR #5). Las pruebas unitarias con backend simulado **no** son evidencia de aislamiento; este documento sí.

## Entorno

| Dato | Valor observado |
|---|---|
| Podman | 4.9.3 |
| Rootless | `true` |
| cgroups | v2, gestor `systemd` |
| Controladores delegados | `cpu`, `memory`, `pids` |
| Runtime OCI | `crun` |
| `subuid` / `subgid` | configurados para el usuario del job |
| Requisito previo | delegación para el gestor de usuario: `Delegate=cpu cpuset io memory pids` en `user@.service.d`, `enable-linger`, `XDG_RUNTIME_DIR` y `DBUS_SESSION_BUS_ADDRESS` |

## Preflight del runner con sonda dentro del contenedor

Todas las comprobaciones en `OK` en https://github.com/martinDTV/agentforge/actions/runs/40170680633:

`unprivileged_user`, `rootless`, `cgroups_v2`, `cgroup_controllers`, `podman_version`, `image:python-3.13`, `probe:uid_unprivileged`, `probe:root_readonly`, `probe:work_writable`, `probe:work_quota` (33554432 bytes, los pedidos), `probe:network_blocked` (interfaces `['lo']`), `probe:no_capabilities`, `probe:no_new_privs`, `probe:memory_max` (167772160, igual a memoria + tmpfs), `probe:pids_max` (32).

## Pruebas reales (`tests/runner_integration`)

| Prueba | Qué demuestra | Resultado |
|---|---|---|
| `test_ruff_check_runs_inside_the_sandbox` | `ruff check` real dentro del contenedor, con hallazgo esperado | PASSED |
| `test_pytest_sees_no_network_and_a_read_only_root` | `pytest` no puede abrir conexiones y no puede escribir en `/` | PASSED |
| `test_memory_limit_kills_the_process` | 256 MiB tocados con límite de 128 MiB + 16 MiB de tmpfs: el proceso muere | PASSED |
| `test_wall_clock_timeout_terminates_the_container` | tiempo de pared de 5 s termina el contenedor y se informa `timed_out` | PASSED |
| `test_kill_stops_a_running_container` | la cancelación mata el contenedor real | PASSED |
| `test_work_quota_is_enforced_by_the_tmpfs` | escribir 40 MiB en `/work` con cuota de 16 MiB falla con `ENOSPC` | PASSED |
| `test_each_container_gets_its_own_workspace` | dos ejecuciones no comparten `/work` | PASSED |
| `test_no_containers_are_left_behind` | ningún contenedor `agentforge-*` queda tras la suite | PASSED |

Historial de la validación, útil para entender qué corrigió cada iteración:

| Run | Commit | Resultado | Causa |
|---|---|---|---|
| https://github.com/martinDTV/agentforge/actions/runs/40170132841 | `d40297b` | fallo | plantilla Go del paso de diagnóstico del workflow |
| https://github.com/martinDTV/agentforge/actions/runs/40170309719 | `2e009ec` | 5 de 7 | el `--timeout` de Podman cortaba antes que el temporizador del runner y se clasificaba como `completed`; la prueba de memoria reservaba sin tocar páginas |
| https://github.com/martinDTV/agentforge/actions/runs/40170544529 | `d561b2a` | 7 de 8 | la comprobación de limpieza detectó el contenedor de la sonda, que `--rm=false` dejaba atrás |
| https://github.com/martinDTV/agentforge/actions/runs/40170680633 | `349cc8d` | **8 de 8** | — |

## Límites comprobados y no comprobados

Comprobado en esta máquina: rootless, cgroups v2 con los tres controladores, memoria y procesos aplicados y leídos desde dentro, CPU acotada, red deshabilitada, raíz de solo lectura, cuota de disco por ejecución, timeout, cancelación, separación de ejecuciones y limpieza.

No comprobado todavía: concurrencia bajo carga, comportamiento en una VM persistente con reinicios, red privada del despliegue (Tailscale), rotación de credenciales en caliente, imágenes construidas a partir de una base fijada por digest.

## Cómo reproducirlo

- En GitHub Actions: el workflow `runner-podman` se dispara en pull requests que tocan `runner/`, `tests/runner_integration/` o el propio workflow, y manualmente (`workflow_dispatch`) ahora que existe en `main`.
- En una máquina Linux: `agentforge-runner preflight` y `RUNNER_TEST_IMAGE=<imagen@sha256:…> uv run pytest tests/runner_integration -v`, según `runner/README.md` y `docs/runbooks/runner-proxmox.md`.
