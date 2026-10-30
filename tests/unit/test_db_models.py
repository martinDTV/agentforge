"""Schema design checks that need no database: they inspect ``Base.metadata``."""

from sqlalchemy import CheckConstraint, Enum, ForeignKeyConstraint, UniqueConstraint

import app.infrastructure.db.models  # noqa: F401  (registers the tables)
from app.contracts.execution import RunStatus
from app.contracts.lifecycle import TERMINAL_STATUSES, RunEventKind, RunLifecycleStatus
from app.infrastructure.db.base import NAMING_CONVENTION, Base
from app.infrastructure.db.schema import expected_head_revision

runs = Base.metadata.tables["runs"]
approvals = Base.metadata.tables["run_approvals"]
events = Base.metadata.tables["run_events"]


def test_exactly_the_twelve_business_tables_are_registered():
    assert set(Base.metadata.tables) == {
        "runs",
        "run_approvals",
        "run_decisions",
        "run_events",
        "tool_invocations",
        "mirror_syncs",
        "publications",
        "notification_outbox",
        "route_decisions",
        "goals",
        "goal_tasks",
        "goal_events",
    }


def test_metadata_uses_the_naming_convention():
    assert Base.metadata.naming_convention == NAMING_CONVENTION
    assert runs.primary_key.name == "pk_runs"
    check_names = {c.name for c in runs.constraints if isinstance(c, CheckConstraint)}
    assert check_names >= {
        "ck_runs_revision_non_negative",
        "ck_runs_llm_calls_non_negative",
        "ck_runs_attempt_non_negative",
        "ck_runs_lease_fields_paired",
        "ck_runs_error_fields_paired",
    }


def test_api_and_persistence_share_one_status_definition():
    assert RunStatus is RunLifecycleStatus
    assert TERMINAL_STATUSES == {
        RunLifecycleStatus.APPROVED,
        RunLifecycleStatus.REJECTED,
        RunLifecycleStatus.COMPLETED,
        RunLifecycleStatus.CANCELLED,
        RunLifecycleStatus.EXPIRED,
        RunLifecycleStatus.PUBLISHED,
        RunLifecycleStatus.FAILED,
    }


def test_status_and_kind_columns_store_enum_values_not_names():
    status_type = runs.c.status.type
    kind_type = events.c.kind.type
    assert isinstance(status_type, Enum) and isinstance(kind_type, Enum)
    assert status_type.enums == [s.value for s in RunLifecycleStatus]
    assert kind_type.enums == [k.value for k in RunEventKind]
    assert status_type.native_enum is False and kind_type.native_enum is False
    assert status_type.create_constraint is True


def test_idempotency_index_is_scoped_to_project_and_principal():
    index = next(i for i in runs.indexes if i.name == "ix_runs_idempotency_scope")
    assert index.unique is True
    assert [c.name for c in index.columns] == ["project_id", "owner_subject", "idempotency_key"]
    assert index.dialect_options["postgresql"]["where"] is not None
    assert not any(i.name == "ix_runs_idempotency_key" for i in runs.indexes)


def test_ownership_columns_and_actor_columns_exist():
    assert runs.c.owner_subject.nullable is False and runs.c.project_id.nullable is False
    assert approvals.c.decided_by.nullable is True  # NULL only for pre-auth approvals
    assert events.c.actor_subject.nullable is True  # NULL for agent/system events
    names = {i.name for i in runs.indexes}
    assert {"ix_runs_project_id_created_at", "ix_runs_owner_subject_created_at"} <= names


def test_sweeper_index_covers_status_and_lease_expiry():
    index = next(i for i in runs.indexes if i.name == "ix_runs_status_lease_expires_at")
    assert [c.name for c in index.columns] == ["status", "lease_expires_at"]


def test_one_approval_per_run_is_enforced_by_a_unique_constraint():
    uniques = [c for c in approvals.constraints if isinstance(c, UniqueConstraint)]
    assert [[col.name for col in u.columns] for u in uniques] == [["run_id"]]
    assert approvals.c.applied_at.nullable is True


def test_events_have_a_natural_unique_key():
    uniques = [c for c in events.constraints if isinstance(c, UniqueConstraint)]
    assert [[col.name for col in u.columns] for u in uniques] == [["run_id", "kind", "ordinal"]]
    assert next(u.name for u in uniques) == "uq_run_events_run_id_kind_ordinal"


def test_foreign_keys_cascade_on_run_deletion():
    for table in (approvals, events):
        fk = next(c for c in table.constraints if isinstance(c, ForeignKeyConstraint))
        assert fk.ondelete == "CASCADE"
        assert fk.referred_table is runs


def test_timestamps_are_timezone_aware_with_server_defaults():
    for column in (
        runs.c.created_at,
        runs.c.updated_at,
        approvals.c.decided_at,
        events.c.created_at,
    ):
        assert column.type.timezone is True
        assert column.server_default is not None
    assert runs.c.updated_at.onupdate is not None


def test_run_id_is_uuid_and_documents_thread_id():
    assert runs.c.id.primary_key is True
    assert runs.c.id.type.python_type.__name__ == "UUID"


def test_alembic_head_is_the_latest_migration():
    assert expected_head_revision() == "0011"
