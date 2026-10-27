"""GitHub App integration (Fase 7, Paso 2; ADR 0010). Platform-only.

The App's private key and every installation token live exclusively in this
package. Nothing here is reachable from an agent: the tool gateway registers no
GitHub tool, the agents' Git adapter cannot authenticate, and the sandbox has
no network. A static test keeps ``GITHUB_APP_*`` reads confined to this package.
"""

from .client import GitHubClient, InstallationInfo, PullRequestInfo, RepositoryIdentity
from .credentials import (
    PUBLISH_PERMISSIONS,
    PULLS_PERMISSIONS,
    PUSH_PERMISSIONS,
    READ_ONLY_PERMISSIONS,
    GitHubAppCredentials,
    InstallationToken,
    InstallationTokenProvider,
)
from .errors import GitHubError
from .identity import verify_identity
from .publisher import GitHubPublisher

__all__ = [
    "PUBLISH_PERMISSIONS",
    "PULLS_PERMISSIONS",
    "PUSH_PERMISSIONS",
    "READ_ONLY_PERMISSIONS",
    "GitHubAppCredentials",
    "GitHubClient",
    "GitHubError",
    "GitHubPublisher",
    "InstallationInfo",
    "InstallationToken",
    "InstallationTokenProvider",
    "PullRequestInfo",
    "RepositoryIdentity",
    "verify_identity",
]
