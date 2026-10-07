"""Request and response models."""

import re
import uuid
from datetime import date, datetime
from typing import Any, Literal

from email_validator import EmailNotValidError, validate_email
from pydantic import BaseModel, ConfigDict, Field, SkipValidation, field_validator, model_validator
from pydantic_core import PydanticCustomError

from app.models import JobStatus

# Application limit for one submission (not a requirement of the assignment).
MAX_RECIPIENTS_PER_JOB = 1000

ISO_DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}")


class HealthResponse(BaseModel):
    status: Literal["ok", "unavailable"]
    database: Literal["ok", "unavailable"]


class RecipientInput(BaseModel):
    """One recipient entry, validated on its own (see app/services/validation.py)."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=150, examples=["Asha Sharma"])
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

    event_name: str = Field(min_length=1, max_length=200)
    issuer_name: str = Field(min_length=1, max_length=200)
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
