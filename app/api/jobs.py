import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.urls import JOBS_PREFIX, certificate_download_url, job_recipients_url, job_status_url
from app.db import get_db
from app.models import JobStatus, RecipientStatus
from app.schemas import (
    AllRecipientsInvalidDetail,
    AllRecipientsInvalidResponse,
    DispatchFailedDetail,
    DispatchFailedResponse,
    ErrorResponse,
    FieldError,
    JobCreatedResponse,
    JobCreateRequest,
    JobStatusResponse,
    RecipientErrors,
    RecipientPageResponse,
    RecipientResult,
)
from app.services import jobs
from app.services.progress import JobNotFoundError, load_job_summary
from app.services.results import MAX_PAGE_SIZE, list_recipients

router = APIRouter(prefix=JOBS_PREFIX, tags=["jobs"])

DbSession = Annotated[Session, Depends(get_db)]
JOB_NOT_FOUND = {status.HTTP_404_NOT_FOUND: {"model": ErrorResponse, "description": "No job with this ID (`job_not_found`)."}}


def _job_not_found(job_id: uuid.UUID) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={"code": "job_not_found", "message": f"Job {job_id} does not exist."},
    )


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=JobCreatedResponse,
    responses={
        status.HTTP_422_UNPROCESSABLE_CONTENT: {
            "model": AllRecipientsInvalidResponse,
            "description": (
                "Every recipient is invalid (`all_recipients_invalid`, shown here), or the shared "
                "fields / recipient list are invalid (FastAPI's standard `detail` list of errors). "
                "No job is created in either case."
            ),
        },
        status.HTTP_503_SERVICE_UNAVAILABLE: {
            "model": DispatchFailedResponse,
            "description": (
                "The job was saved but could not be queued (message broker unavailable). "
                "It is marked FAILED and will not be processed; submit it again later."
            ),
        },
    },
)
def create_job(payload: JobCreateRequest, db: DbSession) -> JobCreatedResponse:
    """Accept a batch. Invalid recipients are recorded as INVALID instead of rejecting the batch."""
    try:
        created = jobs.submit_job(db, payload)
    except jobs.AllRecipientsInvalidError as exc:
        detail = AllRecipientsInvalidDetail(
            message="Every recipient is invalid; no job was created.",
            recipient_errors=[
                RecipientErrors(
                    input_index=invalid.input_index,
                    errors=[FieldError.model_validate(error) for error in invalid.errors],
                )
                for invalid in exc.invalid
            ],
        )
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail.model_dump()) from exc
    except jobs.JobDispatchError as exc:
        if exc.job_status is JobStatus.FAILED:
            message = "The job was saved but could not be queued for processing, so it was marked FAILED."
        else:
            message = "The job was saved but could not be queued, and its status could not be updated."
        detail = DispatchFailedDetail(
            message=message,
            job_id=exc.job_id,
            job_status=exc.job_status,
            status_url=job_status_url(exc.job_id),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail.model_dump(mode="json")
        ) from exc

    return JobCreatedResponse(
        job_id=created.job_id,
        status=created.status,
        total_count=created.total_count,
        valid_count=created.valid_count,
        invalid_count=created.invalid_count,
        status_url=job_status_url(created.job_id),
    )


@router.get("/{job_id}", response_model=JobStatusResponse, responses=JOB_NOT_FOUND)
def get_job(job_id: uuid.UUID, db: DbSession) -> JobStatusResponse:
    """Job status and progress, counted from recipient rows in one consistent read."""
    try:
        job, progress = load_job_summary(db, job_id)
    except JobNotFoundError as exc:
        raise _job_not_found(job_id) from exc

    return JobStatusResponse(
        job_id=job.id,
        status=job.status,
        event_name=job.event_name,
        issuer_name=job.issuer_name,
        issue_date=job.issue_date,
        total_count=progress.total_count,
        pending_count=progress.pending_count,
        processing_count=progress.processing_count,
        success_count=progress.success_count,
        invalid_count=progress.invalid_count,
        failed_count=progress.failed_count,
        processed_count=progress.processed_count,
        progress_percent=round(progress.progress_percent, 2),
        error_code=job.error_code,
        error_message=job.error_message,
        created_at=job.created_at,
        started_at=job.started_at,
        completed_at=job.completed_at,
        recipients_url=job_recipients_url(job.id),
    )


@router.get("/{job_id}/recipients", response_model=RecipientPageResponse, responses=JOB_NOT_FOUND)
def get_job_recipients(
    job_id: uuid.UUID,
    db: DbSession,
    limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RecipientPageResponse:
    """Per-recipient outcomes in submission order (input_index), one page at a time."""
    try:
        page = list_recipients(db, job_id, limit=limit, offset=offset)
    except JobNotFoundError as exc:
        raise _job_not_found(job_id) from exc

    return RecipientPageResponse(
        job_id=job_id,
        total=page.total,
        limit=limit,
        offset=offset,
        items=[
            RecipientResult(
                recipient_id=row.id,
                input_index=row.input_index,
                status=row.status,
                name=row.name,
                email=row.email,
                achievement=row.achievement,
                errors=row.error_details,
                download_url=certificate_download_url(row.id) if row.status is RecipientStatus.SUCCESS else None,
                started_at=row.started_at,
                completed_at=row.completed_at,
            )
            for row in page.items
        ],
    )
