import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import JobStatus
from app.schemas import (
    AllRecipientsInvalidDetail,
    AllRecipientsInvalidResponse,
    DispatchFailedDetail,
    DispatchFailedResponse,
    FieldError,
    JobCreatedResponse,
    JobCreateRequest,
    RecipientErrors,
)
from app.services import jobs

JOBS_PREFIX = "/api/v1/jobs"

router = APIRouter(prefix=JOBS_PREFIX, tags=["jobs"])


def job_status_url(job_id: uuid.UUID) -> str:
    # The status endpoint itself is implemented in Phase 6.
    return f"{JOBS_PREFIX}/{job_id}"


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
def create_job(payload: JobCreateRequest, db: Annotated[Session, Depends(get_db)]) -> JobCreatedResponse:
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
