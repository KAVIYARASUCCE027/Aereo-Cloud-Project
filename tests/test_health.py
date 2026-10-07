from collections.abc import Iterator

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db import get_db
from app.main import app


def test_health_reports_ok_when_database_is_reachable(client: TestClient) -> None:
    # Uses the real (test) database configured in conftest.py.
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok"}


def test_health_returns_503_when_database_is_unreachable() -> None:
    # Port 1 on localhost refuses connections immediately: a real connection failure.
    unreachable = create_engine(
        "postgresql+psycopg://nobody:nothing@127.0.0.1:1/missing",
        connect_args={"connect_timeout": 2},
    )

    def unreachable_db() -> Iterator[Session]:
        with Session(unreachable) as session:
            yield session

    app.dependency_overrides[get_db] = unreachable_db
    try:
        with TestClient(app) as client:
            response = client.get("/health")
    finally:
        app.dependency_overrides.clear()
        unreachable.dispose()

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable", "database": "unavailable"}
