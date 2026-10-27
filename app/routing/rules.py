"""Deterministic rules that force the full route. Spanish and English, lower-cased.

A rule matches on words of the task text; it never consults the model. The
list is deliberately conservative: a false positive costs a planning round,
a false negative would skip architecture review on a risky change.
"""

import re
from collections.abc import Sequence

MANDATORY_FULL_RULES: dict[str, tuple[str, ...]] = {
    "architecture_or_infrastructure": (
        "arquitectura",
        "architecture",
        "infraestructura",
        "infrastructure",
        "terraform",
        "kubernetes",
        "k8s",
        "docker",
        "contenedor",
        "container",
        "despliegue",
        "deploy",
        "deployment",
        "ci/cd",
        "pipeline",
        "workflow",
        "github actions",
        "proxmox",
        "nginx",
        "load balancer",
        "balanceador",
        "dns",
        "red privada",
        "vpn",
        "tailscale",
    ),
    "data_or_schema_migration": (
        "migración",
        "migracion",
        "migration",
        "alembic",
        "esquema",
        "schema",
        "tabla",
        "table",
        "columna",
        "column",
        "base de datos",
        "database",
        "índice",
        "index",
        "backfill",
        "etl",
        "modelo de datos",
        "data model",
    ),
    "auth_or_security": (
        "autenticación",
        "autenticacion",
        "authentication",
        "autorización",
        "autorizacion",
        "authorization",
        "oauth",
        "oidc",
        "jwt",
        "token",
        "contraseña",
        "password",
        "secreto",
        "secret",
        "credencial",
        "credential",
        "permiso",
        "permission",
        "rol ",
        "role",
        "cifrado",
        "encryption",
        "tls",
        "certificado",
        "certificate",
        "seguridad",
        "security",
        "vulnerabilidad",
        "vulnerability",
        "cve",
    ),
    "public_contract_change": (
        "breaking",
        "incompatible",
        "contrato público",
        "contrato publico",
        "public api",
        "api pública",
        "api publica",
        "versión de la api",
        "api version",
        "renombrar endpoint",
        "eliminar endpoint",
        "remove endpoint",
        "deprecar",
        "deprecate",
        "formato de respuesta",
    ),
    "multiple_subsystems": (
        "todos los módulos",
        "todos los modulos",
        "all modules",
        "varios módulos",
        "varios modulos",
        "several modules",
        "multiple modules",
        "rediseñ",
        "redesign",
        "reescrib",
        "rewrite",
        "refactor global",
        "end-to-end",
        "extremo a extremo",
        "en toda la aplicación",
        "across the codebase",
        "monorepo",
    ),
}
_AMBIGUITY_MARKERS = (
    "?",
    "no sé",
    "no se ",
    "quizá",
    "quizas",
    "tal vez",
    "etc.",
    "lo que haga falta",
    "lo que sea necesario",
    "whatever",
    "maybe",
    "not sure",
    "y más",
    "and more",
)
MIN_TASK_CHARS = 25


def _normalise(text: str) -> str:
    return " " + re.sub(r"\s+", " ", text.lower()).strip() + " "


def mandatory_full_rules(task: str) -> list[str]:
    """Names of the mandatory rules the task triggers (empty = none)."""
    text = _normalise(task)
    hits: list[str] = []
    for name, needles in MANDATORY_FULL_RULES.items():
        if any(needle in text for needle in needles):
            hits.append(name)
    if len(task.strip()) < MIN_TASK_CHARS or any(marker in text for marker in _AMBIGUITY_MARKERS):
        hits.append("ambiguous_requirements")
    return hits


def summarize(rules: Sequence[str]) -> str:
    return ", ".join(rules) if rules else "none"
