"""Minimal GitHub REST client for what the platform needs and nothing more.

Three calls: mint an installation token, read one repository's identity and
list the repositories an installation can reach. Responses are parsed into
small models; bodies are never logged; errors carry a status-derived code and
no payload. Transient failures (5xx, transport) are retried once.
"""

import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from .errors import GitHubError

logger = logging.getLogger(__name__)

API_VERSION = "2022-11-28"
USER_AGENT = "agentforge-platform"
DEFAULT_TIMEOUT_SECONDS = 15.0
RETRY_DELAY_SECONDS = 0.5
MAX_PAGES = 20


class RepositoryIdentity(BaseModel):
    model_config = ConfigDict(frozen=True)

    github_id: int = Field(gt=0)
    owner: str
    name: str
    full_name: str
    default_branch: str
    private: bool = True
    archived: bool = False
    node_id: str | None = None


class InstallationInfo(BaseModel):
    """What ``GET /app/installations/{id}`` reports: how the App is installed."""

    model_config = ConfigDict(frozen=True)

    id: int = Field(gt=0)
    app_id: int = Field(gt=0)
    account: str
    repository_selection: str  # "selected" is the only acceptable value (ADR 0010)
    permissions: dict[str, str]
    suspended: bool = False


class PullRequestInfo(BaseModel):
    model_config = ConfigDict(frozen=True)

    number: int = Field(gt=0)
    url: str
    state: str  # "open" | "closed"
    draft: bool
    head_ref: str
    head_sha: str
    base_ref: str
    node_id: str | None = None
    # Integration evidence (ADR 0012 §4.3): set once GitHub reports the PR merged.
    # With squash or rebase merges ``merge_commit_sha`` is *not* ``head_sha``.
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None

    @property
    def merged(self) -> bool:
        return self.merged_at is not None


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _pull_request(payload: dict[str, Any]) -> PullRequestInfo:
    try:
        head = payload.get("head") or {}
        base = payload.get("base") or {}
        merged_at = _parse_time(payload.get("merged_at"))
        merge_commit = payload.get("merge_commit_sha")
        return PullRequestInfo(
            number=int(payload["number"]),
            url=str(payload["html_url"]),
            state=str(payload.get("state", "open")),
            draft=bool(payload.get("draft", False)),
            head_ref=str(head.get("ref", "")),
            head_sha=str(head.get("sha", "")),
            base_ref=str(base.get("ref", "")),
            node_id=payload.get("node_id"),
            merged_at=merged_at,
            merge_commit_sha=str(merge_commit) if merged_at and merge_commit else None,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError("github_protocol_error", "respuesta de pull request ilegible") from exc


def _identity(payload: dict[str, Any]) -> RepositoryIdentity:
    try:
        owner = payload.get("owner") or {}
        return RepositoryIdentity(
            github_id=int(payload["id"]),
            owner=str(owner.get("login") or str(payload["full_name"]).split("/")[0]),
            name=str(payload["name"]),
            full_name=str(payload["full_name"]),
            default_branch=str(payload.get("default_branch") or "main"),
            private=bool(payload.get("private", True)),
            archived=bool(payload.get("archived", False)),
            node_id=payload.get("node_id"),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise GitHubError("github_protocol_error", "respuesta de repositorio ilegible") from exc


class GitHubClient:
    def __init__(
        self,
        base_url: str = "https://api.github.com",
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        sleep: Any = time.sleep,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url.rstrip("/"),
            transport=transport,
            timeout=timeout_seconds,
            trust_env=False,  # no proxies, no .netrc, no ambient credentials
            headers={
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
                "User-Agent": USER_AGENT,
            },
        )
        self._sleep = sleep

    def close(self) -> None:
        self._client.close()

    # -- calls --------------------------------------------------------------------------

    def create_installation_token(
        self,
        app_jwt: str,
        installation_id: int,
        *,
        repositories: list[str],
        permissions: dict[str, str],
    ) -> tuple[str, datetime]:
        """``POST /app/installations/{id}/access_tokens`` narrowed to the given
        repositories and permissions. Returns the token and its expiry."""
        response = self._request(
            "POST",
            f"/app/installations/{int(installation_id)}/access_tokens",
            bearer=app_jwt,
            json={"repositories": repositories, "permissions": permissions},
            purpose="token",
        )
        try:
            body = response.json()
            token = str(body["token"])
            expires = datetime.fromisoformat(str(body["expires_at"]).replace("Z", "+00:00"))
        except (KeyError, TypeError, ValueError) as exc:
            raise GitHubError("github_protocol_error", "respuesta de token ilegible") from exc
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        granted = body.get("permissions")
        if isinstance(granted, dict):
            extra = {k: v for k, v in granted.items() if permissions.get(k) != v}
            if extra:
                raise GitHubError(
                    "permission_not_allowed",
                    f"el token concedido tiene permisos distintos de los pedidos: {sorted(extra)}",
                )
        return token, expires

    def installation(self, app_jwt: str, installation_id: int) -> InstallationInfo:
        """Installation details, authenticated as the App (JWT), not as the installation."""
        response = self._request(
            "GET", f"/app/installations/{int(installation_id)}", bearer=app_jwt, purpose="token"
        )
        payload = response.json()
        try:
            account = payload.get("account") or {}
            return InstallationInfo(
                id=int(payload["id"]),
                app_id=int(payload["app_id"]),
                account=str(account.get("login", "")),
                repository_selection=str(payload.get("repository_selection", "")),
                permissions={str(k): str(v) for k, v in (payload.get("permissions") or {}).items()},
                suspended=payload.get("suspended_at") is not None,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as exc:
            raise GitHubError("github_protocol_error", "respuesta de instalación ilegible") from exc

    def branch_rules(self, token: str, owner: str, name: str, branch: str) -> list[str]:
        """Rule types active on ``branch`` through repository rulesets (read-only).

        GitHub only offers rulesets and branch protection on private repositories
        of paid plans; the 403 it returns then becomes ``branch_rules_unavailable``
        so the preflight can say exactly why ``main`` cannot be verified."""
        try:
            response = self._request(
                "GET",
                f"/repos/{owner}/{name}/rules/branches/{branch}",
                bearer=token,
                purpose="rules",
            )
        except GitHubError as exc:
            if exc.code == "installation_forbidden":
                raise GitHubError(
                    "branch_rules_unavailable",
                    "GitHub no expone reglas de rama para este repositorio (plan o permisos)",
                ) from exc
            raise
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubError("github_protocol_error", "reglas de rama ilegibles")
        return sorted({str(item.get("type")) for item in payload if isinstance(item, dict)})

    def branch_head(self, token: str, owner: str, name: str, branch: str) -> str:
        """Current commit of ``branch`` as GitHub sees it (``contents: read``)."""
        response = self._request(
            "GET", f"/repos/{owner}/{name}/branches/{branch}", bearer=token, purpose="repository"
        )
        payload = response.json()
        try:
            sha = str(payload["commit"]["sha"])
        except (KeyError, TypeError) as exc:
            raise GitHubError("github_protocol_error", "respuesta de rama ilegible") from exc
        if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
            raise GitHubError("github_protocol_error", "SHA de rama ilegible")
        return sha

    def repository(self, token: str, owner: str, name: str) -> RepositoryIdentity:
        response = self._request(
            "GET", f"/repos/{owner}/{name}", bearer=token, purpose="repository"
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubError("github_protocol_error", "respuesta de repositorio ilegible")
        return _identity(payload)

    def installation_repositories(self, token: str) -> list[RepositoryIdentity]:
        """Every repository the installation token can reach (paginated)."""
        found: list[RepositoryIdentity] = []
        for page in range(1, MAX_PAGES + 1):
            response = self._request(
                "GET",
                "/installation/repositories",
                bearer=token,
                params={"per_page": 100, "page": page},
                purpose="installation",
            )
            payload = response.json()
            items = payload.get("repositories") if isinstance(payload, dict) else None
            if not isinstance(items, list):
                raise GitHubError("github_protocol_error", "listado de instalación ilegible")
            found.extend(_identity(item) for item in items if isinstance(item, dict))
            if len(items) < 100:
                break
        return found

    def find_pull_requests(
        self, token: str, owner: str, name: str, *, head_branch: str
    ) -> list[PullRequestInfo]:
        """Pull requests whose head is ``owner:head_branch`` (any state, newest first).

        The lookup every publication does *before* creating: a branch name is
        unique per run, so an existing pull request here is ours."""
        response = self._request(
            "GET",
            f"/repos/{owner}/{name}/pulls",
            bearer=token,
            params={
                "head": f"{owner}:{head_branch}",
                "state": "all",
                "sort": "created",
                "direction": "desc",
                "per_page": 10,
            },
            purpose="pulls",
        )
        payload = response.json()
        if not isinstance(payload, list):
            raise GitHubError("github_protocol_error", "listado de pull requests ilegible")
        return [_pull_request(item) for item in payload if isinstance(item, dict)]

    def get_pull_request(self, token: str, owner: str, name: str, number: int) -> PullRequestInfo:
        """``GET /repos/{o}/{n}/pulls/{number}``: state, ``merged_at`` and ``merge_commit_sha``.

        Read-only; the token needs ``pull_requests: read``. This is how the goal
        Supervisor learns that a dependency was integrated (ADR 0012 §4.3)."""
        response = self._request(
            "GET", f"/repos/{owner}/{name}/pulls/{int(number)}", bearer=token, purpose="pulls"
        )
        payload = response.json()
        if not isinstance(payload, dict):
            raise GitHubError("github_protocol_error", "pull request ilegible")
        return _pull_request(payload)

    def create_draft_pull_request(
        self,
        token: str,
        owner: str,
        name: str,
        *,
        title: str,
        body: str,
        head_branch: str,
        base_branch: str,
    ) -> PullRequestInfo:
        """``POST /repos/{o}/{n}/pulls`` with ``draft: true``; never merges, never updates."""
        response = self._request(
            "POST",
            f"/repos/{owner}/{name}/pulls",
            bearer=token,
            json={
                "title": title,
                "body": body,
                "head": head_branch,
                "base": base_branch,
                "draft": True,
                "maintainer_can_modify": True,
            },
            purpose="pulls",
        )
        created = _pull_request(response.json())
        if not created.draft:
            raise GitHubError(
                "pull_request_not_draft", f"GitHub creó el PR #{created.number} sin borrador"
            )
        return created

    def revoke_installation_token(self, token: str) -> bool:
        """``DELETE /installation/token``: the token stops working immediately."""
        try:
            response = self._client.request(
                "DELETE", "/installation/token", headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.TransportError:
            return False
        return response.status_code in (204, 401)  # 401: already invalid

    # -- plumbing -----------------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        bearer: str,
        purpose: str,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response:
        last: GitHubError | None = None
        for attempt in range(2):
            try:
                response = self._client.request(
                    method,
                    path,
                    json=json,
                    params=params,
                    headers={"Authorization": f"Bearer {bearer}"},
                )
            except httpx.TransportError as exc:
                last = GitHubError(
                    "github_unreachable",
                    f"GitHub inaccesible: {type(exc).__name__}",
                    retryable=True,
                )
            else:
                if response.status_code < 400:
                    return response
                last = _error_for(response.status_code, purpose)
                if not last.retryable:
                    raise last
            if attempt == 0:
                self._sleep(RETRY_DELAY_SECONDS)
        assert last is not None
        raise last


def _error_for(status: int, purpose: str) -> GitHubError:
    if status == 401:
        code = "app_auth_failed" if purpose == "token" else "token_invalid"
        return GitHubError(code, f"GitHub rechazó la credencial ({purpose})")
    if status == 403:
        return GitHubError("installation_forbidden", f"GitHub denegó la operación ({purpose})")
    if status == 404:
        code = "installation_not_found" if purpose == "token" else "repository_not_found"
        return GitHubError(code, f"recurso no encontrado en GitHub ({purpose})")
    if status == 422 and purpose == "pulls":
        return GitHubError(
            "pull_request_rejected",
            "GitHub rechazó la creación del pull request (422): rama inexistente, sin cambios "
            "o ya existe uno para esa rama",
        )
    if status == 422 and purpose == "token":
        # GitHub refuses the narrowing: a permission the installation does not
        # grant, or a repository the installation does not reach.
        return GitHubError(
            "token_request_rejected",
            "GitHub rechazó el canje: permiso no concedido a la instalación o repositorio "
            "fuera de ella (422)",
        )
    if status >= 500:
        return GitHubError(
            "github_failed", f"GitHub respondió {status} ({purpose})", retryable=True
        )
    return GitHubError("github_failed", f"GitHub respondió {status} ({purpose})")
