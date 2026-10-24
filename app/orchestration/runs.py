from uuid import uuid4

from langchain_core.runnables import RunnableConfig
from langgraph.types import Command

from app.contracts.execution import RunResponse
from app.orchestration.exceptions import RunAlreadyDecidedError, RunNotFoundError
from app.orchestration.graph import RunGraph
from app.orchestration.status import run_response_from_snapshot


def _config(run_id: str) -> RunnableConfig:
    return {"configurable": {"thread_id": run_id}}


class RunService:
    """Use cases over a compiled run graph. Raises domain errors, never HTTP ones.

    The graph (and its checkpointer) is shared for the lifetime of the app so
    that creating, reading and approving a run see the same state per run_id.
    All operations are blocking: callers from async code must offload them.
    """

    def __init__(self, graph: RunGraph) -> None:
        self._graph = graph

    def create(self, task: str) -> RunResponse:
        run_id = str(uuid4())
        self._graph.invoke({"task": task}, _config(run_id))
        return self.get(run_id)

    def get(self, run_id: str) -> RunResponse:
        snapshot = self._graph.get_state(_config(run_id))
        if "task" not in snapshot.values:
            raise RunNotFoundError(run_id)
        return run_response_from_snapshot(run_id, snapshot)

    def approve(self, run_id: str, approved: bool) -> RunResponse:
        current = self.get(run_id)
        if current.status != "waiting_approval":
            raise RunAlreadyDecidedError(run_id, current.status)
        self._graph.invoke(Command(resume={"approved": approved}), _config(run_id))
        return self.get(run_id)
