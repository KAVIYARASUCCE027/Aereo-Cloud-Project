"""Publishing jobs to Celery, the task wrapper, and publish failures.

Publishing uses Celery's real in-memory transport (see conftest.py), so these
tests read back the actual message. A broker outage is simulated by replacing
the task object in app.services.dispatch with one whose apply_async raises the
error kombu raises for an unreachable Redis.

These are not worker integration tests: no Celery worker consumes the queue
here. The real Redis + worker path is checked by the Phase 5 demo and by the
Phase 7 integration test.
"""

import functools
import logging
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient
from kombu.exceptions import OperationalError as BrokerOperationalError
from sqlalchemy import event, select

from app import worker
from app.db import SessionLocal, engine
from app.models import GenerationJob, JobRecipient, JobStatus, RecipientStatus
from app.services import dispatch, generation
from app.services.generation import claim_job
from app.services.progress import JobNotFoundError, get_job_progress

JOBS_URL = "/api/v1/jobs"
Payload = Callable[..., dict[str, Any]]


class BrokerDownTask:
    """Stands in for the Celery task while Redis is unreachable."""

    def __init__(self, before_failing: Callable[[list[str]], None] | None = None) -> None:
        self.before_failing = before_failing

    def apply_async(self, args: list[str], **_: Any) -> None:
        if self.before_failing:
            self.before_failing(args)
        raise BrokerOperationalError("Error 111 connecting to redis:6379. Connection refused.")


def load_job(job_id: uuid.UUID) -> GenerationJob:
    with SessionLocal() as session:
        return session.get(GenerationJob, job_id)


def load_recipients(job_id: uuid.UUID) -> list[JobRecipient]:
    with SessionLocal() as session:
        query = select(JobRecipient).where(JobRecipient.job_id == job_id).order_by(JobRecipient.input_index)
        return list(session.scalars(query))


@pytest.fixture
def worker_storage(monkeypatch: pytest.MonkeyPatch, storage_root: Path) -> Path:
    """Make the task wrapper write PDFs to temporary storage instead of CERTIFICATE_STORAGE_DIR."""
    monkeypatch.setattr(worker, "process_job", functools.partial(generation.process_job, storage_root=storage_root))
    return storage_root


# --- publishing -------------------------------------------------------------------


def test_submission_publishes_one_task_carrying_only_the_job_id(
    client: TestClient, broker_messages: Callable[[], list[Any]], job_payload: Payload
) -> None:
    response = client.post(JOBS_URL, json=job_payload([{"name": "Asha"}, {"name": "Rohan"}]))

    assert response.status_code == 202
    assert broker_messages() == [("certificates.process_job", [response.json()["job_id"]], {})]


def test_published_message_runs_the_job_when_executed(
    client: TestClient, broker_messages: Callable[[], list[Any]], job_payload: Payload, worker_storage: Path
) -> None:
    response = client.post(JOBS_URL, json=job_payload([{"name": "Asha"}, {"name": ""}]))
    [(task_name, args, kwargs)] = broker_messages()

    # Execute exactly what a worker would: the registered task, with the message's arguments.
    result = worker.celery_app.tasks[task_name](*args, **kwargs)

    job_id = uuid.UUID(response.json()["job_id"])
    assert result == {"job_id": str(job_id), "outcome": "processed", "status": "COMPLETED_WITH_ERRORS"}
    assert [r.status for r in load_recipients(job_id)] == [RecipientStatus.SUCCESS, RecipientStatus.INVALID]
    assert len(list(worker_storage.glob("*.pdf"))) == 1


# --- the task wrapper -------------------------------------------------------------------


def test_task_is_registered_under_its_stable_name() -> None:
    # process_job_task is a lazy proxy, so compare the registered task by name.
    assert worker.process_job_task.name == worker.PROCESS_JOB_TASK
    assert worker.celery_app.tasks[worker.PROCESS_JOB_TASK].name == worker.PROCESS_JOB_TASK


def test_delivery_settings_match_the_documented_behaviour() -> None:
    conf = worker.celery_app.conf

    assert (conf.task_acks_late, conf.task_reject_on_worker_lost, conf.worker_prefetch_multiplier) == (True, True, 1)
    assert (conf.task_serializer, conf.accept_content, conf.task_ignore_result) == ("json", ["json"], True)
    assert conf.broker_transport_options["visibility_timeout"] == worker.VISIBILITY_TIMEOUT_SECONDS


def test_redelivered_message_for_a_finished_job_regenerates_nothing(
    create_job: Callable[..., uuid.UUID], worker_storage: Path
) -> None:
    job_id = create_job([{"name": "Asha"}])
    worker.process_job_task(str(job_id))
    files = {path.name: path.stat().st_mtime_ns for path in worker_storage.iterdir()}

    again = worker.process_job_task(str(job_id))

    assert again == {"job_id": str(job_id), "outcome": "already_finished", "status": "COMPLETED"}
    assert {path.name: path.stat().st_mtime_ns for path in worker_storage.iterdir()} == files


