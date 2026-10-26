# 0008. Flujo Developer + QA sobre worktrees aislados (Fase 6)

- Status: accepted (2026-10-26). La aceptación cubre **únicamente el MVP de operador local** sobre repositorios desechables `sandbox-*`, con las limitaciones listadas en Consequences. **No autoriza** usar el flujo con repositorios reales, exponerlo por la API ni desplegarlo en staging o producción: cada una de esas extensiones requiere su propia decisión y su propio ADR o revisión
- Date: 2026-10-26
- Deciders: martin, Claude (Fase 6)

## Context

Hasta la versión 0.5.0 el grafo de AgentForge terminaba en una aprobación humana del plan y ningún agente usaba herramientas: la puerta global de riesgo solo habilitaba `read`, `exec.run` no estaba concedido a nadie y no existía ninguna herramienta de escritura. La Fase 6 pide el primer flujo real de colaboración: Developer modifica código en un worktree aislado, QA lo valida en el Runner remoto y devuelve observaciones, con un máximo de tres correcciones.

Restricciones vigentes (ADR 0005, 0006 y 0007, reglas del usuario): ningún comando llega del modelo; la ejecución solo ocurre en el Runner con Podman rootless; la autorización nace de un contexto confiable y nunca de parámetros enviados por clientes ni de texto del modelo; `exec.run` no se concede de forma global; los repositorios reales no se tocan hasta demostrar el flujo en uno desechable.

## Decision

### Alcance de autoridad efímero (`app/security/development.py`)

`DevelopmentScope` describe la única autoridad que existe durante una ejecución de desarrollo: `run_id`, proyecto, repositorio, `workspace_root` (el worktree de esa ejecución), caducidad, la lista explícita de archivos escribibles (máximo 16, en forma canónica `a/b.py`, sin `..`, absolutos ni barras finales; se valida al construir) y un indicador de revocación. Rechaza proyectos y repositorios que no empiecen por `sandbox-`. `revoke()` retira la autoridad de inmediato, sea cual sea el reloj; el punto de entrada la revoca al terminar la ejecución, incluso ante cancelación o rechazo, de modo que un hilo rezagado no pueda usarla.

`DevelopmentToolPolicy` envuelve la política de herramientas existente: antes de los pasos habituales (identidad de automatización, permiso `tools:execute`, proyecto, concesión por agente, puerta de riesgo) exige que el contexto coincida con el alcance; cualquier otro `run_id`, proyecto, worktree, una caducidad vencida o la revocación devuelven `development_scope_denied`. Dentro del alcance, Developer tiene `workspace.*` de lectura, `git.*` de lectura, `workspace.write_file` y `exec.run`; QA tiene lo mismo menos la escritura. Las concesiones predeterminadas (`DEFAULT_AGENT_GRANTS`) y `ENABLED_RISK_CLASSES` del servicio no cambian: la API sigue sin habilitar `write` ni `exec` y nunca construye este alcance.

### Escritura acotada (`app/tools/write_file.py`)

`workspace.write_file` (riesgo `write`) reemplaza un archivo completo. No interpreta parches ni ejecuta nada. Exige: invocación a través del gateway, alcance vigente, repositorio del worktree igual al del alcance, ruta exactamente en la lista escribible y en forma canónica, sin directorios reservados ni nombres sensibles, contenido UTF-8 sin NUL, de 32 KiB como máximo y sin patrones de credenciales. Recorre el árbol con descriptores de directorio y `O_NOFOLLOW`, rechaza enlaces simbólicos y archivos con más de un enlace duro, compara el SHA-256 del archivo existente con `expected_sha256` (un archivo nuevo exige `null`; una diferencia es `write_conflict`) y escribe de forma atómica con `fsync` y `os.replace`.

### Gateway: cancelación con efectos

El gateway no abandona el hilo de una herramienta `write` al cancelar: espera a que termine. Una invocación cancelada de una herramienta de solo lectura se audita como `failed/cancelled`; la de una herramienta con efectos (`write`, `exec`) como `uncertain/cancelled`, porque su efecto pudo producirse, y un intento posterior del mismo paso se rechaza con `uncertain_effects`. Para `exec.run` la cancelación pide al Runner detener el contenedor.

### Grafo (`app/orchestration/development.py`)

`START → developer → apply_changes → qa → END`, con `qa → revise_development → developer` mientras QA no apruebe y queden correcciones. Reglas:

