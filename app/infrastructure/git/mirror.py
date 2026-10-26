"""Local mirrors of GitHub repositories (Fase 7, Paso 2; ADR 0010). Platform only.

A mirror is a bare repository under ``GIT_REPOSITORIES_ROOT/<project>/<name>.git``
whose only remote is the URL *derived* from the policy (never configured by
hand, never taken from the model). Before every synchronisation the service:

1. re-reads the operator policy and refuses repositories without ``remote``;
2. verifies against GitHub that ``owner/name`` still is repository
   ``github_repository_id`` with the expected default branch and that the
   authorised installation reaches it (transfers, substitutions and archived
   repositories are refused);
3. checks that the mirror's marker and ``origin`` URL still match the policy;
4. fetches ``refs/heads/*`` only, with a short-lived read-only installation
   token delivered through ``GIT_ASKPASS``; nothing is pushed, ever;
5. records the new ``base_sha`` of the default branch in the marker.

One ``flock`` per repository serialises operations across processes; a second
caller gets ``mirror_locked`` instead of waiting. Every outcome, including
failures, is described by a ``MirrorSync`` for the audit table; it never
contains a token. Agents never reach this module: it is called by the
orchestrator before the Scoper runs and by the operator script.
"""

import asyncio
import fcntl
import logging
import os
import shutil
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from app.git import GitOperationError, RepositoryRegistry
from app.git.mirrors import (
    MIRROR_LOCK_SUFFIX,
    MIRROR_MARKER_SUFFIX,
    MirrorError,
    MirrorRecord,
    MirrorSync,
    MirrorSyncRepository,
)
from app.git.remote import RemoteSpec
from app.infrastructure.github import (
    GitHubClient,
    GitHubError,
    InstallationToken,
    InstallationTokenProvider,
    RepositoryIdentity,
    verify_identity,
)
from app.projects import PolicyError, ProjectPolicyRegistry, RepositoryPolicy

from .askpass import askpass_credential
from .local import LocalGitAdapter

logger = logging.getLogger(__name__)

Operation = Literal["create", "refresh", "check"]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class MirrorOutcome:
    record: MirrorRecord
    sync: MirrorSync

    @property
    def changed(self) -> bool:
        return self.sync.changed


