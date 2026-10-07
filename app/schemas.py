"""Request and response models."""

import re
import uuid
from datetime import date, datetime
from typing import Annotated, Any, Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    SkipValidation,
    StringConstraints,
    field_validator,
    model_validator,
)
from pydantic_core import PydanticCustomError

from app import text_policy
from app.models import JobStatus, RecipientStatus

# Application limit for one submission (not a requirement of the assignment).
MAX_RECIPIENTS_PER_JOB = 1000

ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


def _normalize_certificate_text(value: Any) -> Any:
    # Runs before the length check, so a decomposed accent counts as one character.
    return text_policy.normalize(value) if isinstance(value, str) else value


def _require_supported_characters(value: str) -> str:
    unsupported = text_policy.unsupported_characters(value)
    if unsupported:
        raise PydanticCustomError(
            "unsupported_characters",
            "Contains characters certificates cannot display: {characters}",
            {"characters": text_policy.describe_characters(unsupported)},
        )
    return value


# Text printed on the certificate, in validation order: NFC-normalize and trim,
# check 1..N characters, then check every character against the text policy.
# The renderer applies the same policy (app/text_policy.py).
RecipientNameText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=150),
    BeforeValidator(_normalize_certificate_text),
    AfterValidator(_require_supported_characters),
]
SharedCertificateText = Annotated[
    str,
    StringConstraints(min_length=1, max_length=200),
    BeforeValidator(_normalize_certificate_text),
    AfterValidator(_require_supported_characters),
]


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    database: Literal["ok", "unavailable"]


class RecipientInput(BaseModel):
    """One recipient entry, validated on its own (see app/services/validation.py)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: RecipientNameText = Field(examples=["Asha Sharma"])
    # Email is never printed, so the certificate text policy does not apply to it.
    email: str | None = Field(default=None, examples=["asha@example.com"])

    @model_validator(mode="before")
    @classmethod
    def entry_must_be_an_object(cls, data: Any) -> Any:
        # Same error code Pydantic uses, without its message naming this Python class.
        if not isinstance(data, dict):
            raise PydanticCustomError("model_type", "Recipient entry must be a JSON object")
        return data

    @field_validator("email")
    @classmethod
    def email_must_be_valid(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            # Syntax check only; no DNS lookup. Returns the normalized address
            # (domain lowercased, Unicode normalized).
            return validate_email(value, check_deliverability=False).normalized
        except EmailNotValidError as exc:
            raise PydanticCustomError("email_invalid", "Invalid email address: {reason}", {"reason": str(exc)}) from exc


class JobCreateRequest(BaseModel):
    model_config = ConfigDict(
        str_strip_whitespace=True,
        json_schema_extra={
            "examples": [
                {
                    "event_name": "Backend Development Workshop",
                    "issuer_name": "ABC Academy",
                    "issue_date": "2026-10-07",
                    "recipients": [
                        {"name": "Asha Sharma", "email": "asha@example.com"},
                        {"name": "Rohan Gupta", "email": "rohan@example.com"},
                        {"name": "", "email": "invalid-email"},
                    ],
                }
            ]
        },
    )

    event_name: SharedCertificateText
    issuer_name: SharedCertificateText
    issue_date: date
    # Only the list itself (type and length) is validated here. SkipValidation
    # leaves each entry exactly as submitted, so one malformed entry cannot
    # reject the whole request; entries are validated one by one with
    # RecipientInput by the validation service. It also keeps the RecipientInput
    # schema visible in the API docs. At runtime the items are raw JSON values.
    recipients: list[SkipValidation[RecipientInput]] = Field(min_length=1, max_length=MAX_RECIPIENTS_PER_JOB)

    @field_validator("issue_date", mode="before")
    @classmethod
    def issue_date_must_be_iso_format(cls, value: Any) -> Any:
        # Pydantic alone would also accept Unix timestamps and other formats.
        if isinstance(value, date) and not isinstance(value, datetime):
            return value
        if isinstance(value, str) and ISO_DATE_PATTERN.fullmatch(value):
            return value  # Pydantic then checks it is a real calendar date.
        raise PydanticCustomError("date_format", "Issue date must be an ISO date in YYYY-MM-DD format")


class JobCreatedResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    total_count: int
    valid_count: int
    invalid_count: int
    status_url: str


class FieldError(BaseModel):
    # None when the whole entry is wrong (for example null or not an object).
    field: str | None
    code: str
    message: str


class RecipientErrors(BaseModel):
    input_index: int
    errors: list[FieldError]


class AllRecipientsInvalidDetail(BaseModel):
    code: Literal["all_recipients_invalid"] = "all_recipients_invalid"
    message: str
    recipient_errors: list[RecipientErrors]


class AllRecipientsInvalidResponse(BaseModel):
    detail: AllRecipientsInvalidDetail


class DispatchFailedDetail(BaseModel):
    code: Literal["dispatch_failed"] = "dispatch_failed"
    message: str
    job_id: uuid.UUID
    # FAILED once the failure was recorded; null if even that could not be saved.
    job_status: JobStatus | None
    status_url: str


class DispatchFailedResponse(BaseModel):
    detail: DispatchFailedDetail


class JobStatusResponse(BaseModel):
    job_id: uuid.UUID
    status: JobStatus
    event_name: str
    issuer_name: str
    issue_date: date
    total_count: int
    pending_count: int
    processing_count: int
    success_count: int
    invalid_count: int
    failed_count: int
    # success + invalid + failed: entries with a final outcome.
    processed_count: int
    # processed / total * 100, rounded to 2 decimals. 100 means every entry has
    # a final outcome, not that every certificate succeeded.
    progress_percent: float
    # Set only when the job as a whole failed (e.g. dispatch_failed).
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    recipients_url: str


class RecipientResult(BaseModel):
    # Also the certificate ID of a SUCCESS entry.
    recipient_id: uuid.UUID
    input_index: int
    status: RecipientStatus
    # Validated values; null for INVALID entries.
    name: str | None
    email: str | None
    # For INVALID and FAILED entries.
    errors: list[FieldError] | None
    # Only for SUCCESS entries.
    download_url: str | None
    started_at: datetime | None
    completed_at: datetime | None


class RecipientPageResponse(BaseModel):
    job_id: uuid.UUID
    total: int
    limit: int
    offset: int
    items: list[RecipientResult]


class ErrorDetail(BaseModel):
    code: str
    message: str


class ErrorResponse(BaseModel):
    detail: ErrorDetail


class CertificateNotAvailableDetail(BaseModel):
    code: Literal["certificate_not_ready", "certificate_not_available"]
    message: str
    status: RecipientStatus


class CertificateNotAvailableResponse(BaseModel):
    detail: CertificateNotAvailableDetail
