"""Repository policy: operator-owned limits for development runs on one repository."""

import json
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.contracts.scope import MAX_SCOPE_FILES
from app.git.remote import RemoteSpec

from .errors import PolicyError
from .globs import compile_glob

# Paths AgentForge never writes, whatever a project allows: CI definitions,
# lockfiles, container and infrastructure files, anything that smells of a
# credential, and the project's own policy. A project may only add to this list.
DEFAULT_DENIED_GLOBS: tuple[str, ...] = (
    ".github/**",
    ".gitlab-ci.yml",
    "**/*.lock",
    "**/Dockerfile*",
    "**/docker-compose*",
    "**/*.pem",
    "**/*.key",
    "**/.env",
    "**/.env.*",
    "**/secrets*",
    "**/*secret*",
    "**/*credential*",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/.netrc",
    "**/.npmrc",
    "**/.pypirc",
    "**/*.tfstate*",
    "infra/**",
    "terraform/**",
    "deploy/**",
    "agentforge.json",
)

# Files that tell *agents* what to do. A remote repository can carry them
# (``AGENTS.md``, ``CLAUDE.md``, Cursor/Copilot rules, ...); AgentForge treats
# them as data about the project, never as platform instructions, so they are
# hidden from the Scoper's context and refused as scope entries by default.
DEFAULT_EXCLUDED_CONTEXT_GLOBS: tuple[str, ...] = (
    "**/AGENTS.md",
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",
    "**/GEMINI.md",
    "**/.claude/**",
    "**/.cursor/**",
    "**/.cursorrules",
    "**/.windsurfrules",
    "**/.clinerules",
    "**/.aider*",
    "**/.github/copilot-instructions.md",
    "**/.github/instructions/**",
    "**/.agentforge/**",
    "**/agentforge.json",
)

PublishMode = Literal["manual"]  # ``after_qa`` is reserved: a human always approves publication


