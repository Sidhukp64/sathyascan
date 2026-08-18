"""
Response Generation tool — final WhatsApp message assembly.

Deliberately NOT an LLM call: reasoning_text is already written in the
target language by EvidenceSynthesisTool, so this step is pure, deterministic
template rendering (decisions.md: keeps the multilingual final-formatting
step cheap, fast, and fully unit-testable without mocking Claude at all).
"""

from app.agent.schemas import ClaimVerdict
from app.agent.tools.audio_forensics import AudioForensicsResult
from app.agent.tools.url_analyzer import UrlContentStatus
from app.agent.tools.url_safety import UrlSafetyResult
from app.agent.tools.video_forensics import VideoForensicsResult
from app.i18n.templates import (
    MEDIA_FORENSICS_LABELS,
    RESULT_LABELS,
    RISK_LEVEL_LABELS,
    UI_STRINGS,
    URL_SAFETY_FINDING_TEMPLATES,
    URL_SAFETY_RECOMMENDATIONS,
    resolve_language,
)

_MAX_EVIDENCE_SHOWN = 3


def _confidence_bucket(confidence: float | None, strings: dict[str, str]) -> str | None:
    if confidence is None:
        return None
    if confidence >= 0.75:
        return strings["confidence_high"]
    if confidence >= 0.45:
        return strings["confidence_medium"]
    return strings["confidence_low"]


def _format_claim_block(verdict: ClaimVerdict, language: str) -> str:
    labels = RESULT_LABELS.get(language, RESULT_LABELS["en"])
    strings = UI_STRINGS.get(language, UI_STRINGS["en"])

    lines = [
        f'{strings["claim_label"]} "{verdict.claim_text}"',
        f'{strings["status_label"]} {labels.get(verdict.result, verdict.result)}',
        f'{strings["why_label"]} {verdict.reasoning_text}',
    ]

    if verdict.evidence:
        lines.append(strings["evidence_label"])
        for i, item in enumerate(verdict.evidence[:_MAX_EVIDENCE_SHOWN], start=1):
            lines.append(f"{i}. {item.domain} — {item.title}\n   {item.url}")
    elif verdict.investigation_complete:
        lines.append(strings["no_evidence"])

    confidence_label = _confidence_bucket(verdict.claim_confidence, strings)
    if confidence_label:
        lines.append(f'{strings["confidence_label"]} {confidence_label}')

    return "\n".join(lines)


