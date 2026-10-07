# Bundled fonts

| File | Font | Used for |
|---|---|---|
| `DejaVuSerif-Bold.ttf` | DejaVu Serif Bold 2.37 | Title, recipient name, event name |
| `DejaVuSerif.ttf` | DejaVu Serif 2.37 | Issuer, issue date, fixed phrases |
| `DejaVuSans.ttf` | DejaVu Sans 2.37 | Subtitle, labels, certificate ID |

Source: Debian package `fonts-dejavu-core` 2.37-8 (upstream
https://dejavu-fonts.github.io/). License: Bitstream Vera license (with the
DejaVu public-domain changes), see `LICENSE`. It permits redistribution and
embedding. ReportLab embeds only the glyphs a certificate uses.

Bundling the fonts makes rendering identical on every machine instead of
depending on system fonts. Supported character coverage is defined in
`app/services/pdf.py` (`SUPPORTED_RANGES`) and documented in `docs/design.md`.
