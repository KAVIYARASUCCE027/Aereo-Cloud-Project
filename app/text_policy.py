"""Rules for text printed on a certificate, shared by submission validation
(app/schemas.py) and PDF rendering (app/services/pdf.py).

Having one definition keeps the two in agreement: text accepted at submission
can be rendered, and the renderer rejects the same things validation does.

Supported characters are the code points in SUPPORTED_RANGES that both serif
fonts used for user text can draw, excluding control, format, unassigned, and
combining characters, and characters that NFC normalization would replace.
These are left-to-right scripts that need no glyph shaping, because ReportLab
does not shape text. Everything else (for example Devanagari, Arabic, Hebrew,
Thai, CJK, emoji, invisible characters such as a zero-width space, combining
marks that do not compose) is unsupported.
"""

import unicodedata
from collections.abc import Sequence
from functools import cache

from app.fonts import TEXT_FONTS, glyph_table

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

# Control, format (e.g. soft hyphen), unassigned, and combining characters.
EXCLUDED_CATEGORIES = frozenset({"Cc", "Cf", "Cn", "Mn", "Me"})

DESCRIBED_CHARACTERS_LIMIT = 5


def normalize(value: str) -> str:
    """NFC-normalize, then trim surrounding whitespace.

    NFC composes 'e' + combining acute accent into the single character 'é',
    so lengths are counted the way people read them.
    """
    return unicodedata.normalize("NFC", value).strip()


@cache
def supported_code_points() -> frozenset[int]:
    """Code points in SUPPORTED_RANGES that every text font can draw and that
    can appear in normalized text."""
    tables = [glyph_table(font) for font in TEXT_FONTS]
    return frozenset(
        code_point
        for start, end, _ in SUPPORTED_RANGES
        for code_point in range(start, end + 1)
        if unicodedata.category(chr(code_point)) not in EXCLUDED_CATEGORIES
        # e.g. U+0374 GREEK NUMERAL SIGN normalizes to U+02B9, outside the ranges
        and unicodedata.normalize("NFC", chr(code_point)) == chr(code_point)
        and all(code_point in table for table in tables)
    )


def unsupported_characters(value: str) -> list[str]:
    """Distinct unsupported characters in value, in code point order."""
    supported = supported_code_points()
    return sorted({char for char in value if ord(char) not in supported})


def describe_characters(chars: Sequence[str]) -> str:
    """Human-readable list such as 'U+0906 DEVANAGARI LETTER AA, ... and 3 more'."""
    shown = [f"U+{ord(char):04X} {_character_name(char)}" for char in chars]
    if len(shown) > DESCRIBED_CHARACTERS_LIMIT:
        hidden = len(shown) - DESCRIBED_CHARACTERS_LIMIT
        shown = [*shown[:DESCRIBED_CHARACTERS_LIMIT], f"and {hidden} more"]
    return ", ".join(shown)


def _character_name(char: str) -> str:
    if unicodedata.category(char) == "Cc":
        return "CONTROL CHARACTER"  # newline, tab, ...: Unicode gives these no name
    return unicodedata.name(char, "UNNAMED CHARACTER")
