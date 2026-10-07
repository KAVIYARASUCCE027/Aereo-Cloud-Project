"""Per-recipient validation.

Each submitted entry is validated on its own with RecipientInput. A malformed
entry (null, a string, missing fields, wrong types) becomes an InvalidRecipient
and never affects the other entries. The entry's position in the submitted
list is kept as input_index.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from app.schemas import RecipientInput

ErrorDetail = dict[str, str | None]


@dataclass(frozen=True)
class ValidRecipient:
    input_index: int
    name: str
    email: str | None


@dataclass(frozen=True)
class InvalidRecipient:
    input_index: int
    errors: list[ErrorDetail]


@dataclass(frozen=True)
class RecipientValidationResult:
    valid: list[ValidRecipient]
    invalid: list[InvalidRecipient]


def validate_recipients(entries: Sequence[Any]) -> RecipientValidationResult:
    valid: list[ValidRecipient] = []
    invalid: list[InvalidRecipient] = []

    for input_index, entry in enumerate(entries):
        try:
            recipient = RecipientInput.model_validate(entry)
        except ValidationError as exc:
            invalid.append(InvalidRecipient(input_index=input_index, errors=error_details(exc)))
        else:
            valid.append(ValidRecipient(input_index=input_index, name=recipient.name, email=recipient.email))

    return RecipientValidationResult(valid=valid, invalid=invalid)


def error_details(exc: ValidationError) -> list[ErrorDetail]:
    """Convert a ValidationError into plain, JSON-serializable dicts.

    Keeps only field, code, and message. The exception object, the submitted
    value, and Pydantic's context objects are deliberately dropped.
    """
    return [
        {
            "field": ".".join(str(part) for part in error["loc"]) or None,
            "code": error["type"],
            "message": error["msg"],
        }
        for error in exc.errors(include_url=False, include_input=False, include_context=False)
    ]