- Developer lee a través del gateway los archivos de contexto y los escribibles (uno escribible puede no existir todavía: se le pide crearlo) y devuelve el contenido completo de cada archivo que cambia; toda ruta fuera de la lista termina la ejecución con `path_denied`.
- `apply_changes` escribe con el hash observado al leer y ejecuta `ruff_check` y `ruff_format_check` en el Runner como Developer.
- QA relee, toma el diff, ejecuta `ruff_check`, `ruff_format_check` y `pytest` en el Runner y luego pide la revisión al modelo. Si algún programa termina con código distinto de cero, el orquestador fuerza `approved=false` y añade observaciones `technical_checks_failed`: ningún dictamen del modelo convierte un fallo técnico en éxito. Con `ruff_diff` activado y un fallo de `ruff_check`, QA ejecuta además `ruff_check_diff` (`ruff check --diff`, solo lectura; el Runner rechaza `--fix` y `--unsafe-fixes` con `argv_rejected`) y adjunta la corrección exacta; si un Runner antiguo no conoce el programa, el adjunto se marca como no disponible y nada más cambia. El programa está desplegado en el Runner de Proxmox desde el 2026-10-26.
- Developer recibe en cada corrección la revisión de QA, los resultados técnicos literales y `repeated_failures`: los programas que fallaron de forma idéntica en las dos últimas rondas de QA, con la instrucción de cambiar de hipótesis.
- Finalización explícita: `completed` (QA aprueba con todos los programas en cero), `correction_limit` (tres correcciones agotadas), código de la herramienta o del Runner que falló, `development_budget_exceeded`, `development_timeout`, `unsafe_model_context` (lectura truncada o con secretos) o cancelación. No existe reintento implícito.
- Límites: `max_corrections ≤ 3`; `BudgetedLLM` reserva, antes de cada llamada y bajo un cerrojo, bytes del prompt más esquema más la salida máxima, multiplicado por el número de turnos que el adaptador puede emitir (reparaciones incluidas), con cota de llamadas, de tokens reservados, de coste (a 20 USD por millón, muy por encima de la tarifa de referencia) y de tamaño de prompt; tiempo de pared por ejecución, tiempo por llamada al modelo (por defecto nunca inferior al peor caso del adaptador con sus reintentos y reparaciones) y timeout del gateway por herramienta.

### Estado, persistencia y recuperación

El flujo se ejecuta con el mismo `RunService`, repositorio de `runs`, `run_events`, `tool_invocations` y checkpointer PostgreSQL que la API. La migración 0005 añade `runs.workflow_kind` (`planning` para toda fila anterior, `development` para este flujo) y el estado terminal `completed`; el downgrade restaura la restricción anterior y se niega mientras exista alguna fila `completed`, para no reescribir datos. `approved` y `rejected` siguen significando una decisión humana; QA que aprueba termina en `completed`. Todas las claves de estado de cualquier flujo se declaran en `RunState`, así el grafo de planificación de la API puede leer un checkpoint de desarrollo, derivar su estado y reconstruir su auditoría. Se añaden los eventos `developer_completed`, `developer_applied`, `qa_reviewed` y `run_completed`, también reconstruibles desde el historial de checkpoints. El barrido de recuperación y la reanudación manual cierran toda ejecución de desarrollo no terminada como `failed/development_interrupted` sin reconducirla: repetir escrituras, llamadas facturables o programas de resultado incierto no es aceptable sin una nueva autorización; una ejecución terminada que no llegó a proyectarse simplemente se proyecta.

### Contenido no confiable

El modelo nunca elige herramientas ni argumentos: el orquestador decide cada invocación y el modelo solo aporta, en JSON validado, contenidos de archivos de la lista y texto de revisión. El contenido del repositorio y las salidas de los programas llegan a los prompts como datos, acotados y redactados; una lectura truncada o con secretos aborta la ejecución. Lo peor que puede lograr una inyección desde el repositorio es código malicioso en los archivos autorizados, que solo se ejecuta dentro del sandbox sin red.

### Punto de entrada

El MVP se invoca con `scripts/development_mvp.py` desde la máquina del operador, nunca desde la API, con perfiles de repositorio desechable (`mailcheck`, `lint`): solo en `ENVIRONMENT=local`, con Runner configurado, verificado y listo, repositorio registrado bajo `GIT_REPOSITORIES_ROOT` en un proyecto `sandbox-*`, confirmación del operador, Claude vía Vertex salvo `--fake-model` explícito (y rechazo si `USE_FAKE_MODEL=true`). Los cambios del Runner instalado se preparan con `scripts/stage_runner_deploy.py` y se aplican con `scripts/deploy_runner_files.py`, con manifiesto de digests, respaldo, self-test y reversión; ninguna credencial se lee ni se imprime.

## Consequences

- Positivas: primer flujo multiagente real con todas las acciones auditadas por paso, worktree por ejecución, escrituras acotadas por lista y hash, validación técnica en el Runner, bucle de corrección acotado y estado terminal propio; las políticas globales del servicio permanecen cerradas; la recuperación y la auditoría funcionan desde cualquier proceso.
- Negativas y limitaciones: (1) el Developer devuelve archivos completos (máximo ocho por revisión, 32 KiB cada uno), suficiente para el MVP y no para refactorizaciones grandes; (2) el flujo no está expuesto por la API: hacerlo exige configurar por proyecto qué repositorios y rutas pueden escribirse y una aprobación humana previa, decisión pendiente; (3) la ejecución de desarrollo interrumpida se cierra como fallida en lugar de reanudarse; (4) una llamada al modelo que supera el tiempo de espera del orquestador puede seguir ejecutándose en el proveedor hasta su propio timeout, por eso el tiempo por llamada no baja del peor caso del adaptador; (5) el coste se acota por reserva conservadora, no por medición previa; (6) el downgrade de 0005 exige resolver antes las filas `completed`; (7) el `backend.py` instalado en el Runner conserva la anotación `remove() -> None` de `HEAD` mientras la implementación devuelve `bool`, deriva sin efecto en tiempo de ejecución que se cierra con el siguiente despliegue completo del paquete.
