"""Certificate PDF rendering.

render_certificate() turns CertificateData into PDF bytes. It does not use
FastAPI, Celery, the database, or the filesystem (apart from reading the
bundled fonts), so it can be tested and reused on its own.

Layout: one fixed A4 landscape template. Variable text (name, event, issuer,
date, certificate ID) is placed in fixed areas. Each text is wrapped and its
font size reduced step by step until it fits its area; a word too long for a
line is broken between characters. If text cannot fit even at the minimum
size, TextDoesNotFitError is raised instead of drawing clipped text.

Characters: text is NFC-normalized, then checked against SUPPORTED_RANGES and
the font's glyph table. Anything else (for example Devanagari, CJK, Arabic,
emoji, control characters) raises UnsupportedCharactersError rather than
producing a certificate with missing glyphs.
"""

import io
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Literal

from reportlab.lib.colors import Color, HexColor
from reportlab.lib.pagesizes import A4, landscape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen.canvas import Canvas

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
SERIF = "DejaVuSerif"
SERIF_BOLD = "DejaVuSerif-Bold"
SANS = "DejaVuSans"
FONT_FILES = {SERIF: "DejaVuSerif.ttf", SERIF_BOLD: "DejaVuSerif-Bold.ttf", SANS: "DejaVuSans.ttf"}

# Vertical extent of DejaVu glyphs, in em, from the fonts' hhea table.
# ReportLab's own ascent (0.76 em) is lower than accented capitals reach.
FONT_ASCENT = 0.94
FONT_DESCENT = 0.24
# Line height as a multiple of font size; must exceed FONT_ASCENT + FONT_DESCENT.
LEADING = 1.25
FONT_SIZE_STEP = 0.5

# Left-to-right scripts that need no glyph shaping, so ReportLab draws them correctly.
SUPPORTED_RANGES: tuple[tuple[int, int, str], ...] = (
    (0x0020, 0x007E, "Basic Latin (printable)"),
    (0x00A0, 0x00FF, "Latin-1 Supplement"),
    (0x0100, 0x017F, "Latin Extended-A"),
    (0x0180, 0x024F, "Latin Extended-B"),
    (0x0370, 0x03FF, "Greek and Coptic"),
    (0x0400, 0x04FF, "Cyrillic"),
    (0x1E00, 0x1EFF, "Latin Extended Additional"),
    (0x2010, 0x2027, "Dashes, quotation marks, bullet, ellipsis"),
)

PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)  # 841.9 x 595.3 points
BORDER_INSET = 18.0
INNER_BORDER_INSET = 26.0
CONTENT_LEFT = 80.0
CONTENT_WIDTH = PAGE_WIDTH - 2 * CONTENT_LEFT

NAVY = HexColor("#1F2A44")
GOLD = HexColor("#B08D57")
INK = HexColor("#2B2B2B")
MUTED = HexColor("#6B6B6B")

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)  # fmt: skip


@dataclass(frozen=True)
class CertificateData:
    certificate_id: uuid.UUID
    recipient_name: str
    event_name: str
    issuer_name: str
    issue_date: date


@dataclass(frozen=True)
class Box:
    """A rectangle in PDF coordinates: (x, y) is the bottom-left corner."""

    x: float
    y: float
    width: float
    height: float

    @property
    def top(self) -> float:
        return self.y + self.height


@dataclass(frozen=True)
class FieldSpec:
    font_name: str
    max_size: float
    min_size: float
    box: Box
    vertical_align: Literal["middle", "bottom"]
    color: Color


@dataclass(frozen=True)
class TextBlock:
    field: str
    lines: tuple[str, ...]
    font_size: float
    spec: FieldSpec

    @property
    def line_height(self) -> float:
        return self.font_size * LEADING


class CertificateRenderError(Exception):
    """Base class for rendering failures. `code` is a stable, machine-readable reason."""

    code = "render_failed"

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


class UnsupportedCharactersError(CertificateRenderError):
    code = "unsupported_characters"


class TextDoesNotFitError(CertificateRenderError):
    code = "text_does_not_fit"


