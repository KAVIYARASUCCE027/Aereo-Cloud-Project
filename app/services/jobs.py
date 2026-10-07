"""Job creation: validate recipients, persist atomically, then hand off for dispatch."""

import dataclasses
import logging
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import GenerationJob, JobRecipient, JobStatus, RecipientStatus
from app.schemas import JobCreateRequest
from app.services import dispatch
from app.services.validation import InvalidRecipient, validate_recipients

logger = logging.getLogger(__name__)

DISPATCH_FAILED = "dispatch_failed"
DISPATCH_FAILED_JOB_MESSAGE = "The job could not be queued for processing because the message broker was unavailable."
DISPATCH_FAILED_RECIPIENT_ERROR = {
    "field": None,
    "code": DISPATCH_FAILED,
    "message": "Not processed: the job could not be queued for processing.",
}


class AllRecipientsInvalidError(Exception):
    """No recipient passed validation, so no job is created."""

    def __init__(self, invalid: list[InvalidRecipient]) -> None:
        super().__init__(f"All {len(invalid)} recipients are invalid")
        self.invalid = invalid


class JobDispatchError(Exception):
    """The job was saved but could not be queued.

    job_status is FAILED when the failure was recorded, or None when even that
    database update failed (the job then remains QUEUED with no message).
    """

    def __init__(self, job_id: uuid.UUID, job_status: JobStatus | None) -> None:
        super().__init__(f"Job {job_id} could not be queued for processing")
        self.job_id = job_id
        self.job_status = job_status


@dataclass(frozen=True)
class DispatchFailureRecord:
    marked_failed: bool  # this call changed the job to FAILED
    status: JobStatus | None  # the job's status afterwards; None if it could not be read


@dataclass(frozen=True)
class CreatedJob:
    job_id: uuid.UUID
    status: JobStatus
    total_count: int
    valid_count: int
    invalid_count: int


def create_job(session: Session, request: JobCreateRequest) -> CreatedJob:
    """Persist the job and one row per submitted recipient in a single transaction.

    Valid recipients become PENDING rows; invalid ones become INVALID rows that
    keep only their input_index and error details. Raises
    AllRecipientsInvalidError before touching the database if nothing is valid.
    If the commit fails, the transaction is rolled back (no job, no recipients)
    and the error is re-raised.
    """
    validation = validate_recipients(request.recipients)
    if not validation.valid:
        raise AllRecipientsInvalidError(validation.invalid)

    rows = [
        JobRecipient(
            id=uuid.uuid4(),
            input_index=recipient.input_index,
            name=recipient.name,
            email=recipient.email,
            status=RecipientStatus.PENDING,
        )
        for recipient in validation.valid
    ] + [
        JobRecipient(
            id=uuid.uuid4(),
            input_index=recipient.input_index,
            status=RecipientStatus.INVALID,
            error_details=recipient.errors,
        )
        for recipient in validation.invalid
    ]
    rows.sort(key=lambda row: row.input_index)

    job = GenerationJob(
        id=uuid.uuid4(),
        event_name=request.event_name,
        issuer_name=request.issuer_name,
        issue_date=request.issue_date,
        status=JobStatus.QUEUED,
        total_count=len(request.recipients),
        recipients=rows,
    )
    session.add(job)
    try:
        session.commit()
    except Exception:
        session.rollback()
        raise

    return CreatedJob(
        job_id=job.id,
        status=job.status,
        total_count=job.total_count,
        valid_count=len(validation.valid),
        invalid_count=len(validation.invalid),
    )


def submit_job(session: Session, request: JobCreateRequest) -> CreatedJob:
    """Create the job, then publish it. Dispatch only ever sees committed jobs.

    If publishing fails, the job is marked FAILED (unless a worker already
    claimed it) and JobDispatchError is raised, so a submission is never
    reported as accepted when nothing will process it.
    """
    created = create_job(session, request)
    try:
        dispatch.enqueue_job(created.job_id)
    except dispatch.DispatchError as exc:
        record = record_dispatch_failure(session, created.job_id)
        if record.marked_failed or record.status is None:
            raise JobDispatchError(created.job_id, record.status) from exc
        # The publish reported an error, yet a worker has claimed the job, so the
        # message did reach the broker. The job is being processed; report that.
        logger.warning("Publishing job %s reported an error, but a worker already has it (%s)", created.job_id, record.status)
        return dataclasses.replace(created, status=record.status)
    return created


def record_dispatch_failure(session: Session, job_id: uuid.UUID) -> DispatchFailureRecord:
    """Mark a job that could not be queued as FAILED, together with its PENDING recipients.

    The update only applies while the job is still QUEUED, using the same
    row-locked conditional UPDATE as the worker's claim, so exactly one of the
    two wins: a job a worker has claimed is never overwritten, and a job marked
    FAILED here can no longer be claimed. INVALID recipients keep their errors.
    """
    try:
        marked = session.execute(
            update(GenerationJob)
            .where(GenerationJob.id == job_id, GenerationJob.status == JobStatus.QUEUED)
            .values(
                status=JobStatus.FAILED,
                error_code=DISPATCH_FAILED,
                error_message=DISPATCH_FAILED_JOB_MESSAGE,
                completed_at=func.now(),
            )
            .returning(GenerationJob.id)
        ).one_or_none()
        if marked is None:
            status = session.scalar(select(GenerationJob.status).where(GenerationJob.id == job_id))
            session.commit()
            return DispatchFailureRecord(marked_failed=False, status=status)

        # Never started, so started_at stays NULL; the outcome is still final.
        session.execute(
            update(JobRecipient)
            .where(JobRecipient.job_id == job_id, JobRecipient.status == RecipientStatus.PENDING)
            .values(
                status=RecipientStatus.FAILED,
                error_details=[DISPATCH_FAILED_RECIPIENT_ERROR],
                completed_at=func.now(),
            )
        )
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        logger.exception("Job %s could not be queued and could not be marked FAILED; it remains QUEUED", job_id)
        return DispatchFailureRecord(marked_failed=False, status=None)

    logger.error("Job %s could not be queued; marked FAILED (%s)", job_id, DISPATCH_FAILED)
    return DispatchFailureRecord(marked_failed=True, status=JobStatus.FAILED)
