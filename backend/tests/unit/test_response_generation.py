"""
ResponseGenerationTool tests — WhatsApp formatting, English, and Malayalam.
No mocking needed: this tool makes no LLM call at all (see module docstring
in app/agent/tools/response_generation.py).
"""

from app.agent.schemas import ClaimVerdict, EvidenceItem
from app.agent.tools.response_generation import ResponseGenerationTool

tool = ResponseGenerationTool()


def _verified_verdict() -> ClaimVerdict:
    return ClaimVerdict(
        claim_text="The government announced a new scholarship.",
        category="gov_scheme",
        result="verified",
        investigation_complete=True,
        claim_confidence=0.9,
        evidence_strength=0.9,
        evidence_tier_met=1,
        reasoning_text="An official government notice confirms this.",
        evidence=[
            EvidenceItem(
                title="Official Notice",
                url="https://pib.gov.in/notice",
                domain="pib.gov.in",
                snippet="...",
                credibility_tier="tier_1_gov_official",
                tier_rank=1,
                stance="supporting",
            )
        ],
    )


def _insufficient_evidence_verdict() -> ClaimVerdict:
    return ClaimVerdict(
        claim_text="Some claim.",
        category="other",
        result="insufficient_evidence",
        investigation_complete=False,
        incomplete_reason="search_limit",
        claim_confidence=None,
        evidence_strength=None,
        evidence_tier_met=None,
        reasoning_text="We weren't able to complete enough research to check this claim fully.",
        evidence=[],
    )


class TestWhatsAppFormatting:
    def test_includes_claim_status_reasoning_and_evidence(self):
        text = tool.generate([_verified_verdict()], "en")
        assert "government announced" in text
        assert "Verified" in text
        assert "official government notice" in text.lower()
        assert "pib.gov.in" in text
        assert "https://pib.gov.in/notice" in text

    def test_includes_disclaimer(self):
        text = tool.generate([_verified_verdict()], "en")
        assert "may make mistakes" in text.lower()

    def test_multiple_claims_are_all_present(self):
        v1 = _verified_verdict()
        v2 = _insufficient_evidence_verdict()
        text = tool.generate([v1, v2], "en")
        assert "government announced" in text
        assert "Some claim." in text

    def test_no_claims_produces_clarifying_message_not_empty_string(self):
        text = tool.generate([], "en")
        assert len(text) > 0
        assert "couldn't find" in text.lower()

    def test_insufficient_evidence_shows_no_confidence_line(self):
        text = tool.generate([_insufficient_evidence_verdict()], "en")
        assert "Confidence:" not in text  # None confidence -> line omitted, never fabricated


class TestEnglishResponse:
    def test_status_label_is_english(self):
        text = tool.generate([_verified_verdict()], "en")
        assert "✅ Verified" in text

    def test_ui_strings_are_english(self):
        text = tool.generate([_verified_verdict()], "en")
        assert "*Claim:*" in text
        assert "*Evidence:*" in text


class TestMalayalamResponse:
    def test_status_label_is_malayalam(self):
        text = tool.generate([_verified_verdict()], "ml")
        assert "സ്ഥിരീകരിച്ചു" in text

    def test_ui_strings_are_malayalam(self):
        text = tool.generate([_verified_verdict()], "ml")
        assert "അവകാശവാദം" in text  # "Claim"
        assert "തെളിവുകൾ" in text  # "Evidence"

    def test_claim_text_and_urls_are_preserved_verbatim(self):
        # The claim text / evidence content itself isn't translated (Phase 2
        # design note: reasoning is generated in-language by the LLM at
        # synthesis time; the template only translates fixed UI labels).
        text = tool.generate([_verified_verdict()], "ml")
        assert "https://pib.gov.in/notice" in text

    def test_no_claims_message_is_malayalam(self):
        text = tool.generate([], "ml")
        assert len(text) > 0
        assert text != tool.generate([], "en")

    def test_unsupported_language_falls_back_to_english(self):
        text = tool.generate([_verified_verdict()], "fr")
        assert "✅ Verified" in text  # falls back to English, not a crash


class TestAuxiliaryReplies:
    def test_analyzing_ack_differs_by_language(self):
        en = tool.generate_analyzing_ack("en")
        ml = tool.generate_analyzing_ack("ml")
        assert en != ml
        assert len(en) > 0 and len(ml) > 0

    def test_at_capacity_reply_exists_for_both_languages(self):
        assert tool.generate_at_capacity_reply("en")
        assert tool.generate_at_capacity_reply("ml")

    def test_processing_error_reply_handles_none_language(self):
        # None language (e.g. failure before a user record could be loaded)
        # must not crash — falls back to the default language.
        text = tool.generate_processing_error_reply(None)
        assert len(text) > 0
