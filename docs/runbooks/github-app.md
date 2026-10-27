# Runbook: GitHub App de AgentForge e instalación en `agentforge-sandbox-demo`

Objetivo: crear la GitHub App dedicada de AgentForge, instalarla **únicamente** en el
repositorio privado y desechable `agentforge-sandbox-demo`, entregar la clave privada al
host de la API con permisos mínimos y crear el primer espejo local. Nada de esto lo
hace AgentForge por sí mismo: la App, la instalación y el repositorio los crea una
persona con su propia sesión de GitHub. Referencia de diseño: ADR 0010.

Resultado esperado al terminar: `scripts/mirror_repository.py --check` responde `"result": "ok"`
con el `github_repository_id` de la política, una fila `check` en `mirror_syncs`, y ningún
token en logs, auditoría ni archivos.

## Qué necesito de ti y qué no debes enviarme

Solo tú creas el repositorio, la App y la instalación, y solo tú descargas la clave
privada. Cuando termines los pasos 1 a 4, pásame **únicamente estos valores públicos**:

| Valor | Dónde se ve | Ejemplo |
| --- | --- | --- |
| `owner` | Dueño del repositorio (cuenta u organización) | `martinDTV` |
| `github_repository_id` | `gh api repos/<owner>/agentforge-sandbox-demo --jq .id` | `1056789012` |
| App ID | Cabecera de la página de la App (*Settings → Developer settings → GitHub Apps → agentforge-sandbox*) | `1234567` |
| `installation_id` | Último segmento de la URL de la instalación: `.../settings/installations/<id>` | `78901234` |
| Ruta local de la clave | La que elijas en el paso 4 | `/Users/martin/.config/agentforge/agentforge-sandbox.private-key.pem` |

**No me envíes ni pegues** el contenido del `.pem`, ningún token (`ghs_…`, `ghp_…`),
ni el *client secret* (no hace falta generarlo). Yo no leeré la clave: el preflight solo
comprueba su modo, propietario y ubicación, y la App la usa en memoria para firmar.

## 0. Antes de empezar

- La rama `feat/phase-7-real-repositories` con el Paso 2 desplegada en el host de la API
  (local del operador en esta fase), migraciones aplicadas hasta `0007`:
  `uv run alembic upgrade head`.
- Variables ya operativas del Paso 1: `GIT_REPOSITORIES_ROOT`, `GIT_REPOSITORIES_FILE`,
  `WORKTREES_ROOT`, `PROJECTS_FILE`. Todas rutas absolutas y **en el `.env`** (la demostración
  del Paso 1 pasaba `PROJECTS_FILE` por entorno; los scripts de este paso leen el `.env`).
- Decide los identificadores locales. En este runbook: `project_id = sandbox-github`,
  `repository_id = sandbox-github` (`sandbox-demo` ya existe en el registro local de la Fase 5). Los proyectos del MVP deben empezar por `sandbox-`
  (`DevelopmentScope` lo exige).

## 1. Crear el repositorio desechable

1. En GitHub, con la cuenta u organización que será propietaria del sandbox
   (`<owner>`), crea `agentforge-sandbox-demo` **privado**, con rama por defecto `main` y
   un `README.md` inicial. No añadas secretos, workflows ni colaboradores.
2. Añade un contenido mínimo de prueba desde tu máquina (nunca desde AgentForge):
   `src/textkit/__init__.py`, `src/textkit/slug.py`, `tests/test_slug.py`, `pyproject.toml`
   con `ruff` y `pytest` (puedes copiar el proyecto demo de `scripts/development_mvp.py`).
3. Anota el **id numérico** del repositorio. Es la identidad estable que la política fija:

   ```bash
   gh api repos/<owner>/agentforge-sandbox-demo --jq '{id, full_name, default_branch, private}'
   ```

   Debe devolver `private: true` y `default_branch: "main"`.

## 2. Crear la GitHub App

Ruta: *Settings → Developer settings → GitHub Apps → New GitHub App* (en la cuenta
personal) o *Organization settings → Developer settings → GitHub Apps* (en la
organización propietaria del sandbox).