def test_redelivered_message_for_a_claimed_job_does_nothing(
    create_job: Callable[..., uuid.UUID], worker_storage: Path
) -> None:
    # As after a worker crash: the job was claimed, then its message is delivered again.
    job_id = create_job([{"name": "Asha"}])
    assert claim_job(job_id).job is not None

    result = worker.process_job_task(str(job_id))

    assert result == {"job_id": str(job_id), "outcome": "already_processing", "status": "PROCESSING"}
    assert load_recipients(job_id)[0].status is RecipientStatus.PENDING
    assert not worker_storage.exists()


def test_task_errors_propagate_so_celery_logs_them(worker_storage: Path) -> None:
    with pytest.raises(JobNotFoundError):
        worker.process_job_task(str(uuid.uuid4()))


# --- publish failures ----------------------------------------------------------------------


def test_broker_outage_returns_503_and_marks_the_job_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_payload: Payload, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(dispatch, "process_job_task", BrokerDownTask())

    with caplog.at_level(logging.ERROR):
        response = client.post(JOBS_URL, json=job_payload([{"name": "Asha"}, {"name": ""}, {"name": "Rohan"}]))

    assert response.status_code == 503
    detail = response.json()["detail"]
    job_id = uuid.UUID(detail["job_id"])
    assert detail == {
        "code": "dispatch_failed",
        "message": "The job was saved but could not be queued for processing, so it was marked FAILED.",
        "job_id": str(job_id),
        "job_status": "FAILED",
        "status_url": f"/api/v1/jobs/{job_id}",
    }
    assert any("Could not publish job" in r.getMessage() and r.exc_info for r in caplog.records)

    job = load_job(job_id)
    assert (job.status, job.error_code, job.started_at) == (JobStatus.FAILED, "dispatch_failed", None)
    assert job.error_message and job.completed_at is not None
    asha, invalid, rohan = load_recipients(job_id)
    for row in (asha, rohan):
        assert (row.status, row.started_at, row.storage_key) == (RecipientStatus.FAILED, None, None)
        assert row.completed_at is not None
        assert row.error_details == [
            {"field": None, "code": "dispatch_failed", "message": "Not processed: the job could not be queued for processing."}
        ]
    assert invalid.status is RecipientStatus.INVALID
    assert invalid.error_details[0]["code"] == "string_too_short"  # keeps its own error

    with SessionLocal() as session:
        assert get_job_progress(session, job_id).progress_percent == 100  # every entry has a final outcome
    assert claim_job(job_id).job is None  # a FAILED job can no longer be claimed by a late message


def test_dispatch_failure_never_overwrites_a_job_a_worker_already_claimed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_payload: Payload
) -> None:
    # The publish reaches Redis and a worker claims the job, but the API still
    # sees an error (for example the connection drops before the reply).
    def worker_claims_first(args: list[str]) -> None:
        assert claim_job(uuid.UUID(args[0])).job is not None

    monkeypatch.setattr(dispatch, "process_job_task", BrokerDownTask(before_failing=worker_claims_first))

    response = client.post(JOBS_URL, json=job_payload([{"name": "Asha"}]))

    assert response.status_code == 202
    assert response.json()["status"] == "PROCESSING"
    job_id = uuid.UUID(response.json()["job_id"])
    job = load_job(job_id)
    assert (job.status, job.error_code, job.completed_at) == (JobStatus.PROCESSING, None, None)
    assert load_recipients(job_id)[0].status is RecipientStatus.PENDING


def test_if_the_failure_cannot_be_recorded_the_response_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_payload: Payload
) -> None:
    monkeypatch.setattr(dispatch, "process_job_task", BrokerDownTask())

    def database_fails_on_job_update(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        if statement.startswith("UPDATE generation_jobs"):
            raise psycopg.OperationalError("simulated database outage")

    event.listen(engine, "before_cursor_execute", database_fails_on_job_update)
    try:
        response = client.post(JOBS_URL, json=job_payload([{"name": "Asha"}]))
    finally:
        event.remove(engine, "before_cursor_execute", database_fails_on_job_update)

    assert response.status_code == 503
    detail = response.json()["detail"]
    assert detail["job_status"] is None
    assert detail["message"] == "The job was saved but could not be queued, and its status could not be updated."
    # Documented gap: the job remains QUEUED with no message to process it.
    assert load_job(uuid.UUID(detail["job_id"])).status is JobStatus.QUEUED


def test_unexpected_publish_errors_are_reported_the_same_way(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, job_payload: Payload
) -> None:
    class BrokenTask:
        def apply_async(self, args: list[str], **_: Any) -> None:
            raise RuntimeError("serializer exploded")

    monkeypatch.setattr(dispatch, "process_job_task", BrokenTask())

    response = client.post(JOBS_URL, json=job_payload([{"name": "Asha"}]))

    assert response.status_code == 503
    assert response.json()["detail"]["job_status"] == "FAILED"
