"""
Full image-pipeline scenario tests, exercised directly against ImagePipeline
(mirrors tests/integration/test_text_pipeline.py's structure and reuses its
conventions) so each scenario can script the fakes precisely. Uses a real
in-memory SQLite DB so persistence (including "no media bytes ever
persisted") is tested against real queries, not mocks.
"""

import fakeredis.aioredis

from app.agent.image_pipeline import ImagePipeline, ImagePipelineDependencies
from app.agent.safety_gate import SafetyCheckResult, SafetyGate, SafetyOutcome
from app.agent.tools.image_analysis import ImageAnalysisResult, ImageAnalysisStatus, ImageAnalysisTool
from app.agent.tools.ocr import OCRResult, OCRStatus, OCRTool
from app.agent.user_service import get_or_create_user
from app.core.config import Settings
from app.integrations.evidence_search_client import SearchResult
from app.media.downloader import MediaTooLargeError, UnsafeMediaURLError
from app.models.media_attachment import MediaAttachment
from app.models.safety_gate_event import SafetyGateEvent
from sqlalchemy import select

from tests.conftest import (
    TEST_PHONE_ENCRYPTION_KEY,
    TEST_PHONE_PEPPER,
    FakeEvidenceSearchProvider,
    FakeLLMClient,
    FakeMediaClient,
    make_sample_jpeg_bytes,
    make_text_response,
    make_tool_response,
)


def _settings(**overrides) -> Settings:
    base = dict(
        WHATSAPP_APP_SECRET="x",
        WHATSAPP_WEBHOOK_VERIFY_TOKEN="x",
        WHATSAPP_ACCESS_TOKEN="x",
        WHATSAPP_PHONE_NUMBER_ID="x",
        PHONE_HASH_PEPPER=TEST_PHONE_PEPPER,
        PHONE_ENCRYPTION_KEY=TEST_PHONE_ENCRYPTION_KEY,
        MAX_EVIDENCE_SEARCHES_PER_ANALYSIS=5,
        MAX_LLM_TOOL_CALLS_PER_ANALYSIS=10,
        DAILY_SPEND_CIRCUIT_BREAKER_USD=50.0,
        GLOBAL_LLM_SEARCH_CONCURRENCY_LIMIT=20,
        MIN_CONCURRING_TIER2_SOURCES=2,
        MAX_IMAGE_PIXELS=40_000_000,
        MAX_UPLOAD_SIZE_BYTES_IMAGE=10_485_760,
    )
    base.update(overrides)
    return Settings(**base)


async def _make_user(db_session, phone="15551234567"):
    return await get_or_create_user(db_session, phone, TEST_PHONE_PEPPER, TEST_PHONE_ENCRYPTION_KEY)


class _TurnScript:
    def __init__(self, claims_response, synthesis_response=None, investigation_turns=None):
        self.claims_response = claims_response
        self.synthesis_response = synthesis_response
        self.investigation_turns = list(investigation_turns or [])
        self._i = 0

    def __call__(self, ctx):
        if ctx.forced_tool_name == "record_claims":
            return self.claims_response
        if ctx.forced_tool_name == "record_synthesis":
            return self.synthesis_response
        if self._i < len(self.investigation_turns):
            resp = self.investigation_turns[self._i]
            self._i += 1
            return resp
        return make_text_response()


def _pipeline(db_session, settings, llm, search, media_client, safety_provider, ocr_provider, image_provider) -> ImagePipeline:
    deps = ImagePipelineDependencies(
        llm=llm,
        search_provider=search,
        redis=fakeredis.aioredis.FakeRedis(decode_responses=True),
        media_client=media_client,
        safety_gate=SafetyGate(safety_provider),
        ocr_tool=OCRTool(ocr_provider),
        image_analysis_tool=ImageAnalysisTool(image_provider),
    )
    return ImagePipeline(db_session, deps, settings)


def _passed_safety_provider():
    class _P:
        async def check(self, media_bytes, mime_type):
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    return _P()


def _blocked_safety_provider():
    class _P:
        async def check(self, media_bytes, mime_type):
            return SafetyCheckResult(outcome=SafetyOutcome.BLOCKED, check_type="content_classifier", provider_name="fake")

    return _P()


def _raising_safety_provider():
    class _P:
        async def check(self, media_bytes, mime_type):
            raise RuntimeError("simulated safety provider crash")

    return _P()


def _ocr_provider_returning(result: OCRResult):
    class _P:
        async def extract_text(self, image_bytes, mime_type, language_hint):
            return result

    return _P()


