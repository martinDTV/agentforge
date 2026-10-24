"""Map domain exceptions to RFC 9457 Problem Details responses."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.exceptions import ConflictError, DomainError, NotFoundError

logger = logging.getLogger(__name__)

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
}


def _problem(status_code: int, title: str, detail: str, code: str) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        content={
            "type": "about:blank",
            "title": title,
            "status": status_code,
            "detail": detail,
            "code": code,
        },
    )


async def domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):  # pragma: no cover - registered for DomainError only
        raise exc
    status_code = next(
        (code for cls, code in _STATUS_BY_ERROR.items() if isinstance(exc, cls)),
        status.HTTP_400_BAD_REQUEST,
    )
    return _problem(status_code, type(exc).__name__, exc.detail, exc.code)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _problem(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "InternalServerError",
        "Ocurrió un error inesperado.",
        "internal_error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
