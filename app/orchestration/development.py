"""Bounded Developer/QA LangGraph using the existing audited Tool Gateway.

Flow (every edge is explicit, every loop is bounded):

    START -> developer -> apply_changes -> qa -> END
                ^                           |
                +---- revise_development <--+   (QA not satisfied, corrections left)

- ``developer`` reads the authorized files through the gateway and asks the
  model for the complete content of the files it may write.
- ``apply_changes`` writes those files through ``workspace.write_file`` with
  the hash observed at read time (a changed file is a conflict, never an
  overwrite) and runs the Developer's own lint checks in the remote sandbox.
- ``qa`` re-reads the files, takes the diff, runs ruff check, ruff format
  --check and pytest in the remote sandbox and asks the model for a review.
  A review can never turn a failed technical check into an approval: the
  orchestrator forces ``approved=False`` and adds the failing programs as
  observations.
- ``revise_development`` increments the revision; at most ``max_corrections``
  corrections happen, after which the run ends with ``correction_limit``.
- QA approval ends the run as ``completed`` (never ``approved``, which is a
  human verdict in this platform).

Any tool failure, model failure, budget exhaustion, timeout or cancellation
ends the run with an explicit error; nothing is retried implicitly.
"""

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from app.agents.development import DeveloperAgent, QAAgent, QAObservation, QAReview
from app.contracts.lifecycle import WorkflowKind
from app.contracts.tools import AgentKind, ToolCall, ToolOutcome, ToolResult
from app.infrastructure.llm.budget import BudgetedLLM
from app.infrastructure.llm.errors import LLMError
from app.security import AutomationIdentity
from app.security.development import DevelopmentScope
from app.tools import ToolContext, ToolGateway

from .graph import RunState

DEVELOPMENT_WORKFLOW = WorkflowKind.DEVELOPMENT.value
TECHNICAL_FAILURE_CODE = "technical_checks_failed"
MAX_CORRECTIONS = 3
DEVELOPER_PROGRAMS: tuple[str, ...] = ("ruff_check", "ruff_format_check")
QA_PROGRAMS: tuple[str, ...] = ("ruff_check", "ruff_format_check", "pytest")
RUFF_DIFF_PROGRAM = "ruff_check_diff"
FIX_PREVIEW_CHARS = 4000


# The development keys live in ``RunState`` so the API's planning graph can
# read a development checkpoint (status, recovery, audit back-fill).
DevelopmentState = RunState


class DevelopmentFailure(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class DevelopmentDeps:
    """Trusted inputs of one development run; nothing here comes from a model."""

    scope: DevelopmentScope
    gateway: ToolGateway
    llm: BudgetedLLM
    # Files the agents must read for context; every one must exist.
    read_paths: tuple[str, ...]
    # Paths handed to ruff and pytest (pytest stays confined to the
    # repository's authorized test paths by ``exec.run`` itself).
    lint_paths: tuple[str, ...] = ("src", "tests")
    test_paths: tuple[str, ...] = ("tests",)
    max_corrections: int = MAX_CORRECTIONS
    wall_seconds: float = 600
    model_timeout_seconds: float = 120
    # When QA's ruff check fails, also run ``ruff check --diff`` (read-only) so
    # the agents see the exact fix instead of guessing. Needs a runner whose
    # allowlist knows ``ruff_check_diff``; an older runner refuses it and the
    # preview is simply reported as unavailable.
    ruff_diff: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.max_corrections <= MAX_CORRECTIONS:
            raise ValueError(f"max_corrections debe estar entre 0 y {MAX_CORRECTIONS}")
        if self.wall_seconds <= 0 or self.model_timeout_seconds <= 0:
            raise ValueError("límites de tiempo inválidos")
        if not self.read_paths or not self.lint_paths or not self.test_paths:
            raise ValueError("read_paths, lint_paths y test_paths no pueden estar vacíos")

    @property
    def writable_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.scope.writable_paths))


