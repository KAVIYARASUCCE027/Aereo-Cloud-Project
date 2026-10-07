"""Certificate generation for one job: process_job(job_id).

Callable directly (tests, `python -m app.cli process-job JOB_ID`) and, from
Phase 5, from a thin Celery task. It opens its own short database sessions and
never keeps a transaction open while a PDF is rendered or written. Only plain
values (UUIDs, strings, dates) cross transaction boundaries, never ORM
objects, so no lazy load can silently start a new transaction.

1. claim_job: one conditional UPDATE moves the job QUEUED -> PROCESSING and
   sets started_at, then commits. PostgreSQL row locking lets one caller win.
2. For each PENDING recipient, in input_index order:
   a. short transaction: PENDING -> PROCESSING, started_at; read the name
   b. no transaction: render the PDF, save it atomically
   c. short transaction: SUCCESS + storage_key + completed_at, or
      FAILED + error_details + completed_at
   A render or storage failure affects only that recipient.
3. finalize: derive the terminal status from recipient rows, set completed_at.

A database error is not a recipient failure: its transaction is rolled back,
processing stops, and JobProcessingError is raised. Results committed before
that are kept. The job (and possibly one recipient) stays PROCESSING; recovery
of such jobs is not implemented yet. If the error happens after a PDF was
saved, that file exists without a SUCCESS row referencing it; only the
database status says whether a certificate succeeded.
"""

import errno
import logging
import uuid
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

from sqlalchemy import Update, func, select, update
from sqlalchemy.exc import SQLAlchemyError

from app.config import get_settings
from app.db import SessionLocal
from app.models import GenerationJob, JobRecipient, JobStatus, RecipientStatus
from app.services.pdf import CertificateData, CertificateRenderError, render_certificate
from app.services.progress import JobNotFoundError, JobProgress, final_job_status, get_job_progress
from app.services.storage import certificate_storage_key, write_file_atomically

logger = logging.getLogger(__name__)

STORAGE_ERROR = "storage_error"
GENERATION_ERROR = "generation_error"

# Renderer field names -> request field names, as used in error_details.
_REQUEST_FIELDS = {
    "recipient_name": "name",
    "event_name": "event_name",
    "issuer_name": "issuer_name",
    "issue_date": "issue_date",
}


class ProcessOutcome(StrEnum):
    PROCESSED = "processed"  # this call claimed the job and finished it
    ALREADY_PROCESSING = "already_processing"  # another execution holds the claim
    ALREADY_FINISHED = "already_finished"  # terminal job; nothing regenerated


@dataclass(frozen=True)
class ProcessJobResult:
    job_id: uuid.UUID
    outcome: ProcessOutcome
    status: JobStatus
    progress: JobProgress


class JobProcessingError(RuntimeError):
    """Processing stopped because job state could not be read or persisted."""

    def __init__(self, job_id: uuid.UUID, message: str) -> None:
        super().__init__(f"Job {job_id}: {message}")
        self.job_id = job_id


@dataclass(frozen=True)
class ClaimedJob:
    """Shared certificate fields, copied out of the database at claim time."""

    job_id: uuid.UUID
    event_name: str
    issuer_name: str
    issue_date: date


@dataclass(frozen=True)
class JobClaim:
    status: JobStatus  # the job's status after this attempt
    job: ClaimedJob | None  # set only if this caller won the claim


@dataclass(frozen=True)
class RecipientFailure:
    code: str
    message: str
    field: str | None = None

    def as_error_detail(self) -> dict[str, str | None]:
        return {"field": self.field, "code": self.code, "message": self.message}


def process_job(job_id: uuid.UUID, storage_root: Path | None = None) -> ProcessJobResult:
    """Generate certificates for every PENDING recipient of a QUEUED job.

    Raises JobNotFoundError for an unknown job ID and JobProcessingError when
    the database fails or job state is inconsistent.
    """
    root = storage_root if storage_root is not None else get_settings().certificate_storage_dir
    try:
        claim = claim_job(job_id)
        if claim.job is None:
            outcome = (
                ProcessOutcome.ALREADY_PROCESSING
                if claim.status is JobStatus.PROCESSING
                else ProcessOutcome.ALREADY_FINISHED
            )
            logger.info("Job %s not processed: %s (status %s)", job_id, outcome, claim.status)
            return ProcessJobResult(job_id, outcome, claim.status, _read_progress(job_id))

        for recipient_id in _pending_recipient_ids(job_id):
            _process_recipient(claim.job, recipient_id, root)

        status, progress = _finalize_job(job_id)
    except SQLAlchemyError as exc:
        logger.exception("Database error while processing job %s; processing stopped", job_id)
        raise JobProcessingError(job_id, "database error; processing stopped and the job remains PROCESSING") from exc

    logger.info(
        "Job %s finished as %s: %d succeeded, %d failed, %d invalid",
        job_id,
        status,
        progress.success_count,
        progress.failed_count,
        progress.invalid_count,
    )
    return ProcessJobResult(job_id, ProcessOutcome.PROCESSED, status, progress)