FIELD_SPECS: dict[str, FieldSpec] = {
    "recipient_name": FieldSpec(SERIF_BOLD, 34, 12, Box(CONTENT_LEFT, 295, CONTENT_WIDTH, 104), "middle", NAVY),
    "event_name": FieldSpec(SERIF_BOLD, 22, 8, Box(CONTENT_LEFT, 184, CONTENT_WIDTH, 78), "middle", INK),
    "issuer_name": FieldSpec(SERIF, 16, 7, Box(CONTENT_LEFT, 106, 420, 62), "bottom", INK),
    "issue_date": FieldSpec(SERIF, 16, 10, Box(PAGE_WIDTH - CONTENT_LEFT - 200, 106, 200, 62), "bottom", INK),
    "certificate_id": FieldSpec(SANS, 8.5, 8.5, Box(CONTENT_LEFT, 40, CONTENT_WIDTH, 16), "middle", MUTED),
}


def render_certificate(data: CertificateData) -> bytes:
    """Render one certificate and return the PDF bytes."""
    blocks = layout_certificate(data)

    buffer = io.BytesIO()
    # initialFontName: otherwise ReportLab references a non-embedded Helvetica.
    canvas = Canvas(buffer, pagesize=(PAGE_WIDTH, PAGE_HEIGHT), pageCompression=1, initialFontName=SANS)
    canvas.setTitle(f"Certificate of Participation - {data.recipient_name}")
    canvas.setAuthor(data.issuer_name)
    canvas.setSubject(data.event_name)
    canvas.setCreator("Bulk Certificate Generator")

    _draw_template(canvas)
    for block in blocks:
        _draw_block(canvas, block)

    canvas.showPage()
    canvas.save()
    return buffer.getvalue()


def layout_certificate(data: CertificateData) -> list[TextBlock]:
    """Fit every variable text into its area. Raises CertificateRenderError subclasses."""
    _register_fonts()
    texts = {
        "recipient_name": data.recipient_name,
        "event_name": data.event_name,
        "issuer_name": data.issuer_name,
        "issue_date": format_issue_date(data.issue_date),
        "certificate_id": f"Certificate ID: {data.certificate_id}",
    }
    blocks = []
    for field, text in texts.items():
        spec = FIELD_SPECS[field]
        blocks.append(fit_text(field, prepare_text(field, text, spec.font_name), spec))
    return blocks


def format_issue_date(value: date) -> str:
    # Built by hand so the output does not depend on the system locale.
    return f"{value.day} {MONTHS[value.month - 1]} {value.year}"


def prepare_text(field: str, text: str, font_name: str) -> str:
    """NFC-normalize (so 'e' + combining accent becomes 'é') and check every character."""
    normalized = unicodedata.normalize("NFC", text).strip()
    if not normalized:
        raise CertificateRenderError(field, f"{field} is empty")

    glyphs = _glyph_table(font_name)
    unsupported = sorted({char for char in normalized if not _in_supported_ranges(char) or ord(char) not in glyphs})
    if unsupported:
        code_points = ", ".join(f"U+{ord(char):04X}" for char in unsupported)
        raise UnsupportedCharactersError(
            field, f"{field} contains characters the certificate font cannot render: {code_points}"
        )
    return normalized


def fit_text(field: str, text: str, spec: FieldSpec) -> TextBlock:
    """Use the largest font size at which the wrapped text fits inside spec.box."""
    steps = round((spec.max_size - spec.min_size) / FONT_SIZE_STEP)
    for step in range(steps + 1):
        size = spec.max_size - step * FONT_SIZE_STEP
        lines = wrap_text(text, spec.font_name, size, spec.box.width)
        if len(lines) * size * LEADING <= spec.box.height:
            return TextBlock(field=field, lines=tuple(lines), font_size=size, spec=spec)
    raise TextDoesNotFitError(field, f"{field} does not fit its area even at {spec.min_size}pt")


def wrap_text(text: str, font_name: str, font_size: float, max_width: float) -> list[str]:
    """Greedy word wrap. Words wider than max_width are broken between characters."""

    def width(value: str) -> float:
        return pdfmetrics.stringWidth(value, font_name, font_size)

    lines: list[str] = []
    current = ""
    for word in text.split():
        for piece in _break_word(word, width, max_width):
            candidate = f"{current} {piece}" if current else piece
            if width(candidate) <= max_width:
                current = candidate
            else:
                lines.append(current)
                current = piece
    if current:
        lines.append(current)
    return lines


