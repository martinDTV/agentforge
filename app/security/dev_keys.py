"""Local signing keys for development and tests (never for production).

Generates an RSA key pair, exposes the public half as a JWKS document and
signs short-lived access tokens with the private half. Production settings
reject a JWKS file, so nothing here can be wired into a deployed instance.
"""

import json
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from jwt.algorithms import RSAAlgorithm

DEFAULT_ISSUER = "https://agentforge.local/dev-issuer"
DEFAULT_AUDIENCE = "agentforge"


class DevIssuer:
    def __init__(
        self,
        *,
        issuer: str = DEFAULT_ISSUER,
        audience: str = DEFAULT_AUDIENCE,
        kid: str | None = None,
        private_key: RSAPrivateKey | None = None,
    ) -> None:
        self.issuer = issuer
        self.audience = audience
        self.kid = kid or f"dev-{uuid.uuid4().hex[:8]}"
        self._key = private_key or rsa.generate_private_key(public_exponent=65537, key_size=2048)

    @property
    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(RSAAlgorithm.to_jwk(self._key.public_key()))
        jwk.update(kid=self.kid, use="sig", alg="RS256")
        return {"keys": [jwk]}

    def token(
        self,
        subject: str,
        *,
        roles: Iterable[str] = (),
        projects: Iterable[str] = (),
        kind: str | None = None,
        ttl_seconds: int = 3600,
        issued_at: int | None = None,
        audience: str | None = None,
        issuer: str | None = None,
        algorithm: str = "RS256",
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = issued_at if issued_at is not None else int(time.time())
        claims: dict[str, Any] = {
            "sub": subject,
            "iss": issuer or self.issuer,
            "aud": audience or self.audience,
            "iat": now,
            "exp": now + ttl_seconds,
            "jti": uuid.uuid4().hex,
            "roles": sorted(roles),
            "projects": sorted(projects),
        }
        if kind is not None:
            claims["kind"] = kind
        if extra:
            claims.update(extra)
        return jwt.encode(claims, self._key, algorithm=algorithm, headers={"kid": self.kid})

    # -- persistence for `scripts/dev_keys.py` / `scripts/dev_token.py` ---------

    def write(self, directory: Path) -> tuple[Path, Path]:
        directory.mkdir(parents=True, exist_ok=True)
        jwks_path = directory / "jwks.json"
        private_path = directory / "jwk_private.pem"
        jwks_path.write_text(json.dumps(self.jwks, indent=2) + "\n")
        pem = self._key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        private_path.write_bytes(pem)
        private_path.chmod(0o600)
        return jwks_path, private_path

    @classmethod
    def load(
        cls, directory: Path, *, issuer: str = DEFAULT_ISSUER, audience: str = DEFAULT_AUDIENCE
    ) -> "DevIssuer":
        private_path = directory / "jwk_private.pem"
        jwks = json.loads((directory / "jwks.json").read_text())
        key = serialization.load_pem_private_key(private_path.read_bytes(), password=None)
        assert isinstance(key, RSAPrivateKey)
        return cls(issuer=issuer, audience=audience, kid=jwks["keys"][0]["kid"], private_key=key)
