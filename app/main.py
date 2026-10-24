from uuid import uuid4

from fastapi import FastAPI, HTTPException
from langgraph.types import Command

from app.contracts import Approval, ArchitectureReview, CreateRun, DevelopmentPlan, RunResponse
from app.infrastructure.llm import build_llm
from app.infrastructure.llm.port import LLMPort
from app.orchestration.graph import build_graph


def create_app(llm: LLMPort | None = None) -> FastAPI:
    """Build the FastAPI app with a graph bound to the given LLMPort.

    The graph (and its in-memory checkpointer) must be a singleton for the
    lifetime of the app: it is what lets /runs and /runs/{id}/approve share
    state across requests for the same run_id. Tests call this factory with
    a FakeLLMAdapter to get an isolated app/graph pair with no network calls.
    """
    app = FastAPI(title="Multiagent Starter", version="0.1.0")
    graph = build_graph(llm or build_llm())

    def config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}}

    def read_run(run_id: str) -> RunResponse:
        snapshot = graph.get_state(config(run_id))
        values = snapshot.values
        if not values or "plan" not in values:
            raise HTTPException(404, "Ejecución no encontrada")
        if snapshot.next:
            status = "waiting_approval"
        else:
            status = "approved" if values.get("approved") is True else "rejected"
        architecture = values.get("architecture")
        return RunResponse(
            run_id=run_id,
            status=status,
            plan=DevelopmentPlan.model_validate(values["plan"]),
            architecture=ArchitectureReview.model_validate(architecture) if architecture else None,
        )

    @app.post("/runs", response_model=RunResponse, status_code=201)
    def create_run(body: CreateRun) -> RunResponse:
        run_id = str(uuid4())
        graph.invoke({"task": body.task}, config(run_id))
        return read_run(run_id)

    @app.get("/runs/{run_id}", response_model=RunResponse)
    def get_run(run_id: str) -> RunResponse:
        return read_run(run_id)

    @app.post("/runs/{run_id}/approve", response_model=RunResponse)
    def approve_run(run_id: str, body: Approval) -> RunResponse:
        current = read_run(run_id)
        if current.status != "waiting_approval":
            raise HTTPException(409, "La ejecución ya fue decidida")
        graph.invoke(Command(resume={"approved": body.approved}), config(run_id))
        return read_run(run_id)

    return app


app = create_app()
