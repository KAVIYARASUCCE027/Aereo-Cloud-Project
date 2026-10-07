"""The bundled certificate fonts (see app/assets/fonts/README.md)."""

from collections.abc import Mapping
from functools import cache
from pathlib import Path

from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont

FONT_DIR = Path(__file__).resolve().parent / "assets" / "fonts"
SERIF = "DejaVuSerif"
SERIF_BOLD = "DejaVuSerif-Bold"
SANS = "DejaVuSans"
FONT_FILES = {SERIF: "DejaVuSerif.ttf", SERIF_BOLD: "DejaVuSerif-Bold.ttf", SANS: "DejaVuSans.ttf"}

# Fonts that draw user-supplied text (recipient name, event name, issuer).
TEXT_FONTS = (SERIF, SERIF_BOLD)


@cache  # registers once per process
def register_fonts() -> None:
    for name, filename in FONT_FILES.items():
        pdfmetrics.registerFont(TTFont(name, FONT_DIR / filename))


def glyph_table(font_name: str) -> Mapping[int, int]:
    """Code point -> glyph ID for every character the font can draw."""
    register_fonts()
    return pdfmetrics.getFont(font_name).face.charToGlyph
