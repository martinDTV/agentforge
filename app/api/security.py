"""Authentication dependencies (ADR 0005).

``get_principal`` is a plain ``def`` on purpose: resolving a signing key may
be a blocking HTTP call to the provider's JWKS endpoint on cache miss, so
FastAPI runs it in the threadpool. Every router under ``/api/v1`` depends on
it; health probes do not.
"""

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.security import AuthenticationError, Principal, TokenVerifier

bearer_scheme = HTTPBearer(auto_error=False, description="Token de acceso OIDC (JWT)")


def get_token_verifier(request: Request) -> TokenVerifier:
    verifier: TokenVerifier = request.app.state.token_verifier
    return verifier


def get_principal(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    verifier: Annotated[TokenVerifier, Depends(get_token_verifier)],
) -> Principal:
    if credentials is None or not credentials.credentials:
        if verifier.allows_anonymous:
            return verifier.verify("")
        raise AuthenticationError("missing_token")
    return verifier.verify(credentials.credentials)


CurrentPrincipalDep = Annotated[Principal, Depends(get_principal)]
