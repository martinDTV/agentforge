"""Ownership, project isolation, actors and the legacy migration against PostgreSQL."""

import uuid

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.infrastructure.llm.fake import FakeLLMAdapter
from app.security import LEGACY_OWNER, LEGACY_PROJECT, AuthorizationPolicy
from app.security.dev_keys import DevIssuer
from tests.integration.conftest import (
    TemporaryDatabases,
    downgrade,
    make_app,
    migrate,
    setup_checkpoints,
)
from tests.integration.test_business_persistence import approval_row, run_row
from tests.integration.test_checkpoints import query

RUNS = "/api/v1/runs"
TASK = {"task": "Crear módulo de pacientes con pruebas"}


def bearer(
    issuer: DevIssuer, subject: str, roles: list[str], projects: list[str]
) -> dict[str, str]:
    return {"Authorization": f"Bearer {issuer.token(subject, roles=roles, projects=projects)}"}


def events_with_actor(url: str, run_id: str) -> list[tuple[str, str | None]]:
    return [
        (str(kind), None if actor is None else str(actor))
        for kind, actor in query(
            url, "SELECT kind, actor_subject FROM run_events WHERE run_id = %s ORDER BY id", run_id
        )
    ]


# 2, 3, 4, 8, 12 ----------------------------------------------------------------


