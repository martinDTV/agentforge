"""AgentForge from the terminal: create a task, follow a run, decide approvals.

    uv run python -m scripts.agentforge task "Implementa X en src/a.py" \\
        --project sandbox-github --repository sandbox-github [--mode auto|fast|full]
    uv run python -m scripts.agentforge runs [--status waiting_approval] [--project P]
    uv run python -m scripts.agentforge show <run_id>
    uv run python -m scripts.agentforge pending <run_id>      # what is waiting and its digest
    uv run python -m scripts.agentforge approve <run_id>      # shows the proposal, asks, approves
    uv run python -m scripts.agentforge reject <run_id>
    uv run python -m scripts.agentforge events <run_id>

The identity is ``AGENTFORGE_SUBJECT`` (default: the OS user) with the
``project_operator`` role on ``--project`` (default ``AGENTFORGE_PROJECT``); with
``AUTH_MODE=oidc`` the token comes from the local development issuer (``.local/``)
and is never printed. Approving requires typing the first eight characters of the
digest shown, so the decision is bound to that exact proposal.
"""

import argparse
import getpass
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx
from dotenv import dotenv_values

DEFAULT_API = "http://127.0.0.1:8011/api/v1"


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value is None:
        value = dotenv_values(".env").get(name)
    return value if value not in (None, "") else default


def headers_for(project: str | None, subject: str) -> dict[str, str]:
    """Bearer for the dev issuer when the API runs with OIDC; nothing when disabled."""
    if (_env("AUTH_MODE", "oidc") or "oidc") != "oidc":
        return {}
    from app.security.dev_keys import DevIssuer

    issuer = _env("OIDC_ISSUER")
    audience = _env("OIDC_AUDIENCE")
    if issuer is None or audience is None:
        raise SystemExit("AUTH_MODE=oidc requiere OIDC_ISSUER y OIDC_AUDIENCE en .env")
    dev = DevIssuer.load(Path(".local"), issuer=issuer, audience=audience)
    token = dev.token(
        subject,
        roles=["project_operator"],
        projects=[project] if project else [],
        ttl_seconds=900,
    )
    return {"Authorization": f"Bearer {token}"}


def _fail(response: httpx.Response) -> int:
    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:300]}
    print(json.dumps({"http": response.status_code, **body}, ensure_ascii=False, indent=1))
    return 1


def _summary(run: dict[str, Any]) -> dict[str, Any]:
    publication = run.get("publication") or {}
    return {
        "run_id": run.get("run_id"),
        "status": run.get("status"),
        "pending_stage": run.get("pending_stage"),
        "route": run.get("route"),
        "route_mode": run.get("route_mode"),
        "approval_deadline": run.get("approval_deadline"),
        "pr_url": publication.get("pr_url"),
        "publication_state": publication.get("state"),
        "error": run.get("error"),
    }


def _describe(pending: dict[str, Any]) -> list[str]:
    stage = pending.get("stage")
    proposal = pending.get("proposal") or {}
    lines = [f"Etapa pendiente: {stage}", f"Caduca: {pending.get('deadline')}"]
    if stage == "scope":
        lines.append(f"Resumen: {proposal.get('summary', '')}")
        for f in proposal.get("files", []):
            lines.append(f"  - {f.get('action', '?'):6} {f.get('path')}  ({f.get('reason', '')})")
        lines.append(f"Pruebas: {', '.join(proposal.get('tests', []) or [])}")
    elif stage == "publish":
        lines.append(
            f"Repositorio: {proposal.get('repository')} → rama {proposal.get('remote_branch')}"
        )
        lines.append(
            f"Base {proposal.get('base_branch')}@{proposal.get('base_sha')}"
            f" · head {proposal.get('head_sha')}"
        )
        for f in proposal.get("files", []):
            lines.append(
                f"  - {f.get('change', '?'):8} {f.get('path')}"
                f"  sha256 {str(f.get('sha256', ''))[:12]}"
            )
        checks = proposal.get("qa_checks") or []
        lines.append("QA: " + ", ".join(f"{c.get('program')}={c.get('exit_code')}" for c in checks))
        findings = (proposal.get("scan") or {}).get("findings") or []
        lines.append(f"Hallazgos del escaneo: {len(findings)}")
    elif stage == "plan":
        plan = proposal.get("plan") or {}
        lines.append(f"Plan: {plan.get('summary', '')}")
        for t in plan.get("tasks", []):
            lines.append(f"  - {t.get('id')}: {t.get('title')}")
    digest = pending.get("publish_digest") if stage == "publish" else pending.get("scope_digest")
    lines.append(f"Digest: {digest}")
    return lines


