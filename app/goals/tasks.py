"""Deterministic text of a child task and its duplicate-detection fingerprint."""

import hashlib
import re

from app.contracts.planning import DevelopmentTask

OBJECTIVE_SNIPPET_CHARS = 600
MAX_TASK_TEXT_CHARS = 4000  # ``CreateRun.task`` upper bound


def _snippet(text: str, limit: int) -> str:
    one = " ".join(text.split())
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def single_task_text(objective: str) -> str:
    return " ".join(objective.split())[:MAX_TASK_TEXT_CHARS]


def task_text(task: DevelopmentTask, *, index: int, total: int, objective: str) -> str:
    """Title, description, acceptance criteria and the objective it belongs to.

    Built by the platform from the human-approved plan; the only model text in
    it is the plan's own, which a human already read when approving it.
    """
    criteria = "; ".join(_snippet(c, 300) for c in task.acceptance_criteria)
    lines = [
        f"{_snippet(task.title, 200)}. {_snippet(task.description, 1200)}",
        f"Criterios de aceptación: {criteria}.",
        f"Parte {index}/{total} del objetivo: {_snippet(objective, OBJECTIVE_SNIPPET_CHARS)}",
    ]
    return "\n".join(lines)[:MAX_TASK_TEXT_CHARS]


_SPACES = re.compile(r"\s+")


def fingerprint(repository_id: str, text: str) -> str:
    """sha256 of the repository and the normalised text (case and whitespace folded)."""
    normalised = _SPACES.sub(" ", text.strip().lower())
    return hashlib.sha256(f"{repository_id}\n{normalised}".encode()).hexdigest()
