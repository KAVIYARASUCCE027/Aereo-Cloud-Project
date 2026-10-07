"""POST /api/v1/jobs against the isolated test database. Dispatch is recorded, not executed."""

import uuid
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.db import SessionLocal, engine
from app.main import app
from app.models import GenerationJob, JobRecipient, JobStatus, RecipientStatus
from app.schemas import MAX_RECIPIENTS_PER_JOB
from app.services import jobs as jobs_service
from app.services.validation import RecipientValidationResult, ValidRecipient

JOBS_URL = "/api/v1/jobs"

Payload = Callable[..., dict[str, Any]]


def count_rows(session: Session, model: type) -> int:
    return session.scalar(select(func.count()).select_from(model))


def recipients_of(session: Session, job_id: str) -> list[JobRecipient]:
    query = select(JobRecipient).where(JobRecipient.job_id == uuid.UUID(job_id)).order_by(JobRecipient.input_index)
    return list(session.scalars(query))


def assert_nothing_persisted(session: Session) -> None:
    assert count_rows(session, GenerationJob) == 0
    assert count_rows(session, JobRecipient) == 0


# --- accepted batches -------------------------------------------------------


def test_valid_batch_creates_queued_job_and_pending_recipients(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    payload = job_payload(
        [{"name": "Asha Sharma", "email": "asha@example.com"}, {"name": "Rohan Gupta", "email": "rohan@example.com"}]
    )

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 202
    body = response.json()
    job_id = body["job_id"]
    assert body == {
        "job_id": job_id,
        "status": "QUEUED",
        "total_count": 2,
        "valid_count": 2,
        "invalid_count": 0,
        "status_url": f"/api/v1/jobs/{job_id}",
    }

    job = db_session.get(GenerationJob, uuid.UUID(job_id))
    assert job is not None
    assert (job.event_name, job.issuer_name, job.issue_date.isoformat()) == (
        "Backend Development Workshop",
        "ABC Academy",
        "2026-10-07",
    )
    assert job.status is JobStatus.QUEUED
    assert job.total_count == 2
    assert job.created_at.tzinfo is not None
    assert (job.started_at, job.completed_at, job.error_code, job.error_message) == (None, None, None, None)

    rows = recipients_of(db_session, job_id)
    assert [(r.input_index, r.name, r.email, r.status) for r in rows] == [
        (0, "Asha Sharma", "asha@example.com", RecipientStatus.PENDING),
        (1, "Rohan Gupta", "rohan@example.com", RecipientStatus.PENDING),
    ]
    assert all(r.error_details is None and r.storage_key is None for r in rows)

    assert dispatched == [uuid.UUID(job_id)]


def test_mixed_batch_keeps_valid_and_invalid_outcomes(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    payload = job_payload(
        [
            {"name": "Asha Sharma", "email": "asha@example.com"},
            {"name": "Rohan Gupta", "email": "rohan@example.com"},
            {"name": "", "email": "invalid-email"},
        ]
    )

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 202
    body = response.json()
    assert (body["status"], body["total_count"], body["valid_count"], body["invalid_count"]) == ("QUEUED", 3, 2, 1)

    rows = recipients_of(db_session, body["job_id"])
    assert [(r.input_index, r.status) for r in rows] == [
        (0, RecipientStatus.PENDING),
        (1, RecipientStatus.PENDING),
        (2, RecipientStatus.INVALID),
    ]
    invalid = rows[2]
    # The rejected values are not stored in the typed columns.
    assert (invalid.name, invalid.email, invalid.storage_key) == (None, None, None)
    assert sorted((e["field"], e["code"]) for e in invalid.error_details) == [
        ("email", "email_invalid"),
        ("name", "string_too_short"),
    ]
    assert all(e["message"] for e in invalid.error_details)
    assert len(dispatched) == 1


def test_malformed_entries_do_not_reject_a_valid_sibling(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    payload = job_payload([None, "Asha Sharma", {"name": "Valid Person"}, 42, ["x"], {"name": 7}])

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 202
    body = response.json()
    assert (body["total_count"], body["valid_count"], body["invalid_count"]) == (6, 1, 5)
    rows = recipients_of(db_session, body["job_id"])
    assert [(r.input_index, r.status) for r in rows] == [
        (0, RecipientStatus.INVALID),
        (1, RecipientStatus.INVALID),
        (2, RecipientStatus.PENDING),
        (3, RecipientStatus.INVALID),
        (4, RecipientStatus.INVALID),
        (5, RecipientStatus.INVALID),
    ]
    assert rows[2].name == "Valid Person"
    assert rows[0].error_details == [{"field": None, "code": "model_type", "message": rows[0].error_details[0]["message"]}]
    assert rows[5].error_details[0]["field"] == "name"
    assert rows[5].error_details[0]["code"] == "string_type"


def test_input_indexes_match_submission_order(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    payload = job_payload([{"name": " "}, {"name": "B"}, {"name": "C", "email": "bad"}, {"name": "D"}, {}])

    response = client.post(JOBS_URL, json=payload)

    rows = recipients_of(db_session, response.json()["job_id"])
    assert [(r.input_index, r.name, r.status) for r in rows] == [
        (0, None, RecipientStatus.INVALID),
        (1, "B", RecipientStatus.PENDING),
        (2, None, RecipientStatus.INVALID),
        (3, "D", RecipientStatus.PENDING),
        (4, None, RecipientStatus.INVALID),
    ]


def test_email_may_be_omitted_or_null(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    response = client.post(JOBS_URL, json=job_payload([{"name": "No Email"}, {"name": "Null Email", "email": None}]))

    assert response.status_code == 202
    rows = recipients_of(db_session, response.json()["job_id"])
    assert [(r.name, r.email, r.status) for r in rows] == [
        ("No Email", None, RecipientStatus.PENDING),
        ("Null Email", None, RecipientStatus.PENDING),
    ]


def test_duplicate_names_get_distinct_recipient_ids(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    same = {"name": "Asha Sharma", "email": "asha@example.com"}

    response = client.post(JOBS_URL, json=job_payload([same, same, same]))

    assert response.status_code == 202
    rows = recipients_of(db_session, response.json()["job_id"])
    assert len({r.id for r in rows}) == 3
    assert [(r.input_index, r.name, r.status) for r in rows] == [
        (0, "Asha Sharma", RecipientStatus.PENDING),
        (1, "Asha Sharma", RecipientStatus.PENDING),
        (2, "Asha Sharma", RecipientStatus.PENDING),
    ]


def test_exactly_the_recipient_limit_is_accepted(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    recipients = [{"name": f"Recipient {i}"} for i in range(MAX_RECIPIENTS_PER_JOB)]

    response = client.post(JOBS_URL, json=job_payload(recipients))

    assert MAX_RECIPIENTS_PER_JOB == 1000
    assert response.status_code == 202
    assert response.json()["total_count"] == 1000
    assert count_rows(db_session, JobRecipient) == 1000
    assert db_session.scalar(select(func.max(JobRecipient.input_index))) == 999


def test_job_is_committed_before_it_is_dispatched(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_payload: Payload
) -> None:
    seen_at_dispatch: list[tuple[uuid.UUID, int]] = []

    def check_committed(job_id: uuid.UUID) -> None:
        # A separate session only sees committed data.
        with SessionLocal() as other:
            seen_at_dispatch.append((job_id, count_rows(other, JobRecipient)))

    monkeypatch.setattr(jobs_service.dispatch, "enqueue_job", check_committed)

    response = client.post(JOBS_URL, json=job_payload([{"name": "A"}, {"name": ""}]))

    assert response.status_code == 202
    assert seen_at_dispatch == [(uuid.UUID(response.json()["job_id"]), 2)]


# --- rejected requests --------------------------------------------------------


def test_all_invalid_recipients_return_422_with_indexed_errors(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    payload = job_payload([{"name": ""}, None, {"name": "Asha", "email": "nope"}])

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["code"] == "all_recipients_invalid"
    assert [item["input_index"] for item in detail["recipient_errors"]] == [0, 1, 2]
    assert detail["recipient_errors"][0]["errors"][0]["field"] == "name"
    assert detail["recipient_errors"][1]["errors"][0] == {
        "field": None,
        "code": "model_type",
        "message": detail["recipient_errors"][1]["errors"][0]["message"],
    }
    assert detail["recipient_errors"][2]["errors"][0]["code"] == "email_invalid"
    assert_nothing_persisted(db_session)
    assert dispatched == []


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"event_name": None}, "event_name"),
        ({"event_name": "   "}, "event_name"),
        ({"event_name": "x" * 201}, "event_name"),
        ({"event_name": 123}, "event_name"),
        ({"issuer_name": ""}, "issuer_name"),
        ({"issuer_name": "x" * 201}, "issuer_name"),
        ({"issue_date": "2026-13-01"}, "issue_date"),
        ({"issue_date": "2026-02-30"}, "issue_date"),
        ({"issue_date": "07/10/2026"}, "issue_date"),
        ({"issue_date": "2026-10-07T10:00:00"}, "issue_date"),
        ({"issue_date": 1791331200}, "issue_date"),
        ({"issue_date": ""}, "issue_date"),
        ({"recipients": None}, "recipients"),
        ({"recipients": {"name": "Asha"}}, "recipients"),
        ({"recipients": "Asha"}, "recipients"),
        ({"recipients": []}, "recipients"),
    ],
)
def test_invalid_shared_fields_return_422_and_create_nothing(
    client: TestClient,
    db_session: Session,
    dispatched: list[Any],
    job_payload: Payload,
    overrides: dict[str, Any],
    field: str,
) -> None:
    payload = job_payload([{"name": "Asha"}]) | overrides

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 422
    assert ["body", field] in [error["loc"][:2] for error in response.json()["detail"]]
    assert_nothing_persisted(db_session)
    assert dispatched == []


@pytest.mark.parametrize("missing", ["event_name", "issuer_name", "issue_date", "recipients"])
def test_missing_shared_field_returns_422(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload, missing: str
) -> None:
    payload = job_payload([{"name": "Asha"}])
    del payload[missing]

    response = client.post(JOBS_URL, json=payload)

    assert response.status_code == 422
    assert {"loc": ["body", missing], "type": "missing"} in [
        {"loc": e["loc"], "type": e["type"]} for e in response.json()["detail"]
    ]
    assert_nothing_persisted(db_session)


def test_empty_recipient_list_returns_422(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    response = client.post(JOBS_URL, json=job_payload([]))

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "too_short"
    assert_nothing_persisted(db_session)


def test_more_than_the_recipient_limit_returns_422(
    client: TestClient, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    recipients = [{"name": f"Recipient {i}"} for i in range(MAX_RECIPIENTS_PER_JOB + 1)]

    response = client.post(JOBS_URL, json=job_payload(recipients))

    assert response.status_code == 422
    error = response.json()["detail"][0]
    assert (error["loc"], error["type"]) == (["body", "recipients"], "too_long")
    assert_nothing_persisted(db_session)
    assert dispatched == []


# --- persistence failure ----------------------------------------------------------


def test_failed_persistence_rolls_back_job_and_recipients(
    monkeypatch: pytest.MonkeyPatch, db_session: Session, dispatched: list[Any], job_payload: Payload
) -> None:
    # Force a real database error part-way through the transaction: two valid
    # recipients claim input_index 0, so the recipient INSERT violates
    # uq_job_recipients_job_id_input_index after the job row was already inserted.
    duplicate_index = RecipientValidationResult(
        valid=[ValidRecipient(0, "First", None), ValidRecipient(0, "Second", None)],
        invalid=[],
    )
    monkeypatch.setattr(jobs_service, "validate_recipients", lambda entries: duplicate_index)

    statements: list[str] = []

    def record(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", record)
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.post(JOBS_URL, json=job_payload([{"name": "First"}, {"name": "Second"}]))
    finally:
        event.remove(engine, "before_cursor_execute", record)

    assert response.status_code == 500
    # The job row was written inside the transaction before the failure...
    assert any(s.startswith("INSERT INTO generation_jobs") for s in statements)
    assert any(s.startswith("INSERT INTO job_recipients") for s in statements)
    # ...and the rollback removed it along with everything else.
    assert_nothing_persisted(db_session)
    assert dispatched == []