def run(argv: Sequence[str] | None = None, *, client: httpx.Client | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentforge", description=__doc__.split("\n", 1)[0])
    parser.add_argument("--api", default=_env("AGENTFORGE_API_URL", DEFAULT_API))
    parser.add_argument("--project", default=_env("AGENTFORGE_PROJECT"))
    parser.add_argument(
        "--as", dest="subject", default=_env("AGENTFORGE_SUBJECT", getpass.getuser())
    )
    sub = parser.add_subparsers(dest="command", required=True)
    task = sub.add_parser("task", help="create a development run")
    task.add_argument("text")
    task.add_argument("--repository", default=_env("AGENTFORGE_REPOSITORY"))
    task.add_argument("--mode", choices=["auto", "fast", "full"], default=None)
    runs = sub.add_parser("runs", help="list runs")
    runs.add_argument("--status", default=None)
    runs.add_argument("--limit", type=int, default=20)
    for name in ("show", "pending", "approve", "reject", "events"):
        p = sub.add_parser(name)
        p.add_argument("run_id")
        if name in ("approve", "reject"):
            p.add_argument("--yes", action="store_true", help="no interactive confirmation")
            p.add_argument("--digest", default=None, help="full digest, non-interactive")
    args = parser.parse_args(argv)
    own_client = client is None
    client = client or httpx.Client(base_url=args.api, timeout=1800.0)
    try:
        h = headers_for(args.project, args.subject)
        if args.command == "task":
            if not args.project or not args.repository:
                raise SystemExit(
                    "task requiere --project y --repository (o AGENTFORGE_PROJECT/REPOSITORY)"
                )
            body: dict[str, Any] = {
                "task": args.text,
                "project_id": args.project,
                "workflow_kind": "development",
                "repository_id": args.repository,
            }
            if args.mode:
                body["route_mode"] = args.mode
            r = client.post("/runs", json=body, headers=h)
            if r.status_code != 201:
                return _fail(r)
            print(json.dumps(_summary(r.json()), ensure_ascii=False, indent=1))
            return 0
        if args.command == "runs":
            params: dict[str, Any] = {"limit": args.limit}
            if args.status:
                params["status"] = args.status
            if args.project:
                params["project_id"] = args.project
            r = client.get("/runs", params=params, headers=h)
            if r.status_code != 200:
                return _fail(r)
            for item in r.json().get("items", []):
                print(json.dumps(_summary(item), ensure_ascii=False))
            return 0
        if args.command == "show":
            r = client.get(f"/runs/{args.run_id}", headers=h)
            return (
                _fail(r)
                if r.status_code != 200
                else print(json.dumps(_summary(r.json()), ensure_ascii=False, indent=1)) or 0
            )
        if args.command == "events":
            r = client.get(f"/runs/{args.run_id}/events", params={"limit": 100}, headers=h)
            if r.status_code != 200:
                return _fail(r)
            for e in r.json().get("items", []):
                print(
                    f"{e.get('created_at', '')}  {e.get('kind'):22} "
                    f"{json.dumps(e.get('payload', {}), ensure_ascii=False)[:120]}"
                )
            return 0
        r = client.get(f"/runs/{args.run_id}/decisions", headers=h)
        if r.status_code != 200:
            return _fail(r)
        pending = r.json().get("pending")
        if pending is None:
            print(
                json.dumps(
                    {"run_id": args.run_id, "pending": None, "status": r.json().get("status")}
                )
            )
            return 0 if args.command == "pending" else 1
        print("\n".join(_describe(pending)))
        if args.command == "pending":
            return 0
        stage = pending["stage"]
        digest = (
            pending.get("publish_digest") if stage == "publish" else pending.get("scope_digest")
        )
        approved = args.command == "approve"
        if args.digest is not None and args.digest != digest:
            print("El digest indicado no coincide con el mostrado; nada decidido.")
            return 1
        if not args.yes and args.digest is None:
            verb = "APROBAR" if approved else "RECHAZAR"
            prompt = (
                f"\nEscribe los 8 primeros caracteres del digest para {verb} (vacío = cancelar): "
            )
            typed = input(prompt).strip()
            if not typed or not str(digest).startswith(typed) or len(typed) < 8:
                print("Cancelado; nada decidido.")
                return 1
        payload: dict[str, Any] = {"approved": approved, "stage": stage}
        if digest is not None:
            payload["publish_digest" if stage == "publish" else "scope_digest"] = digest
        r = client.post(f"/runs/{args.run_id}/approve", json=payload, headers=h)
        if r.status_code != 200:
            return _fail(r)
        print(json.dumps(_summary(r.json()), ensure_ascii=False, indent=1))
        return 0
    finally:
        if own_client:
            client.close()


if __name__ == "__main__":
    sys.exit(run())
