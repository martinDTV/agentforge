"""Generate a local signing key pair for development (never for production).

Writes ``.local/jwks.json`` (public keys, what the API reads through
``OIDC_JWKS_FILE``) and ``.local/jwk_private.pem`` (what ``scripts/dev_token.py``
signs with). ``.local/`` is git-ignored. Settings refuse a JWKS file in
staging and production, so these keys cannot be wired into a deployment.

    uv run python -m scripts.dev_keys [--dir .local]
"""

import argparse
from pathlib import Path

from app.security.dev_keys import DEFAULT_AUDIENCE, DEFAULT_ISSUER, DevIssuer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dir", default=".local", help="directorio de salida (por defecto .local)")
    args = parser.parse_args()
    jwks_path, private_path = DevIssuer().write(Path(args.dir))
    print(f"JWKS público:  {jwks_path}")
    print(f"Clave privada: {private_path} (permisos 600, no la compartas)")
    print("Añade a tu .env:")
    print("AUTH_MODE=oidc")
    print(f"OIDC_ISSUER={DEFAULT_ISSUER}")
    print(f"OIDC_AUDIENCE={DEFAULT_AUDIENCE}")
    print(f"OIDC_JWKS_FILE={jwks_path}")


if __name__ == "__main__":
    main()
