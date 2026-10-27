"""Bounded Developer/QA LangGraph using the existing audited Tool Gateway.

Flow (every edge is explicit, every loop is bounded):

    START -> developer -> apply_changes -> qa -> END
                ^                           |
                +---- revise_development <--+   (QA not satisfied, corrections left)

- ``developer`` reads the authorized files through the gateway and asks the
  model for the complete content of the files it may write.
- ``apply_changes`` writes those files through ``workspace.write_file`` with
  the hash observed at read time (a changed file is a conflict, never an
  overwrite), enforces the per-file and per-run size limits and runs the
  Developer's own lint checks in the remote sandbox.
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

``DevelopmentSession`` holds the per-run pieces (scope, gateway, budget) so the
same node logic serves the operator graph of Fase 6 (one session per graph)
and the governed graph of Fase 7 (one session rebuilt per node from the
checkpoint, after a human approved the scope).
"""

import asyncio
import hashlib
import time
from collections.abc import Callable
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
MAX_WRITE_BYTES = 32_768  # the hard limit of workspace.write_file

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
    # Size limits of one revision (ADR 0009): per file and for all files together.
    max_file_bytes: int = MAX_WRITE_BYTES
    max_change_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if not 0 <= self.max_corrections <= MAX_CORRECTIONS:
            raise ValueError(f"max_corrections debe estar entre 0 y {MAX_CORRECTIONS}")
        if self.wall_seconds <= 0 or self.model_timeout_seconds <= 0:
            raise ValueError("límites de tiempo inválidos")
        if not self.read_paths or not self.lint_paths or not self.test_paths:
            raise ValueError("read_paths, lint_paths y test_paths no pueden estar vacíos")
        if not 0 < self.max_file_bytes <= MAX_WRITE_BYTES or self.max_change_bytes <= 0:
            raise ValueError("límites de tamaño inválidos")

    @property
    def writable_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.scope.writable_paths))


