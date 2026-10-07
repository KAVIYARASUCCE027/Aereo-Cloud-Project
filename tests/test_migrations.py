"""The migrated schema: matches the models and enforces its constraints."""

import uuid

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, inspect, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import IntegrityError

from app.db import engine
from app.models import Base
from tests.database import run_alembic


def insert_job(connection: Connection, status: str = "QUEUED") -> uuid.UUID:
    job_id = uuid.uuid4()
    connection.execute(
        text(
            "INSERT INTO generation_jobs (id, event_name, issuer_name, issue_date, status, total_count) "
            "VALUES (:id, 'Event', 'Issuer', '2026-10-07', :status, 1)"
        ),
        {"id": job_id, "status": status},
    )
    return job_id


def insert_recipient(connection: Connection, job_id: uuid.UUID, **values: object) -> None:
    row = {
        "id": uuid.uuid4(),
        "job_id": job_id,
        "input_index": 0,
        "name": "Asha",
        "email": None,
        "status": "PENDING",
        "error_details": None,
    } | values
    connection.execute(
        text(
            "INSERT INTO job_recipients (id, job_id, input_index, name, email, status, error_details) "
            "VALUES (:id, :job_id, :input_index, :name, :email, :status, CAST(:error_details AS jsonb))"
        ),
        row,
    )


def test_models_match_the_migrated_schema() -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection, opts={"compare_type": True})
        differences = compare_metadata(context, Base.metadata)

    assert differences == []


def test_expected_constraints_and_index_exist() -> None:
    inspector = inspect(engine)

    assert {"name": "uq_job_recipients_job_id_input_index", "column_names": ["job_id", "input_index"]} in [
        {"name": c["name"], "column_names": c["column_names"]} for c in inspector.get_unique_constraints("job_recipients")
    ]
    assert ["job_id", "status"] in [i["column_names"] for i in inspector.get_indexes("job_recipients")]
    assert {fk["referred_table"] for fk in inspector.get_foreign_keys("job_recipients")} == {"generation_jobs"}


def test_database_rejects_unknown_job_status() -> None:
    with pytest.raises(IntegrityError, match="ck_generation_jobs_status_valid"), engine.begin() as connection:
        insert_job(connection, status="DONE")


def test_database_rejects_unknown_recipient_status() -> None:
    with pytest.raises(IntegrityError, match="ck_job_recipients_status_valid"), engine.begin() as connection:
        insert_recipient(connection, insert_job(connection), status="SKIPPED")


def test_database_rejects_duplicate_input_index_within_a_job() -> None:
    with pytest.raises(IntegrityError, match="uq_job_recipients_job_id_input_index"), engine.begin() as connection:
        job_id = insert_job(connection)
        insert_recipient(connection, job_id, input_index=0)
        insert_recipient(connection, job_id, input_index=0)


def test_database_rejects_invalid_row_that_stores_submitted_values() -> None:
    with pytest.raises(IntegrityError, match="ck_job_recipients_name_matches_status"), engine.begin() as connection:
        insert_recipient(connection, insert_job(connection), status="INVALID", name="", error_details='[{"code": "x"}]')


def test_database_rejects_pending_row_without_a_name() -> None:
    with pytest.raises(IntegrityError, match="ck_job_recipients_name_matches_status"), engine.begin() as connection:
        insert_recipient(connection, insert_job(connection), name=None)


def test_database_rejects_invalid_row_without_error_details() -> None:
    with pytest.raises(IntegrityError, match="ck_job_recipients_invalid_has_error_details"), engine.begin() as connection:
        insert_recipient(connection, insert_job(connection), status="INVALID", name=None)


def test_migration_downgrades_and_upgrades_cleanly(test_database: URL) -> None:
    run_alembic(test_database, "downgrade", "base")
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}

    run_alembic(test_database, "upgrade", "head")
    assert {"generation_jobs", "job_recipients"} <= set(inspect(engine).get_table_names())