def _unavailable_image_provider():
    class _P:
        async def analyze(self, image_bytes, mime_type):
            return ImageAnalysisResult(status=ImageAnalysisStatus.UNAVAILABLE)

    return _P()


async def test_safe_image_full_pipeline_ocr_to_classification(db_session, seeded_registry):
    llm = FakeLLMClient()
    llm.responder = _TurnScript(
        claims_response=make_tool_response("record_claims", {"claims": [{"text": "The scheme was announced.", "category": "gov_scheme"}]}),
        synthesis_response=make_tool_response(
            "record_synthesis",
            {"evidence_stances": ["supporting"], "suggested_result": "verified", "claim_confidence": 0.9, "reasoning_text": "Confirmed by an official source."},
        ),
        investigation_turns=[make_tool_response("search_evidence", {"query": "scheme announcement"})],
    )
    search = FakeEvidenceSearchProvider(
        responder=lambda q: [SearchResult(title="Official notice", url="https://pib.gov.in/notice", snippet="Confirmed.")]
    )
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(
        OCRResult(status=OCRStatus.SUCCESS, extracted_text="The scheme was announced.", provider_name="fake")
    )

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-1", source_wamid="wamid.i1", language="en")

    assert outcome.analysis.status == "completed"
    assert outcome.analysis.overall_result == "verified"
    assert outcome.analysis.input_type == "image"


async def test_unsafe_image_is_stopped_before_any_analysis(db_session, seeded_registry):
    llm = FakeLLMClient()  # default responder — must never be called
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS, extracted_text="should never be read"))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _blocked_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-2", source_wamid="wamid.i2", language="en")

    assert outcome.analysis.status == "blocked"
    assert llm.call_log == []  # no claim extraction, no synthesis — nothing ran

    # A safety_gate_events row was recorded, with no content.
    result = await db_session.execute(select(SafetyGateEvent).where(SafetyGateEvent.analysis_id == outcome.analysis.id))
    events = result.scalars().all()
    assert len(events) == 1
    assert events[0].outcome == "blocked"


async def test_safety_provider_failure_fails_closed_and_blocks(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))

    user = await _make_user(db_session)
    pipeline = _pipeline(
        db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client,
        _raising_safety_provider(), _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS, extracted_text="x")),
        _unavailable_image_provider(),
    )
    outcome = await pipeline.run(user=user, media_id="media-3", source_wamid="wamid.i3", language="en")

    assert outcome.analysis.status == "blocked"
    assert llm.call_log == []


async def test_ocr_provider_unavailable_yields_insufficient_evidence(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.PROVIDER_UNAVAILABLE))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-4", source_wamid="wamid.i4", language="en")

    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert outcome.analysis.error_code == "provider_unavailable"


async def test_ocr_failure_yields_insufficient_evidence_not_no_text(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.FAILED))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-5", source_wamid="wamid.i5", language="en")

    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert outcome.analysis.error_code == "infra_error"


async def test_no_text_found_is_a_friendly_reply_not_insufficient_evidence(db_session, seeded_registry):
    """decisions.md §1A distinction: OCR running successfully and finding
    nothing is a completed investigation with nothing to check — NOT the
    same as the investigation failing to complete."""
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.NO_TEXT_FOUND))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-6", source_wamid="wamid.i6", language="en")

    assert outcome.analysis.overall_result == "no_claims_detected"
    assert outcome.analysis.status == "completed"


async def test_budget_limit_before_ocr_yields_insufficient_evidence(db_session, seeded_registry):
    """MAX_LLM_TOOL_CALLS_PER_ANALYSIS=0 — the very first record_llm_call()
    for OCR must fail, and OCR must never actually be invoked."""
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_calls: list[str] = []

    class _CountingOCR:
        async def extract_text(self, image_bytes, mime_type, language_hint):
            ocr_calls.append("called")
            return OCRResult(status=OCRStatus.SUCCESS, extracted_text="x")

    user = await _make_user(db_session)
    settings = _settings(MAX_LLM_TOOL_CALLS_PER_ANALYSIS=0)
    pipeline = _pipeline(db_session, settings, llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _CountingOCR(), _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-7", source_wamid="wamid.i7", language="en")

    assert outcome.analysis.overall_result == "insufficient_evidence"
    assert outcome.analysis.error_code == "tool_call_limit"
    assert ocr_calls == []  # OCR itself was never reached


async def test_oversized_media_is_a_friendly_pre_analysis_error(db_session, seeded_registry):
    llm = FakeLLMClient()

    def _raise(mid):
        raise MediaTooLargeError("too big")

    media_client = FakeMediaClient(responder=_raise)

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS)), _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-8", source_wamid="wamid.i8", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "file_too_large"
    assert outcome.analysis.overall_result is None  # no claim-level outcome — nothing was ever investigated
    assert llm.call_log == []


