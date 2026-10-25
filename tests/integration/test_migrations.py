"""Alembic migrations apply to a clean database and match the SQLAlchemy models."""

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.ext.asyncio import create_async_engine

import app.infrastructure.db.models  # noqa: F401
from app.infrastructure.db import SchemaMismatchError, expected_head_revision, verify_schema
from app.infrastructure.db.base import Base
from tests.integration.conftest import downgrade, migrate

BUSINESS_TABLES = {"runs", "run_approvals", "run_events", "tool_invocations"}


def test_upgrade_head_on_a_clean_database_creates_the_business_schema(empty_database_url: str):
    migrate(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        inspector = inspect(engine)
        assert set(inspector.get_table_names()) == BUSINESS_TABLES | {"alembic_version"}
        with engine.connect() as conn:
            version = conn.scalar(text("SELECT version_num FROM alembic_version"))
        assert version == expected_head_revision() == "0004"
    finally:
        engine.dispose()


def test_models_match_the_migrated_schema_exactly(migrated_database_url: str):
    engine = create_engine(migrated_database_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(
                conn, opts={"compare_type": True, "compare_server_default": True}
            )
            diff = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert diff == [], f"Deriva entre modelos y esquema: {diff}"


def test_downgrade_to_base_removes_everything_and_upgrade_is_repeatable(empty_database_url: str):
    migrate(empty_database_url)
    downgrade(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        with engine.connect() as conn:
            assert conn.scalar(text("SELECT count(*) FROM alembic_version")) == 0
    finally:
        engine.dispose()
    migrate(empty_database_url)
    engine = create_engine(empty_database_url)
    try:
        assert set(inspect(engine).get_table_names()) == BUSINESS_TABLES | {"alembic_version"}
    finally:
        engine.dispose()


def test_migrated_schema_has_the_designed_constraints(migrated_database_url: str):
    engine = create_engine(migrated_database_url)
    try:
        inspector = inspect(engine)
        runs_checks = {c["name"] for c in inspector.get_check_constraints("runs")}
        assert runs_checks >= {
            "ck_runs_run_status",
            "ck_runs_lease_fields_paired",
            "ck_runs_error_fields_paired",
            "ck_runs_revision_non_negative",
            "ck_runs_llm_calls_non_negative",
            "ck_runs_attempt_non_negative",
        }
        runs_indexes = {i["name"]: i for i in inspector.get_indexes("runs")}
        idem = runs_indexes["ix_runs_idempotency_scope"]
        assert idem["unique"] is True
        assert idem["column_names"] == ["project_id", "owner_subject", "idempotency_key"]
        assert "idempotency_key IS NOT NULL" in str(
            idem.get("dialect_options", {}).get("postgresql_where", "")
        )
        approvals_uniques = {u["name"] for u in inspector.get_unique_constraints("run_approvals")}
        assert "uq_run_approvals_run_id" in approvals_uniques
        events_uniques = {u["name"] for u in inspector.get_unique_constraints("run_events")}
        assert "uq_run_events_run_id_kind_ordinal" in events_uniques
        for table in ("run_approvals", "run_events"):
            fk = inspector.get_foreign_keys(table)[0]
            assert fk["referred_table"] == "runs"
            assert fk["options"].get("ondelete") == "CASCADE"
    finally:
        engine.dispose()


@pytest.mark.anyio
async def test_verify_schema_accepts_head_and_rejects_anything_else(
    migrated_database_url: str, empty_database_url: str, stale_schema_database_url: str
):
    good = create_async_engine(migrated_database_url)
    try:
        assert await verify_schema(good) == "0004"
    finally:
        await good.dispose()

    empty = create_async_engine(empty_database_url)
    try:
        with pytest.raises(SchemaMismatchError) as missing:
            await verify_schema(empty)
        assert missing.value.current is None
        assert "alembic upgrade head" in str(missing.value)
    finally:
        await empty.dispose()

    stale = create_async_engine(stale_schema_database_url)
    try:
        with pytest.raises(SchemaMismatchError) as wrong:
            await verify_schema(stale)
        assert (wrong.value.current, wrong.value.expected) == ("9999", "0004")
    finally:
        await stale.dispose()
