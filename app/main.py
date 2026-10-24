from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import api_router
from app.api.errors import register_exception_handlers
from app.infrastructure.llm import build_llm
from app.infrastructure.llm.port import LLMPort
from app.orchestration.graph import build_graph
from app.orchestration.limits import RunLimits
from app.orchestration.runs import RunService
from app.settings import get_settings


def create_app(llm: LLMPort | None = None, limits: RunLimits | None = None) -> FastAPI:
    """Build the FastAPI app.

    Wiring happens in ``lifespan`` (not at import time) and lands on
    ``app.state.run_service``. With no arguments everything comes from
    ``Settings`` (production). Tests pass a ``FakeLLMAdapter`` and explicit
    ``RunLimits`` so they never touch Settings or the network.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        port, run_limits = llm, limits
        if port is None:
            settings = get_settings()
            port = build_llm(settings)
            run_limits = run_limits or RunLimits(
                max_revisions=settings.max_plan_revisions,
                max_llm_calls=settings.max_llm_calls_per_run,
            )
        app.state.run_service = RunService(build_graph(port, run_limits or RunLimits()))
        yield

    app = FastAPI(title="AgentForge", version="0.2.0", lifespan=lifespan)
    register_exception_handlers(app)
    app.include_router(api_router)
    return app


app = create_app()
