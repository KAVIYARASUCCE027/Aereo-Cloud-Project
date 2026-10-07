"""Test-database helpers, including the guards that keep tests away from the
application database.

Every function that creates, drops, migrates, or truncates checks the database
name first. A test database name must end with "_test" and differ from the
application database name.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.pool import NullPool

PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_DATABASE_SUFFIX = "_test"
SAFE_DATABASE_NAME = re.compile(r"[A-Za-z0-9_]+")


class UnsafeTestDatabaseError(RuntimeError):
    pass


def require_test_database_name(name: str | None) -> str:
    if not name or not name.endswith(TEST_DATABASE_SUFFIX) or not SAFE_DATABASE_NAME.fullmatch(name):
        raise UnsafeTestDatabaseError(
            f"test database name {name!r} must end with {TEST_DATABASE_SUFFIX!r} "
            "and contain only letters, digits, and underscores"
        )
    return name


def check_test_database_url(test_url: str | None, app_url: str | None) -> URL:
    """Validate TEST_DATABASE_URL against the application's DATABASE_URL."""
    if not test_url:
        raise UnsafeTestDatabaseError(
            "TEST_DATABASE_URL is not set; point it at a dedicated database whose name ends with '_test'"
        )
    url = make_url(test_url)
    name = require_test_database_name(url.database)
    # Compare names only: host aliases (db, localhost, 127.0.0.1) can hide
    # that two URLs reach the same server, so equal names are always refused.
    if app_url and make_url(app_url).database == name:
        raise UnsafeTestDatabaseError(
            f"TEST_DATABASE_URL and DATABASE_URL both name database {name!r}; "
            "tests must never run against the application database"
        )
    return url


def recreate_test_database(url: URL) -> None:
    """Drop and recreate the test database so every run starts from an empty schema."""
    name = require_test_database_name(url.database)
    # Connect to the server's maintenance database; a database cannot drop itself.
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        admin.dispose()


def run_alembic(url: URL, *args: str) -> None:
    """Run the real Alembic CLI against the test database, exactly as deployment does."""
    require_test_database_name(url.database)
    environment = {**os.environ, "DATABASE_URL": url.render_as_string(hide_password=False)}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=PROJECT_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(args)} failed:\n{result.stdout}\n{result.stderr}")


def truncate_tables(engine: Engine, table_names: list[str]) -> None:
    require_test_database_name(engine.url.database)
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE {', '.join(table_names)}"))
