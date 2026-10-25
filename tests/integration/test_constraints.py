"""The database enforces the invariants the application will rely on."""

import uuid
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.orm import Session

from app.contracts.lifecycle import RunEventKind, RunLifecycleStatus
from app.infrastructure.db.models import RunApprovalRecord, RunEventRecord, RunRecord


@pytest.fixture
def session(migrated_database_url: str) -> Iterator[Session]:
    engine = create_engine(migrated_database_url)
    with Session(engine) as session:
        yield session
        session.rollback()
        session.execute(delete(RunRecord))
        session.commit()
    engine.dispose()


def new_run(**overrides: object) -> RunRecord:
    values: dict[str, object] = {
        "id": uuid.uuid4(),
        "task": "Diseñar módulo de pacientes",
        "status": RunLifecycleStatus.RUNNING,
        "owner_subject": "alice",
        "project_id": "alpha",
    }
    values.update(overrides)
    return RunRecord(**values)


def expect_integrity_error(session: Session, *rows: object) -> None:
    with pytest.raises(IntegrityError):
        with session.begin_nested():
            session.add_all(rows)
            session.flush()


def test_idempotency_key_is_unique_only_when_present(session: Session):
    session.add_all([new_run(idempotency_key=None), new_run(idempotency_key=None)])
    session.flush()
    session.add(new_run(idempotency_key="k-1"))
    session.flush()
    expect_integrity_error(session, new_run(idempotency_key="k-1"))


def test_status_outside_the_lifecycle_is_rejected(session: Session):
    with pytest.raises((LookupError, StatementError)):
        session.add(new_run(status="paused"))  # type: ignore[arg-type]
        session.flush()


def test_lease_fields_must_come_together(session: Session):
    expect_integrity_error(session, new_run(lease_owner="host:1:abc"))


def test_error_fields_must_come_together(session: Session):
    expect_integrity_error(session, new_run(error_code="llm_timeout"))


def test_counters_cannot_be_negative(session: Session):
    expect_integrity_error(session, new_run(revision=-1))
    expect_integrity_error(session, new_run(llm_calls=-1))
    expect_integrity_error(session, new_run(attempt=-1))


def test_only_one_approval_per_run(session: Session):
    run = new_run(status=RunLifecycleStatus.WAITING_APPROVAL)
    session.add(run)
    session.flush()
    session.add(RunApprovalRecord(run_id=run.id, approved=True))
    session.flush()
    expect_integrity_error(session, RunApprovalRecord(run_id=run.id, approved=False))


def test_events_dedupe_on_natural_key_but_allow_new_ordinals(session: Session):
    run = new_run()
    session.add(run)
    session.flush()
    session.add(RunEventRecord(run_id=run.id, kind=RunEventKind.PLANNER_COMPLETED, ordinal=0))
    session.add(RunEventRecord(run_id=run.id, kind=RunEventKind.PLANNER_COMPLETED, ordinal=1))
    session.flush()
    expect_integrity_error(
        session, RunEventRecord(run_id=run.id, kind=RunEventKind.PLANNER_COMPLETED, ordinal=1)
    )


def test_server_defaults_fill_counters_payload_and_timestamps(session: Session):
    run = new_run()
    session.add(run)
    session.flush()
    session.add(RunEventRecord(run_id=run.id, kind=RunEventKind.RUN_CREATED))
    session.commit()
    session.expire_all()
    stored = session.get(RunRecord, run.id)
    assert stored is not None
    assert (stored.revision, stored.llm_calls, stored.attempt) == (0, 0, 0)
    assert stored.created_at.tzinfo is not None
    event = session.scalars(select(RunEventRecord).where(RunEventRecord.run_id == run.id)).one()
    assert (event.ordinal, event.payload) == (0, {})


def test_deleting_a_run_cascades_to_approvals_and_events(session: Session):
    run = new_run(status=RunLifecycleStatus.WAITING_APPROVAL)
    session.add(run)
    session.flush()
    session.add(RunApprovalRecord(run_id=run.id, approved=True))
    session.add(RunEventRecord(run_id=run.id, kind=RunEventKind.RUN_CREATED))
    session.commit()
    session.delete(run)
    session.commit()
    assert session.scalars(select(RunApprovalRecord)).all() == []
    assert session.scalars(select(RunEventRecord)).all() == []


def test_approval_and_event_require_an_existing_run(session: Session):
    expect_integrity_error(session, RunApprovalRecord(run_id=uuid.uuid4(), approved=True))
    expect_integrity_error(
        session, RunEventRecord(run_id=uuid.uuid4(), kind=RunEventKind.RUN_CREATED)
    )