class DevelopmentSession:
    """Node logic bound to one run's scope, gateway and budget.

    ``deadline`` is an absolute epoch second; when omitted the wall clock starts
    with the first call (the operator graph), otherwise it comes from the
    checkpoint (the governed graph) and survives process restarts.
    """

    def __init__(
        self,
        deps: DevelopmentDeps,
        *,
        deadline: float | None = None,
        now: Callable[[], float] = time.time,
    ) -> None:
        self.deps = deps
        self.developer = DeveloperAgent(deps.llm)
        self.qa = QAAgent(deps.llm)
        self.identity = AutomationIdentity()
        self._deadline = deadline
        self._now = now

    # -- limits -------------------------------------------------------------------

    def remaining(self) -> float:
        if self._deadline is None:
            self._deadline = self._now() + self.deps.wall_seconds
        left = self._deadline - self._now()
        if left <= 0:
            raise DevelopmentFailure("development_timeout", "se agotó el tiempo de ejecución")
        return left

    # -- gateway --------------------------------------------------------------------

    def context(self, agent: AgentKind) -> ToolContext:
        scope = self.deps.scope
        return ToolContext(
            run_id=scope.run_id,
            project_id=scope.project_id,
            agent=agent,
            principal=self.identity.principal_for(agent, scope.project_id),
            workspace_root=scope.workspace_root,
        )

    async def call(
        self, agent: AgentKind, name: str, args: dict[str, Any], step: str
    ) -> ToolResult:
        self.remaining()
        return await asyncio.wait_for(
            self.deps.gateway.invoke(
                self.context(agent), ToolCall(name=name, arguments=args), step=step
            ),
            timeout=self.remaining(),
        )

    async def invoke(
        self, agent: AgentKind, name: str, args: dict[str, Any], step: str
    ) -> dict[str, Any]:
        result = await self.call(agent, name, args, step)
        if result.outcome is not ToolOutcome.OK:
            raise DevelopmentFailure(
                result.error.code if result.error else "tool_failed",
                result.error.message if result.error else "falló la herramienta",
            )
        return result.output or {}

    async def files(
        self, agent: AgentKind, revision: int
    ) -> tuple[dict[str, str], dict[str, str | None]]:
        """Contents and hashes of the context files plus the writable ones.

        Context files must exist. A writable file that does not exist yet is
        reported with hash ``None``: the write will then require that it is
        still absent (``expected_sha256=None``).
        """
        deps = self.deps
        contents: dict[str, str] = {}
        hashes: dict[str, str | None] = {}
        ordered = list(deps.read_paths) + [
            p for p in deps.writable_paths if p not in deps.read_paths
        ]
        for index, path in enumerate(ordered):
            result = await self.call(
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

    async def fix_preview(
        self, agent: AgentKind, revision: int, paths: list[str]
    ) -> dict[str, Any]:
        """Best effort: ruff's own fix as a diff. Never changes the run's outcome."""
        result = await self.call(
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
        self, agent: AgentKind, revision: int, programs: tuple[str, ...], *, preview: bool = False
    ) -> list[dict[str, Any]]:
        deps = self.deps
        results = []
        for program in programs:
            paths = list(deps.test_paths if program == "pytest" else deps.lint_paths)
            result = await self.invoke(
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
                result = {**result, **(await self.fix_preview(agent, revision, paths))}
            results.append(result)
        return results

    # -- helpers ---------------------------------------------------------------------

    def failure(self, exc: BaseException) -> dict[str, Any]:
        if isinstance(exc, TimeoutError):
            code, message = "development_timeout", "se agotó el tiempo autorizado"
        else:
            code = str(getattr(exc, "code", "development_failed"))
            message = str(getattr(exc, "message", getattr(exc, "detail", exc)))
        return {"error": {"code": code, "message": message}, "usage": self.deps.llm.usage()}

    @staticmethod
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

    @staticmethod
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

    # -- nodes -----------------------------------------------------------------------

    async def developer_node(self, state: DevelopmentState) -> dict[str, Any]:
        deps = self.deps
        try:
            revision = state.get("revision", 0)
            contents, hashes = await self.files(AgentKind.DEVELOPER, revision)
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
                "repeated_failures": self.repeated_failures(state),
            }
            changes = await asyncio.wait_for(
                asyncio.to_thread(self.developer.execute, prompt),
                timeout=min(self.remaining(), deps.model_timeout_seconds),
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
            return self.failure(exc)

    async def apply_node(self, state: DevelopmentState) -> dict[str, Any]:
        deps = self.deps
        try:
            revision = state.get("revision", 0)
            edits = state["changes"]["edits"]
            sizes = [len(str(edit["content"]).encode("utf-8")) for edit in edits]
            if any(size > deps.max_file_bytes for size in sizes) or sum(sizes) > (
                deps.max_change_bytes
            ):
                raise DevelopmentFailure(
                    "change_too_large",
                    f"el cambio supera los límites del proyecto ({deps.max_file_bytes} bytes por "
                    f"archivo, {deps.max_change_bytes} bytes en total)",
                )
            for index, edit in enumerate(edits):
                await self.invoke(
                    AgentKind.DEVELOPER,
                    "workspace.write_file",
                    {**edit, "expected_sha256": state["file_hashes"].get(edit["path"])},
                    f"developer:{revision}:write:{index}",
                )
            results = await self.checks(AgentKind.DEVELOPER, revision, DEVELOPER_PROGRAMS)
            return {
                "developer_checks": self.technical_summary(results),
                "changed_files": sorted(
                    set(state.get("changed_files", [])) | {edit["path"] for edit in edits}
                ),
            }
        except (DevelopmentFailure, TimeoutError) as exc:
            return self.failure(exc)

    async def qa_node(self, state: DevelopmentState) -> dict[str, Any]:
        deps = self.deps
        try:
            revision = state.get("revision", 0)
            contents, _ = await self.files(AgentKind.QA, revision)
            diff = await self.invoke(AgentKind.QA, "git.diff", {}, f"qa:{revision}:diff")
            results = self.technical_summary(
                await self.checks(AgentKind.QA, revision, QA_PROGRAMS, preview=True)
            )
            review = await asyncio.wait_for(
                asyncio.to_thread(
                    self.qa.execute,
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
                timeout=min(self.remaining(), deps.model_timeout_seconds),
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
            return self.failure(exc)


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


def add_development_edges(builder: StateGraph, *, entry: str) -> None:  # type: ignore[type-arg]
    """The Developer -> apply -> QA -> revise loop, entered from ``entry``."""
    builder.add_edge(entry, "developer")
    builder.add_conditional_edges(
        "developer", after_developer, {"apply_changes": "apply_changes", END: END}
    )
    builder.add_conditional_edges("apply_changes", after_apply, {"qa": "qa", END: END})
    builder.add_conditional_edges(
        "qa", after_qa, {"revise_development": "revise_development", END: END}
    )
    builder.add_edge("revise_development", "developer")


def build_development_graph(
    deps: DevelopmentDeps, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> Any:
    """The operator graph of Fase 6: one session with a scope fixed before the run."""
    session = DevelopmentSession(deps)
    builder = StateGraph(DevelopmentState)
    builder.add_node("developer", session.developer_node)
    builder.add_node("apply_changes", session.apply_node)
    builder.add_node("qa", session.qa_node)
    builder.add_node("revise_development", revise)
    add_development_edges(builder, entry=START)
    return builder.compile(checkpointer=checkpointer or InMemorySaver())