def _break_word(word: str, width: Callable[[str], float], max_width: float) -> list[str]:
    if width(word) <= max_width:
        return [word]
    pieces: list[str] = []
    current = ""
    for char in word:
        if current and width(current + char) > max_width:
            pieces.append(current)
            current = char
        else:
            current += char
    pieces.append(current)
    return pieces


def _in_supported_ranges(char: str) -> bool:
    code_point = ord(char)
    return any(start <= code_point <= end for start, end, _ in SUPPORTED_RANGES)


@cache  # registers once per process
def _register_fonts() -> None:
    for name, filename in FONT_FILES.items():
        pdfmetrics.registerFont(TTFont(name, FONT_DIR / filename))


def _glyph_table(font_name: str) -> dict[int, int]:
    _register_fonts()
    return pdfmetrics.getFont(font_name).face.charToGlyph


def _draw_block(canvas: Canvas, block: TextBlock) -> None:
    spec = block.spec
    text_height = len(block.lines) * block.line_height
    if spec.vertical_align == "middle":
        text_top = spec.box.top - (spec.box.height - text_height) / 2
    else:
        text_top = spec.box.y + text_height
    # Centre the glyphs' full extent (ascent + descent) inside each line.
    padding = (block.line_height - (FONT_ASCENT + FONT_DESCENT) * block.font_size) / 2
    center_x = spec.box.x + spec.box.width / 2

    canvas.setFillColor(spec.color)
    canvas.setFont(spec.font_name, block.font_size)
    for index, line in enumerate(block.lines):
        baseline = text_top - index * block.line_height - padding - FONT_ASCENT * block.font_size
        canvas.drawCentredString(center_x, baseline, line)


def _draw_template(canvas: Canvas) -> None:
    """The fixed parts of the certificate."""
    canvas.setStrokeColor(NAVY)
    canvas.setLineWidth(2.5)
    canvas.rect(BORDER_INSET, BORDER_INSET, PAGE_WIDTH - 2 * BORDER_INSET, PAGE_HEIGHT - 2 * BORDER_INSET)
    canvas.setStrokeColor(GOLD)
    canvas.setLineWidth(0.8)
    canvas.rect(
        INNER_BORDER_INSET,
        INNER_BORDER_INSET,
        PAGE_WIDTH - 2 * INNER_BORDER_INSET,
        PAGE_HEIGHT - 2 * INNER_BORDER_INSET,
    )

    _draw_spaced_centered(canvas, "CERTIFICATE", SERIF_BOLD, 40, 485, char_space=6, color=NAVY)
    _draw_spaced_centered(canvas, "OF PARTICIPATION", SANS, 13, 458, char_space=4, color=GOLD)
    canvas.setStrokeColor(GOLD)
    canvas.setLineWidth(1)
    canvas.line(PAGE_WIDTH / 2 - 80, 441, PAGE_WIDTH / 2 + 80, 441)

    canvas.setFillColor(MUTED)
    canvas.setFont(SERIF, 15)
    canvas.drawCentredString(PAGE_WIDTH / 2, 411, "This is to certify that")
    canvas.drawCentredString(PAGE_WIDTH / 2, 272, "has participated in")

    for field, label in (("issuer_name", "Issued by"), ("issue_date", "Date of issue")):
        box = FIELD_SPECS[field].box
        canvas.setStrokeColor(GOLD)
        canvas.setLineWidth(0.8)
        canvas.line(box.x, 100, box.x + box.width, 100)
        canvas.setFillColor(MUTED)
        canvas.setFont(SANS, 10)
        canvas.drawCentredString(box.x + box.width / 2, 86, label)


def _draw_spaced_centered(
    canvas: Canvas, text: str, font_name: str, size: float, baseline: float, char_space: float, color: Color
) -> None:
    width = pdfmetrics.stringWidth(text, font_name, size) + char_space * (len(text) - 1)
    # Character spacing is part of the PDF graphics state and would otherwise
    # also widen every string drawn later, making it overflow its measured box.
    canvas.saveState()
    text_object = canvas.beginText(PAGE_WIDTH / 2 - width / 2, baseline)
    text_object.setFont(font_name, size)
    text_object.setCharSpace(char_space)
    text_object.setFillColor(color)
    text_object.textOut(text)
    canvas.drawText(text_object)
    canvas.restoreState()
