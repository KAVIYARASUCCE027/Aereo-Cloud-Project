"""Shared test setup: every test runs against a dedicated PostgreSQL test database.

TEST_DATABASE_URL names that database. Before any application module is
imported, DATABASE_URL is replaced with it, so the app under test (its engine,
sessions, and Alembic) can only reach the test database. The run is refused if
the test database could be the application database.
"""

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy.engine import URL

from tests.database import (
    UnsafeTestDatabaseError,
    check_test_database_url,
    recreate_test_database,
    run_alembic,
    truncate_tables,
)

try:
    TEST_DATABASE = check_test_database_url(os.environ.get("TEST_DATABASE_URL"), os.environ.get("DATABASE_URL"))
except UnsafeTestDatabaseError as exc:
    pytest.exit(f"Refusing to run tests: {exc}", returncode=pytest.ExitCode.USAGE_ERROR)

os.environ["DATABASE_URL"] = TEST_DATABASE.render_as_string(hide_password=False)

# Imported only now, so the engine is created with the test database URL.
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db import SessionLocal, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.models import Base  # noqa: E402
from app.services import dispatch  # noqa: E402

if engine.url.database != TEST_DATABASE.database:
    pytest.exit(
        f"Refusing to run tests: the application engine targets {engine.url.database!r}, "
        f"not the test database {TEST_DATABASE.database!r}",
        returncode=pytest.ExitCode.USAGE_ERROR,
    )

TABLE_NAMES = [table.name for table in Base.metadata.sorted_tables]


@pytest.fixture(scope="session", autouse=True)
def test_database() -> Iterator[URL]:
    """Create an empty test database once per run and apply the real migrations."""
    recreate_test_database(TEST_DATABASE)
    run_alembic(TEST_DATABASE, "upgrade", "head")
    yield TEST_DATABASE
    engine.dispose()


@pytest.fixture(autouse=True)
def clean_tables(test_database: URL) -> None:
    """Start every test with empty tables."""
    truncate_tables(engine, TABLE_NAMES)


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db_session() -> Iterator[Session]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Replace enqueue_job with a recorder; the list holds every job ID handed to dispatch."""
    calls: list[Any] = []
    monkeypatch.setattr(dispatch, "enqueue_job", calls.append)
    return calls


@pytest.fixture
def job_payload() -> Callable[..., dict[str, Any]]:
    def build(recipients: list[Any], **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "event_name": "Backend Development Workshop",
            "issuer_name": "ABC Academy",
            "issue_date": "2026-10-07",
            "recipients": recipients,
        }
        payload.update(overrides)
        return payload

    return build
