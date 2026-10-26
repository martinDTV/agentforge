# CLAUDE.md — AgentForge

Reglas permanentes de este repositorio. Mantener conciso; reflejar solo lo que existe, no funcionalidad imaginada.

## Arquitectura FastAPI

Antes de implementar o modificar cualquier componente de FastAPI (routers, schemas, dependencias, configuración, persistencia, testing, seguridad), consultar la skill `/fastapi-architect:fastapi-architect` si está disponible en la sesión. Es la referencia principal de estándares técnicos para este proyecto, y sus estándares se aplican siempre, no se difieren como deuda: códigos HTTP correctos, excepciones de dominio mapeadas a Problem Details en `app/api/errors.py`, prefijo `/api/v1`, dependencias tipadas con `Annotated`, recursos creados en `lifespan` y `def` plano en endpoints bloqueantes.

- No reorganizar carpetas ni reescribir componentes solo para igualar visualmente la skill.
- Toda desviación intencional respecto a la skill debe documentarse como ADR en `docs/adr/`.
- Ver `docs/adr/0001-layout-por-capa-tecnica.md` para la decisión sobre por qué `app/` está organizado por capa técnica (`agents/`, `contracts/`, `infrastructure/`, `orchestration/`, `api/`) y no por módulo de negocio. Ver `docs/adr/0002-politica-de-errores-reintentos-y-presupuesto-llm.md` para la política de errores LLM, reintentos y presupuesto por run.

## Seguridad

`app/security/` no importa FastAPI: ahí viven `Principal`, la verificación de tokens y la política de autorización; `app/api/security.py` solo las adapta a dependencias. Los servicios reciben el principal como argumento. `AUTH_MODE=disabled` y `OIDC_JWKS_FILE` existen solo para local y pruebas y Settings los rechaza en staging/production. Nunca registrar tokens ni correos: en logs y auditoría se usa el sujeto opaco. Ver `docs/adr/0005-identidad-y-autorizacion.md`.

## Herramientas (Tool Gateway)

`app/tools/` solo importa `contracts` y `security`; la orquestación lo llama y `app/infrastructure/db` implementa su auditoría. Toda herramienta se ejecuta únicamente a través de `ToolGateway.invoke` con un `ToolContext` construido por código confiable: nunca a partir de texto del modelo, que solo aporta `ToolCall`. Los modelos de entrada declaran `extra="forbid"`. Los agentes actúan como la identidad de automatización `agentforge:<agente>` (rol `agent`, solo `tools:execute`), sin heredar credenciales ni entorno del host. En la Fase 5 solo la clase de riesgo `read` es ejecutable; no se añaden herramientas de escritura, ejecución o red sin el paso correspondiente aprobado. Las invocaciones se identifican por `(run_id, step, attempt)`: quien llame al gateway debe pasar un `step` determinista. Ver `docs/adr/0006-tool-gateway.md`.

Flujo de desarrollo (Fase 6): `app/security/development.py` define `DevelopmentScope` y `DevelopmentToolPolicy`, la única vía por la que Developer obtiene `workspace.write_file` y `exec.run` y QA `exec.run`, siempre ligada a un `run_id`, un proyecto `sandbox-*`, un worktree y una caducidad; `DEFAULT_AGENT_GRANTS` y `ENABLED_RISK_CLASSES` no se amplían. `app/tools/write_file.py` escribe archivos completos de una lista explícita con hash esperado; no se añaden herramientas de parche, shell ni Git de escritura. `app/orchestration/development.py` es el grafo Developer → QA con máximo tres correcciones, presupuesto `BudgetedLLM` reservado antes de cada llamada, tiempo de pared y fin explícito; un fallo técnico de QA nunca se convierte en aprobación y el éxito termina en `completed`, nunca en `approved` (decisión humana). El punto de entrada es `scripts/development_mvp.py` (operador, local, confirmación, Runner verificado); la API no expone este flujo. Las ejecuciones llevan `workflow_kind` en la fila (migración 0005) y la recuperación cierra las de desarrollo como `development_interrupted` sin reconducirlas. Todas las claves de estado de cualquier flujo se declaran en `RunState` para que el grafo de la API pueda leer cualquier checkpoint. Los archivos del Runner instalado solo se cambian con `scripts/stage_runner_deploy.py` y `scripts/deploy_runner_files.py` (manifiesto con digests, respaldo, self-test y reversión), nunca a mano. Ver `docs/adr/0008-flujo-developer-qa.md`.

Desarrollo gobernado (Fase 7, ADR 0009): `app/projects/` guarda la política por repositorio (`PROJECTS_FILE`), que es dato del operador y nunca llega al modelo; una ejecución `development` creada por la API exige `repos:write` (rol `project_operator` o `admin`), un `repository_id` con política y se detiene en la etapa `scope` hasta que un humano aprueba el digest exacto de la propuesta filtrada; `DevelopmentScope` se construye solo desde la lista aprobada. El Scoper (`AgentKind.SCOPER`) solo lee: su gateway no registra `exec.run` ni escritura. Las decisiones viven en `run_decisions` por etapa (`plan`, `scope`, `publish`); `run_approvals` se conserva y se escribe para `plan`. `publish.mode` solo admite `manual`; `repos:publish` no tiene rol. Los estados `cancelled` y `expired` son terminales. No existe `push` ni remoto en el Paso 1.

