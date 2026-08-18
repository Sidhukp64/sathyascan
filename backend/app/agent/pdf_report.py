"""
Phase 8 PDF report generation (roadmap §8.2). Generated on demand and
streamed directly in the HTTP response — never written to disk, matching
this project's established no-blob-storage precedent (Phase 3's media
handling, Phase 6's "no `data_deletion_requests`-style queued artifacts"
pattern). Reuses `app.agent.claim_pipeline_shared.load_verdicts` for
claims+evidence assembly — the SAME function the History API's detail
endpoint uses — rather than a second query/assembly path.

**Labels are multilingual** (roadmap: "Translate:... Reports"), via
`app.i18n.templates.PDF_REPORT_STRINGS` — the same en/ml/hi/ta
dict-of-dicts pattern every other user-facing string in this codebase uses.
`reasoning_text` itself is NOT re-translated here — it was already
generated directly in the analysis's own language back in Phase 2
(EvidenceSynthesisTool writes reasoning_text in the target language at
generation time, no separate translation call) — this module only
translates the surrounding LABELS.

**Unicode rendering (fixed — see docs/risks-and-open-questions.md's Phase 8
hardening entry for the history of this fix)**: fpdf2's built-in core fonts
(Helvetica) only support Latin-1, so an `en`-language report keeps using
Helvetica unchanged — zero behavior change, zero regression risk for the
common case. For `ml`/`hi`/`ta` reports, a real Unicode TrueType font
bundled under `app/assets/fonts/` (Noto Sans Malayalam / Devanagari / Tamil,
SIL Open Font License — see that directory's NOTICE.md) is registered via
fpdf2's `add_font()` and used for the WHOLE report, not just the labels —
so claim/reasoning text in that script now renders as real glyphs, not '?'.
Each of these fonts also covers Basic Latin (verified at font-selection
time; see `_FONT_CMAPS`), so mixed-script content (English proper nouns,
numbers, URLs inside a Malayalam/Hindi/Tamil report) renders correctly in
one pass without per-character font switching.

**Still-honest residual fallback**: a font's glyph repertoire is never
literally "every Unicode codepoint" — if a claim happens to mix in a THIRD
script the selected font doesn't cover (e.g. Chinese text quoted inside a
Hindi-language report), those specific characters are replaced with `?` and
the PDF's footer discloses this occurred, exactly as before — the fallback
now only fires for genuinely uncovered characters, not for the entire
Malayalam/Tamil/Hindi script the way it did previously.
"""

from datetime import timezone
from pathlib import Path

from fontTools.ttLib import TTFont as _TTFontReader
from fpdf import FPDF
from fpdf.enums import XPos, YPos
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.claim_pipeline_shared import load_verdicts
from app.agent.schemas import ClaimVerdict
from app.i18n.templates import PDF_REPORT_STRINGS
from app.models.analysis import Analysis

_FONT_DIR = Path(__file__).resolve().parent.parent / "assets" / "fonts"

# language -> (fpdf2 font-family name, {"": regular path, "B": bold path}).
# "en" and any language without an entry here use the Helvetica core font
# (see _resolve_font below) — deliberately not "every SUPPORTED_LANGUAGES
# entry", so adding a language to app.i18n.templates without a bundled font
# degrades to the same honest Latin-1 '?' fallback this module always had,
# rather than crashing.
_UNICODE_FONTS: dict[str, dict[str, str]] = {
    "ml": {
        "family": "NotoSansMalayalam",
        "": str(_FONT_DIR / "NotoSansMalayalam-Regular.ttf"),
        "B": str(_FONT_DIR / "NotoSansMalayalam-Bold.ttf"),
    },
    "hi": {
        "family": "NotoSansDevanagari",
        "": str(_FONT_DIR / "NotoSansDevanagari-Regular.ttf"),
        "B": str(_FONT_DIR / "NotoSansDevanagari-Bold.ttf"),
    },
    "ta": {
        "family": "NotoSansTamil",
        "": str(_FONT_DIR / "NotoSansTamil-Regular.ttf"),
        "B": str(_FONT_DIR / "NotoSansTamil-Bold.ttf"),
    },
}


def _load_cmap(path: str) -> frozenset[int]:
    """Real glyph-coverage set for a bundled font file, read once via
    fontTools independent of fpdf2's own internals — used only to decide
    which characters need the '?' fallback, not for rendering itself."""
    return frozenset(_TTFontReader(path).getBestCmap().keys())


# Loaded once at import time (six small static TTFs, a few ms total) —
# same one-time-construction-cost pattern as every provider client built in
# main.py's lifespan.
_FONT_CMAPS: dict[str, frozenset[int]] = {
    lang: _load_cmap(cfg[""]) for lang, cfg in _UNICODE_FONTS.items()
}


