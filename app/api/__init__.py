from fastapi import APIRouter

from app.api import runs

api_router = APIRouter(prefix="/api/v1")
api_router.include_router(runs.router)

__all__ = ["api_router"]