Git: `app/git/` define el puerto, la validación de referencias y el registro confiable de repositorios; `app/infrastructure/git/` ejecuta el binario sin shell, con argumentos construidos por la aplicación, entorno saneado y timeout. Ningún comando Git llega del modelo. Solo las ramas `run/<run_id>` son escribibles; `main`, la rama por defecto, `release/*` y las etiquetas están protegidas; el `push` está deshabilitado. Las pruebas usan repositorios temporales bajo `tmp_path` y nunca el repositorio de AgentForge ni otros del host. Los worktrees (`app/workspaces/`) viven bajo `WORKTREES_ROOT/<proyecto>/<run_id>/tree` con su marcador fuera del checkout.

Ejecución aislada: `app/sandbox/` define los contratos y la lista de programas (`ruff_check`, `ruff_format_check`, `pytest`); `argv` lo construye la aplicación, nunca el modelo. El único adaptador que ejecuta es el runner remoto (`app/infrastructure/sandbox/remote.py`); `UnconfiguredSandbox` rechaza y no existe ejecución local de reserva. La suite unitaria bloquea todo subproceso que no sea `git`. El servicio runner vive en `runner/` como paquete independiente (`agentforge_runner`): no importa `app/` ni `app/` lo importa; sus contratos son una copia del protocolo v1 y una prueba de conformidad mantiene ambas copias iguales. Las pruebas reales con Podman están en `tests/runner_integration/` y en el workflow manual `runner-podman`; no forman parte de `pytest` por defecto y las pruebas con `FakeBackend` nunca se presentan como evidencia de aislamiento. El dominio (`app/sandbox`, `app/tools`, `app/orchestration`) no menciona Tailscale, Proxmox ni Podman: el transporte privado es cosa del adaptador y del despliegue. Ver `docs/adr/0007-sandbox-ejecucion-aislada.md` y `runner/README.md`.

## GitHub App y espejos locales

`app/infrastructure/github/` es el único lugar que lee `GITHUB_APP_ID` y `GITHUB_APP_PRIVATE_KEY_FILE` y el único que posee tokens de instalación (en memoria, `contents:read` + `metadata:read`, una hora). Ningún módulo de `app/agents`, `app/tools`, `app/sandbox`, `app/security`, `app/git`, `app/workspaces`, `app/contracts`, `app/projects` ni `app/api` importa ese paquete; ningún tool expone GitHub ni `fetch`; los agentes siguen sin red, sin credenciales y sin `push`. La identidad de un repositorio remoto es su `github_repository_id` más `owner/name` en la política (`RemoteSpec`); la URL se deriva y nunca se configura. Los espejos (`MirrorService`) se sincronizan solo desde la plataforma, con `flock` por repositorio, `fetch` de `refs/heads/*` y la credencial entregada mediante `app/infrastructure/git/askpass.py` a ese único proceso Git; el `LocalGitAdapter` de los agentes conserva `GIT_ASKPASS=false`. El contenido remoto es dato: los archivos de instrucciones para agentes (`DEFAULT_EXCLUDED_CONTEXT_GLOBS`) no se muestran al Scoper ni pueden aprobarse como alcance. Las pruebas usan `tests/unit/github_fixtures.py::FakeGitHub` y remotos `file://` locales; jamás una App ni un repositorio real. Ver `docs/adr/0010-github-app-y-espejos-locales.md` y `docs/runbooks/github-app.md`.

## Proveedor LLM

Claude vía Vertex AI (`AnthropicVertex`) es el proveedor principal. No cambiar de proveedor sin autorización explícita. Los agentes dependen de `LLMPort` (Protocol), nunca instancian `VertexClaudeAdapter` directamente — reciben el adaptador por inyección.

## Fases de desarrollo

El proyecto avanza fase por fase, con aprobación explícita antes de iniciar cada una. No implementar funcionalidad de una fase futura mientras se trabaja en la actual. Ver roadmap en `README.md`.

## Pruebas

- `pytest` sin argumentos ejecuta solo `tests/unit` (`testpaths` en `pyproject.toml`): esa suite no puede depender de ningún servicio externo. Las pruebas que necesitan PostgreSQL viven en `tests/integration`, se ejecutan explícitamente y deben **fallar** con mensaje claro si no hay servidor, nunca saltarse. Los scripts de demostración viven en `scripts/`, protegidos con `if __name__ == "__main__":`.
- Ningún test automatizado debe llamar a Vertex AI ni a ningún proveedor LLM real. Usar `FakeLLMAdapter` inyectado explícitamente; `tests/conftest.py` bloquea la construcción del cliente real durante todo pytest.
- Migraciones de Alembic y `setup()` del checkpointer son pasos de despliegue: nunca se ejecutan en el arranque de FastAPI.

## Verificación obligatoria antes de reportar una tarea como completa

```
uv run ruff check .
uv run ruff format --check .
uv run mypy app
uv run mypy runner/agentforge_runner   # el runner se tipa aparte
uv run pytest -q
uv run pytest tests/integration -q   # requiere PostgreSQL accesible
```

La CI (`.github/workflows/ci.yml`) ejecuta exactamente estos comandos contra un servicio PostgreSQL; un cambio que no pase localmente no debe publicarse.

## Git

No crear commits ni hacer push sin autorización explícita del usuario, incluso si el código y las pruebas están en verde.