class PublishPolicy(BaseModel):
    """How a run may leave the host (ADR 0011): always a human decision, always a draft."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: PublishMode = "manual"
    base_branch: str = Field(default="main", min_length=1, max_length=200)
    draft: Literal[True] = True
    # Reserved namespace of the branches AgentForge creates: ``<prefix><run_id>``.
    branch_prefix: str = Field(default="agentforge/", min_length=2, max_length=50)
    approval_ttl_seconds: float = Field(default=3 * 86_400.0, gt=0, le=30 * 86_400)
    wall_seconds: float = Field(default=300.0, gt=0, le=1800)
    title_prefix: str = Field(default="[AgentForge]", min_length=1, max_length=32)

    @field_validator("branch_prefix")
    @classmethod
    def _reserved_prefix(cls, value: str) -> str:
        from app.publishing.plan import validate_branch_prefix

        return validate_branch_prefix(value)

    @field_validator("title_prefix")
    @classmethod
    def _plain_title_prefix(cls, value: str) -> str:
        if any(ch in value for ch in "\r\n@`") or value != value.strip():
            raise ValueError("title_prefix no admite saltos de línea, '@' ni acentos graves")
        return value


class RepositoryPolicy(BaseModel):
    """Everything a development run on this repository is allowed to do."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repository_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    project_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    writable_globs: list[str] = Field(min_length=1, max_length=32)
    denied_globs: list[str] = Field(default_factory=list, max_length=64)
    # Where the Scoper may point tests and where ``pytest`` may run.
    test_paths: list[str] = Field(default_factory=lambda: ["tests"], min_length=1, max_length=8)
    lint_paths: list[str] = Field(default_factory=lambda: ["src", "tests"], min_length=1)
    max_files_per_run: int = Field(default=8, ge=1, le=MAX_SCOPE_FILES)
    max_file_bytes: int = Field(default=32_768, ge=256, le=32_768)
    max_change_bytes: int = Field(default=262_144, ge=1_024, le=4 * 1024 * 1024)
    max_corrections: int = Field(default=3, ge=0, le=3)
    max_model_calls: int = Field(default=10, ge=2, le=20)
    max_reserved_usd: float = Field(default=8.0, gt=0, le=50)
    wall_seconds: float = Field(default=1500.0, gt=0, le=3600)
    model_timeout_seconds: float = Field(default=400.0, gt=0, le=1800)
    scope_approval_ttl_seconds: float = Field(default=86_400.0, gt=0, le=7 * 86_400)
    sandbox_profile: str = Field(default="python-3.13", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    ruff_diff: bool = True
    publish: PublishPolicy = PublishPolicy()
    # Fase 7, Paso 2 (ADR 0010): the GitHub repository this local mirror stands
    # for. ``None`` keeps a purely local repository (sandbox-* demos). Content
    # that arrives through a remote is untrusted data: these globs are never
    # shown to the Scoper and never writable, whatever ``writable_globs`` says.
    remote: RemoteSpec | None = None
    excluded_context_globs: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("writable_globs", "denied_globs", "excluded_context_globs")
    @classmethod
    def _valid_globs(cls, value: list[str]) -> list[str]:
        for pattern in value:
            compile_glob(pattern)  # raises ValueError on a bad pattern
        return list(dict.fromkeys(value))

    @field_validator("test_paths", "lint_paths")
    @classmethod
    def _plain_paths(cls, value: list[str]) -> list[str]:
        for path in value:
            if not path or path.startswith(("/", "-", "~", ".")) or ".." in path.split("/"):
                raise ValueError(f"ruta no admitida: {path!r}")
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def _coherent(self) -> "RepositoryPolicy":
        if self.model_timeout_seconds > self.wall_seconds:
            raise ValueError("model_timeout_seconds no puede superar wall_seconds")
        return self

    @model_validator(mode="after")
    def _remote_matches_publish(self) -> "RepositoryPolicy":
        if self.remote is not None and self.remote.default_branch != self.publish.base_branch:
            raise ValueError(
                "publish.base_branch debe coincidir con remote.default_branch "
                f"({self.publish.base_branch!r} != {self.remote.default_branch!r})"
            )
        return self

    @property
    def all_denied_globs(self) -> tuple[str, ...]:
        return DEFAULT_DENIED_GLOBS + tuple(self.denied_globs)

    @property
    def all_excluded_context_globs(self) -> tuple[str, ...]:
        """Agent-instruction files and the project's extra exclusions (ADR 0010 §6)."""
        return DEFAULT_EXCLUDED_CONTEXT_GLOBS + tuple(self.excluded_context_globs)


class ProjectPolicyRegistry:
    """Trusted set of repository policies. Deny by default: no policy, no run."""

    def __init__(self, policies: Iterable[RepositoryPolicy] = ()) -> None:
        self._policies: dict[str, RepositoryPolicy] = {}
        for policy in policies:
            if policy.repository_id in self._policies:
                raise PolicyError(
                    "policy_invalid", f"política duplicada para {policy.repository_id}"
                )
            self._policies[policy.repository_id] = policy

    def get(self, repository_id: str) -> RepositoryPolicy | None:
        return self._policies.get(repository_id)

    def require(self, repository_id: str, project_id: str) -> RepositoryPolicy:
        policy = self._policies.get(repository_id)
        if policy is None or policy.project_id != project_id:
            raise PolicyError(
                "repository_not_authorized",
                f"el repositorio '{repository_id}' no tiene política de desarrollo "
                f"en el proyecto '{project_id}'",
            )
        return policy

    def ids(self) -> list[str]:
        return sorted(self._policies)

    @classmethod
    def from_file(cls, file: Path) -> "ProjectPolicyRegistry":
        """``{"repositories": [{"repository_id": ..., "project_id": ..., ...}, ...]}``."""
        data = json.loads(file.read_text(encoding="utf-8"))
        entries = data.get("repositories", []) if isinstance(data, dict) else data
        return cls(RepositoryPolicy.model_validate(entry) for entry in entries)
