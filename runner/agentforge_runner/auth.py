"""Bearer-token authentication. The runner holds only the token's SHA-256."""

import hashlib
import hmac
import ipaddress
import secrets
from collections.abc import Iterable


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def bearer_matches(authorization: str | None, expected_sha256: str) -> bool:
    if not authorization:
        return False
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token or len(token) > 512:
        return False
    return hmac.compare_digest(token_digest(token.strip()), expected_sha256)


def client_allowed(host: str | None, cidrs: Iterable[str]) -> bool:
    if host is None:
        return False
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(cidr) for cidr in cidrs)


def generate_token() -> str:
    return secrets.token_urlsafe(48)
