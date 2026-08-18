"""
Deterministic multimodal fusion (Phase 5b) — combines a video's transcript
(spoken content) and OCR'd on-screen text (visual content) into ONE
structured, clearly-delimited document, then hands it to the EXISTING
ClaimExtractionTool for a single extraction call.

Deliberately NOT a second extraction/deduplication system: agent-
architecture.md already establishes that claim splitting/deduplication is a
genuine Claude judgment task (PRD §13's NASA/earthquake example), not
something to re-implement deterministically here. This module's only job is
STRUCTURING the raw multimodal inputs into one well-delimited text — by
presenting spoken and on-screen content as ONE coherent document (not two
separately-extracted claim lists merged after the fact), the existing
extraction call naturally avoids re-extracting the same fact twice when both
modalities state it, the same way it already handles a single long message
containing a repeated claim. "Clearly distinguish transcript claims / OCR-
derived claims" (the user's explicit Phase 5 requirement) is satisfied by
the `[SPOKEN CONTENT]` / `[ON-SCREEN TEXT]` section labels below, which
Claude sees and can reference in its reasoning/category assignment.

Untrusted-content discipline (decisions.md §15) is preserved: this produces
plain text DATA, never instructions — the resulting document is passed to
ClaimExtractionTool exactly as any other claim source (message text, OCR'd
image text) already is, with no special trust conferred on either modality.

The existing evidence/classification guard (app/agent/classification.py)
remains the sole authority for factual verdicts — this module never touches
evidence, credibility, or result classification in any way.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FrameTextSource:
    """One sampled video frame's OCR result, tagged with its own timestamp
    — kept distinct from the transcript so the origin of each piece of
    on-screen text stays traceable."""

    timestamp_seconds: float
    extracted_text: str


def _dedupe_consecutive(frame_texts: list[FrameTextSource]) -> list[FrameTextSource]:
    """Cheap, purely mechanical dedup: identical text on consecutive sampled
    frames (the same on-screen caption held for several seconds) collapses
    to one entry. This is NOT semantic claim deduplication (that stays
    Claude's job downstream) — just removing literal repeated OCR output."""
    deduped: list[FrameTextSource] = []
    last_text: str | None = None
    for source in frame_texts:
        normalized = source.extracted_text.strip()
        if not normalized:
            continue
        if normalized == last_text:
            continue
        deduped.append(FrameTextSource(timestamp_seconds=source.timestamp_seconds, extracted_text=normalized))
        last_text = normalized
    return deduped


def build_multimodal_document(transcript: str | None, frame_texts: list[FrameTextSource]) -> str:
    """Returns a single, structured text combining spoken and on-screen
    content, ready for ClaimExtractionTool.extract(). Returns "" if there's
    nothing from either modality — the caller treats that the same as "no
    claims to check", matching the audio/image pipelines' precedent."""
    sections: list[str] = []

    if transcript and transcript.strip():
        sections.append(f"[SPOKEN CONTENT]\n{transcript.strip()}")

    deduped = _dedupe_consecutive(frame_texts)
    if deduped:
        on_screen_lines = "\n".join(f"- {source.extracted_text}" for source in deduped)
        sections.append(f"[ON-SCREEN TEXT]\n{on_screen_lines}")

    return "\n\n".join(sections)
