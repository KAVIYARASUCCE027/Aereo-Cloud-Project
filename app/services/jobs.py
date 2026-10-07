"""Job creation: validate recipients, persist atomically, then hand off for dispatch."""

import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import GenerationJob, JobRecipient, JobStatus, RecipientStatus
from app.schemas import JobCreateRequest
from app.services import dispatch
from app.services.validation import InvalidRecipient, validate_recipients


class AllRecipientsInvalidError(Exception):
    """No recipient passed validation, so no job is created."""

    def __init__(self, invalid: list[InvalidRecipient]) -> None:
        super().__init__(f"All {len(invalid)} recipients are invalid")
        self.invalid = invalid


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
    """Create the job, then hand it to dispatch. Dispatch only ever sees committed jobs."""
    created = create_job(session, request)
    dispatch.enqueue_job(created.job_id)
    return created