def claim_job(job_id: uuid.UUID) -> JobClaim:
    """Atomically move a QUEUED job to PROCESSING and commit the claim.

    With two concurrent callers, PostgreSQL locks the row for the first UPDATE;
    the second waits, re-checks `status = 'QUEUED'` against the committed row,
    and updates nothing. Exactly one caller gets the job.
    """
    with SessionLocal.begin() as session:
        row = session.execute(claim_statement(job_id)).one_or_none()
        if row is not None:
            return JobClaim(JobStatus.PROCESSING, ClaimedJob(job_id, row.event_name, row.issuer_name, row.issue_date))
        status = session.scalar(select(GenerationJob.status).where(GenerationJob.id == job_id))
    if status is None:
        raise JobNotFoundError(job_id)
    return JobClaim(status, None)


def claim_statement(job_id: uuid.UUID) -> Update:
    return (
        update(GenerationJob)
        .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.QUEUED)
        .values(status=JobStatus.PROCESSING, started_at=func.now())
        .returning(GenerationJob.event_name, GenerationJob.issuer_name, GenerationJob.issue_date)
    )


def _pending_recipient_ids(job_id: uuid.UUID) -> list[uuid.UUID]:
    with SessionLocal.begin() as session:
        query = (
            select(JobRecipient.id)
            .where(JobRecipient.job_id == job_id, JobRecipient.status == RecipientStatus.PENDING)
            .order_by(JobRecipient.input_index)
        )
        return list(session.scalars(query))


def _process_recipient(job: ClaimedJob, recipient_id: uuid.UUID, root: Path) -> None:
    name = _start_recipient(recipient_id)
    if name is None:
        return  # no longer PENDING, so another execution handled it; skip

    # The recipient's own ID is the certificate ID, so every attempt for this
    # recipient produces the same certificate ID and the same file name.
    data = CertificateData(
        certificate_id=recipient_id,
        recipient_name=name,
        event_name=job.event_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
    )
    outcome = _generate_certificate(job.job_id, data, root)

    # Database writes happen here, outside the render/storage error handling,
    # so a database error is never mistaken for a certificate failure.
    if isinstance(outcome, RecipientFailure):
        _finish_recipient(job.job_id, recipient_id, RecipientStatus.FAILED, error_details=[outcome.as_error_detail()])
    else:
        _finish_recipient(job.job_id, recipient_id, RecipientStatus.SUCCESS, storage_key=outcome)


def _start_recipient(recipient_id: uuid.UUID) -> str | None:
    """PENDING -> PROCESSING in its own transaction. Returns the name, or None if not PENDING."""
    with SessionLocal.begin() as session:
        return session.scalar(
            update(JobRecipient)
            .where(JobRecipient.id == recipient_id, JobRecipient.status == RecipientStatus.PENDING)
            .values(status=RecipientStatus.PROCESSING, started_at=func.now())
            .returning(JobRecipient.name)
        )


def _generate_certificate(job_id: uuid.UUID, data: CertificateData, root: Path) -> str | RecipientFailure:
    """Render and save one PDF; returns its storage key or the failure. No database access."""
    try:
        pdf = render_certificate(data)
    except CertificateRenderError as exc:
        logger.warning("Certificate %s of job %s not rendered: %s", data.certificate_id, job_id, exc)
        return RecipientFailure(exc.code, str(exc), _REQUEST_FIELDS.get(exc.field))
    except Exception:
        logger.exception("Unexpected error rendering certificate %s of job %s", data.certificate_id, job_id)
        return RecipientFailure(GENERATION_ERROR, "Unexpected error while generating the certificate")

    storage_key = certificate_storage_key(data.certificate_id)
    try:
        write_file_atomically(root, storage_key, pdf)
    except Exception as exc:
        logger.exception("Could not store certificate %s of job %s", data.certificate_id, job_id)
        return RecipientFailure(STORAGE_ERROR, _storage_error_message(exc))
    return storage_key


def _storage_error_message(exc: Exception) -> str:
    # The path and OS message stay in the logs; the stored error names only the kind.
    reason = errno.errorcode.get(exc.errno) if isinstance(exc, OSError) and exc.errno else None
    return "The certificate file could not be saved" + (f" ({reason})" if reason else "")


def _finish_recipient(
    job_id: uuid.UUID,
    recipient_id: uuid.UUID,
    status: RecipientStatus,
    *,
    storage_key: str | None = None,
    error_details: list[dict[str, Any]] | None = None,
) -> None:
    """PROCESSING -> SUCCESS or FAILED in its own transaction."""
    values: dict[str, Any] = {"status": status, "completed_at": func.now()}
    if storage_key is not None:
        values["storage_key"] = storage_key
    if error_details is not None:
        values["error_details"] = error_details

    with SessionLocal.begin() as session:
        result = session.execute(
            update(JobRecipient)
            .where(JobRecipient.id == recipient_id, JobRecipient.status == RecipientStatus.PROCESSING)
            .values(**values)
        )
        if result.rowcount != 1:
            raise JobProcessingError(job_id, f"recipient {recipient_id} was no longer PROCESSING; not recorded")


def _finalize_job(job_id: uuid.UUID) -> tuple[JobStatus, JobProgress]:
    with SessionLocal.begin() as session:
        progress = get_job_progress(session, job_id)
        try:
            status = final_job_status(progress)
        except ValueError as exc:
            raise JobProcessingError(job_id, f"not finalized: {exc}") from exc

        result = session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.PROCESSING)
            .values(status=status, completed_at=func.now())
        )
        if result.rowcount != 1:
            raise JobProcessingError(job_id, "job was no longer PROCESSING; not finalized")
    return status, progress


def _read_progress(job_id: uuid.UUID) -> JobProgress:
    with SessionLocal.begin() as session:
        return get_job_progress(session, job_id)
