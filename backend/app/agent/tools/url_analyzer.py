"""
URL Analyzer — agent-architecture.md's contract:
{url} -> {title, extracted_text, publisher, published_at, claims[]} (claims[]
are produced by a subsequent, SHARED step — the existing
ClaimExtractionTool / claim_pipeline_shared.py investigation pipeline, fed
extracted_text — exactly the same pattern OCR'd text uses in
app/agent/image_pipeline.py; this tool's own job stops at extraction).

Deliberately NOT a provider/Null-stub pair like OCR/image-analysis — there
is no external, credentialed service being swapped out here. HTML→text
extraction (app/web/html_extraction.py) is a deterministic, local operation,
not a third-party API call, so there's nothing to stub.

The actual network fetch (app/web/fetcher.py's SecureUrlFetcher) is owned by
the CALLER (app/agent/url_pipeline.py), not this tool — the fetched bytes
are shared between this tool and UrlSafetyTool so a URL is only ever fetched
ONCE per analysis (efficiency + politeness to the target site, consistent
with decisions.md §3's robots.txt-respecting policy).
"""

from dataclasses import dataclass
from enum import Enum

from app.web.fetcher import FetchResult
from app.web.html_extraction import extract_page_content

_HTML_CONTENT_TYPE_HINTS = ("text/html", "application/xhtml+xml")


class UrlContentStatus(str, Enum):
    SUCCESS = "success"
    NO_TEXT_FOUND = "no_text_found"
    UNSUPPORTED_CONTENT_TYPE = "unsupported_content_type"  # e.g. PDF, image, video — not attempted
    FETCH_FAILED = "fetch_failed"  # no fetch_result was available at all


@dataclass(frozen=True)
class UrlContentResult:
    status: UrlContentStatus
    title: str | None = None
    extracted_text: str = ""
    publisher: str | None = None
    published_at: str | None = None


def _is_html(content_type: str | None) -> bool:
    if not content_type:
        return False
    return any(hint in content_type.lower() for hint in _HTML_CONTENT_TYPE_HINTS)


class UrlAnalyzerTool:
    def analyze(self, fetch_result: FetchResult | None, *, max_text_length_chars: int) -> UrlContentResult:
        if fetch_result is None:
            return UrlContentResult(status=UrlContentStatus.FETCH_FAILED)

        if not _is_html(fetch_result.content_type):
            return UrlContentResult(status=UrlContentStatus.UNSUPPORTED_CONTENT_TYPE)

        page = extract_page_content(fetch_result.content_bytes, max_text_length_chars=max_text_length_chars)

        if not page.extracted_text:
            return UrlContentResult(
                status=UrlContentStatus.NO_TEXT_FOUND,
                title=page.title,
                publisher=page.publisher,
                published_at=page.published_at,
            )

        return UrlContentResult(
            status=UrlContentStatus.SUCCESS,
            title=page.title,
            extracted_text=page.extracted_text,
            publisher=page.publisher,
            published_at=page.published_at,
        )