class ResponseGenerationTool:
    def generate(self, verdicts: list[ClaimVerdict], requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])

        if not verdicts:
            return strings["no_claims_message"]

        blocks = [strings["result_header"]]
        for verdict in verdicts:
            blocks.append(_format_claim_block(verdict, language))
        blocks.append(strings["disclaimer"])

        return "\n\n".join(blocks)

    def generate_no_claims_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["no_claims_message"]

    def generate_at_capacity_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["at_capacity"]

    def generate_account_suspended_reply(self, requested_language: str | None) -> str:
        """Phase 9 — admin "Suspend user" (roadmap §9.1), enforced on the
        WhatsApp side (app/webhook/whatsapp/router.py) — same reply for
        every message type, since a suspended account can't use ANY
        pipeline, not just one."""
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["account_suspended_message"]

    def generate_analyzing_ack(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["analyzing_ack"]

    def generate_processing_error_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["processing_error"]

    def generate_processing_timeout_reply(self, requested_language: str | None) -> str:
        """decisions.md §12/§15's wall-clock timeout (app/core/pipeline_timeout.py) —
        distinct wording from the generic processing_error so a genuinely
        slow/stuck analysis isn't confused with an outright failure."""
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["processing_timeout"]

    # ==== Phase 3 (image pipeline) ====

    def generate_analyzing_image_ack(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["analyzing_image_ack"]

    def generate_content_declined_reply(self, requested_language: str | None) -> str:
        """Safety-gate BLOCKED/ERROR outcome. Deliberately generic — never
        reveals why (decisions.md §6/§15: safety-gate internals must never
        be exposed to the user)."""
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["content_declined"]

    def generate_no_text_found_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["no_text_found"]

    def generate_file_too_large_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["file_too_large"]

    def generate_unsupported_media_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["unsupported_media"]

    # ==== Phase 4 (URL pipeline) ====

    def generate_analyzing_url_ack(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["analyzing_url_ack"]

    def render_url_recommendation(self, risk_level: str, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        recommendations = URL_SAFETY_RECOMMENDATIONS.get(language, URL_SAFETY_RECOMMENDATIONS["en"])
        return recommendations.get(risk_level, recommendations["safe"])

    def _format_url_safety_block(self, url: str, safety_result: UrlSafetyResult, language: str) -> str:
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])
        risk_labels = RISK_LEVEL_LABELS.get(language, RISK_LEVEL_LABELS["en"])
        finding_templates = URL_SAFETY_FINDING_TEMPLATES.get(language, URL_SAFETY_FINDING_TEMPLATES["en"])

        risk_value = safety_result.risk_level.value
        lines = [
            strings["url_safety_header"],
            f'{strings["url_label"]} {url}',
            f'{strings["risk_label"]} {risk_labels.get(risk_value, risk_value)}',
        ]

        if safety_result.findings:
            lines.append(strings["reasons_label"])
            for finding in safety_result.findings:
                template = finding_templates.get(finding.code.value, finding.code.value)
                try:
                    rendered = template.format(**finding.params)
                except (KeyError, IndexError):
                    # A missing/unexpected param must never break the reply —
                    # fall back to the unformatted template rather than raise.
                    rendered = template
                lines.append(f"• {rendered}")
        else:
            lines.append(strings["no_safety_concerns"])

        lines.append(f'{strings["recommendation_label"]} {self.render_url_recommendation(risk_value, language)}')
        return "\n".join(lines)

    def generate_url_analysis_reply(
        self,
        url: str,
        safety_result: UrlSafetyResult,
        verdicts: list[ClaimVerdict],
        content_status: UrlContentStatus | None,
        requested_language: str | None,
        *,
        robots_disallowed: bool = False,
    ) -> str:
        """Combines the URL Safety block with the claim-credibility block
        into ONE WhatsApp message, per phased-plan.md's Phase 4 done-when
        criterion: "a forwarded URL returns both a credibility assessment
        and a distinct safety-risk block." Safety renders first (more
        urgent/actionable), credibility second — the safety assessment is
        ALWAYS shown, even when no claims could be checked (fetch failed,
        SSRF-blocked, robots.txt-disallowed, unsupported content type, or
        the page simply has no checkable claim)."""
        language = resolve_language(requested_language)
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])

        blocks = [self._format_url_safety_block(url, safety_result, language)]

        if verdicts:
            blocks.append(strings["result_header"])
            for verdict in verdicts:
                blocks.append(_format_claim_block(verdict, language))
            blocks.append(strings["disclaimer"])
        elif robots_disallowed:
            blocks.append(strings["url_robots_disallowed_note"])
        elif content_status in (UrlContentStatus.NO_TEXT_FOUND, UrlContentStatus.SUCCESS):
            # SUCCESS-with-empty-verdicts means content fetched fine but
            # ClaimExtractionTool found no checkable claim on the page —
            # same friendly message as "the page had no legible text".
            blocks.append(strings["url_no_claims_message"])
        elif content_status == UrlContentStatus.UNSUPPORTED_CONTENT_TYPE:
            blocks.append(strings["unsupported_url_content"])
        else:
            # FETCH_FAILED (timeout/SSRF-blocked/download error) — a
            # generic "couldn't assess credibility" note.
            blocks.append(strings["url_content_unavailable_note"])

        return "\n\n".join(blocks)

    # ==== Phase 5 (audio pipeline) ====

    def generate_analyzing_audio_ack(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["analyzing_audio_ack"]

    def generate_transcription_failed_reply(self, requested_language: str | None) -> str:
        """Per the user's exact Phase 5 spec: transcription failure must
        show status insufficient_evidence, never a guessed verdict."""
        language = resolve_language(requested_language)
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])
        labels = RESULT_LABELS.get(language, RESULT_LABELS["en"])
        return (
            f'{strings["transcription_failed"]}\n\n'
            f'{strings["status_label"]}\n{labels.get("insufficient_evidence", "insufficient_evidence")}'
        )

    def format_media_authenticity_block(
        self, forensics_result: AudioForensicsResult | VideoForensicsResult, requested_language: str | None
    ) -> str:
        """Shared by audio AND video (app/agent/tools/video_forensics.py's
        VideoForensicsResult carries the identical status vocabulary) —
        deliberately a STANDALONE block, never merged into the fact-check
        block, per the user's explicit instruction: media authenticity and
        factual verification are independent results, never conflated."""
        language = resolve_language(requested_language)
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])
        labels = MEDIA_FORENSICS_LABELS.get(language, MEDIA_FORENSICS_LABELS["en"])
        label = labels.get(forensics_result.status.value, labels["provider_unavailable"])
        return f'{strings["media_authenticity_label"]}\n{label}'

    def generate_audio_analysis_reply(
        self,
        transcript: str | None,
        verdicts: list[ClaimVerdict],
        forensics_result: AudioForensicsResult,
        requested_language: str | None,
        *,
        no_speech_found: bool = False,
    ) -> str:
        """Combines the transcript, the fact-check block(s), and a SEPARATE
        media-authenticity block into ONE WhatsApp message — matching the
        user's exact Phase 5 example format. The media-authenticity block is
        ALWAYS present, even when no claims could be checked."""
        language = resolve_language(requested_language)
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])

        blocks = [strings["audio_result_header"]]
        if transcript:
            blocks.append(f'{strings["transcript_label"]}\n"{transcript}"')

        if verdicts:
            for verdict in verdicts:
                blocks.append(_format_claim_block(verdict, language))
            blocks.append(strings["disclaimer"])
        elif no_speech_found:
            blocks.append(strings["no_speech_found"])
        else:
            blocks.append(strings["audio_no_claims_message"])

        blocks.append(self.format_media_authenticity_block(forensics_result, language))

        return "\n\n".join(blocks)

    def generate_audio_duration_too_long_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["duration_too_long"]

    # ==== Phase 5b (video pipeline) ====

    def generate_analyzing_video_ack(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["analyzing_video_ack"]

    def generate_video_duration_too_long_reply(self, requested_language: str | None) -> str:
        language = resolve_language(requested_language)
        return UI_STRINGS.get(language, UI_STRINGS["en"])["video_duration_too_long"]

    def _format_video_claim_block(self, verdict: ClaimVerdict, language: str) -> str:
        """Same fields as _format_claim_block (Claim/Status/Why/Evidence/
        Confidence) but with the user's exact video wording for the status
        line ("Fact-check status:") to distinguish it from media
        authenticity, which uses its own separate block/label entirely."""
        labels = RESULT_LABELS.get(language, RESULT_LABELS["en"])
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])

        lines = [
            f'{strings["claim_label"]} "{verdict.claim_text}"',
            f'{strings["video_fact_check_status_label"]} {labels.get(verdict.result, verdict.result)}',
            f'{strings["why_label"]} {verdict.reasoning_text}',
        ]

        if verdict.evidence:
            lines.append(strings["evidence_label"])
            for i, item in enumerate(verdict.evidence[:_MAX_EVIDENCE_SHOWN], start=1):
                lines.append(f"{i}. {item.domain} — {item.title}\n   {item.url}")
        elif verdict.investigation_complete:
            lines.append(strings["no_evidence"])

        confidence_label = _confidence_bucket(verdict.claim_confidence, strings)
        if confidence_label:
            lines.append(f'{strings["confidence_label"]} {confidence_label}')

        return "\n".join(lines)

    def generate_video_analysis_reply(
        self,
        transcript: str | None,
        on_screen_text_summary: str | None,
        verdicts: list[ClaimVerdict],
        forensics_result: VideoForensicsResult,
        requested_language: str | None,
    ) -> str:
        """Combines the spoken-claim block, the on-screen-text block, the
        fact-check block(s), and a SEPARATE media-authenticity block into
        ONE WhatsApp message — matching the user's exact Phase 5 example
        format, plus an explicit independence note (the user's explicit
        instruction: media authenticity and factual verification must never
        be conflated, in either direction)."""
        language = resolve_language(requested_language)
        strings = UI_STRINGS.get(language, UI_STRINGS["en"])

        blocks = [strings["video_result_header"]]
        if transcript:
            blocks.append(f'{strings["spoken_claim_label"]}\n"{transcript}"')
        if on_screen_text_summary:
            blocks.append(f'{strings["on_screen_text_label"]}\n"{on_screen_text_summary}"')

        if verdicts:
            for verdict in verdicts:
                blocks.append(self._format_video_claim_block(verdict, language))
            blocks.append(strings["disclaimer"])
        else:
            blocks.append(strings["video_no_claims_message"])

        blocks.append(self.format_media_authenticity_block(forensics_result, language))
        blocks.append(strings["media_independence_note"])

        return "\n\n".join(blocks)
