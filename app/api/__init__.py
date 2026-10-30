from fastapi import APIRouter, Depends

from app.api import admin, goals, runs
from app.api.security import get_principal

# Every business route requires an authenticated principal; health does not.
api_router = APIRouter(prefix="/api/v1", dependencies=[Depends(get_principal)])
api_router.include_router(runs.router)
api_router.include_router(admin.router)
api_router.include_router(goals.router)

__all__ = ["api_router"]
