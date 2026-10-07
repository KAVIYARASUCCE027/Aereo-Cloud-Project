"""Certificate rendering: the PDF opens, contains the expected text, and long or
unusual text is either laid out without overflow or rejected explicitly."""

import io
import subprocess
import sys
import uuid
from dataclasses import replace
from datetime import date

import pytest
from pypdf import PdfReader
from pypdf.generic import ContentStream
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFontFile

from app.fonts import FONT_DIR, FONT_FILES
from app.schemas import JobCreateRequest, RecipientInput
from app.services.pdf import (
    FIELD_SPECS,
    FONT_ASCENT,
    FONT_DESCENT,
    INNER_BORDER_INSET,
    LEADING,
    PAGE_HEIGHT,
    PAGE_WIDTH,
    Box,
    CertificateData,
    FieldSpec,
    TextBlock,
    TextDoesNotFitError,
    UnsupportedCharactersError,
    fit_text,
    format_issue_date,
    layout_certificate,
    render_certificate,
    wrap_text,
)
from app.text_policy import supported_code_points
from tests.database import PROJECT_ROOT

CERTIFICATE_ID = uuid.UUID("7e524b50-7651-49c0-a997-aedcaa1c434d")
SAMPLE = CertificateData(
    certificate_id=CERTIFICATE_ID,
    recipient_name="Asha Sharma",
    event_name="Backend Development Workshop",
    issuer_name="ABC Academy",
    issue_date=date(2026, 10, 7),
)


def page_text(pdf: bytes) -> str:
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    return reader.pages[0].extract_text()


def squash(text: str) -> str:
    """Remove all whitespace, so line breaks added by wrapping do not matter."""
    return "".join(text.split())


def max_length(model: type, field: str) -> int:
    return next(item.max_length for item in model.model_fields[field].metadata if hasattr(item, "max_length"))


def widest_supported_char(font_name: str) -> str:
    candidates = [chr(code_point) for code_point in supported_code_points()]
    return max(candidates, key=lambda char: pdfmetrics.stringWidth(char, font_name, 10))


def assert_block_fits(block: TextBlock) -> None:
    box = block.spec.box
    for line in block.lines:
        assert pdfmetrics.stringWidth(line, block.spec.font_name, block.font_size) <= box.width
    assert len(block.lines) * block.line_height <= box.height


# --- the PDF itself ------------------------------------------------------------


def test_pdf_opens_as_a_single_landscape_a4_page() -> None:
    pdf = render_certificate(SAMPLE)

    assert pdf.startswith(b"%PDF-")
    reader = PdfReader(io.BytesIO(pdf))
    assert len(reader.pages) == 1
    media_box = reader.pages[0].mediabox
    assert (round(float(media_box.width)), round(float(media_box.height))) == (842, 595)
    assert reader.metadata.title == "Certificate of Participation - Asha Sharma"


def test_pdf_contains_every_certificate_detail() -> None:
    text = squash(page_text(render_certificate(SAMPLE)))

    for expected in [
        "CERTIFICATE",
        "OF PARTICIPATION",
        "Asha Sharma",
        "Backend Development Workshop",
        "ABC Academy",
        "7 October 2026",
        f"Certificate ID: {CERTIFICATE_ID}",
    ]:
        assert squash(expected) in text


@pytest.mark.parametrize(
    "name",
    [
        "José Müller",
        "Zoë Ångström-Łukasiewicz",
        "Søren Kierkegaard Ærø",
        "Nguyễn Thị Minh Khai",
        "Αλέξανδρος Παπαδόπουλος",
        "Дмитрий Шостакович",
        "Seán O’Brien – “Ollie”",
    ],
)
def test_supported_accented_and_non_latin_names_render_correctly(name: str) -> None:
    text = page_text(render_certificate(replace(SAMPLE, recipient_name=name)))

    assert squash(name) in squash(text)


