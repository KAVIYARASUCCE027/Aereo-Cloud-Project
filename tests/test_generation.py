"""process_job against the isolated test database and temporary storage.

The successful path uses the real renderer and storage. Failures are injected
by replacing functions with monkeypatch, never through a public parameter.
"""

import errno
import io
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest
from pypdf import PdfReader
from sqlalchemy import event, select, text
from sqlalchemy.exc import OperationalError

from app.db import SessionLocal, engine
from app.models import GenerationJob, JobRecipient, JobStatus, RecipientStatus
from app.services import generation, storage
from app.services.generation import JobProcessingError, ProcessOutcome, claim_job, claim_statement, process_job
from app.services.pdf import CertificateData, TextDoesNotFitError
from app.services.progress import JobNotFoundError, JobProgress, get_job_progress

real_render = generation.render_certificate

CreateJob = Callable[..., uuid.UUID]  # the create_job fixture in conftest.py


def load_job(job_id: uuid.UUID) -> GenerationJob:
    with SessionLocal() as session:
        return session.get(GenerationJob, job_id)


def load_recipients(job_id: uuid.UUID) -> list[JobRecipient]:
    with SessionLocal() as session:
        query = select(JobRecipient).where(JobRecipient.job_id == job_id).order_by(JobRecipient.input_index)
        return list(session.scalars(query))


def files_in(root: Path) -> list[str]:
    return sorted(path.name for path in root.iterdir()) if root.exists() else []


def pdf_text(path: Path) -> str:
    return PdfReader(io.BytesIO(path.read_bytes())).pages[0].extract_text()


def render_failing_for(name: str, error: Exception) -> Callable[[CertificateData], bytes]:
    def render(data: CertificateData) -> bytes:
        if data.recipient_name == name:
            raise error
        return real_render(data)

    return render


# --- successful path ------------------------------------------------------------


def test_all_valid_recipients_produce_real_parseable_pdfs(create_job: CreateJob, storage_root: Path) -> None:
    job_id = create_job([{"name": "Asha Sharma"}, {"name": "Zoë Ångström"}, {"name": "Дмитрий"}])

    result = process_job(job_id, storage_root=storage_root)

    assert (result.outcome, result.status) == (ProcessOutcome.PROCESSED, JobStatus.COMPLETED)
    rows = load_recipients(job_id)
    assert [r.status for r in rows] == [RecipientStatus.SUCCESS] * 3
    assert files_in(storage_root) == sorted(f"{r.id}.pdf" for r in rows)  # no temporary files left
    for row in rows:
        assert row.storage_key == f"{row.id}.pdf"  # the recipient ID is the certificate ID
        assert row.error_details is None
        text = pdf_text(storage_root / row.storage_key)
        assert row.name in text
        assert "Backend Development Workshop" in text
        assert str(row.id) in text


def test_duplicate_names_get_distinct_certificate_ids_and_files(create_job: CreateJob, storage_root: Path) -> None:
    same = {"name": "Asha Sharma", "email": "asha@example.com"}
    job_id = create_job([same, same, same])

    process_job(job_id, storage_root=storage_root)

    rows = load_recipients(job_id)
    assert [r.status for r in rows] == [RecipientStatus.SUCCESS] * 3
    assert len({r.id for r in rows}) == len({r.storage_key for r in rows}) == 3
    assert files_in(storage_root) == sorted(f"{r.id}.pdf" for r in rows)
    for row in rows:  # each file carries its own certificate ID
        assert str(row.id) in pdf_text(storage_root / row.storage_key)


def test_invalid_recipients_are_skipped(create_job: CreateJob, storage_root: Path) -> None:
    job_id = create_job([{"name": "Asha"}, {"name": "", "email": "bad"}, {"name": "Rohan"}])
    invalid_before = load_recipients(job_id)[1]

    result = process_job(job_id, storage_root=storage_root)

    assert result.status is JobStatus.COMPLETED_WITH_ERRORS
    rows = load_recipients(job_id)
    assert [r.status for r in rows] == [RecipientStatus.SUCCESS, RecipientStatus.INVALID, RecipientStatus.SUCCESS]
    invalid = rows[1]
    assert (invalid.started_at, invalid.completed_at, invalid.storage_key) == (None, None, None)
    assert invalid.error_details == invalid_before.error_details
    assert invalid.updated_at == invalid_before.updated_at  # not touched at all
    assert f"{invalid.id}.pdf" not in files_in(storage_root)


