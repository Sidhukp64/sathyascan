"""
HTTP-level webhook tests. Phase 1 mechanics (signature, idempotency,
rate-limit, GET verification, status events) are deliberately exercised with
DOCUMENT messages, not text or image — those mechanics are message-type-
agnostic, and using document payloads keeps these tests decoupled from both
the Phase 2 text pipeline and the Phase 3 image pipeline entirely (no fake
scripting needed just to prove idempotency works). Documents remain Phase 1
behavior — out of scope, unaffected by either later phase.

Dedicated tests at the bottom wire the fakes and prove the text (Phase 2),
image (Phase 3), URL (Phase 4), audio (Phase 5a), and video (Phase 5b)
pipelines are each actually invoked end-to-end through the real HTTP path —
including the Phase 4 routing decision itself (a bare-URL text message goes
to UrlPipeline; a URL alongside other commentary stays on TextPipeline, per
app/agent/url_detection.py's documented scope boundary).
"""

import hashlib
import hmac
import json
from unittest.mock import patch

from app.agent.tools.ocr import OCRResult, OCRStatus
from app.agent.tools.speech_to_text import STTResult, STTStatus
from tests.conftest import (
    TEST_APP_SECRET,
    TEST_VERIFY_TOKEN,
    make_sample_jpeg_bytes,
    make_sample_mp4_bytes,
    make_sample_ogg_bytes,
    make_text_response,
    make_tool_response,
)


def _ocr_success(text: str) -> OCRResult:
    return OCRResult(status=OCRStatus.SUCCESS, extracted_text=text, provider_name="fake")


def _sign(body: bytes) -> str:
    digest = hmac.new(TEST_APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def _post_webhook(client, payload: dict, sign: bool = True):
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        headers["X-Hub-Signature-256"] = _sign(body)
    return await client.post("/webhook/whatsapp", content=body, headers=headers)


def _document_message_payload(wamid: str, from_phone: str = "15551234567") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-id",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": wamid,
                                    "type": "document",
                                    "document": {"id": "media-doc", "mime_type": "application/pdf"},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _image_message_payload(wamid: str, from_phone: str = "15551234567") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-id",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": wamid,
                                    "type": "image",
                                    "image": {"id": "media-abc", "mime_type": "image/jpeg"},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _audio_message_payload(wamid: str, from_phone: str = "15551234567") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-id",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": wamid,
                                    "type": "audio",
                                    "audio": {"id": "media-voice", "mime_type": "audio/ogg"},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _video_message_payload(wamid: str, from_phone: str = "15551234567") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-id",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": wamid,
                                    "type": "video",
                                    "video": {"id": "media-video", "mime_type": "video/mp4"},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


def _text_message_payload(wamid: str, from_phone: str = "15551234567", text: str = "hello") -> dict:
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-id",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {"phone_number_id": "1234567890"},
                            "messages": [
                                {
                                    "from": from_phone,
                                    "id": wamid,
                                    "timestamp": "1699999999",
                                    "type": "text",
                                    "text": {"body": text},
                                }
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }


async def test_health_check(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] in {"ok", "degraded"}  # degraded if Postgres genuinely unreachable — DB here is SQLite via override, Redis is faked


async def test_health_reports_null_provider_state_when_no_credentials_configured(client):
    """Phase 5 final-validation addition: /health must honestly report
    which of STT/audio-forensics/video-forensics are backed by a real
    provider vs the Null stub. In this test environment (and in this
    session's actual deployment) no real credentials are configured, so
    all three must read "null" — never silently "real" just because the
    provider CLASSES exist in the codebase."""
    response = await client.get("/health")
    body = response.json()
    assert body["speech_to_text_provider"] == "null"
    assert body["audio_forensics_provider"] == "null"
    assert body["video_forensics_provider"] == "null"


async def test_health_reports_secret_configuration_state(client, monkeypatch):
    """Phase 7 addition: /health's config-readiness flags read the REAL
    global Settings() (no .env file exists in this sandbox), not the
    per-request test_settings override /health deliberately bypasses (same
    established pattern jwt_configured/safety_gate_provider_configured
    already use) — so in this environment every one of these correctly
    reads False, proving the flags are live-introspected, not hardcoded."""
    monkeypatch.setenv("JWT_SECRET", "")
    monkeypatch.setenv("PHONE_HASH_PEPPER", "")
    monkeypatch.setenv("PHONE_ENCRYPTION_KEY", "")
    from app.core.config import get_settings
    get_settings.cache_clear()
    try:
        response = await client.get("/health")
        body = response.json()
        assert body["jwt_configured"] is False
        assert body["phone_hash_pepper_configured"] is False
        assert body["phone_encryption_key_configured"] is False
        assert body["phase"] == 9
    finally:
        get_settings.cache_clear()


async def test_get_verification_success(client):
    response = await client.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": TEST_VERIFY_TOKEN, "hub.challenge": "12345"},
    )
    assert response.status_code == 200
    assert response.text == "12345"


