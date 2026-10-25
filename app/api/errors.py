"""Map domain exceptions and database outages to RFC 9457 Problem Details."""

import logging

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from app.exceptions import ConflictError, DomainError, NotFoundError, UnavailableError
from app.infrastructure.db.errors import DatabaseUnavailableError, as_unavailable
from app.security import AuthenticationError, PermissionDeniedError

logger = logging.getLogger(__name__)

RETRY_AFTER_SECONDS = "5"

_STATUS_BY_ERROR: dict[type[DomainError], int] = {
    AuthenticationError: status.HTTP_401_UNAUTHORIZED,
    PermissionDeniedError: status.HTTP_403_FORBIDDEN,
    NotFoundError: status.HTTP_404_NOT_FOUND,
    ConflictError: status.HTTP_409_CONFLICT,
    UnavailableError: status.HTTP_503_SERVICE_UNAVAILABLE,
}


def _problem(
    status_code: int,
    title: str,
    detail: str,
    code: str,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        media_type="application/problem+json",
        headers=headers,
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
    headers: dict[str, str] | None = None
    if status_code == status.HTTP_503_SERVICE_UNAVAILABLE:
        headers = {"Retry-After": RETRY_AFTER_SECONDS}
    elif isinstance(exc, AuthenticationError):
        # RFC 6750: tell the client how to authenticate, never why it failed in detail.
        challenge = "Bearer"
        if exc.reason != "missing_token":
            challenge += ' error="invalid_token"'
        headers = {"WWW-Authenticate": challenge}
    return _problem(status_code, type(exc).__name__, exc.detail, exc.code, headers=headers)


def _database_unavailable(request: Request, exc: DatabaseUnavailableError) -> JSONResponse:
    # Logged once, sanitized by the infrastructure layer (no DSN, no credentials).
    logger.warning(
        "PostgreSQL no disponible en %s %s: %s", request.method, request.url.path, exc.message
    )
    return _problem(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "ServiceUnavailable",
        "La base de datos no está disponible en este momento; reintenta en unos segundos.",
        "database_unavailable",
        headers={"Retry-After": RETRY_AFTER_SECONDS},
    )


async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DatabaseUnavailableError):  # pragma: no cover
        raise exc
    return _database_unavailable(request, exc)


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    # Raw driver errors can surface from the LangGraph checkpointer, which does
    # not go through our repository. Only connection-level ones become 503.
    unavailable = as_unavailable(exc)
    if unavailable is not None:
        return _database_unavailable(request, unavailable)
    logger.exception("Unhandled error on %s %s", request.method, request.url.path)
    return _problem(
        status.HTTP_500_INTERNAL_SERVER_ERROR,
        "InternalServerError",
        "Ocurrió un error inesperado.",
        "internal_error",
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, domain_error_handler)
    app.add_exception_handler(DatabaseUnavailableError, database_unavailable_handler)
    app.add_exception_handler(Exception, unhandled_error_handler)
