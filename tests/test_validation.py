"""Per-recipient validation, tested without the API or database."""

import json

import pytest

from app.services.validation import InvalidRecipient, ValidRecipient, validate_recipients


def only_invalid(entry: object) -> InvalidRecipient:
    result = validate_recipients([entry])
    assert result.valid == []
    assert len(result.invalid) == 1
    return result.invalid[0]


def test_valid_entry_is_trimmed_and_email_normalized() -> None:
    result = validate_recipients([{"name": "  Asha Sharma  ", "email": " Asha@Example.COM "}])

    assert result.invalid == []
    assert result.valid == [ValidRecipient(input_index=0, name="Asha Sharma", email="Asha@example.com")]


@pytest.mark.parametrize("entry", [{"name": "Rohan"}, {"name": "Rohan", "email": None}])
def test_email_is_optional(entry: dict[str, object]) -> None:
    result = validate_recipients([entry])

    assert result.valid == [ValidRecipient(input_index=0, name="Rohan", email=None)]


def test_accented_and_non_latin_names_are_accepted() -> None:
    result = validate_recipients([{"name": "José Müller"}, {"name": "आशा शर्मा"}])

    assert [recipient.name for recipient in result.valid] == ["José Müller", "आशा शर्मा"]


def test_name_length_limit_is_150_characters() -> None:
    result = validate_recipients([{"name": "a" * 150}, {"name": "a" * 151}])

    assert [recipient.input_index for recipient in result.valid] == [0]
    assert result.invalid[0].errors[0]["code"] == "string_too_long"


@pytest.mark.parametrize(
    ("entry", "field", "code"),
    [
        ({"name": "   "}, "name", "string_too_short"),
        ({"name": ""}, "name", "string_too_short"),
        ({}, "name", "missing"),
        ({"email": "a@example.com"}, "name", "missing"),
        ({"name": 123}, "name", "string_type"),
        ({"name": None}, "name", "string_type"),
        ({"name": "Asha", "email": "invalid-email"}, "email", "email_invalid"),
        ({"name": "Asha", "email": ""}, "email", "email_invalid"),
        ({"name": "Asha", "email": "   "}, "email", "email_invalid"),
        ({"name": "Asha", "email": 42}, "email", "string_type"),
    ],
)
def test_invalid_field_is_reported_with_field_and_code(entry: object, field: str, code: str) -> None:
    invalid = only_invalid(entry)

    assert {"field": field, "code": code} in [{"field": e["field"], "code": e["code"]} for e in invalid.errors]


@pytest.mark.parametrize("entry", [None, "Asha Sharma", 42, ["Asha"], True])
def test_non_object_entry_is_invalid_as_a_whole(entry: object) -> None:
    invalid = only_invalid(entry)

    assert invalid.errors == [{"field": None, "code": "model_type", "message": "Recipient entry must be a JSON object"}]


def test_every_problem_in_one_entry_is_reported() -> None:
    invalid = only_invalid({"name": "", "email": "invalid-email"})

    assert sorted(error["field"] for error in invalid.errors) == ["email", "name"]


def test_errors_are_plain_json_with_field_code_and_message_only() -> None:
    invalid = only_invalid({"name": " ", "email": "not-an-email"})

    json.dumps(invalid.errors)  # raises if anything is not JSON-serializable
    for error in invalid.errors:
        assert set(error) == {"field", "code", "message"}
        assert all(isinstance(value, str) for value in error.values())


def test_mixed_entries_keep_their_original_indexes() -> None:
    entries = [None, {"name": "Asha"}, {"name": ""}, {"name": "Rohan", "email": "rohan@example.com"}, "text"]

    result = validate_recipients(entries)

    assert [recipient.input_index for recipient in result.valid] == [1, 3]
    assert [recipient.input_index for recipient in result.invalid] == [0, 2, 4]


def test_duplicate_names_and_emails_are_each_valid() -> None:
    entry = {"name": "Asha Sharma", "email": "asha@example.com"}

    result = validate_recipients([entry, dict(entry)])

    assert [recipient.input_index for recipient in result.valid] == [0, 1]