async def test_unsafe_media_url_is_a_friendly_pre_analysis_error(db_session, seeded_registry):
    llm = FakeLLMClient()

    def _raise(mid):
        raise UnsafeMediaURLError("blocked")

    media_client = FakeMediaClient(responder=_raise)

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS)), _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-9", source_wamid="wamid.i9", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "unsafe_url"


async def test_unsupported_format_is_rejected_after_download_before_safety_gate(db_session, seeded_registry):
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (b"not a real image", "image/jpeg"))
    safety_provider_called = []

    class _TrackedSafety:
        async def check(self, media_bytes, mime_type):
            safety_provider_called.append(True)
            return SafetyCheckResult(outcome=SafetyOutcome.PASSED, check_type="other", provider_name="fake")

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _TrackedSafety(), _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS)), _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-10", source_wamid="wamid.i10", language="en")

    assert outcome.analysis.status == "failed"
    assert outcome.analysis.error_code == "unsupported_format"
    assert safety_provider_called == []  # validation happens before the safety gate


async def test_no_media_bytes_are_ever_persisted(db_session, seeded_registry):
    """Phase 3 stores nothing durable — media_attachments.storage_path
    stays NULL and no blob is written anywhere."""
    llm = FakeLLMClient()
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.NO_TEXT_FOUND))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-11", source_wamid="wamid.i11", language="en")

    result = await db_session.execute(select(MediaAttachment).where(MediaAttachment.analysis_id == outcome.analysis.id))
    attachment = result.scalar_one()
    assert attachment.storage_path is None
    assert attachment.mime_type == "image/jpeg"  # the real sniffed type is still recorded — just no bytes


async def test_unavailable_image_analysis_does_not_block_classification(db_session, seeded_registry):
    """agent-architecture.md's Multimodal Fusion policy: a missing modality
    is disclosed, never silently forces insufficient_evidence when OCR'd
    text evidence alone can meet the tier."""
    llm = FakeLLMClient()
    llm.responder = _TurnScript(
        claims_response=make_tool_response("record_claims", {"claims": [{"text": "A verifiable claim.", "category": "other"}]}),
        synthesis_response=make_tool_response(
            "record_synthesis",
            {"evidence_stances": ["supporting"], "suggested_result": "verified", "claim_confidence": 0.9, "reasoning_text": "Confirmed."},
        ),
        investigation_turns=[make_tool_response("search_evidence", {"query": "verifiable claim"})],
    )
    search = FakeEvidenceSearchProvider(responder=lambda q: [SearchResult(title="t", url="https://pib.gov.in/a", snippet="s")])
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS, extracted_text="A verifiable claim."))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, search, media_client, _passed_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-12", source_wamid="wamid.i12", language="en")

    assert outcome.analysis.overall_result == "verified"  # NOT forced to insufficient_evidence
    assert "visual" in outcome.reply_text.lower() or "wasn't available" in outcome.reply_text.lower()


async def test_evidence_tier_guard_still_applies_to_image_derived_claims(db_session, seeded_registry):
    """The deterministic guard (app/agent/classification.py) is reused
    as-is — an LLM overclaiming 'verified' with no qualifying evidence is
    still downgraded, exactly as it is for text claims."""
    llm = FakeLLMClient()
    llm.responder = _TurnScript(
        claims_response=make_tool_response("record_claims", {"claims": [{"text": "An unsupported claim.", "category": "other"}]}),
        synthesis_response=make_tool_response(
            "record_synthesis",
            {"evidence_stances": [], "suggested_result": "verified", "claim_confidence": 0.99, "reasoning_text": "Overclaiming despite no evidence."},
        ),
        investigation_turns=[],
    )
    media_client = FakeMediaClient(responder=lambda mid: (make_sample_jpeg_bytes(), "image/jpeg"))
    ocr_provider = _ocr_provider_returning(OCRResult(status=OCRStatus.SUCCESS, extracted_text="An unsupported claim."))

    user = await _make_user(db_session)
    pipeline = _pipeline(db_session, _settings(), llm, FakeEvidenceSearchProvider(), media_client, _passed_safety_provider(), ocr_provider, _unavailable_image_provider())
    outcome = await pipeline.run(user=user, media_id="media-13", source_wamid="wamid.i13", language="en")

    assert outcome.analysis.overall_result == "unverified"  # guard forced the downgrade