async def test_get_verification_wrong_token_rejected(client):
    response = await client.get(
        "/webhook/whatsapp",
        params={"hub.mode": "subscribe", "hub.verify_token": "wrong-token", "hub.challenge": "12345"},
    )
    assert response.status_code == 403


async def test_post_invalid_signature_is_rejected(client, fake_sender):
    response = await _post_webhook(client, _document_message_payload("wamid.BAD_SIG"), sign=False)
    assert response.status_code == 403
    assert fake_sender.sent == []


async def test_duplicate_wamid_is_not_replied_to_twice(client, fake_sender):
    payload = _document_message_payload("wamid.DUP1")

    r1 = await _post_webhook(client, payload)
    r2 = await _post_webhook(client, payload)

    assert r1.status_code == 200
    assert r2.status_code == 200
    assert len(fake_sender.sent) == 1  # second delivery was deduped


async def test_rate_limit_is_enforced_per_sender(client, fake_sender):
    # test_settings fixture sets RATE_LIMIT_PER_USER_PER_MINUTE=3
    for i in range(3):
        await _post_webhook(client, _document_message_payload(f"wamid.RL{i}", from_phone="15559990000"))

    over_limit_response = await _post_webhook(client, _document_message_payload("wamid.RL3", from_phone="15559990000"))

    assert over_limit_response.status_code == 200
    assert len(fake_sender.sent) == 4  # 3 document replies + 1 rate-limit notice
    assert "too quickly" in fake_sender.sent[-1][2]


async def test_document_message_gets_not_analyzed_yet_reply(client, fake_sender):
    """Documents (and audio/video/stickers) are unchanged from Phase 1 — out
    of scope for both Phase 2 (text) and Phase 3 (image)."""
    response = await _post_webhook(client, _document_message_payload("wamid.DOC1", from_phone="15551237777"))

    assert response.status_code == 200
    assert len(fake_sender.sent) == 1
    reply_text = fake_sender.sent[0][2]
    assert "document" in reply_text
    assert "isn't available yet" in reply_text


async def test_status_event_is_acknowledged_but_not_replied_to(client, fake_sender):
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "waba-id",
                "changes": [
                    {
                        "value": {
                            "messaging_product": "whatsapp",
                            "statuses": [
                                {"id": "wamid.STATUS", "status": "delivered", "recipient_id": "15551234567"}
                            ],
                        },
                        "field": "messages",
                    }
                ],
            }
        ],
    }

    response = await _post_webhook(client, payload)

    assert response.status_code == 200
    assert response.json()["message_count"] == 0
    assert fake_sender.sent == []


async def test_text_message_is_routed_through_the_real_pipeline_end_to_end(client, fake_sender, fake_llm):
    """Phase 2's key behavior change from Phase 1: a text message no longer
    gets an echo — it goes through claim extraction (no evidence configured
    here, so it resolves quickly) and the caller gets TWO messages: the
    immediate ack, then the real result."""

    def responder(ctx):
        if ctx.forced_tool_name == "record_claims":
            return make_tool_response(
                "record_claims", {"claims": [{"text": "The sky is blue.", "category": "other"}]}
            )
        if ctx.forced_tool_name == "record_synthesis":
            return make_tool_response(
                "record_synthesis",
                {
                    "evidence_stances": [],
                    "suggested_result": "unverified",
                    "claim_confidence": 0.2,
                    "reasoning_text": "No evidence was available to check this claim.",
                },
            )
        # investigation loop turn: signal "done, no more searches needed"
        return make_text_response()

    fake_llm.responder = responder

    response = await _post_webhook(client, _text_message_payload("wamid.TXT1", text="The sky is blue."))

    assert response.status_code == 200
    assert len(fake_sender.sent) == 2  # ack + result
    assert "Analyzing" in fake_sender.sent[0][2] or "🔍" in fake_sender.sent[0][2]
    assert "Unverified" in fake_sender.sent[1][2] or "unverified" in fake_sender.sent[1][2].lower()


