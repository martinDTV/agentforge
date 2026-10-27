"""Identity of a remote repository (Fase 7, Paso 2; ADR 0010).

A remote is named by its provider, owner and name **and** by the provider's
stable repository id. Owner and name derive the clone URL; the id is what the
platform verifies against the provider before every synchronisation, so a
transferred, renamed or substituted repository is detected and refused.
Nothing here is ever read from a model or from an API client.
"""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .errors import GitOperationError
from .refs import validate_branch_name

OWNER_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$"
NAME_PATTERN = r"^[A-Za-z0-9._-]{1,100}$"
_NAME_RE = re.compile(NAME_PATTERN)
GITHUB_HOST = "github.com"


class RemoteSpec(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: Literal["github"] = "github"
    owner: str = Field(pattern=OWNER_PATTERN)
    name: str = Field(pattern=NAME_PATTERN)
    # The provider's immutable numeric id: survives renames and transfers, so a
    # mismatch means the configured name now points at a different repository.
    github_repository_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)
    default_branch: str = Field(default="main", min_length=1, max_length=200)
    # Optional shallow mirror for very large repositories (operator decision).
    fetch_depth: int | None = Field(default=None, ge=1, le=10_000)

    @field_validator("name")
    @classmethod
    def _plain_name(cls, value: str) -> str:
        if not _NAME_RE.fullmatch(value) or value in (".", "..") or value.startswith("."):
            raise ValueError(f"nombre de repositorio no admitido: {value!r}")
        if value.lower().endswith(".git"):
            raise ValueError("el nombre no debe terminar en .git")
        return value

    @field_validator("default_branch")
    @classmethod
    def _valid_branch(cls, value: str) -> str:
        try:
            return validate_branch_name(value)
        except GitOperationError as exc:
            raise ValueError(exc.detail) from exc

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def clone_url(self) -> str:
        """Derived, never configured: the only remote the mirror may talk to."""
        return f"https://{GITHUB_HOST}/{self.owner}/{self.name}.git"
