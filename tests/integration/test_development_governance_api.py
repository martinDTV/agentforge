"""Fase 7, Paso 1: a governed development run through the API on PostgreSQL."""

import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.agents.development import DeveloperChanges, FileEdit, QAReview
from app.contracts.scope import ScopeAction, ScopeFile, ScopeProposal
from app.infrastructure.checkpoints import PostgresCheckpointer, build_serializer
from app.infrastructure.db import Database, SqlAlchemyToolInvocationRepository
from app.infrastructure.llm.fake import FakeLLMAdapter
from app.infrastructure.sandbox import FakeSandbox
from app.main import create_app
from app.orchestration.limits import RunLimits
from app.projects import ProjectPolicyRegistry, RepositoryPolicy
from app.security import AuthorizationPolicy, JwtVerifier, VerificationRules
from app.security.dev_keys import DevIssuer
from app.security.tokens import StaticKeys
from tests.integration.conftest import libpq_dsn
from tests.unit.git_fixtures import make_sandbox

PROJECT = "sandbox-gov"
REPO = "sandbox-gov"
RUNS = "/api/v1/runs"


def test_governed_run_through_the_api_on_postgres(
    fake_llm: FakeLLMAdapter, fresh_checkpoint_database_url: str, tmp_path: Path
):
    url = fresh_checkpoint_database_url
    box = make_sandbox(tmp_path, project=PROJECT, repository=REPO)
    fake_llm.register(
        ScopeProposal,
        ScopeProposal(
            summary="dos archivos",
            files=[
                ScopeFile(path="src/app.py", action=ScopeAction.EDIT, reason="implementar"),
                ScopeFile(path="tests/test_greet.py", action=ScopeAction.CREATE, reason="probar"),
            ],
            tests=["tests"],
        ),
    )
    fake_llm.register(
        DeveloperChanges,
        DeveloperChanges(
            summary="hecho",
            edits=[
                FileEdit(path="src/app.py", content="def greet(n):\n    return n\n"),
                FileEdit(path="tests/test_greet.py", content="def test_ok():\n    assert 1\n"),
            ],
        ),
    )
    fake_llm.register(QAReview, QAReview(approved=True, summary="ok", issues=[]))
    issuer = DevIssuer()
    verifier = JwtVerifier(
        StaticKeys(issuer.jwks), VerificationRules(issuer=issuer.issuer, audience=issuer.audience)
    )
    database = Database(url)
    app = create_app(
        fake_llm,
        RunLimits(),
        database=database,
        checkpoints=PostgresCheckpointer(libpq_dsn(url), serde=build_serializer(None)),
        token_verifier=verifier,
        authorization=AuthorizationPolicy(separation_of_duties=True),
        tool_audit=SqlAlchemyToolInvocationRepository(database.session_factory),
        worktrees=box.manager,
        sandbox=FakeSandbox(),
        projects=ProjectPolicyRegistry(
            [
                RepositoryPolicy(
                    repository_id=REPO, project_id=PROJECT, writable_globs=["src/**", "tests/**"]
                )
            ]
        ),
    )
    olivia = {
        "Authorization": "Bearer "
        + issuer.token("olivia", roles=["project_operator"], projects=[PROJECT])
    }
    omar = {
        "Authorization": "Bearer "
        + issuer.token("omar", roles=["project_operator"], projects=[PROJECT])
    }
    body = {
        "task": "Añade greet en src/app.py y pruebas",
        "project_id": PROJECT,
        "workflow_kind": "development",
        "repository_id": REPO,
    }
    with TestClient(app) as client:
        run = client.post(RUNS, json=body, headers=olivia).json()
        assert run["status"] == "waiting_approval" and run["pending_stage"] == "scope"
        digest = client.get(f"{RUNS}/{run['run_id']}/decisions", headers=olivia).json()["pending"][
            "scope_digest"
        ]
        own = client.post(
            f"{RUNS}/{run['run_id']}/approve",
            json={"approved": True, "scope_digest": digest},
            headers=olivia,
        )
        assert own.status_code == 403 and own.json()["code"] == "self_approval_forbidden"
        approved = client.post(
            f"{RUNS}/{run['run_id']}/approve",
            json={"approved": True, "scope_digest": digest},
            headers=omar,
        )
        assert approved.status_code == 200, approved.text
        assert approved.json()["status"] == "completed"
        decisions = client.get(f"{RUNS}/{run['run_id']}/decisions", headers=olivia).json()
        assert decisions["pending"] is None
        assert [
            (d["stage"], d["decided_by"], d["scope_digest"]) for d in decisions["decisions"]
        ] == [("scope", "omar", digest)]
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT status, workflow_kind, pending_stage, lease_owner "
                    "FROM runs WHERE id = :id"
                ),
                {"id": run["run_id"]},
            ).one()
            assert tuple(row) == ("completed", "development", None, None)
            stages = conn.execute(
                text(
                    "SELECT stage, approved, decided_by, applied_at IS NOT NULL "
                    "FROM run_decisions WHERE run_id=:id"
                ),
                {"id": run["run_id"]},
            ).all()
            assert stages == [("scope", True, "omar", True)]
            risks = dict(
                conn.execute(
                    text(
                        "SELECT agent || '/' || risk, count(*) FROM tool_invocations "
                        "WHERE run_id=:id GROUP BY 1"
                    ),
                    {"id": run["run_id"]},
                ).all()
            )
            assert risks.get("scoper/read") and risks.get("developer/write") == 2
            assert "scoper/write" not in risks and "scoper/exec" not in risks
            kinds = [
                k
                for (k,) in conn.execute(
                    text("SELECT kind FROM run_events WHERE run_id=:id ORDER BY id"),
                    {"id": run["run_id"]},
                ).all()
            ]
            for kind in (
                "run_created",
                "scope_proposed",
                "waiting_approval",
                "approval_recorded",
                "approval_applied",
                "run_completed",
            ):
                assert kind in kinds, kind
    finally:
        engine.dispose()
    worktree = box.manager.get(uuid.UUID(run["run_id"]), PROJECT)
    assert worktree is not None and worktree.released
    assert (worktree.path / "tests/test_greet.py").is_file()
    assert (worktree.path / "README.md").read_text() == "# Demo\n"