def _resolve_font(language: str) -> tuple[str, dict[str, str], frozenset[int] | None] | None:
    """Returns (fpdf_family_name, {"":regular,"B":bold} paths, coverage) for
    a bundled Unicode font, or None to mean "use the Helvetica core font"
    (coverage=None in that case signals the caller to fall back to the
    original Latin-1 encode-check instead of a cmap lookup)."""
    cfg = _UNICODE_FONTS.get(language)
    if cfg is None:
        return None
    return cfg["family"], {"": cfg[""], "B": cfg["B"]}, _FONT_CMAPS[language]


def _strings(language: str) -> dict[str, str]:
    return PDF_REPORT_STRINGS.get(language, PDF_REPORT_STRINGS["en"])


def _char_safe(text: str, coverage: frozenset[int] | None) -> tuple[str, bool]:
    """Returns (safe_text, had_loss).

    `coverage=None` means the Helvetica core font is active (Latin-1 range
    only — the ORIGINAL fallback path, unchanged, still used for `en`
    reports and any language without a bundled Unicode font).

    `coverage` given means a bundled Unicode TTF font is active — this now
    only replaces characters genuinely missing from THAT font's own
    repertoire (e.g. a stray third script), not "any non-Latin script" as
    before the fix.
    """
    if coverage is None:
        try:
            text.encode("latin-1")
            return text, False
        except UnicodeEncodeError:
            return text.encode("latin-1", errors="replace").decode("latin-1"), True
    missing = any(ord(ch) not in coverage for ch in text)
    if not missing:
        return text, False
    return "".join(ch if ord(ch) in coverage else "?" for ch in text), True


def _safe_style(base_font: str, style: str) -> str:
    """The bundled Unicode fonts only register Regular ("") and Bold ("B")
    — no Italic, since Noto Sans Devanagari/Tamil/Malayalam don't ship a
    distinct italic design (a real, disclosed limitation, not an oversight;
    faux-italic would need synthetic slanting fpdf2 doesn't do for TTF
    fonts). Helvetica (the `en`-report path) keeps its Italic style
    unchanged — zero behavior change for the common case."""
    if base_font == "Helvetica":
        return style
    return style.replace("I", "")


class _ReportPDF(FPDF):
    """`report_title`/`report_subtitle`/`base_font`/`char_coverage` are set
    on the instance right after construction (fpdf2's `header()`/`footer()`
    callbacks take no extra arguments — they're invoked internally by
    `add_page()` — so the per-report state has to be stashed on the object
    itself)."""

    report_title: str = "SathyaScan Fact-Check Report"
    report_subtitle: str = "AI-assisted, evidence-based fact-checking"
    base_font: str = "Helvetica"
    char_coverage: frozenset[int] | None = None

    def header(self) -> None:
        self.set_font(self.base_font, _safe_style(self.base_font, "B"), 16)
        title, _ = _char_safe(self.report_title, self.char_coverage)
        self.cell(0, 10, title, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_font(self.base_font, _safe_style(self.base_font, ""), 9)
        self.set_text_color(100, 100, 100)
        subtitle, _ = _char_safe(self.report_subtitle, self.char_coverage)
        self.cell(0, 6, subtitle, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_text_color(0, 0, 0)
        self.ln(4)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(self.base_font, _safe_style(self.base_font, "I"), 8)
        self.cell(0, 10, f"Page {self.page_no()}", align="C")


def _write_field(pdf: _ReportPDF, label: str, value: str, *, had_encoding_loss: list[bool]) -> None:
    safe_label, lost_label = _char_safe(label, pdf.char_coverage)
    safe_value, lost = _char_safe(value, pdf.char_coverage)
    had_encoding_loss.append(lost_label)
    had_encoding_loss.append(lost)
    pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, "B"), 11)
    pdf.cell(0, 7, safe_label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, ""), 10)
    pdf.multi_cell(0, 6, safe_value or "-", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)