def test_project_isolation_and_actors_are_persisted(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    issuer = DevIssuer()
    alice = bearer(issuer, "alice", ["operator", "approver"], ["alpha"])
    ana = bearer(issuer, "ana", ["approver"], ["alpha"])
    bob = bearer(issuer, "bob", ["admin"], ["beta"])
    app = make_app(fake_llm, url, issuer=issuer, authorization=AuthorizationPolicy())
    with TestClient(app) as client:
        created = client.post(RUNS, json=TASK, headers=alice)
        assert created.status_code == 201
        run_id = created.json()["run_id"]
        assert (created.json()["owner"], created.json()["project_id"]) == ("alice", "alpha")

        row = run_row(url, run_id)
        assert query(url, "SELECT owner_subject, project_id FROM runs WHERE id = %s", run_id) == [
            ("alice", "alpha")
        ]
        assert row["status"] == "waiting_approval"

        assert client.get(f"{RUNS}/{run_id}", headers=bob).status_code == 404
        assert (
            client.post(
                f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=bob
            ).status_code
            == 404
        )
        self_approval = client.post(
            f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=alice
        )
        assert self_approval.status_code == 403
        assert self_approval.json()["code"] == "self_approval_forbidden"
        assert approval_row(url, run_id) is None  # nothing was written by the refused attempts

        approved = client.post(f"{RUNS}/{run_id}/approve", json={"approved": True}, headers=ana)
        assert approved.status_code == 200 and approved.json()["status"] == "approved"

    assert query(
        url,
        "SELECT decided_by, applied_at IS NOT NULL FROM run_approvals WHERE run_id = %s",
        run_id,
    ) == [("ana", True)]
    actors = dict(events_with_actor(url, run_id))
    assert actors["run_created"] == "alice"
    assert actors["planner_completed"] is None and actors["architect_reviewed"] is None
    assert actors["approval_recorded"] == "ana"
    assert actors["run_approved"] == "ana" and actors["approval_applied"] == "ana"


# 9 ---------------------------------------------------------------------------


def test_idempotency_scope_is_enforced_by_the_database(
    fake_llm: FakeLLMAdapter, checkpoint_database_url: str
):
    url = checkpoint_database_url
    issuer = DevIssuer()
    key = {"Idempotency-Key": f"k-{uuid.uuid4().hex[:6]}"}
    with TestClient(
        make_app(fake_llm, url, issuer=issuer, authorization=AuthorizationPolicy())
    ) as client:
        alice = client.post(
            RUNS, json=TASK, headers={**bearer(issuer, "alice", ["operator"], ["alpha"]), **key}
        )
        again = client.post(
            RUNS, json=TASK, headers={**bearer(issuer, "alice", ["operator"], ["alpha"]), **key}
        )
        bob = client.post(
            RUNS, json=TASK, headers={**bearer(issuer, "bob", ["operator"], ["alpha"]), **key}
        )
        beta = client.post(
            RUNS, json=TASK, headers={**bearer(issuer, "alice", ["operator"], ["beta"]), **key}
        )
    assert [r.status_code for r in (alice, again, bob, beta)] == [201, 200, 201, 201]
    assert again.json()["run_id"] == alice.json()["run_id"]
    rows = query(
        url,
        "SELECT owner_subject, project_id FROM runs WHERE idempotency_key = %s "
        "ORDER BY owner_subject, project_id",
        key["Idempotency-Key"],
    )
    assert rows == [("alice", "alpha"), ("alice", "beta"), ("bob", "alpha")]
    indexes = {i["name"]: i for i in inspect(create_engine(url)).get_indexes("runs")}
    assert indexes["ix_runs_idempotency_scope"]["unique"] is True
    assert indexes["ix_runs_idempotency_scope"]["column_names"] == [
        "project_id",
        "owner_subject",
        "idempotency_key",
    ]
    assert "ix_runs_idempotency_key" not in indexes


# 10 --------------------------------------------------------------------------


def test_legacy_rows_are_quarantined_not_assigned_to_new_users(
    fake_llm: FakeLLMAdapter, temporary_databases: TemporaryDatabases
):
    url = temporary_databases.create()
    migrate(url, "0002")
    legacy_id = str(uuid.uuid4())
    query(
        url,
        "INSERT INTO runs (id, task, status, idempotency_key) "
        "VALUES (%s, %s, 'waiting_approval', 'k-legacy') RETURNING id",
        legacy_id,
        "Tarea anterior a la autenticación",
    )
    query(
        url,
        "INSERT INTO run_approvals (run_id, approved) VALUES (%s, true) RETURNING id",
        legacy_id,
    )
    query(
        url,
        "INSERT INTO run_events (run_id, kind, ordinal) VALUES (%s, 'run_created', 0) RETURNING id",
        legacy_id,
    )

    migrate(url)  # 0002 -> 0003

    assert query(
        url, "SELECT owner_subject, project_id, idempotency_key FROM runs WHERE id = %s", legacy_id
    ) == [(LEGACY_OWNER, LEGACY_PROJECT, "k-legacy")]
    assert query(url, "SELECT decided_by FROM run_approvals WHERE run_id = %s", legacy_id) == [
        (None,)
    ]
    assert query(url, "SELECT actor_subject FROM run_events WHERE run_id = %s", legacy_id) == [
        (None,)
    ]

    setup_checkpoints(url)
    issuer = DevIssuer()
    with TestClient(
        make_app(fake_llm, url, issuer=issuer, authorization=AuthorizationPolicy())
    ) as client:
        # Even an admin whose token claims "legacy" cannot see it; only projects:all can.
        sneaky = bearer(issuer, "adm", ["admin"], ["legacy", "alpha"])
        assert client.get(f"{RUNS}/{legacy_id}", headers=sneaky).status_code == 404
        assert (
            client.post(RUNS, json={**TASK, "project_id": "legacy"}, headers=sneaky).status_code
            == 403
        )
        root = bearer(issuer, "root", ["platform_admin"], [])
        visible = client.get(f"{RUNS}/{legacy_id}", headers=root)
        assert visible.status_code == 200
        assert (visible.json()["owner"], visible.json()["project_id"]) == (
            LEGACY_OWNER,
            LEGACY_PROJECT,
        )
        # The legacy key does not collide with a real principal's key.
        fresh = client.post(
            RUNS,
            json=TASK,
            headers={
                **bearer(issuer, "alice", ["operator"], ["alpha"]),
                "Idempotency-Key": "k-legacy",
            },
        )
        assert fresh.status_code == 201

    # Downgrade keeps the schema consistent: the oldest row per key keeps it.
    downgrade(url, "0002")
    engine = create_engine(url)
    try:
        columns = {c["name"] for c in inspect(engine).get_columns("runs")}
        assert "owner_subject" not in columns and "project_id" not in columns
        indexes = {i["name"] for i in inspect(engine).get_indexes("runs")}
        assert "ix_runs_idempotency_key" in indexes
    finally:
        engine.dispose()
    assert query(url, "SELECT count(*) FROM runs WHERE idempotency_key = 'k-legacy'") == [(1,)]