| Campo | Valor |
| --- | --- |
| GitHub App name | `agentforge-sandbox` (o similar; debe ser único en GitHub) |
| Homepage URL | URL del repositorio de AgentForge o cualquier URL informativa |
| Webhook | **Desactivado** (desmarca *Active*; sin URL ni secreto). En esta fase AgentForge solo consulta |
| Repository permissions | `Contents: Read-only`, `Metadata: Read-only` (Metadata se marca sola) |
| Organization permissions | ninguno |
| Account permissions | ninguno |
| Subscribe to events | ninguno |
| Where can this GitHub App be installed? | **Only on this account** |

No concedas `Workflows`, `Administration`, `Secrets`, `Actions`, `Members`, `Pull requests`
ni `Issues`. `Pull requests: write` y `Contents: write` se pedirán en el Paso 3 con su
propio runbook; sin `Workflows` GitHub rechaza cualquier cambio bajo `.github/workflows`
aunque todo lo demás fallara.

Si la App ya existía sin permisos (o con otros), cámbialos en *Permissions & events*, guarda,
y después **acepta la solicitud de permisos en la instalación** (Settings → Applications →
Installed GitHub Apps → Configure): hasta entonces la instalación sigue con los permisos
anteriores y el canje devuelve 422. El preflight exige exactamente `contents: read` y
`metadata: read`: con `contents: write` falla `installation_restricted` aunque la plataforma
solo pida lectura.

Tras crear la App:

1. Anota el **App ID** (cabecera de la página de la App).
2. En *Private keys → Generate a private key*, descarga el `.pem`. GitHub lo genera una
   sola vez; si se pierde, se genera otra y se revoca la anterior.
3. En *Optional features* no actives nada. Deja *Client secrets* sin generar: AgentForge
   no usa OAuth de usuario.

## 3. Instalar la App solo en el sandbox

1. En la página de la App, *Install App* → elige la cuenta propietaria del sandbox.
2. En *Repository access* marca **Only select repositories** y selecciona únicamente
   `agentforge-sandbox-demo`. Nunca *All repositories*.
3. Instala. La URL resultante termina en `/settings/installations/<installation_id>`;
   anota ese `installation_id`.
4. Comprueba la instalación desde tu sesión (no desde AgentForge):

   ```bash
   gh api /user/installations --jq '.installations[] | {id, app_slug, repository_selection}'
   # repository_selection debe ser "selected"
   gh api /user/installations/<installation_id>/repositories --jq '.repositories[].full_name'
   # debe listar solo <owner>/agentforge-sandbox-demo
   ```

## 4. Entregar la clave privada al host de la API sin mostrarla

Elige una ruta **fuera** de `GIT_REPOSITORIES_ROOT`, de `WORKTREES_ROOT` y del repositorio
de AgentForge; en local, `~/.config/agentforge/agentforge-sandbox.private-key.pem`.
El `.gitignore` del repositorio ya excluye `*.pem` y `*.private-key.*`, pero la clave no
debe estar dentro del árbol de trabajo de todos modos.

```bash
install -d -m 0700 ~/.config/agentforge
# El navegador deja la descarga en ~/Downloads con un nombre como
# agentforge-sandbox.2026-10-26.private-key.pem; muévela sin abrirla ni imprimirla:
install -m 0600 ~/Downloads/agentforge-sandbox.*.private-key.pem \
  ~/.config/agentforge/agentforge-sandbox.private-key.pem
rm -P ~/Downloads/agentforge-sandbox.*.private-key.pem 2>/dev/null \
  || rm ~/Downloads/agentforge-sandbox.*.private-key.pem
ls -l ~/.config/agentforge/           # debe mostrar -rw------- y tu usuario
head -c 0 ~/.config/agentforge/agentforge-sandbox.private-key.pem && echo "legible por el proceso"
```

No ejecutes `cat`, `pbcopy` ni pegues la clave en chats, issues o variables de entorno:
Settings solo recibe la **ruta**. Settings rechazan arrancar si el archivo no es regular,
no es `0600`, no es tuyo, la ruta es relativa o vive bajo alguna raíz de Git. No copies
la clave al Runner, a la VM de Proxmox ni al sandbox: solo el proceso de la API la lee.