def _render_claim(pdf: _ReportPDF, verdict: ClaimVerdict, strings: dict[str, str], had_encoding_loss: list[bool]) -> None:
    pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, "B"), 12)
    pdf.set_fill_color(240, 240, 240)
    result_line, lost = _char_safe(f"{strings['result_label']} {verdict.result.upper()}", pdf.char_coverage)
    had_encoding_loss.append(lost)
    pdf.cell(0, 8, result_line, new_x=XPos.LMARGIN, new_y=YPos.NEXT, fill=True)
    pdf.ln(1)

    _write_field(pdf, strings["claim_label"], verdict.claim_text, had_encoding_loss=had_encoding_loss)
    if verdict.claim_confidence is not None:
        _write_field(
            pdf, strings["credibility_score_label"], f"{verdict.claim_confidence:.2f}", had_encoding_loss=had_encoding_loss
        )
    _write_field(pdf, strings["explanation_label"], verdict.reasoning_text, had_encoding_loss=had_encoding_loss)

    if not verdict.investigation_complete:
        note = strings["incomplete_investigation_note"].format(reason=verdict.incomplete_reason or "unknown")
        _write_field(pdf, strings["note_label"], note, had_encoding_loss=had_encoding_loss)

    if verdict.evidence:
        pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, "B"), 11)
        safe_evidence_label, lost_el = _char_safe(strings["evidence_sources_label"], pdf.char_coverage)
        had_encoding_loss.append(lost_el)
        pdf.cell(0, 7, safe_evidence_label, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, ""), 9)
        for item in verdict.evidence:
            line = f"- [{item.stance or 'neutral'}] {item.domain}: {item.title}"
            safe_line, lost = _char_safe(line, pdf.char_coverage)
            had_encoding_loss.append(lost)
            pdf.multi_cell(0, 5, safe_line, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            if item.url:
                safe_url, lost_url = _char_safe(item.url, pdf.char_coverage)
                had_encoding_loss.append(lost_url)
                pdf.set_text_color(0, 0, 200)
                pdf.multi_cell(0, 5, safe_url, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
                pdf.set_text_color(0, 0, 0)
        pdf.ln(2)
    else:
        pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, "I"), 9)
        safe_no_sources, lost_ns = _char_safe(strings["no_sources_found"], pdf.char_coverage)
        had_encoding_loss.append(lost_ns)
        pdf.cell(0, 6, safe_no_sources, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(2)


async def generate_analysis_report_pdf(session: AsyncSession, analysis: Analysis) -> bytes:
    """**Phase 10 performance note — a measured non-finding, recorded so it
    isn't re-investigated later.** Everything after the single `await` below
    is synchronous, CPU-bound fpdf2 work (font loading, glyph subsetting,
    layout, `output()`) running on the event loop. Phase 10's load test
    measured it at concurrency 10: p50 262ms, p95 1223ms, p99 1749ms — the
    classic signature of a blocking-CPU bottleneck.

    The textbook fix (`starlette.concurrency.run_in_threadpool`) was
    implemented and A/B measured against this version. It produced **no
    improvement whatsoever**: /health degraded by an identical 3.84x under
    concurrent PDF pressure either way, and the PDF endpoint's own p95/p99
    were unchanged within noise. The reason is CPython's GIL — fpdf2 is
    pure-Python, so a worker thread contends for exactly the same lock and
    nothing actually runs in parallel. The change was therefore REVERTED
    rather than kept as unhelpful indirection.

    The real mitigation is process-level, not thread-level: run multiple
    uvicorn workers (or replicas) so PDF generation on one worker cannot
    stall requests on another. See docs/production-deployment.md §5 for the
    accompanying caveat that the three in-process periodic jobs must be
    pinned to a single replica if you do that.
    """
    strings = _strings(analysis.language)
    verdicts = await load_verdicts(session, analysis.id)

    pdf = _ReportPDF()
    font_choice = _resolve_font(analysis.language)
    if font_choice is not None:
        family, paths, coverage = font_choice
        pdf.add_font(family, "", paths[""])
        pdf.add_font(family, "B", paths["B"])
        pdf.base_font = family
        pdf.char_coverage = coverage
    # else: pdf.base_font stays "Helvetica", pdf.char_coverage stays None
    # (the class defaults) — the original, unchanged behavior for `en`.

    pdf.report_title = strings["title"]
    pdf.report_subtitle = strings["subtitle"]
    pdf.set_auto_page_break(auto=True, margin=20)
    pdf.add_page()
    had_encoding_loss: list[bool] = []

    _write_field(pdf, strings["input_type_label"], analysis.input_type, had_encoding_loss=had_encoding_loss)
    _write_field(pdf, strings["language_label"], analysis.language, had_encoding_loss=had_encoding_loss)
    checked_at = analysis.completed_at or analysis.created_at
    _write_field(
        pdf,
        strings["checked_at_label"],
        checked_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if checked_at else "-",
        had_encoding_loss=had_encoding_loss,
    )
    pdf.ln(4)

    if not verdicts:
        pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, "I"), 10)
        safe_no_claims, lost_nc = _char_safe(strings["no_claims_found"], pdf.char_coverage)
        had_encoding_loss.append(lost_nc)
        pdf.cell(0, 6, safe_no_claims, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    else:
        for verdict in verdicts:
            _render_claim(pdf, verdict, strings, had_encoding_loss)
            pdf.ln(4)

    pdf.set_font(pdf.base_font, _safe_style(pdf.base_font, "I"), 8)
    pdf.set_text_color(100, 100, 100)
    safe_disclaimer, lost_disclaimer = _char_safe(strings["disclaimer"], pdf.char_coverage)
    had_encoding_loss.append(lost_disclaimer)
    pdf.multi_cell(0, 5, safe_disclaimer, new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    if any(had_encoding_loss):
        pdf.ln(2)
        # The encoding-note ITSELF is always rendered in English, in the
        # Helvetica core font, regardless of the report's language — it
        # must stay readable even when the very reason it's shown is that
        # some characters couldn't render, and it never needs the bundled
        # Unicode fonts since it's pure ASCII.
        pdf.set_font("Helvetica", "I", 8)
        pdf.multi_cell(
            0,
            5,
            PDF_REPORT_STRINGS["en"]["encoding_note"],
            new_x=XPos.LMARGIN,
            new_y=YPos.NEXT,
        )

    return bytes(pdf.output())
