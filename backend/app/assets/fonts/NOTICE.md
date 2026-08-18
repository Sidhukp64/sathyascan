# Bundled fonts — licensing notice

This directory bundles six font files used by `app/agent/pdf_report.py` to
render Malayalam, Tamil, and Hindi (Devanagari) script glyphs in generated
PDF reports (fpdf2's built-in core fonts only support Latin-1 — see that
module's docstring for the full rationale).

| File | Family | Script |
|---|---|---|
| `NotoSansDevanagari-Regular.ttf` / `-Bold.ttf` | Noto Sans Devanagari | Hindi |
| `NotoSansTamil-Regular.ttf` / `-Bold.ttf` | Noto Sans Tamil | Tamil |
| `NotoSansMalayalam-Regular.ttf` / `-Bold.ttf` | Noto Sans Malayalam | Malayalam |

**Source**: [google/fonts](https://github.com/google/fonts), `ofl/notosansdevanagari/`,
`ofl/notosanstamil/`, `ofl/notosansmalayalam/` — each a static instance
(`wght=400` / `wght=700`, `wdth=100`) generated from the upstream variable
font via `fonttools varLib.instancer`, so fpdf2 embeds a genuinely static
TrueType font rather than a variable one.

**License**: SIL Open Font License, Version 1.1 — see `OFL-Devanagari.txt`,
`OFL-Tamil.txt`, `OFL-Malayalam.txt` in this directory (the exact license
text fetched from each font's own upstream `OFL.txt`). The OFL explicitly
permits bundling, embedding, and redistribution, including in a PDF's
embedded font subset, provided the license text accompanies the font —
satisfied by this notice and the three `OFL-*.txt` files sitting alongside
the fonts in this repository.

Copyright 2022 The Noto Project Authors (https://github.com/notofonts).