async def test_image_message_is_routed_through_the_real_pipeline_end_to_end(
    client, fake_sender, fake_llm, fake_media_client, fake_ocr_provider
):
    """Phase 3's key behavior change from Phase 1: an image message no
    longer gets "not analyzed yet" — it goes through download, safety gate
    (default fake: PASSED), OCR (scripted here to succeed), and the same
    claim pipeline text messages use. Caller gets TWO messages: the image
    ack, then the real result."""
    fake_media_client.responder = lambda media_id: (make_sample_jpeg_bytes(), "image/jpeg")
    fake_ocr_provider.responder = lambda b, m, lang: _ocr_success("The sky is blue.")

    def responder(ctx):
        if ctx.forced_tool_name == "record_claims":
            return make_tool_response(
                "record_claims", {"claims": [{"text": "The sky is blue.", "category": "other"}]}
            )
        if ctx.forced_tool_name == "record_synthesis":
            return make_tool_response(
                "record_synthesis",
                {
                    "evidence_stances": [],
                    "suggested_result": "unverified",
                    "claim_confidence": 0.2,
                    "reasoning_text": "No evidence was available to check this claim.",
                },
            )
        return make_text_response()

    fake_llm.responder = responder

    response = await _post_webhook(client, _image_message_payload("wamid.IMG1"))

    assert response.status_code == 200
    assert len(fake_sender.sent) == 2  # image ack + result
    assert "image" in fake_sender.sent[0][2].lower() or "🔍" in fake_sender.sent[0][2]
    assert "Unverified" in fake_sender.sent[1][2] or "unverified" in fake_sender.sent[1][2].lower()


async def test_bare_url_message_is_routed_to_url_pipeline_end_to_end(
    client, fake_sender, fake_llm, fake_url_fetcher
):
    """Phase 4's key behavior change: a text message whose ENTIRE body is a
    single URL no longer goes through plain claim extraction — it's routed
    to UrlPipeline instead (app/agent/url_detection.py). Caller gets TWO
    messages: the link-checking ack, then the combined safety+credibility
    result."""

    def responder(ctx):
        if ctx.forced_tool_name == "record_claims":
            return make_tool_response(
                "record_claims", {"claims": [{"text": "The scheme was announced.", "category": "gov_scheme"}]}
            )
        if ctx.forced_tool_name == "record_synthesis":
            return make_tool_response(
                "record_synthesis",
                {
                    "evidence_stances": [],
                    "suggested_result": "unverified",
                    "claim_confidence": 0.2,
                    "reasoning_text": "No evidence was available to check this claim.",
                },
            )
        return make_text_response()

    fake_llm.responder = responder

    with patch("app.agent.url_pipeline.resolve_and_check_hostname", return_value=["93.184.216.34"]):
        response = await _post_webhook(client, _text_message_payload("wamid.URL1", text="https://example.com/article"))

    assert response.status_code == 200
    assert len(fake_sender.sent) == 2  # link-checking ack + result
    assert "link" in fake_sender.sent[0][2].lower() or "🔍" in fake_sender.sent[0][2]
    assert "Link Safety" in fake_sender.sent[1][2] or "🛡️" in fake_sender.sent[1][2]
    assert "Unverified" in fake_sender.sent[1][2] or "unverified" in fake_sender.sent[1][2].lower()


async def test_url_with_surrounding_commentary_stays_on_text_pipeline(client, fake_sender, fake_llm):
    """Scope boundary confirmed during the Phase 4 review: a message that
    merely MENTIONS a URL alongside other text is NOT routed to UrlPipeline
    — it stays on the plain Phase 2 text-claim path, unchanged."""
    fake_llm.responder = lambda ctx: make_tool_response("record_claims", {"claims": []})

    response = await _post_webhook(
        client, _text_message_payload("wamid.URL2", text="Is this true? https://example.com/article")
    )

    assert response.status_code == 200
    assert len(fake_sender.sent) == 2  # regular text ack + result
    assert "Link Safety" not in fake_sender.sent[1][2]  # never entered UrlPipeline
    assert "🛡️" not in fake_sender.sent[1][2]