def test_decomposed_accents_are_normalized_before_rendering() -> None:
    decomposed = "Jose\u0301 Mu\u0308ller"  # e + combining acute, u + combining diaeresis

    text = page_text(render_certificate(replace(SAMPLE, recipient_name=decomposed)))

    assert "JoséMüller" in squash(text)


def test_issue_date_is_formatted_without_depending_on_locale() -> None:
    assert format_issue_date(date(2026, 1, 5)) == "5 January 2026"
    assert format_issue_date(date(2026, 12, 31)) == "31 December 2026"


def test_each_certificate_carries_its_own_id() -> None:
    other_id = uuid.uuid4()

    text = page_text(render_certificate(replace(SAMPLE, certificate_id=other_id)))

    assert str(other_id) in text
    assert str(CERTIFICATE_ID) not in text


def test_only_the_two_title_lines_use_character_spacing() -> None:
    # Character spacing (PDF operator Tc) persists in the graphics state. If it
    # leaked, viewers would draw later text wider than the layout measured it.
    reader = PdfReader(io.BytesIO(render_certificate(SAMPLE)))
    operations = ContentStream(reader.pages[0].get_contents(), reader).operations

    char_space, saved, spaced_strings = 0.0, [], 0
    for operands, operator in operations:
        if operator == b"q":
            saved.append(char_space)
        elif operator == b"Q":
            char_space = saved.pop()
        elif operator == b"Tc":
            char_space = float(operands[0])
        elif operator in (b"Tj", b"TJ", b"'", b'"') and char_space != 0:
            spaced_strings += 1

    assert spaced_strings == 2  # "CERTIFICATE" and "OF PARTICIPATION"
    assert char_space == 0


def test_every_font_in_the_pdf_is_embedded() -> None:
    reader = PdfReader(io.BytesIO(render_certificate(SAMPLE)))
    fonts = reader.pages[0]["/Resources"]["/Font"]

    assert len(fonts) == 3
    for font in fonts.values():
        font = font.get_object()
        assert "/FontFile2" in font["/FontDescriptor"], font["/BaseFont"]


# --- long text ---------------------------------------------------------------------


def test_long_name_wraps_and_keeps_every_character() -> None:
    name = (
        "Maximiliana Alexandrina Wolfeschlegelsteinhausenbergerdorff "
        "de la Santísima Trinidad Montgomery-Fitzgerald Okonkwo-Vanderbilt Adebayo-Smithson-Lee III"
    )
    assert len(name) == max_length(RecipientInput, "name")
    data = replace(SAMPLE, recipient_name=name)

    name_block = layout_certificate(data)[0]
    text = page_text(render_certificate(data))

    assert len(name_block.lines) > 1
    assert_block_fits(name_block)
    assert squash(name) in squash(text)


@pytest.mark.parametrize(
    ("field", "model", "length_field"),
    [
        ("recipient_name", RecipientInput, "name"),
        ("event_name", JobCreateRequest, "event_name"),
        ("issuer_name", JobCreateRequest, "issuer_name"),
    ],
)
def test_longest_accepted_text_of_the_widest_glyph_still_fits(field: str, model: type, length_field: str) -> None:
    # Worst case for a validated value: the maximum length, as a single unbreakable
    # word made of the widest character the font supports.
    spec = FIELD_SPECS[field]
    text = widest_supported_char(spec.font_name) * max_length(model, length_field)

    block = next(b for b in layout_certificate(replace(SAMPLE, **{field: text})) if b.field == field)

    assert_block_fits(block)
    assert "".join(block.lines) == text


