"""A simulated GitHub REST API for the unit suite (``httpx.MockTransport``).

Implements the three calls the platform makes, with the checks GitHub itself
performs: the App JWT must be signed by the App's key and short-lived, an
installation token is narrowed to the repositories and permissions requested,
and tokens expire or can be revoked. Every request is recorded so tests can
assert what crossed the wire (and what never did).
"""

import secrets
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any

import httpx
import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

OWNER = "agentforge-sandbox"
NAME = "agentforge-sandbox-demo"
REPOSITORY_ID = 987_654_321
INSTALLATION_ID = 55
APP_ID = 4242
ALLOWED_PERMISSIONS = {"contents": "read", "metadata": "read"}


@lru_cache(maxsize=1)
def app_key_pair() -> tuple[bytes, bytes]:
    """One RSA key per test session (generation is the slow part)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public = key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    return private, public


def repository_payload(
    *,
    github_id: int = REPOSITORY_ID,
    owner: str = OWNER,
    name: str = NAME,
    default_branch: str = "main",
    archived: bool = False,
) -> dict[str, Any]:
    return {
        "id": github_id,
        "node_id": f"R_{github_id}",
        "name": name,
        "full_name": f"{owner}/{name}",
        "owner": {"login": owner, "id": 1},
        "private": True,
        "archived": archived,
        "default_branch": default_branch,
        "html_url": f"https://github.com/{owner}/{name}",
    }


class FakeGitHub:
    def __init__(
        self,
        *,
        clock: Callable[[], datetime] | None = None,
        repository: dict[str, Any] | None = None,
    ) -> None:
        self.app_id = APP_ID
        self.installation_id = INSTALLATION_ID
        self.private_key, self.public_key = app_key_pair()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.repository = repository or repository_payload()
        # Repositories the installation may reach (by name); the token is narrowed to them.
        self.installation_repositories: dict[str, dict[str, Any]] = {
            self.repository["name"]: self.repository
        }
        self.token_ttl = timedelta(hours=1)
        self.repository_selection = "selected"
        self.app_permissions: dict[str, str] = dict(ALLOWED_PERMISSIONS)
        self.account = OWNER
        self.suspended = False
        # ``GET /repos/{o}/{n}/branches/{b}``: branch -> sha; a callable answers dynamically.
        self.branch_heads: dict[str, str] | Callable[[str], str | None] = {}
        self.installation_lists_nothing = False
        # Pull requests by number; ``pulls_fail_next`` injects statuses for POST /pulls only;
        # ``lose_create_response`` makes the POST succeed but the response never arrive.
        self.pulls: dict[int, dict[str, Any]] = {}
        self.pulls_fail_next: list[int] = []
        self.lose_create_response = 0
        self.branches: dict[str, str] = {}  # remote branches known to the fake (name -> sha)
        # Flow tests push over file:// to a local repository; this callable lets the REST
        # fake see those branches when a pull request is created.
        self.branch_resolver: Callable[[str], str | None] | None = None
        self.revoked_by_api: list[str] = []
        # ``GET /repos/{o}/{n}/rules/branches/{b}``: rule types per branch; ``None`` = the
        # plan does not expose rulesets (GitHub answers 403 "Upgrade to GitHub Pro").
        self.branch_rules: dict[str, list[str]] | None = {}
        self.tokens: dict[str, dict[str, Any]] = {}
        self.revoked: set[str] = set()
        self.reject_app_jwt = False
        self.fail_next: list[int] = []  # status codes served to the next requests, in order
        self.requests: list[httpx.Request] = []
        self.jwts: list[dict[str, Any]] = []

    # -- wiring ----------------------------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def minted(self) -> list[str]:
        return list(self.tokens)

    def identity_now(self, **changes: Any) -> None:
        """What ``GET /repos/{owner}/{name}`` reports from now on."""
        self.repository = repository_payload(**changes)

    # -- handler ---------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_next:
            return httpx.Response(self.fail_next.pop(0), json={"message": "simulated"})
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return httpx.Response(401, json={"message": "Requires authentication"})
        bearer = auth.removeprefix("Bearer ")
        path = request.url.path
        if request.method == "POST" and path.startswith("/app/installations/"):
            return self._mint(request, bearer, path)
        if request.method == "GET" and path.startswith("/app/installations/"):
            return self._installation(bearer, path)
        token = self._token(bearer)
        if token is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.method == "GET" and path == "/installation/repositories":
            names = [] if self.installation_lists_nothing else token["repositories"]
            repos = [self.installation_repositories[n] for n in names]
            return httpx.Response(200, json={"total_count": len(repos), "repositories": repos})
        if request.method == "DELETE" and path == "/installation/token":
            self.revoked.add(bearer)
            self.revoked_by_api.append(bearer)
            return httpx.Response(204)
        if path.startswith("/repos/") and path.endswith("/pulls"):
            return self._pulls(request, token, path)
        if request.method == "GET" and path.startswith("/repos/") and "/rules/branches/" in path:
            if self.branch_rules is None:
                return httpx.Response(
                    403, json={"message": "Upgrade to GitHub Pro or make this repository public"}
                )
            branch = path.rsplit("/", 1)[1]
            return httpx.Response(
                200, json=[{"type": t, "ruleset_id": 1} for t in self.branch_rules.get(branch, [])]
            )
        if request.method == "GET" and path.startswith("/repos/") and "/branches/" in path:
            _, _, owner, name, _, branch = path.split("/", 5)
            if name not in token["repositories"]:
                return httpx.Response(404, json={"message": "Not Found"})
            heads = self.branch_heads
            sha = heads(branch) if callable(heads) else heads.get(branch)
            if sha is None:
                return httpx.Response(404, json={"message": "Branch not found"})
            return httpx.Response(200, json={"name": branch, "commit": {"sha": sha}})
        if request.method == "GET" and path.startswith("/repos/"):
            _, _, owner, name = path.split("/", 3)
            if name not in token["repositories"]:
                return httpx.Response(404, json={"message": "Not Found"})
            observed = self.repository
            if observed["name"] != name and observed["id"] != self.repository["id"]:
                return httpx.Response(404, json={"message": "Not Found"})
            return httpx.Response(200, json=observed)
        return httpx.Response(404, json={"message": "Not Found"})

    def _app_claims(self, bearer: str) -> dict[str, Any] | None:
        try:
            claims = jwt.decode(
                bearer,
                self.public_key,
                algorithms=["RS256"],
                options={
                    "require": ["iat", "exp", "iss"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError:
            return None
        now = int(self.clock().timestamp())
        if self.reject_app_jwt or str(claims.get("iss")) != str(self.app_id):
            return None
        if claims["exp"] <= now or claims["iat"] > now + 60 or claims["exp"] - claims["iat"] > 600:
            return None
        return claims

    def _installation(self, bearer: str, path: str) -> httpx.Response:
        if self._app_claims(bearer) is None:
            return httpx.Response(401, json={"message": "Bad credentials"})
        if int(path.split("/")[3]) != self.installation_id:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(
            200,
            json={
                "id": self.installation_id,
                "app_id": self.app_id,
                "account": {"login": self.account, "type": "User"},
                "repository_selection": self.repository_selection,
                "permissions": self.app_permissions,
                "suspended_at": "2026-01-01T00:00:00Z" if self.suspended else None,
            },
        )

    def _mint(self, request: httpx.Request, bearer: str, path: str) -> httpx.Response:
        try:
            claims = jwt.decode(
                bearer,
                self.public_key,
                algorithms=["RS256"],
                # Times are checked against the fake's clock below, not the wall clock.
                options={
                    "require": ["iat", "exp", "iss"],
                    "verify_exp": False,
                    "verify_iat": False,
                },
            )
        except jwt.PyJWTError:
            return httpx.Response(401, json={"message": "A JSON web token could not be decoded"})
        self.jwts.append(claims)
        now = int(self.clock().timestamp())
        if self.reject_app_jwt or str(claims.get("iss")) != str(self.app_id):
            return httpx.Response(401, json={"message": "Bad credentials"})
        if claims["exp"] <= now or claims["iat"] > now + 60:
            return httpx.Response(401, json={"message": "'Expiration time' claim is invalid"})
        if claims["exp"] - claims["iat"] > 600:
            return httpx.Response(401, json={"message": "Expiration time too far in the future"})
        installation = int(path.split("/")[3])
        if installation != self.installation_id:
            return httpx.Response(404, json={"message": "Not Found"})
        body = request.read()
        import json as _json

        wanted = _json.loads(body) if body else {}
        repositories = list(wanted.get("repositories") or [])
        permissions = dict(wanted.get("permissions") or {})
        unknown = [r for r in repositories if r not in self.installation_repositories]
        if unknown:
            return httpx.Response(422, json={"message": "repository not in installation"})
        levels = {"read": 0, "write": 1}
        too_much = {
            k: v
            for k, v in permissions.items()
            if k not in self.app_permissions
            or levels.get(v, 9) > levels.get(self.app_permissions[k], -1)
        }
        if too_much:
            return httpx.Response(
                422,
                json={"message": "The permissions requested are not granted to this installation."},
            )
        value = "ghs_" + secrets.token_hex(20)
        expires = self.clock() + self.token_ttl
        self.tokens[value] = {
            "expires_at": expires,
            "repositories": repositories or list(self.installation_repositories),
            "permissions": permissions or dict(self.app_permissions),
        }
        return httpx.Response(
            201,
            json={
                "token": value,
                "expires_at": expires.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "permissions": self.tokens[value]["permissions"],
                "repository_selection": "selected",
            },
        )

    def _pulls(self, request: httpx.Request, token: dict[str, Any], path: str) -> httpx.Response:
        _, _, owner, name, _ = path.split("/", 4)
        if name not in token["repositories"]:
            return httpx.Response(404, json={"message": "Not Found"})
        if request.method == "GET":
            if token["permissions"].get("pull_requests") not in ("read", "write"):
                return httpx.Response(403, json={"message": "Resource not accessible"})
            head = request.url.params.get("head", "")
            branch = head.split(":", 1)[1] if ":" in head else head
            items = [p for p in self.pulls.values() if p["head"]["ref"] == branch]
            items.sort(key=lambda p: p["number"], reverse=True)
            return httpx.Response(200, json=items)
        if request.method != "POST":
            return httpx.Response(405, json={"message": "Method Not Allowed"})
        if token["permissions"].get("pull_requests") != "write":
            return httpx.Response(403, json={"message": "Resource not accessible by integration"})
        if self.pulls_fail_next:
            return httpx.Response(self.pulls_fail_next.pop(0), json={"message": "simulated"})
        import json as _json

        body = _json.loads(request.read())
        head_ref = str(body.get("head", ""))
        head_sha = self.branches.get(head_ref)
        if head_sha is None and self.branch_resolver is not None:
            head_sha = self.branch_resolver(head_ref)
        if head_sha is None:
            return httpx.Response(422, json={"message": "Validation Failed: head invalid"})
        if any(p["head"]["ref"] == head_ref and p["state"] == "open" for p in self.pulls.values()):
            return httpx.Response(422, json={"message": "A pull request already exists"})
        number = max(self.pulls, default=0) + 1
        pr = {
            "number": number,
            "html_url": f"https://github.com/{owner}/{name}/pull/{number}",
            "state": "open",
            "draft": bool(body.get("draft")),
            "title": body.get("title"),
            "body": body.get("body"),
            "node_id": f"PR_{number}",
            "head": {"ref": head_ref, "sha": head_sha},
            "base": {"ref": body.get("base")},
        }
        self.pulls[number] = pr
        if self.lose_create_response > 0:
            self.lose_create_response -= 1
            raise httpx.ReadTimeout("response lost after the pull request was created")
        return httpx.Response(201, json=pr)

    def _token(self, bearer: str) -> dict[str, Any] | None:
        token = self.tokens.get(bearer)
        if token is None or bearer in self.revoked or token["expires_at"] <= self.clock():
            return None
        return token
