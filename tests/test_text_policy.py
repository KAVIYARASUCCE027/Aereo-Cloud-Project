"""The shared certificate text policy keeps validation and rendering in agreement."""

from dataclasses import replace
from datetime import date
import uuid

import pytest

from app import text_policy
from app.fonts import TEXT_FONTS, glyph_table
from app.schemas import RecipientInput
from app.services.pdf import CertificateData, UnsupportedCharactersError, render_certificate
from app.services.validation import validate_recipients

SAMPLE = CertificateData(uuid.uuid4(), "Asha Sharma", "Backend Development Workshop", "ABC Academy", date(2026, 10, 7))


def test_normalize_composes_accents_and_trims() -> None:
    assert text_policy.normalize("  Jose\u0301\t") == "José"


def test_every_supported_character_exists_in_every_text_font() -> None:
    tables = [glyph_table(font) for font in TEXT_FONTS]

    supported = text_policy.supported_code_points()

    assert len(supported) > 1000
    assert all(code_point in table for table in tables for code_point in supported)


def test_everything_validation_accepts_can_be_rendered() -> None:
    # Every supported character, split into names no longer than the name limit.
    characters = "".join(chr(code_point) for code_point in sorted(text_policy.supported_code_points()))
    names = [text_policy.normalize(characters[i : i + 150]) for i in range(0, len(characters), 150)]
    names = [name for name in names if name]

    result = validate_recipients([{"name": name} for name in names])
    assert result.invalid == []

    for recipient in result.valid:
        render_certificate(replace(SAMPLE, recipient_name=recipient.name))  # must not raise


@pytest.mark.parametrize("text", ["आशा", "王", "Launch 🚀", "Tab\there", "Ѡ"])
def test_validation_and_renderer_reject_the_same_text(text: str) -> None:
    assert text_policy.unsupported_characters(text_policy.normalize(text))
    assert validate_recipients([{"name": text}]).invalid
    with pytest.raises(UnsupportedCharactersError):
        render_certificate(replace(SAMPLE, recipient_name=text))


def test_description_is_limited_and_names_control_characters() -> None:
    description = text_policy.describe_characters(["\n", *"अआइईउऊ"])

    assert description.startswith("U+000A CONTROL CHARACTER, U+0905 DEVANAGARI LETTER A")
    assert description.endswith("and 2 more")


def test_recipient_name_limit_is_still_150_characters() -> None:
    assert RecipientInput.model_json_schema()["properties"]["name"]["maxLength"] == 150
