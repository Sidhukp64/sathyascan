"""
OCR and image-analysis tool tests (app/agent/tools/{ocr,image_analysis}.py).

OCR failure states are modeled explicitly and must never silently collapse
to "no text found" — see OCRStatus and the tests below distinguishing each
state. "limit_reached" specifically is decided by the orchestrator BEFORE
calling the OCR tool at all (see test_image_pipeline.py's budget tests) —
OCRTool itself only ever returns success/no_text_found/failed/
provider_unavailable, which is what's tested here.
"""

from app.agent.tools.image_analysis import (
    ImageAnalysisResult,
    ImageAnalysisStatus,
    ImageAnalysisTool,
    NullImageForensicsProvider,
)
from app.agent.tools.ocr import NullOCRProvider, OCRResult, OCRStatus, OCRTool


class _SuccessProvider:
    provider_name = "success_provider"

    async def extract_text(self, image_bytes, mime_type, language_hint):
        return OCRResult(
            status=OCRStatus.SUCCESS,
            extracted_text="The government announced a scheme.",
            detected_language=language_hint,
            confidence=0.92,
            provider_name=self.provider_name,
            model_version="v1",
        )


class _NoTextProvider:
    provider_name = "no_text_provider"

    async def extract_text(self, image_bytes, mime_type, language_hint):
        return OCRResult(status=OCRStatus.NO_TEXT_FOUND, extracted_text="", provider_name=self.provider_name)


class _FailingProvider:
    provider_name = "failing_provider"

    async def extract_text(self, image_bytes, mime_type, language_hint):
        raise RuntimeError("simulated OCR provider crash")


class _UnavailableRaisingProvider:
    provider_name = "unavailable_provider"

    async def extract_text(self, image_bytes, mime_type, language_hint):
        from app.agent.tools.ocr import OCRProviderUnavailable

        raise OCRProviderUnavailable("service down")


class TestOCRTool:
    async def test_successful_ocr_returns_extracted_text(self):
        tool = OCRTool(_SuccessProvider())
        result = await tool.extract(b"fake image bytes", "image/jpeg", language_hint="en")
        assert result.status == OCRStatus.SUCCESS
        assert result.extracted_text == "The government announced a scheme."
        assert result.provider_name == "success_provider"
        assert result.model_version == "v1"

    async def test_no_useful_text_is_distinct_from_success_with_empty_text(self):
        tool = OCRTool(_NoTextProvider())
        result = await tool.extract(b"fake image bytes", "image/jpeg")
        assert result.status == OCRStatus.NO_TEXT_FOUND

    async def test_provider_exception_maps_to_failed_not_no_text_found(self):
        """Critical requirement: OCR failure must never silently become
        'no text' — a crashing provider must map to FAILED, a status
        explicitly distinct from NO_TEXT_FOUND."""
        tool = OCRTool(_FailingProvider())
        result = await tool.extract(b"fake image bytes", "image/jpeg")
        assert result.status == OCRStatus.FAILED
        assert result.status != OCRStatus.NO_TEXT_FOUND

    async def test_provider_unavailable_is_its_own_distinct_state(self):
        tool = OCRTool(_UnavailableRaisingProvider())
        result = await tool.extract(b"fake image bytes", "image/jpeg")
        assert result.status == OCRStatus.PROVIDER_UNAVAILABLE
        assert result.status not in {OCRStatus.NO_TEXT_FOUND, OCRStatus.SUCCESS}

    async def test_null_provider_reports_unavailable_not_fabricated_no_text(self):
        """decisions.md §11: no engine has been benchmark-selected — the
        stub must say so honestly (provider_unavailable), never pretend the
        image was actually scanned and found empty."""
        tool = OCRTool(NullOCRProvider())
        result = await tool.extract(b"fake image bytes", "image/jpeg")
        assert result.status == OCRStatus.PROVIDER_UNAVAILABLE

    async def test_language_hint_is_passed_through_to_provider(self):
        tool = OCRTool(_SuccessProvider())
        result = await tool.extract(b"fake image bytes", "image/jpeg", language_hint="ml")
        assert result.detected_language == "ml"


class TestImageAnalysisTool:
    async def test_null_provider_reports_unavailable(self):
        tool = ImageAnalysisTool(NullImageForensicsProvider())
        result = await tool.analyze(b"fake image bytes", "image/jpeg")
        assert result.status == ImageAnalysisStatus.UNAVAILABLE
        assert result.ai_generated_probability is None  # never fabricated

    async def test_provider_exception_maps_to_failed(self):
        class _Raising:
            provider_name = "raising"

            async def analyze(self, image_bytes, mime_type):
                raise RuntimeError("boom")

        tool = ImageAnalysisTool(_Raising())
        result = await tool.analyze(b"bytes", "image/jpeg")
        assert result.status == ImageAnalysisStatus.FAILED

    async def test_successful_result_carries_provider_and_version(self):
        class _Success:
            provider_name = "hive"

            async def analyze(self, image_bytes, mime_type):
                return ImageAnalysisResult(
                    status=ImageAnalysisStatus.SUCCESS,
                    ai_generated_probability=0.87,
                    provider_name="hive",
                    model_version="v3.2",
                )

        tool = ImageAnalysisTool(_Success())
        result = await tool.analyze(b"bytes", "image/jpeg")
        assert result.status == ImageAnalysisStatus.SUCCESS
        assert result.provider_name == "hive"
        assert result.model_version == "v3.2"