Añade al `.env` del host de la API (el `.env` está ignorado por Git):

```dotenv
GITHUB_APP_ID=<app_id>
GITHUB_APP_PRIVATE_KEY_FILE=/Users/<usuario>/.config/agentforge/agentforge-sandbox.private-key.pem
# GITHUB_API_URL=https://api.github.com   (valor por defecto)
```

## 5. Declarar el repositorio en la política y en el registro

`PROJECTS_FILE` (política, fuente de la identidad remota):

```json
{
  "repositories": [
    {
      "repository_id": "sandbox-github",
      "project_id": "sandbox-github",
      "writable_globs": ["src/**", "tests/**"],
      "test_paths": ["tests"],
      "lint_paths": ["src", "tests"],
      "max_files_per_run": 6,
      "remote": {
        "provider": "github",
        "owner": "<owner>",
        "name": "agentforge-sandbox-demo",
        "github_repository_id": <id numérico del paso 1>,
        "installation_id": <installation_id del paso 3>,
        "default_branch": "main"
      }
    }
  ]
}
```

`GIT_REPOSITORIES_FILE` (registro de repositorios que usan los worktrees). La ruta es la
que `MirrorService` deriva: `GIT_REPOSITORIES_ROOT/<project_id>/<name>.git`:

```json
{
  "repositories": [
    {
      "id": "sandbox-github",
      "project_id": "sandbox-github",
      "path": "/ruta/absoluta/GIT_REPOSITORIES_ROOT/sandbox-github/agentforge-sandbox-demo.git",
      "default_branch": "main"
    }
  ]
}
```

El registro valida que la ruta exista al cargarse, así que el orden es: preflight (paso 6)
y creación del espejo (paso 7) con el registro **sin** esa entrada, y añadir la entrada
después (el preflight con la entrada ya presente comprueba que la ruta coincide). `scripts/mirror_repository.py` acepta un registro sin la entrada; si la entrada
existe y apunta a otra ruta, rechaza con `mirror_path_mismatch`.

## 6. Preflight: verificar App, instalación, repositorio y remoto sin crear nada

```bash
uv run alembic upgrade head   # 0007: tabla mirror_syncs
uv run python -m scripts.github_preflight --repository sandbox-github --project sandbox-github
```

Es de solo lectura y debe terminar con `"ok": true` y estas comprobaciones:

| `check` | Qué confirma |
| --- | --- |
| `private_key` | archivo regular, `0600`, de tu usuario, fuera de las raíces (no lee su contenido) |
| `policy` | la política tiene `remote` y muestra `owner/name`, id, instalación, rama y URL derivada |
| `registry` | si el espejo ya está en `GIT_REPOSITORIES_FILE`, la ruta y la rama coinciden |
| `app_auth` | el JWT de la App es aceptado y el `app_id` devuelto es el configurado |
| `installation_restricted` | `repository_selection: "selected"`, permisos exactamente `contents:read` + `metadata:read`, sin `workflows`/`administration`, no suspendida |
| `installation_token` | token de una hora acotado al repositorio y a esos permisos (solo se muestra la caducidad) |
| `repository_identity` | el id numérico, `owner/name`, la rama por defecto, `private: true`, no archivado, alcanzable por la instalación |
| `remote_head` | commit actual de la rama por defecto en GitHub |
| `mirror` | si existe: marcador, `origin`, `local_head` = `marker_base_sha`, `in_sync_with_remote`, sin credenciales en la URL |
| `no_token_leaks` | ningún `ghs_…` bajo las raíces de Git ni en el marcador (con `--scan-db`, tampoco en eventos, auditoría ni checkpoints) |

Si alguna comprobación falla, el informe trae el código (`repository_substituted`,
`installation_not_found`, `app_auth_failed`, …); corrige la política o la instalación y
repite. El preflight no puede enumerar qué *otros* repositorios tiene seleccionados la
instalación (el token se acota al sandbox a propósito): esa lista la confirmas tú en el
paso 3.

## 7. Crear el espejo y verificarlo

