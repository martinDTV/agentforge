"""Per-project repository policy (Fase 7, ADR 0009): what AgentForge may write,
never write, how much, and under which limits. Trusted operator configuration;
loaded in ``lifespan``; never shown to a model."""

from .errors import PolicyError
from .globs import compile_glob, matches_any
from .policy import (
    DEFAULT_DENIED_GLOBS,
    DEFAULT_EXCLUDED_CONTEXT_GLOBS,
    ProjectPolicyRegistry,
    PublishPolicy,
    RepositoryPolicy,
)
from .scope import ScopeDecision, check_against_policy, filter_scope
from .untrusted import (
    DATA_BOUNDARY_NOTICE,
    InjectionSignal,
    hidden_from_agents,
    injection_signals,
)

__all__ = [
    "DATA_BOUNDARY_NOTICE",
    "DEFAULT_DENIED_GLOBS",
    "DEFAULT_EXCLUDED_CONTEXT_GLOBS",
    "InjectionSignal",
    "PolicyError",
    "ProjectPolicyRegistry",
    "PublishPolicy",
    "RepositoryPolicy",
    "ScopeDecision",
    "check_against_policy",
    "compile_glob",
    "filter_scope",
    "hidden_from_agents",
    "injection_signals",
    "matches_any",
]
