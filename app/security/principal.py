from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PrincipalKind = Literal["user", "service"]


class Principal(BaseModel):
    """Who is calling, as proven by a verified token.

    Deliberately holds no e-mail or display name: the opaque ``subject`` is
    what gets attributed in audit and logs. Roles and projects are plain
    strings from the token; what they allow is decided by the authorization
    policy, never here.
    """

    model_config = ConfigDict(frozen=True)

    subject: str = Field(min_length=1, max_length=255)
    kind: PrincipalKind = "user"
    issuer: str
    roles: frozenset[str] = frozenset()
    projects: frozenset[str] = frozenset()
    expires_at: datetime | None = None
    token_id: str | None = None

    def has_role(self, role: str) -> bool:
        return role in self.roles
