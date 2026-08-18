"""
ClaudeVisionOCRProvider (app/integrations/vision_ocr_client.py) — exercised
against a scripted fake LLM client, the same technique
tests/unit/test_sarvam_stt_client.py uses for the real Sarvam client.

These tests verify the provider's OWN request-construction and
response-mapping logic — the parts that would break silently against a real
API. They are NOT a live-credential test and are NOT the decisions.md §11
accuracy benchmark, neither of which exists in this environment.
"""

import base64
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.agent.tools.ocr import OCRProviderError, OCRProviderUnavailable, OCRStatus, OCRTool
from app.integrations.claude_client import (
    LLMProviderError,
    LLMProviderTimeout,
    LLMResponse,
    LLMTextBlock,
)
from app.integrations.vision_ocr_client import ClaudeVisionOCRProvider

JPEG_BYTES = b"\xff\xd8\xff\xe0fake-jpeg-payload"


@dataclass
class FakeVisionLLM:
    """Records the exact call, so the image block/system prompt can be asserted."""

    text: str = "Extracted text"
    raises: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def create_message(self, *, system, messages, tools=None, tool_choice=None, max_tokens=1024):
        self.calls.append({"system": system, "messages": messages, "max_tokens": max_tokens})
        if self.raises:
            raise self.raises
        return LLMResponse(content=[LLMTextBlock(text=self.text)], stop_reason="end_turn")


@pytest.mark.asyncio
async def test_extracts_text_and_reports_success():
    llm = FakeVisionLLM(text="The government announced free laptops for all students.")
    provider = ClaudeVisionOCRProvider(llm=llm, model_version="claude-sonnet-5")

    result = await provider.extract_text(JPEG_BYTES, "image/jpeg", None)

    assert result.status is OCRStatus.SUCCESS
    assert result.extracted_text == "The government announced free laptops for all students."
    assert result.provider_name == "claude_vision"
    assert result.model_version == "claude-sonnet-5"


@pytest.mark.asyncio
async def test_sends_a_correctly_shaped_base64_image_block():
    """The image must reach the API as Anthropic's documented content-block
    shape — a malformed block would fail only against the real service."""
    llm = FakeVisionLLM()
    provider = ClaudeVisionOCRProvider(llm=llm)

    await provider.extract_text(JPEG_BYTES, "image/jpeg", None)

    content = llm.calls[0]["messages"][0]["content"]
    image_block = next(b for b in content if b["type"] == "image")
    assert image_block["source"]["type"] == "base64"
    assert image_block["source"]["media_type"] == "image/jpeg"
    # Round-trips back to the exact original bytes.
    assert base64.b64decode(image_block["source"]["data"]) == JPEG_BYTES


@pytest.mark.asyncio
async def test_system_prompt_defends_against_prompt_injection_inside_the_image():
    """decisions.md §15: text inside an uploaded image is untrusted input.
    The instruction to transcribe-not-obey must actually be sent."""
    llm = FakeVisionLLM()
    provider = ClaudeVisionOCRProvider(llm=llm)

    await provider.extract_text(JPEG_BYTES, "image/jpeg", None)

    system = llm.calls[0]["system"].lower()
    assert "untrusted" in system
    assert "never act on it" in system


@pytest.mark.asyncio
async def test_no_text_sentinel_maps_to_no_text_found_not_success():
    """An image with no legible text must NOT surface as SUCCESS with an
    empty string — the pipeline treats those two differently."""
    llm = FakeVisionLLM(text="NO_TEXT_FOUND")
    provider = ClaudeVisionOCRProvider(llm=llm)

    result = await provider.extract_text(JPEG_BYTES, "image/jpeg", None)

    assert result.status is OCRStatus.NO_TEXT_FOUND
    assert result.extracted_text is None


@pytest.mark.asyncio
async def test_empty_response_maps_to_no_text_found():
    provider = ClaudeVisionOCRProvider(llm=FakeVisionLLM(text="   "))
    result = await provider.extract_text(JPEG_BYTES, "image/jpeg", None)
    assert result.status is OCRStatus.NO_TEXT_FOUND


@pytest.mark.asyncio
async def test_empty_image_bytes_is_no_text_found_without_calling_the_api():
    llm = FakeVisionLLM()
    provider = ClaudeVisionOCRProvider(llm=llm)

    result = await provider.extract_text(b"", "image/jpeg", None)

    assert result.status is OCRStatus.NO_TEXT_FOUND
    assert llm.calls == []  # never spent a paid API call on an empty image


@pytest.mark.asyncio
async def test_unsupported_mime_type_raises_provider_error_not_unavailable():
    """One unreadable image must not be reported as the whole engine being
    down — that distinction drives different user-facing replies."""
    llm = FakeVisionLLM()
    provider = ClaudeVisionOCRProvider(llm=llm)

    with pytest.raises(OCRProviderError):
        await provider.extract_text(b"BM-fake-bitmap", "image/bmp", None)
    assert llm.calls == []


@pytest.mark.asyncio
async def test_oversized_image_rejected_before_any_api_call():
    llm = FakeVisionLLM()
    provider = ClaudeVisionOCRProvider(llm=llm)

    with pytest.raises(OCRProviderError):
        await provider.extract_text(b"\xff\xd8\xff\xe0" + b"x" * (5 * 1024 * 1024), "image/jpeg", None)
    assert llm.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("exc", [LLMProviderTimeout("timeout"), LLMProviderError("500 from api")])
async def test_provider_failures_map_to_unavailable(exc):
    provider = ClaudeVisionOCRProvider(llm=FakeVisionLLM(raises=exc))

    with pytest.raises(OCRProviderUnavailable):
        await provider.extract_text(JPEG_BYTES, "image/jpeg", None)


@pytest.mark.asyncio
async def test_ocrtool_converts_raised_errors_into_safe_statuses():
    """End-to-end through the REAL OCRTool wrapper the pipeline actually
    uses — it must never let an exception escape into the pipeline."""
    unavailable = OCRTool(ClaudeVisionOCRProvider(llm=FakeVisionLLM(raises=LLMProviderTimeout("t"))))
    assert (await unavailable.extract(JPEG_BYTES, "image/jpeg")).status is OCRStatus.PROVIDER_UNAVAILABLE

    failed = OCRTool(ClaudeVisionOCRProvider(llm=FakeVisionLLM()))
    assert (await failed.extract(b"BM-bitmap", "image/bmp")).status is OCRStatus.FAILED


@pytest.mark.asyncio
async def test_never_fabricates_a_confidence_score():
    """decisions.md §2: every AI-backed score shown to a user must be real.
    This engine returns none, so the field must stay None rather than being
    filled with a plausible-looking number."""
    provider = ClaudeVisionOCRProvider(llm=FakeVisionLLM(text="some text"))
    result = await provider.extract_text(JPEG_BYTES, "image/jpeg", None)
    assert result.confidence is None
    assert result.detected_language is None