@pytest.mark.parametrize(
    "data",
    [
        SAMPLE,
        replace(SAMPLE, recipient_name="W" * 150, event_name="M" * 200, issuer_name="Ǆ" * 200),
        replace(SAMPLE, recipient_name="a " * 75, event_name="Long event " * 18, issuer_name="Issuer " * 28),
    ],
    ids=["ordinary", "unbroken", "many-words"],
)
def test_every_block_stays_inside_its_box_and_the_border(data: CertificateData) -> None:
    inner = Box(
        INNER_BORDER_INSET,
        INNER_BORDER_INSET,
        PAGE_WIDTH - 2 * INNER_BORDER_INSET,
        PAGE_HEIGHT - 2 * INNER_BORDER_INSET,
    )

    for block in layout_certificate(data):
        assert_block_fits(block)
        box = block.spec.box
        assert inner.x <= box.x and box.x + box.width <= inner.x + inner.width
        assert inner.y <= box.y and box.top <= inner.top


def test_short_text_uses_the_largest_size() -> None:
    block = layout_certificate(SAMPLE)[0]

    assert block.lines == ("Asha Sharma",)
    assert block.font_size == FIELD_SPECS["recipient_name"].max_size


def test_wrapping_keeps_every_character_in_order() -> None:
    text = "Short words then Averyveryveryverylongwordthatcannotfitononeline and more"

    lines = wrap_text(text, "DejaVuSerif-Bold", 20, 150)

    assert len(lines) > 3
    assert squash("".join(lines)) == squash(text)
    assert all(pdfmetrics.stringWidth(line, "DejaVuSerif-Bold", 20) <= 150 for line in lines)


def test_text_that_cannot_fit_is_rejected_instead_of_clipped() -> None:
    tiny = FieldSpec("DejaVuSerif", 10, 8, Box(0, 0, 50, 10), "middle", FIELD_SPECS["issuer_name"].color)

    with pytest.raises(TextDoesNotFitError) as error:
        fit_text("issuer_name", "This will never fit in such a small area", tiny)

    assert error.value.code == "text_does_not_fit"
    assert error.value.field == "issuer_name"


def test_line_height_leaves_room_for_the_fonts_real_ascent_and_descent() -> None:
    assert FONT_ASCENT + FONT_DESCENT < LEADING
    for filename in FONT_FILES.values():
        font = TTFontFile(str(FONT_DIR / filename))
        font.seek_table("hhea")
        font.skip(4)  # table version
        ascender, descender = font.read_short(), font.read_short()
        assert ascender / font.unitsPerEm <= FONT_ASCENT
        assert -descender / font.unitsPerEm <= FONT_DESCENT


# --- unsupported characters ---------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["आशा शर्मा", "王小明", "محمد", "Asha 🙂", "Asha\nSharma", "Zero\u200bWidth", "Asha\tSharma"],
    ids=["devanagari", "cjk", "arabic", "emoji", "newline", "zero-width-space", "tab"],
)
def test_unsupported_characters_raise_a_specific_error(name: str) -> None:
    with pytest.raises(UnsupportedCharactersError) as error:
        render_certificate(replace(SAMPLE, recipient_name=name))

    assert error.value.code == "unsupported_characters"
    assert error.value.field == "recipient_name"
    assert "U+" in str(error.value)


def test_unsupported_characters_are_reported_for_shared_fields_too() -> None:
    with pytest.raises(UnsupportedCharactersError) as error:
        render_certificate(replace(SAMPLE, event_name="Launch Party 🚀"))

    assert error.value.field == "event_name"
    assert "U+1F680" in str(error.value)


# --- independence -------------------------------------------------------------------


def test_renderer_and_storage_do_not_import_web_queue_or_database_code() -> None:
    script = (
        "import sys, app.services.pdf, app.services.storage; "
        "loaded = sorted({'fastapi', 'starlette', 'celery', 'sqlalchemy', 'app.db', 'app.config'} & set(sys.modules)); "
        "print(loaded); sys.exit(1 if loaded else 0)"
    )

    result = subprocess.run([sys.executable, "-c", script], cwd=PROJECT_ROOT, capture_output=True, text=True)

    assert result.returncode == 0, result.stdout + result.stderr
