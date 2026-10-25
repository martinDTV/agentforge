from typing import Annotated

from fastapi import Depends, Request

from app.orchestration.recovery import RecoveryService
from app.orchestration.runs import RunService


def get_run_service(request: Request) -> RunService:
    service: RunService = request.app.state.run_service
    return service


def get_recovery_service(request: Request) -> RecoveryService:
    recovery: RecoveryService = request.app.state.recovery
    return recovery


RunServiceDep = Annotated[RunService, Depends(get_run_service)]
RecoveryServiceDep = Annotated[RecoveryService, Depends(get_recovery_service)]
