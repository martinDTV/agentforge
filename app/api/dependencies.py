from typing import Annotated

from fastapi import Depends, Request

from app.orchestration.runs import RunService


def get_run_service(request: Request) -> RunService:
    service: RunService = request.app.state.run_service
    return service


RunServiceDep = Annotated[RunService, Depends(get_run_service)]
