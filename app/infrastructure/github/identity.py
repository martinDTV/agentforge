"""Compare the configured remote identity with what GitHub reports (ADR 0010)."""

from collections.abc import Iterable

from app.git.remote import RemoteSpec

from .client import RepositoryIdentity
from .errors import GitHubError


def verify_identity(
    spec: RemoteSpec,
    observed: RepositoryIdentity,
    *,
    installation_repository_ids: Iterable[int] | None = None,
) -> None:
    """Raise unless ``observed`` is exactly the repository the policy names.

    The numeric id is the identity: a different id behind the same name is a
    substitution; the same id behind another name is a transfer or rename;
    either is refused until the operator updates the policy on purpose.
    """
    same_name = observed.full_name.lower() == spec.full_name.lower()
    if observed.github_id != spec.github_repository_id:
        if same_name:
            raise GitHubError(
                "repository_substituted",
                f"{spec.full_name} ya no es el repositorio {spec.github_repository_id}",
            )
        raise GitHubError(
            "repository_identity_mismatch",
            "GitHub devolvió un repositorio distinto del configurado",
        )
    if not same_name:
        raise GitHubError(
            "repository_transferred",
            f"el repositorio {spec.github_repository_id} ahora se llama {observed.full_name}",
        )
    if observed.default_branch != spec.default_branch:
        raise GitHubError(
            "default_branch_changed",
            f"la rama por defecto de {spec.full_name} es '{observed.default_branch}', "
            f"la política dice '{spec.default_branch}'",
        )
    if observed.archived:
        raise GitHubError("repository_archived", f"{spec.full_name} está archivado")
    if installation_repository_ids is not None and spec.github_repository_id not in set(
        installation_repository_ids
    ):
        raise GitHubError(
            "repository_not_in_installation",
            f"la instalación {spec.installation_id} no alcanza {spec.full_name}",
        )