def build_development_graph(
    deps: DevelopmentDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> Any:
    developer, qa = DeveloperAgent(deps.llm), QAAgent(deps.llm)
    identity = AutomationIdentity()
    started: list[float] = []  # wall clock starts with the first node, not at build time

    def remaining() -> float:
        if not started:
            started.append(time.monotonic())
        left = started[0] + deps.wall_seconds - time.monotonic()
        if left <= 0:
            raise DevelopmentFailure("development_timeout", "se agotó el tiempo de ejecución")
        return left

    def context(agent: AgentKind) -> ToolContext:
        return ToolContext(
            run_id=deps.scope.run_id,
            project_id=deps.scope.project_id,
            agent=agent,
            principal=identity.principal_for(agent, deps.scope.project_id),
            workspace_root=deps.scope.workspace_root,
        )

    async def call(agent: AgentKind, name: str, args: dict[str, Any], step: str) -> ToolResult:
        remaining()
        return await asyncio.wait_for(
            deps.gateway.invoke(context(agent), ToolCall(name=name, arguments=args), step=step),
            timeout=remaining(),
        )

    async def invoke(
        agent: AgentKind, name: str, args: dict[str, Any], step: str
    ) -> dict[str, Any]:
        result = await call(agent, name, args, step)
        if result.outcome is not ToolOutcome.OK:
            raise DevelopmentFailure(
                result.error.code if result.error else "tool_failed",
                result.error.message if result.error else "falló la herramienta",
            )
        return result.output or {}

    async def files(
        agent: AgentKind, revision: int
    ) -> tuple[dict[str, str], dict[str, str | None]]:
        """Contents and hashes of the context files plus the writable ones.

        Context files must exist. A writable file that does not exist yet is
        reported with hash ``None``: the write will then require that it is
        still absent (``expected_sha256=None``).
        """
        contents: dict[str, str] = {}
        hashes: dict[str, str | None] = {}
        ordered = list(deps.read_paths) + [
            p for p in deps.writable_paths if p not in deps.read_paths
        ]
        for index, path in enumerate(ordered):
            result = await call(
                agent,
                "workspace.read_file",
                {"path": path},
                f"{agent.value}:{revision}:read:{index}",
            )
            if result.outcome is not ToolOutcome.OK:
                code = result.error.code if result.error else "tool_failed"
                if code == "path_not_found" and path in deps.scope.writable_paths:
                    hashes[path] = None
                    continue
                raise DevelopmentFailure(code, result.error.message if result.error else path)
            output = result.output or {}
            if output.get("truncated") or output.get("redactions"):
                raise DevelopmentFailure(
                    "unsafe_model_context", f"lectura truncada o con secretos: {path}"
                )
            contents[path] = output["content"]
            hashes[path] = hashlib.sha256(output["content"].encode("utf-8")).hexdigest()
        return contents, hashes

    async def fix_preview(agent: AgentKind, revision: int, paths: list[str]) -> dict[str, Any]:
        """Best effort: ruff's own fix as a diff. Never changes the run's outcome."""
        result = await call(
            agent,
            "exec.run",
            {"program": RUFF_DIFF_PROGRAM, "paths": paths},
            f"{agent.value}:{revision}:{RUFF_DIFF_PROGRAM}",
        )
        output = result.output or {}
        if result.outcome is not ToolOutcome.OK or output.get("status") != "completed":
            code = result.error.code if result.error else str(output.get("status"))
            return {"fix_preview_error": code}
        return {"fix_preview": str(output.get("stdout", ""))[:FIX_PREVIEW_CHARS]}

    async def checks(
        agent: AgentKind, revision: int, programs: tuple[str, ...], *, preview: bool = False
    ) -> list[dict[str, Any]]:
        results = []
        for program in programs:
            paths = list(deps.test_paths if program == "pytest" else deps.lint_paths)
            result = await invoke(
                agent,
                "exec.run",
                {"program": program, "paths": paths},
                f"{agent.value}:{revision}:{program}",
            )
            if (
                result.get("status") != "completed"
                or result.get("stdout_truncated")
                or result.get("stderr_truncated")
            ):
                raise DevelopmentFailure(
                    "validation_incomplete",
                    f"{program} terminó en {result.get('status')} o con salida truncada",
                )
            if preview and deps.ruff_diff and program == "ruff_check" and result.get("exit_code"):
                result = {**result, **(await fix_preview(agent, revision, paths))}
            results.append(result)
        return results

    def failure(exc: BaseException) -> dict[str, Any]:
        if isinstance(exc, TimeoutError):
            code, message = "development_timeout", "se agotó el tiempo autorizado"
        else:
            code = str(getattr(exc, "code", "development_failed"))
            message = str(getattr(exc, "message", exc))
        return {"error": {"code": code, "message": message}, "usage": deps.llm.usage()}

    def technical_summary(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """What the model and the audit see: bounded, no raw payload duplication."""
        return [
            {
                "program": r.get("program"),
                "status": r.get("status"),
                "exit_code": r.get("exit_code"),
                "stdout": str(r.get("stdout", ""))[:6000],
                "stderr": str(r.get("stderr", ""))[:3000],
                "duration_ms": r.get("duration_ms"),
                "runner_id": r.get("runner_id"),
                **{k: r[k] for k in ("fix_preview", "fix_preview_error") if k in r},
            }
            for r in results
        ]

    def repeated_failures(state: DevelopmentState) -> list[str]:
        """Programs whose QA output failed identically in the last two QA rounds.

        Told to the model explicitly: repeating the same edit will not help.
        """
        previous = state.get("prior_validation") or []
        current = state.get("validation") or []
        by_program = {r.get("program"): r for r in previous if r.get("exit_code") != 0}
        return sorted(
            str(r.get("program"))
            for r in current
            if r.get("exit_code") != 0
            and r.get("program") in by_program
            and by_program[r.get("program")].get("stdout") == r.get("stdout")
        )

    async def developer_node(state: DevelopmentState) -> dict[str, Any]:
        try:
            revision = state.get("revision", 0)
            contents, hashes = await files(AgentKind.DEVELOPER, revision)
            prompt = {
                "task": state["task"],
                "revision": revision,
                "files": contents,
                "writable_paths": list(deps.writable_paths),
                "must_create": [p for p in deps.writable_paths if hashes.get(p) is None],
                "may_edit": [p for p in deps.writable_paths if hashes.get(p) is not None],
                "previous_summary": (state.get("changes") or {}).get("summary"),
                "previous_qa": state.get("qa_review"),
                "previous_validation": state.get("validation"),
                "repeated_failures": repeated_failures(state),
            }
            changes = await asyncio.wait_for(
                asyncio.to_thread(developer.execute, prompt),
                timeout=min(remaining(), deps.model_timeout_seconds),
            )
            denied = [e.path for e in changes.edits if e.path not in deps.scope.writable_paths]
            if denied:
                raise DevelopmentFailure(
                    "path_denied", f"el modelo propuso editar archivos no autorizados: {denied}"
                )
            return {
                "changes": changes.model_dump(),
                "file_hashes": hashes,
                "llm_calls": 1,
                "usage": deps.llm.usage(),
                "revision": revision,
            }
        except (DevelopmentFailure, LLMError, TimeoutError) as exc:
            return failure(exc)

    async def apply_node(state: DevelopmentState) -> dict[str, Any]:
        try:
            revision = state.get("revision", 0)
            for index, edit in enumerate(state["changes"]["edits"]):
                await invoke(
                    AgentKind.DEVELOPER,
                    "workspace.write_file",
                    {**edit, "expected_sha256": state["file_hashes"].get(edit["path"])},
                    f"developer:{revision}:write:{index}",
                )
            results = await checks(AgentKind.DEVELOPER, revision, DEVELOPER_PROGRAMS)
            return {
                "developer_checks": technical_summary(results),
                "changed_files": sorted(
                    set(state.get("changed_files", []))
                    | {edit["path"] for edit in state["changes"]["edits"]}
                ),
            }
        except (DevelopmentFailure, TimeoutError) as exc:
            return failure(exc)

    async def qa_node(state: DevelopmentState) -> dict[str, Any]:
        try:
            revision = state.get("revision", 0)
            contents, _ = await files(AgentKind.QA, revision)
            diff = await invoke(AgentKind.QA, "git.diff", {}, f"qa:{revision}:diff")
            results = technical_summary(
                await checks(AgentKind.QA, revision, QA_PROGRAMS, preview=True)
            )
            review = await asyncio.wait_for(
                asyncio.to_thread(
                    qa.execute,
                    {
                        "task": state["task"],
                        "revision": revision,
                        "files": contents,
                        "diff": diff.get("patch", ""),
                        "diff_truncated": bool(diff.get("truncated")),
                        "validation": results,
                        "developer_summary": state["changes"]["summary"],
                    },
                ),
                timeout=min(remaining(), deps.model_timeout_seconds),
            )
            failed = [r for r in results if r.get("exit_code") != 0]
            if failed:
                # Technical evidence wins over the model's opinion, always.
                forced = [
                    QAObservation(
                        code=TECHNICAL_FAILURE_CODE,
                        path="",
                        detail=f"{r['program']} terminó con código {r.get('exit_code')}",
                    )
                    for r in failed
                ]
                review = QAReview(
                    approved=False,
                    summary=review.summary,
                    issues=(forced + list(review.issues))[:12],
                )
            update: dict[str, Any] = {
                "qa_review": review.model_dump(),
                "prior_validation": state.get("validation") or [],
                "validation": results,
                "llm_calls": 1,
                "usage": deps.llm.usage(),
                "final_diff": {
                    "patch": str(diff.get("patch", ""))[:60000],
                    "truncated": bool(diff.get("truncated")),
                    "files": diff.get("files", []),
                },
            }
            if review.approved:
                update["completed"] = True
            elif revision >= deps.max_corrections:
                update["error"] = {
                    "code": "correction_limit",
                    "message": (
                        f"QA no aprobó tras {deps.max_corrections} correcciones: "
                        + "; ".join(issue.code for issue in review.issues[:4])
                    ),
                }
            return update
        except (DevelopmentFailure, LLMError, TimeoutError) as exc:
            return failure(exc)

    def revise(state: DevelopmentState) -> dict[str, Any]:
        return {"revision": state.get("revision", 0) + 1}

    def after_developer(state: DevelopmentState) -> str:
        return END if state.get("error") else "apply_changes"

    def after_apply(state: DevelopmentState) -> str:
        return END if state.get("error") else "qa"

    def after_qa(state: DevelopmentState) -> str:
        if state.get("error") or state.get("completed"):
            return END
        return "revise_development"

    builder = StateGraph(DevelopmentState)
    builder.add_node("developer", developer_node)
    builder.add_node("apply_changes", apply_node)
    builder.add_node("qa", qa_node)
    builder.add_node("revise_development", revise)
    builder.add_edge(START, "developer")
    builder.add_conditional_edges(
        "developer", after_developer, {"apply_changes": "apply_changes", END: END}
    )
    builder.add_conditional_edges("apply_changes", after_apply, {"qa": "qa", END: END})
    builder.add_conditional_edges(
        "qa", after_qa, {"revise_development": "revise_development", END: END}
    )
    builder.add_edge("revise_development", "developer")
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