async def test_audio_message_is_routed_through_the_real_pipeline_end_to_end(
    client, fake_sender, fake_llm, fake_audio_media_client, fake_stt_provider
):
    """Phase 5a's key behavior change: an audio message no longer gets "not
    analyzed yet" — it goes through download, safety gate (default fake:
    PASSED), transcription (scripted here to succeed), and the same claim
    pipeline text messages use. Caller gets TWO messages: the link-checking
    ack, then the real result, including a distinct media-authenticity block."""
    fake_audio_media_client.responder = lambda media_id: (make_sample_ogg_bytes(), "audio/ogg")
    fake_stt_provider.responder = lambda b, m, lang: STTResult(
        status=STTStatus.SUCCESS, transcript="The sky is blue.", detected_language="en", provider_name="fake"
    )

    def responder(ctx):
        if ctx.forced_tool_name == "record_claims":
            return make_tool_response(
                "record_claims", {"claims": [{"text": "The sky is blue.", "category": "other"}]}
            )
        if ctx.forced_tool_name == "record_synthesis":
            return make_tool_response(
                "record_synthesis",
                {
                    "evidence_stances": [],
                    "suggested_result": "unverified",
                    "claim_confidence": 0.2,
                    "reasoning_text": "No evidence was available to check this claim.",
                },
            )
        return make_text_response()

    fake_llm.responder = responder

    response = await _post_webhook(client, _audio_message_payload("wamid.AUD1"))

    assert response.status_code == 200
    assert len(fake_sender.sent) == 2  # audio ack + result
    assert "voice" in fake_sender.sent[0][2].lower() or "🔍" in fake_sender.sent[0][2]
    assert "Unverified" in fake_sender.sent[1][2] or "unverified" in fake_sender.sent[1][2].lower()
    assert "AI-generation detection unavailable" in fake_sender.sent[1][2]  # media authenticity, always shown


async def test_video_message_is_routed_through_the_real_pipeline_end_to_end(
    client, fake_sender, fake_llm, fake_video_media_client, fake_stt_provider
):
    """Phase 5b's key behavior change: a video message no longer gets "not
    analyzed yet" — it goes through download, safety gate (default fake:
    PASSED), audio-track extraction + transcription (scripted here to
    succeed), key-frame extraction + OCR (default fake: PROVIDER_UNAVAILABLE,
    so this exercises the audio-only-modality path), and the same claim
    pipeline text/audio messages use. Caller gets TWO messages: the
    analyzing ack, then the real result, including a distinct media-
    authenticity block and the explicit independence note."""
    fake_video_media_client.responder = lambda media_id: (make_sample_mp4_bytes(duration_seconds=2), "video/mp4")
    fake_stt_provider.responder = lambda b, m, lang: STTResult(
        status=STTStatus.SUCCESS, transcript="The sky is blue.", detected_language="en", provider_name="fake"
    )

    def responder(ctx):
        if ctx.forced_tool_name == "record_claims":
            return make_tool_response(
                "record_claims", {"claims": [{"text": "The sky is blue.", "category": "other"}]}
            )
        if ctx.forced_tool_name == "record_synthesis":
            return make_tool_response(
                "record_synthesis",
                {
                    "evidence_stances": [],
                    "suggested_result": "unverified",
                    "claim_confidence": 0.2,
                    "reasoning_text": "No evidence was available to check this claim.",
                },
            )
        return make_text_response()

    fake_llm.responder = responder

    response = await _post_webhook(client, _video_message_payload("wamid.VID1"))

    assert response.status_code == 200
    assert len(fake_sender.sent) == 2  # video ack + result
    assert "video" in fake_sender.sent[0][2].lower() or "🔍" in fake_sender.sent[0][2]
    assert "SathyaScan Video Result" in fake_sender.sent[1][2]
    assert "The sky is blue." in fake_sender.sent[1][2]
    assert "Unverified" in fake_sender.sent[1][2] or "unverified" in fake_sender.sent[1][2].lower()
    assert "AI-generation detection unavailable" in fake_sender.sent[1][2]  # media authenticity, always shown
    assert "independent results" in fake_sender.sent[1][2]  # the explicit non-conflation note
