from typing import Literal

from app.exceptions import DomainError

AuthenticationFailure = Literal["missing_token", "expired_token", "invalid_token"]


class AuthenticationError(DomainError):
    """The request carries no usable identity. Mapped to 401 by the API layer.

    ``reason`` is a closed category, never the token or the library message:
    the client learns whether to re-authenticate, not how verification works.
    """

    code = "unauthenticated"

    def __init__(self, reason: AuthenticationFailure) -> None:
        self.reason = reason
        detail = {
            "missing_token": "Se requiere un token Bearer.",
            "expired_token": "El token ha expirado.",
            "invalid_token": "El token no es válido para este servicio.",
        }[reason]
        super().__init__(detail)


class PermissionDeniedError(DomainError):
    """Authenticated, but not allowed to do this. Mapped to 403."""

    code = "permission_denied"

    def __init__(self, detail: str = "No tienes permiso para esta operación.") -> None:
        super().__init__(detail)


class SelfApprovalForbiddenError(PermissionDeniedError):
    """Separation of duties: the creator of a run may not approve it."""

    code = "self_approval_forbidden"

    def __init__(self) -> None:
        super().__init__("Quien crea una ejecución no puede aprobarla en este entorno.")


class ProjectRequiredError(DomainError):
    """The principal belongs to several projects and did not say which one."""

    code = "project_required"

    def __init__(self, projects: list[str]) -> None:
        self.projects = projects
        super().__init__(
            f"Indica project_id: el principal pertenece a varios proyectos ({', '.join(projects)})."
        )