class MirrorService:
    def __init__(
        self,
        *,
        git: LocalGitAdapter,
        repositories_root: Path,
        policies: ProjectPolicyRegistry,
        github: GitHubClient | None = None,
        tokens: InstallationTokenProvider | None = None,
        registry: RepositoryRegistry | None = None,
        clock: Callable[[], datetime] = _utcnow,
        url_for: Callable[[RemoteSpec], str] | None = None,
        allow_file_remotes: bool = False,
        fetch_timeout_seconds: float = 300.0,
        askpass_dir: Path | None = None,
    ) -> None:
        if not repositories_root.is_absolute():
            raise MirrorError("mirror_unconfigured", "GIT_REPOSITORIES_ROOT debe ser absoluta")
        self._git = git
        self._root = repositories_root
        self._policies = policies
        self._github = github
        self._tokens = tokens
        self._registry = registry
        self._clock = clock
        self._url_for = url_for or (lambda spec: spec.clone_url)
        self._allow_file = allow_file_remotes
        self._fetch_timeout = fetch_timeout_seconds
        self._askpass_dir = askpass_dir

    # -- public ---------------------------------------------------------------------------

    @property
    def configured(self) -> bool:
        return self._github is not None and self._tokens is not None

    def path_for(self, spec: RemoteSpec, project_id: str) -> Path:
        path = self._root / project_id / f"{spec.name}.git"
        if not path.resolve(strict=False).is_relative_to(self._root.resolve(strict=False)):
            raise MirrorError("mirror_unconfigured", "ruta del espejo fuera de la raíz")
        return path

    def record(self, repository_id: str, project_id: str) -> MirrorRecord | None:
        _, spec = self._spec(repository_id, project_id)
        return self._read_marker(self._marker_for(spec, project_id))

    def create(self, repository_id: str, project_id: str) -> MirrorOutcome:
        """Create the mirror (idempotent: an identical existing mirror is refreshed)."""
        policy, spec = self._spec(repository_id, project_id)
        return self._audited("create", policy, spec, lambda: self._create(policy, spec))

    def refresh(self, repository_id: str, project_id: str) -> MirrorOutcome:
        """Verify identity and remote, fetch branches, record the new base."""
        policy, spec = self._spec(repository_id, project_id)
        return self._audited("refresh", policy, spec, lambda: self._refresh(policy, spec))

    def check(self, repository_id: str, project_id: str) -> MirrorOutcome:
        """Validate marker, remote and base without fetching (GitHub identity if configured)."""
        policy, spec = self._spec(repository_id, project_id)
        return self._audited("check", policy, spec, lambda: self._check(policy, spec))

    # -- operations (run under the repository lock) -----------------------------------------

    def _create(self, policy: RepositoryPolicy, spec: RemoteSpec) -> "_Step":
        with self._locked(spec, policy.project_id) as path:
            marker = self._marker_for(spec, policy.project_id)
            existing = self._read_marker(marker)
            if existing is not None:
                self._same_identity(existing, policy, spec)  # raises on mismatch
                return self._refresh_locked(path, marker, existing, policy, spec)
            if path.exists():
                raise MirrorError(
                    "mirror_exists", f"{path} existe sin marcador: no se adopta un directorio ajeno"
                )
            token, _observed = self._verify_remote(spec)
            created_at = self._clock()
            try:
                self._git.init_bare(path)
                self._git.configure_remote(path, self._url_for(spec))
                token = self._fetch(path, spec, token)
                base = self._base_sha(path, spec)
            except BaseException:
                shutil.rmtree(path, ignore_errors=True)  # never leave a half mirror behind
                raise
            record = MirrorRecord(
                repository_id=policy.repository_id,
                project_id=policy.project_id,
                provider=spec.provider,
                owner=spec.owner,
                name=spec.name,
                github_repository_id=spec.github_repository_id,
                installation_id=spec.installation_id,
                default_branch=spec.default_branch,
                remote_url=self._url_for(spec),
                path=path,
                base_sha=base,
                created_at=created_at,
                last_synced_at=self._clock(),
                last_identity_check_at=created_at,
            )
            self._write_marker(marker, record)
            return _Step(record, previous=None, changed=True, token=token)

    def _refresh(self, policy: RepositoryPolicy, spec: RemoteSpec) -> "_Step":
        with self._locked(spec, policy.project_id) as path:
            marker = self._marker_for(spec, policy.project_id)
            existing = self._read_marker(marker)
            if existing is None:
                raise MirrorError("mirror_missing", f"no existe espejo para {spec.full_name}")
            return self._refresh_locked(path, marker, existing, policy, spec)

    def _refresh_locked(
        self,
        path: Path,
        marker: Path,
        existing: MirrorRecord,
        policy: RepositoryPolicy,
        spec: RemoteSpec,
    ) -> "_Step":
        self._same_identity(existing, policy, spec)
        self._same_remote(path, spec)
        token, _observed = self._verify_remote(spec)
        checked_at = self._clock()
        token = self._fetch(path, spec, token)
        base = self._base_sha(path, spec)
        record = existing.model_copy(
            update={
                "base_sha": base,
                "last_synced_at": self._clock(),
                "last_identity_check_at": checked_at,
                "path": path,
            }
        )
        self._write_marker(marker, record)
        return _Step(
            record, previous=existing.base_sha, changed=base != existing.base_sha, token=token
        )

    def _check(self, policy: RepositoryPolicy, spec: RemoteSpec) -> "_Step":
        with self._locked(spec, policy.project_id) as path:
            marker = self._marker_for(spec, policy.project_id)
            existing = self._read_marker(marker)
            if existing is None:
                raise MirrorError("mirror_missing", f"no existe espejo para {spec.full_name}")
            self._same_identity(existing, policy, spec)
            self._same_remote(path, spec)
            local = self._base_sha(path, spec)
            if existing.base_sha is not None and local != existing.base_sha:
                raise MirrorError(
                    "mirror_base_mismatch",
                    f"{spec.default_branch} del espejo apunta a {local[:12]} y el marcador a "
                    f"{existing.base_sha[:12]}",
                )
            token: InstallationToken | None = None
            record = existing
            if self.configured:
                token, _observed = self._verify_remote(spec)
                record = existing.model_copy(update={"last_identity_check_at": self._clock()})
                self._write_marker(marker, record)
            return _Step(record, previous=existing.base_sha, changed=False, token=token)

    # -- checks -----------------------------------------------------------------------------

    def _spec(self, repository_id: str, project_id: str) -> tuple[RepositoryPolicy, RemoteSpec]:
        policy = self._policies.require(repository_id, project_id)
        if policy.remote is None:
            raise MirrorError(
                "mirror_not_remote", f"el repositorio '{repository_id}' no tiene remoto configurado"
            )
        spec = policy.remote
        if self._registry is not None:
            config = self._registry.get(repository_id)
            if config is not None:
                expected = self.path_for(spec, project_id)
                if config.path != expected or config.project_id != project_id:
                    raise MirrorError(
                        "mirror_path_mismatch",
                        f"el registro de repositorios apunta a {config.path}, el espejo vive en "
                        f"{expected}",
                    )
                if config.default_branch != spec.default_branch:
                    raise MirrorError(
                        "mirror_identity_mismatch",
                        "la rama por defecto del registro no coincide con la política",
                    )
        return policy, spec

    def _same_identity(
        self, record: MirrorRecord, policy: RepositoryPolicy, spec: RemoteSpec
    ) -> None:
        expected = {
            "repository_id": policy.repository_id,
            "project_id": policy.project_id,
            "provider": spec.provider,
            "owner": spec.owner,
            "name": spec.name,
            "github_repository_id": spec.github_repository_id,
            "installation_id": spec.installation_id,
            "default_branch": spec.default_branch,
            "remote_url": self._url_for(spec),
        }
        actual = record.model_dump(include=set(expected))
        different = sorted(k for k in expected if actual.get(k) != expected[k])
        if different:
            raise MirrorError(
                "mirror_identity_mismatch",
                f"el marcador del espejo no coincide con la política en: {', '.join(different)}",
            )

    def _same_remote(self, path: Path, spec: RemoteSpec) -> None:
        if not (path / "HEAD").is_file() or not (path / "objects").is_dir():
            raise MirrorError("mirror_missing", f"{path} no es un repositorio Git")
        url = self._git.remote_url(path)
        if url != self._url_for(spec):
            raise MirrorError(
                "mirror_remote_mismatch",
                f"el remoto 'origin' del espejo de {spec.full_name} no es el derivado de la "
                "política",
            )

    def _require_github(self) -> tuple[GitHubClient, InstallationTokenProvider]:
        if self._github is None or self._tokens is None:
            raise MirrorError("mirror_unconfigured", "la GitHub App no está configurada")
        return self._github, self._tokens

    def _verify_remote(self, spec: RemoteSpec) -> tuple[InstallationToken, RepositoryIdentity]:
        github, tokens = self._require_github()
        token = tokens.token_for(spec.installation_id, spec.name)
        try:
            observed = github.repository(token.reveal(), spec.owner, spec.name)
        except GitHubError as exc:
            if exc.code != "token_invalid":
                raise
            tokens.invalidate(token)  # expired or revoked: one fresh token, one retry
            token = tokens.token_for(spec.installation_id, spec.name)
            observed = github.repository(token.reveal(), spec.owner, spec.name)
        reachable = [r.github_id for r in github.installation_repositories(token.reveal())]
        verify_identity(spec, observed, installation_repository_ids=reachable)
        return token, observed

    def _fetch(self, path: Path, spec: RemoteSpec, token: InstallationToken) -> InstallationToken:
        _, tokens = self._require_github()
        for attempt in (1, 2):
            with askpass_credential(token.reveal(), base_dir=self._askpass_dir) as credential:
                try:
                    self._git.fetch_heads(
                        path,
                        credential_env=credential.env,
                        depth=spec.fetch_depth,
                        allow_file_remotes=self._allow_file,
                        timeout=self._fetch_timeout,
                    )
                    return token
                except GitOperationError as exc:
                    if attempt == 2 or exc.code != "git_failed":
                        raise MirrorError("mirror_sync_failed", exc.detail) from exc
            # The fetch may have failed because the token was revoked meanwhile.
            tokens.invalidate(token)
            token = tokens.token_for(spec.installation_id, spec.name)
        raise AssertionError("unreachable")

    def _base_sha(self, path: Path, spec: RemoteSpec) -> str:
        heads = self._git.branch_heads(path)
        sha = heads.get(spec.default_branch)
        if sha is None:
            raise MirrorError(
                "mirror_base_missing",
                f"el espejo de {spec.full_name} no tiene la rama '{spec.default_branch}'",
            )
        return sha

    # -- storage ----------------------------------------------------------------------------

    def _marker_for(self, spec: RemoteSpec, project_id: str) -> Path:
        return self.path_for(spec, project_id).parent / f"{spec.name}{MIRROR_MARKER_SUFFIX}"

    @staticmethod
    def _read_marker(marker: Path) -> MirrorRecord | None:
        try:
            raw = marker.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        try:
            return MirrorRecord.model_validate_json(raw)
        except ValueError as exc:
            raise MirrorError("mirror_marker_invalid", f"marcador ilegible: {marker.name}") from exc

    @staticmethod
    def _write_marker(marker: Path, record: MirrorRecord) -> None:
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(record.model_dump_json(indent=2))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, marker)

    @contextmanager
    def _locked(self, spec: RemoteSpec, project_id: str) -> Iterator[Path]:
        path = self.path_for(spec, project_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = path.parent / f"{spec.name}{MIRROR_LOCK_SUFFIX}"
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise MirrorError(
                    "mirror_locked", f"otra sincronización de {spec.full_name} está en curso"
                ) from exc
            yield path
        finally:
            os.close(fd)  # releases the lock

    # -- audit ------------------------------------------------------------------------------

    def _audited(
        self,
        operation: Operation,
        policy: RepositoryPolicy,
        spec: RemoteSpec,
        step: Callable[[], "_Step"],
    ) -> MirrorOutcome:
        started = self._clock()

        def failed(code: str) -> MirrorSync:
            return MirrorSync(
                repository_id=policy.repository_id,
                project_id=policy.project_id,
                operation=operation,
                result="failed",
                code=code,
                github_repository_id=spec.github_repository_id,
                installation_id=spec.installation_id,
                started_at=started,
                finished_at=self._clock(),
            )

        try:
            result = step()
        except MirrorError as exc:
            exc.sync = failed(exc.code)
            raise
        except (GitHubError, GitOperationError, PolicyError) as exc:
            wrapped = MirrorError(exc.code, exc.detail)
            wrapped.sync = failed(exc.code)
            raise wrapped from exc
        except OSError as exc:
            wrapped = MirrorError("mirror_sync_failed", f"fallo de E/S: {type(exc).__name__}")
            wrapped.sync = failed("mirror_sync_failed")
            raise wrapped from exc
        sync = MirrorSync(
            repository_id=policy.repository_id,
            project_id=policy.project_id,
            operation=operation,
            result="ok",
            base_sha=result.record.base_sha,
            previous_sha=result.previous,
            changed=result.changed,
            github_repository_id=spec.github_repository_id,
            installation_id=spec.installation_id,
            token_expires_at=result.token.expires_at if result.token is not None else None,
            started_at=started,
            finished_at=self._clock(),
        )
        logger.info(
            "Espejo %s %s: %s base=%s cambiado=%s",
            spec.full_name,
            operation,
            sync.result,
            (sync.base_sha or "")[:12],
            sync.changed,
        )
        return MirrorOutcome(result.record, sync)


@dataclass(frozen=True)
class _Step:
    record: MirrorRecord
    previous: str | None
    changed: bool
    token: InstallationToken | None


async def audited(
    syncs: MirrorSyncRepository | None, operation: Callable[[], MirrorOutcome]
) -> MirrorOutcome:
    """Run a blocking mirror operation off the event loop and persist its audit row."""
    try:
        outcome = await asyncio.to_thread(operation)
    except MirrorError as exc:
        if syncs is not None and exc.sync is not None:
            await syncs.record(exc.sync)
        raise
    if syncs is not None:
        await syncs.record(outcome.sync)
    return outcome