```bash
uv run python -m scripts.mirror_repository --repository sandbox-github --project sandbox-github --create
uv run python -m scripts.mirror_repository --repository sandbox-github --project sandbox-github --check
```

Salida esperada (resumen JSON, nunca un token):

```json
{"operation": "create", "result": "ok", "repository": "<owner>/agentforge-sandbox-demo",
 "github_repository_id": <id>, "installation_id": <installation_id>, "default_branch": "main",
 "base_sha": "<40 hex>", "changed": true, "path": ".../sandbox-github/agentforge-sandbox-demo.git"}
```

Comprobaciones manuales:

```bash
git -C "$GIT_REPOSITORIES_ROOT/sandbox-github/agentforge-sandbox-demo.git" remote get-url origin
# https://github.com/<owner>/agentforge-sandbox-demo.git  (sin usuario ni token)
git -C "$GIT_REPOSITORIES_ROOT/sandbox-github/agentforge-sandbox-demo.git" for-each-ref
# solo refs/heads/*; ninguna etiqueta ni refs/pull
cat "$GIT_REPOSITORIES_ROOT/sandbox-github/agentforge-sandbox-demo.mirror.json"
ls -l "$GIT_REPOSITORIES_ROOT/sandbox-github/"   # marcador 0600
psql "$DATABASE_URL" -c "select operation, result, code, left(base_sha, 12), changed, token_expires_at from mirror_syncs order by id desc limit 5"
grep -R "ghs_" "$GIT_REPOSITORIES_ROOT" ~/.local/share/agentforge* 2>/dev/null | wc -l   # debe ser 0
```

Pruebas de rechazo (cada una debe terminar con `"result": "failed"` y el código indicado,
sin modificar el espejo):

| Manipulación | Código |
| --- | --- |
| `github_repository_id` distinto en la política | `repository_substituted` |
| `owner` u `name` que no corresponden a ese id | `repository_transferred` |
| `default_branch` distinta de la real | `default_branch_changed` |
| `installation_id` de otra instalación | `installation_not_found` o `installation_forbidden` |
| `git remote set-url origin` cambiado en el espejo | `mirror_remote_mismatch` |
| Marcador editado o borrado | `mirror_identity_mismatch` / `mirror_missing` |

Restaura la política y vuelve a `--check` antes de seguir. Después de la primera
sincronización real y de la primera ejecución gobernada, ejecuta el barrido completo:

```bash
uv run python -m scripts.github_preflight --repository sandbox-github --project sandbox-github \
  --scan-db --scan /ruta/al/log/de/uvicorn
```

`no_token_leaks` debe seguir en `ok` con `--scan-db` (eventos, `mirror_syncs`,
`tool_invocations`, `runs`, `checkpoints`, `checkpoint_blobs`, `checkpoint_writes`).

## 8. Primera ejecución gobernada sobre el espejo

Con la entrada añadida al registro, arranca la API y crea una ejecución `development`
sobre `sandbox-demo` como en el Paso 1 (`scripts/development_mvp.py` o la API). Observa
en la propuesta de alcance el bloque `base` (`sha`, `changed`, `synced_at`), `hidden_paths`
y `signals`, y en `run_events` el evento `mirror_refreshed` atribuido a
`agentforge:platform`. Developer y QA trabajan en el worktree local; **nada vuelve a
GitHub**: `git.push` sigue deshabilitado y la App no tiene `contents: write`.

## 9. Rotación y revocación

- Rotar la clave: generar una nueva en la App, instalarla con `install -m 0600`, reiniciar
  la API, y solo entonces eliminar la antigua en GitHub. Los tokens de instalación caducan
  solos en una hora como máximo y viven solo en memoria del proceso.
- Revocar por completo: *Uninstall* de la instalación en el sandbox y *Delete GitHub App*.
  Después, borra la clave del host (`rm -P` en macOS, `shred -u` en Linux) y vacía
  `GITHUB_APP_ID` y `GITHUB_APP_PRIVATE_KEY_FILE` del `.env`.
- Nunca reutilices esta App para Clivana ni para otro repositorio: cada repositorio real
  tendrá su propia aprobación y, como mínimo, su propia instalación.
