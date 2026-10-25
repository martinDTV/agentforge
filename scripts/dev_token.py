"""Mint a short-lived access token signed with the local development key.

    uv run python -m scripts.dev_token --sub martin --roles admin --projects demo

Requires ``scripts/dev_keys.py`` to have run first. Prints only the token so it
can be used directly: ``curl -H "Authorization: Bearer $(uv run python -m
scripts.dev_token --sub martin)" ...``. Never use these keys outside local.
"""

import argparse
from pathlib import Path

from app.security.dev_keys import DEFAULT_AUDIENCE, DEFAULT_ISSUER, DevIssuer


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--dir", default=".local")
    parser.add_argument("--sub", required=True, help="sujeto del token (identificador opaco)")
    parser.add_argument("--roles", nargs="*", default=["operator"])
    parser.add_argument("--projects", nargs="*", default=["default"])
    parser.add_argument("--kind", choices=["user", "service"], default="user")
    parser.add_argument("--ttl", type=int, default=3600, help="segundos de validez")
    parser.add_argument("--issuer", default=DEFAULT_ISSUER)
    parser.add_argument("--audience", default=DEFAULT_AUDIENCE)
    args = parser.parse_args()
    issuer = DevIssuer.load(Path(args.dir), issuer=args.issuer, audience=args.audience)
    print(
        issuer.token(
            args.sub,
            roles=args.roles,
            projects=args.projects,
            kind=args.kind,
            ttl_seconds=args.ttl,
        )
    )


if __name__ == "__main__":
    main()
