"""Publication domain (Fase 7, Paso 3; ADR 0011): what leaves the host and under which proof.

A ``PublicationPlan`` pins the exact content (``head_sha``, patch digest, files
with their hashes), the base it was built on, the authorised destination and
the result of the pre-publication scan; its ``publish_digest`` is what a human
approves. The Publisher recomputes the plan from the mirror right before any
remote operation and refuses to continue on any difference. This package
imports only contracts, projects, git and security: no GitHub, no HTTP, no
subprocess. Agents never import it.
"""

from .errors import PublicationError
from .plan import (
    PUBLISHER_ACTOR,
    PublicationFile,
    PublicationPlan,
    publish_digest,
    remote_branch_for,
)
from .ports import PublicationAuditRepository, PublicationRecord
from .scan import (
    SECRET_PATTERNS,
    ChangedEntry,
    ScanFinding,
    ScanReport,
    scan_changes,
)
from .state import PublicationProgress, PublicationState, advance
from .template import (
    commit_message,
    neutralize_mentions,
    pull_request_body,
    pull_request_title,
    quote_untrusted,
)

__all__ = [
    "PUBLISHER_ACTOR",
    "SECRET_PATTERNS",
    "ChangedEntry",
    "PublicationAuditRepository",
    "PublicationError",
    "PublicationFile",
    "PublicationPlan",
    "PublicationProgress",
    "PublicationRecord",
    "PublicationState",
    "ScanFinding",
    "ScanReport",
    "advance",
    "commit_message",
    "neutralize_mentions",
    "publish_digest",
    "pull_request_body",
    "pull_request_title",
    "quote_untrusted",
    "remote_branch_for",
    "scan_changes",
]