# --- failure isolation --------------------------------------------------------------


def test_renderer_failure_for_the_middle_recipient_does_not_stop_the_next(
    create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = create_job([{"name": "First"}, {"name": "Middle"}, {"name": "Last"}])
    rendered: list[str] = []
    failing = render_failing_for("Middle", TextDoesNotFitError("recipient_name", "recipient_name does not fit"))

    def recording_render(data: CertificateData) -> bytes:
        rendered.append(data.recipient_name)
        return failing(data)

    monkeypatch.setattr(generation, "render_certificate", recording_render)

    result = process_job(job_id, storage_root=storage_root)

    assert rendered == ["First", "Middle", "Last"]  # input_index order
    assert result.status is JobStatus.COMPLETED_WITH_ERRORS
    first, middle, last = load_recipients(job_id)
    assert [first.status, middle.status, last.status] == [
        RecipientStatus.SUCCESS,
        RecipientStatus.FAILED,
        RecipientStatus.SUCCESS,
    ]
    assert middle.error_details == [
        {"field": "name", "code": "text_does_not_fit", "message": "recipient_name does not fit"}
    ]
    assert middle.storage_key is None
    assert files_in(storage_root) == sorted([f"{first.id}.pdf", f"{last.id}.pdf"])
    assert "Last" in pdf_text(storage_root / last.storage_key)


def test_unexpected_renderer_error_is_logged_but_not_exposed(
    create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = create_job([{"name": "Asha"}, {"name": "Boom"}])
    monkeypatch.setattr(
        generation, "render_certificate", render_failing_for("Boom", RuntimeError("secret detail at /srv/internal"))
    )

    with caplog.at_level(logging.ERROR, logger="app.services.generation"):
        process_job(job_id, storage_root=storage_root)

    failed = load_recipients(job_id)[1]
    assert failed.error_details == [
        {"field": None, "code": "generation_error", "message": "Unexpected error while generating the certificate"}
    ]
    [record] = [r for r in caplog.records if r.exc_info]
    assert str(job_id) in record.getMessage() and str(failed.id) in record.getMessage()
    assert "secret detail" in str(record.exc_info[1])  # the full error is in the log only


def test_storage_failure_is_recorded_and_processing_continues(
    create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = create_job([{"name": "First"}, {"name": "Middle"}, {"name": "Last"}])
    first_id, middle_id, last_id = (r.id for r in load_recipients(job_id))
    real_replace = os.replace

    def replace_failing_for_middle(source: str, destination: Any) -> None:
        if Path(destination).name == f"{middle_id}.pdf":
            raise OSError(errno.ENOSPC, "No space left on device", str(destination))
        real_replace(source, destination)

    # Fails inside the real atomic write, after the temporary file was written.
    monkeypatch.setattr(storage.os, "replace", replace_failing_for_middle)

    result = process_job(job_id, storage_root=storage_root)

    assert result.status is JobStatus.COMPLETED_WITH_ERRORS
    first, middle, last = load_recipients(job_id)
    assert [first.status, middle.status, last.status] == [
        RecipientStatus.SUCCESS,
        RecipientStatus.FAILED,
        RecipientStatus.SUCCESS,
    ]
    assert middle.error_details == [
        {"field": None, "code": "storage_error", "message": "The certificate file could not be saved (ENOSPC)"}
    ]
    assert str(storage_root) not in str(middle.error_details)
    # The temporary file of the failed write was removed; only real certificates remain.
    assert files_in(storage_root) == sorted([f"{first_id}.pdf", f"{last_id}.pdf"])


def test_job_fails_when_every_valid_recipient_fails(
    create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = create_job([{"name": "A"}, {"name": ""}, {"name": "B"}])

    def always_fails(data: CertificateData) -> bytes:
        raise TextDoesNotFitError("event_name", "event_name does not fit")

    monkeypatch.setattr(generation, "render_certificate", always_fails)

    result = process_job(job_id, storage_root=storage_root)

    assert result.status is JobStatus.FAILED
    assert result.progress == JobProgress(total_count=3, invalid_count=1, failed_count=2)
    job = load_job(job_id)
    assert job.status is JobStatus.FAILED and job.completed_at is not None
    assert load_recipients(job_id)[0].error_details[0]["field"] == "event_name"
    assert files_in(storage_root) == []


def test_older_record_with_unsupported_text_fails_at_render_time(storage_root: Path) -> None:
    # Written directly, as rows created before submission validation checked characters.
    job_id = uuid.uuid4()
    with SessionLocal.begin() as session:
        session.add(
            GenerationJob(
                id=job_id,
                event_name="Legacy Event",
                issuer_name="ABC Academy",
                issue_date=date(2026, 10, 7),
                status=JobStatus.QUEUED,
                total_count=2,
                recipients=[
                    JobRecipient(id=uuid.uuid4(), input_index=0, name="आशा शर्मा", status=RecipientStatus.PENDING),
                    JobRecipient(id=uuid.uuid4(), input_index=1, name="Rohan Gupta", status=RecipientStatus.PENDING),
                ],
            )
        )

    result = process_job(job_id, storage_root=storage_root)

    assert result.status is JobStatus.COMPLETED_WITH_ERRORS
    unsupported, rohan = load_recipients(job_id)
    assert (unsupported.status, rohan.status) == (RecipientStatus.FAILED, RecipientStatus.SUCCESS)
    [error] = unsupported.error_details
    assert (error["field"], error["code"]) == ("name", "unsupported_characters")
    assert "U+0906 DEVANAGARI LETTER AA" in error["message"]


# --- database failures ----------------------------------------------------------------


def test_database_failure_stops_processing_and_keeps_earlier_successes(
    create_job: CreateJob, storage_root: Path, caplog: pytest.LogCaptureFixture
) -> None:
    job_id = create_job([{"name": "First"}, {"name": "Second"}, {"name": "Third"}])
    success_updates: list[Any] = []

    def fail_second_success_update(conn: Any, cursor: Any, statement: str, parameters: Any, *args: Any) -> None:
        # The SUCCESS update is the only recipient UPDATE that sets storage_key.
        if statement.startswith("UPDATE job_recipients") and "storage_key" in statement:
            success_updates.append(parameters)
            if len(success_updates) == 2:
                raise psycopg.OperationalError("simulated database outage")

    event.listen(engine, "before_cursor_execute", fail_second_success_update)
    try:
        with caplog.at_level(logging.ERROR, logger="app.services.generation"):
            with pytest.raises(JobProcessingError) as error:
                process_job(job_id, storage_root=storage_root)
    finally:
        event.remove(engine, "before_cursor_execute", fail_second_success_update)

    assert isinstance(error.value.__cause__, OperationalError)  # surfaced, not swallowed
    assert any(str(job_id) in r.getMessage() and "Database error" in r.getMessage() for r in caplog.records)

    first, second, third = load_recipients(job_id)
    assert first.status is RecipientStatus.SUCCESS  # committed earlier, kept
    assert second.status is RecipientStatus.PROCESSING  # its SUCCESS update was rolled back
    assert third.status is RecipientStatus.PENDING  # never started
    assert second.error_details is None  # not misreported as a certificate failure
    # Documented consistency gap: the second PDF was saved, but no SUCCESS row references it.
    assert (storage_root / f"{second.id}.pdf").exists()
    job = load_job(job_id)
    assert (job.status, job.completed_at) == (JobStatus.PROCESSING, None)


# --- claims and repeated calls ------------------------------------------------------


def test_reprocessing_a_finished_job_generates_nothing(
    create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = create_job([{"name": "Asha"}, {"name": "Rohan"}])
    process_job(job_id, storage_root=storage_root)
    files_before = {path.name: path.stat().st_mtime_ns for path in storage_root.iterdir()}
    rows_before = [(r.id, r.status, r.updated_at) for r in load_recipients(job_id)]

    def must_not_render(data: CertificateData) -> bytes:
        raise AssertionError("a finished job must not be rendered again")

    monkeypatch.setattr(generation, "render_certificate", must_not_render)

    again = process_job(job_id, storage_root=storage_root)

    assert (again.outcome, again.status) == (ProcessOutcome.ALREADY_FINISHED, JobStatus.COMPLETED)
    assert {path.name: path.stat().st_mtime_ns for path in storage_root.iterdir()} == files_before
    assert [(r.id, r.status, r.updated_at) for r in load_recipients(job_id)] == rows_before


def test_unknown_job_raises_job_not_found(storage_root: Path) -> None:
    with pytest.raises(JobNotFoundError):
        process_job(uuid.uuid4(), storage_root=storage_root)


def wait_until(condition: Callable[[], bool], timeout: float = 10.0) -> None:
    """Poll a condition (not a fixed sleep); fail if it is not met in time."""
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def sessions_waiting_for_a_lock() -> int:
    with engine.connect() as connection:
        return connection.scalar(
            text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        )


def test_a_competing_claim_waits_for_the_first_and_then_loses(create_job: CreateJob) -> None:
    job_id = create_job([{"name": "Asha"}])

    with SessionLocal() as first_caller, ThreadPoolExecutor(max_workers=1) as pool:
        # First caller has run its claim UPDATE but not committed yet: it holds the row lock.
        assert first_caller.execute(claim_statement(job_id)).one_or_none() is not None
        second = pool.submit(claim_job, job_id)

        wait_until(lambda: sessions_waiting_for_a_lock() == 1)  # second caller is blocked on the row
        assert not second.done()
        first_caller.commit()

        second_claim = second.result(timeout=10)

    assert second_claim.job is None
    assert second_claim.status is JobStatus.PROCESSING
    assert load_job(job_id).status is JobStatus.PROCESSING


def test_simultaneous_claims_have_exactly_one_winner(create_job: CreateJob) -> None:
    job_id = create_job([{"name": "Asha"}])
    callers = 8
    barrier = threading.Barrier(callers)

    def claim_together() -> Any:
        barrier.wait(timeout=10)
        return claim_job(job_id)

    with ThreadPoolExecutor(max_workers=callers) as pool:
        claims = [future.result(timeout=30) for future in [pool.submit(claim_together) for _ in range(callers)]]

    assert sum(claim.job is not None for claim in claims) == 1
    assert all(claim.status is JobStatus.PROCESSING for claim in claims)


def test_call_during_processing_returns_already_processing_and_progress_is_exact(
    create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = create_job([{"name": "First"}, {"name": "Second"}, {"name": "Third"}, {"name": ""}])
    rendering_second = threading.Event()
    release = threading.Event()

    def pausing_render(data: CertificateData) -> bytes:
        if data.recipient_name == "Second":
            rendering_second.set()
            if not release.wait(timeout=30):
                raise TimeoutError("test did not release the renderer")
        return real_render(data)

    monkeypatch.setattr(generation, "render_certificate", pausing_render)

    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(process_job, job_id, storage_root)
        try:
            assert rendering_second.wait(timeout=30)
            concurrent_call = process_job(job_id, storage_root=storage_root)
            with SessionLocal() as session:
                paused_progress = get_job_progress(session, job_id)
        finally:
            release.set()
        finished = running.result(timeout=60)

    assert (concurrent_call.outcome, concurrent_call.status) == (
        ProcessOutcome.ALREADY_PROCESSING,
        JobStatus.PROCESSING,
    )
    assert paused_progress == JobProgress(
        total_count=4, pending_count=1, processing_count=1, success_count=1, invalid_count=1
    )
    assert paused_progress.progress_percent == 50
    assert (finished.outcome, finished.status) == (ProcessOutcome.PROCESSED, JobStatus.COMPLETED_WITH_ERRORS)


# --- final counts and timestamps --------------------------------------------------------


def test_final_counts_and_timestamps(create_job: CreateJob, storage_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    job_id = create_job([{"name": "A"}, {"name": "Breaks"}, {"name": " "}, {"name": "D"}])
    monkeypatch.setattr(
        generation, "render_certificate", render_failing_for("Breaks", TextDoesNotFitError("recipient_name", "x"))
    )

    result = process_job(job_id, storage_root=storage_root)

    assert result.progress == JobProgress(total_count=4, success_count=2, invalid_count=1, failed_count=1)
    assert result.progress.processed_count == 4
    assert result.progress.progress_percent == 100

    job = load_job(job_id)
    assert job.status is JobStatus.COMPLETED_WITH_ERRORS
    assert job.created_at <= job.started_at <= job.completed_at
    assert job.updated_at == job.completed_at  # updated by the finalizing transaction

    processed = [r for r in load_recipients(job_id) if r.status is not RecipientStatus.INVALID]
    for row in processed:
        assert job.started_at <= row.started_at <= row.completed_at <= job.completed_at
    invalid = next(r for r in load_recipients(job_id) if r.status is RecipientStatus.INVALID)
    assert (invalid.started_at, invalid.completed_at) == (None, None)
