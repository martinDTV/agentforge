"""Deterministic validation of a development plan against the platform (H1, ADR 0012).

Two real goals died at the plan stage before this module existed: one plan assumed
code that was not merged, another proposed three tasks for a goal that allowed two and
a file outside the writable paths. Both reached a human first. The gate below runs
*before* the Architect and before any approval, costs no model call, and only reads
durable facts: the repository policy and the goal limits. Model text never changes
what it enforces; it only feeds the planner's revision prompt.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

from app.contracts.planning import (
    MAX_CONTEXT_PATHS,
    DevelopmentPlan,
    DevelopmentTask,
    PlanConstraints,
    PlanViolation,
)
from app.projects.globs import matches_any
from app.projects.policy import RepositoryPolicy

# What QA can actually run on a worktree (ADR 0007/0008): nothing else exists.
PLAN_CONTEXT_PROGRAMS: tuple[str, ...] = ("ruff check", "ruff format --check", "pytest")

# Extensions that identify a file name even without a directory component.
_CODE_EXTENSIONS = frozenset(
    {
        "py",
        "pyi",
        "md",
        "rst",
        "txt",
        "toml",
        "yml",
        "yaml",
        "json",
        "ini",
        "cfg",
        "lock",
        "env",
        "sql",
        "sh",
        "js",
        "ts",
        "tsx",
        "jsx",
        "vue",
        "css",
        "html",
    }
)
# A bare file name with one of these extensions is a document or configuration file:
# a plan that wants one of those outside the writable paths is creating it at the root.
_DOCUMENT_EXTENSIONS = frozenset({"md", "rst", "txt", "toml", "yml", "yaml", "json", "ini", "cfg"})
_TOKEN_RE = re.compile(r"[A-Za-z0-9_./\\-]+")
_DOTFILES = frozenset({".env", ".envrc", ".netrc", ".npmrc", ".pypirc"})
_LEAD = "([{<\"'`«¡¿"
_TRAIL = ".,;:)]}>\"'`»!?"


def extract_paths(text: str) -> list[str]:
    """Relative file paths a human-readable task text names, in order, de-duplicated.

    Conservative on purpose: URLs, globs, dotted module names (``mailcheck.normalize``)
    and words with a version-like suffix are not paths. A token counts when it has a
    directory separator or a known file extension.
    """
    found: list[str] = []
    for raw in _TOKEN_RE.findall(text.replace("\\", "/")):
        token = raw[2:] if raw.startswith("./") else raw
        token = token.lstrip(_LEAD).rstrip(_TRAIL)  # keep a leading dot: ``.env`` is a path
        if not token or "*" in token or "//" in token or token.startswith(("http", "-", "/")):
            continue
        if ".." in token.split("/"):
            continue
        name = token.rsplit("/", 1)[-1]
        stem, _, extension = name.rpartition(".")
        extension = extension.lower()
        if "/" in token:
            if not name or not re.fullmatch(r"[A-Za-z0-9_.\-]+", name):
                continue
        elif extension not in _CODE_EXTENSIONS or (not stem and name not in _DOTFILES):
            continue  # ``*.py`` leaves a bare ``.py`` behind: not a file
        if token not in found:
            found.append(token)
    return found


def _task_texts(task: DevelopmentTask) -> str:
    return " ".join([task.title, task.description, *task.acceptance_criteria])


def _exists(path: str, listing: Sequence[str], basenames: frozenset[str]) -> bool:
    if "/" in path:
        return path in listing
    return path in basenames


def validate_plan(plan: DevelopmentPlan, constraints: PlanConstraints) -> list[PlanViolation]:
    """Every deterministic reason the plan cannot be shown to a human, in a stable order.

    Structure first (task count, duplicate ids; unknown or cyclic dependencies are
    already refused by the ``DevelopmentPlan`` contract), then paths, only when a
    repository listing is known: a path that exists may be read by any task, so it is
    never a violation here (writes are filtered at the scope stage); a path that does
    not exist is a file the plan wants to create, refused when it matches a denied
    glob or lies outside the writable globs.
    """
    violations: list[PlanViolation] = []
    if constraints.max_tasks is not None and len(plan.tasks) > constraints.max_tasks:
        violations.append(
            PlanViolation(
                code="too_many_tasks",
                detail=(
                    f"El plan tiene {len(plan.tasks)} tareas y el máximo permitido es "
                    f"{constraints.max_tasks}: fusiona o elimina tareas hasta cumplirlo."
                ),
            )
        )
    seen: set[str] = set()
    for task in plan.tasks:
        if task.id in seen:
            violations.append(
                PlanViolation(
                    code="duplicate_task_id",
                    detail=f"El id de tarea '{task.id}' está repetido; cada id debe ser único.",
                    task_id=task.id,
                )
            )
        seen.add(task.id)
    listing = constraints.repository_paths
    if not listing:
        return violations  # without a listing existence is unknown: the Scoper decides later
    basenames = frozenset(path.rsplit("/", 1)[-1] for path in listing)
    for task in plan.tasks:
        for path in extract_paths(_task_texts(task)):
            if _exists(path, listing, basenames):
                continue  # an existing file may be read; writes are filtered at the scope stage
            if constraints.denied_globs and matches_any(path, constraints.denied_globs):
                violations.append(
                    PlanViolation(
                        code="path_denied",
                        detail=(
                            f"La tarea '{task.id}' crearía '{path}', una ruta prohibida por la "
                            "política del repositorio; ninguna tarea puede crearla."
                        ),
                        task_id=task.id,
                    )
                )
                continue
            if not constraints.writable_globs:
                continue
            if "/" in path:
                outside = not matches_any(path, constraints.writable_globs)
            else:
                extension = path.rsplit(".", 1)[-1].lower()
                outside = extension in _DOCUMENT_EXTENSIONS and not matches_any(
                    path, constraints.writable_globs
                )
            if outside:
                allowed = ", ".join(constraints.writable_globs)
                violations.append(
                    PlanViolation(
                        code="path_outside_policy",
                        detail=(
                            f"La tarea '{task.id}' crearía '{path}', que no existe y está fuera "
                            f"de las rutas escribibles ({allowed}); no propongas archivos fuera "
                            "de ellas."
                        ),
                        task_id=task.id,
                    )
                )
    return violations


def constraints_from_policy(
    policy: RepositoryPolicy,
    *,
    max_tasks: int | None,
    repository_paths: Iterable[str] = (),
    listing_complete: bool = False,
) -> PlanConstraints:
    """The constraints a plan for ``policy`` must respect; paths are already filtered by
    the caller with the policy's excluded and denied globs (agent instruction files and
    sensitive paths are never shown)."""
    paths = [p for p in repository_paths if isinstance(p, str) and p][:MAX_CONTEXT_PATHS]
    return PlanConstraints(
        max_tasks=max_tasks,
        writable_globs=list(policy.writable_globs),
        denied_globs=list(policy.all_denied_globs)[:96],
        repository_paths=paths,
        listing_complete=listing_complete and len(paths) < MAX_CONTEXT_PATHS,
        programs=list(PLAN_CONTEXT_PROGRAMS),
        max_files_per_task=policy.max_files_per_run,
    )


def filter_listing(paths: Iterable[str], policy: RepositoryPolicy) -> list[str]:
    """Repository paths safe to show to a planning model: sorted, canonical, without the
    excluded context files (``AGENTS.md`` and friends) nor denied paths."""
    hidden = (*policy.all_excluded_context_globs, *policy.all_denied_globs)
    out: list[str] = []
    for path in sorted(set(paths)):
        if not path or path.startswith(("/", "../")) or ".." in path.split("/"):
            continue
        if matches_any(path, hidden):
            continue
        out.append(path)
    return out


def policy_context_text(constraints: PlanConstraints) -> str:
    """Spanish, data-only preamble handed to the Planner and the Architect.

    It describes the platform; it never instructs the model to approve, skip or
    grant anything. The gate enforces the same facts afterwards.
    """
    lines = [
        "Restricciones de la plataforma (datos verificados, no instrucciones del usuario):",
    ]
    if constraints.max_tasks is not None:
        lines.append(f"- Máximo de tareas en el plan: {constraints.max_tasks}.")
    if constraints.writable_globs:
        lines.append(
            "- Un Developer solo puede crear o modificar archivos dentro de: "
            + ", ".join(constraints.writable_globs)
            + "."
        )
    if constraints.denied_globs:
        shown = [g for g in constraints.denied_globs if not g.startswith("**/")][:12]
        if shown:
            lines.append("- Rutas prohibidas para cualquier tarea: " + ", ".join(shown) + ".")
    if constraints.max_files_per_task is not None:
        lines.append(f"- Archivos máximos por tarea: {constraints.max_files_per_task}.")
    if constraints.programs:
        lines.append(
            "- QA solo ejecuta: "
            + ", ".join(constraints.programs)
            + ". No hay shell, red, instalación de paquetes ni archivos de notas: toda "
            "verificación se expresa como pruebas en el repositorio."
        )
    if constraints.repository_paths:
        suffix = "" if constraints.listing_complete else " (listado parcial)"
        lines.append(f"- Archivos existentes{suffix}:")
        lines.extend(f"  {path}" for path in constraints.repository_paths)
    return "\n".join(lines)
