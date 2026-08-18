"""
HTML content extraction for the URL Analyzer — {title, extracted_text,
publisher, published_at}, per agent-architecture.md's URL Analyzer contract.

Uses BeautifulSoup (stdlib `html.parser` backend, no lxml/C-extension) — the
one new Phase 4 dependency, justified the same way Pillow was justified in
Phase 3: no stdlib-only approach can robustly extract article text/title/
metadata from arbitrary real-world HTML.

Extracted text is deliberately never persisted in full anywhere (see
app/agent/url_pipeline.py) — only used transiently, in memory, to feed the
existing claim-extraction tool, consistent with decisions.md §3's "no
full-text storage of copyrighted articles" applied to the analysis's own
input, not just retrieved evidence.
"""

from bs4 import BeautifulSoup

_TAGS_TO_STRIP = ("script", "style", "noscript", "template", "svg", "iframe")

# Common publisher/site-name meta tag patterns, checked in priority order.
_PUBLISHER_META_CANDIDATES = (
    ("property", "og:site_name"),
    ("name", "application-name"),
    ("name", "publisher"),
    ("name", "author"),
)

# Common published-date meta tag patterns, checked in priority order.
_PUBLISHED_AT_META_CANDIDATES = (
    ("property", "article:published_time"),
    ("name", "publish-date"),
    ("name", "date"),
    ("itemprop", "datePublished"),
)


class PageContent:
    def __init__(
        self,
        title: str | None,
        extracted_text: str,
        publisher: str | None,
        published_at: str | None,
    ) -> None:
        self.title = title
        self.extracted_text = extracted_text
        self.publisher = publisher
        self.published_at = published_at


def _meta_content(soup: BeautifulSoup, attr: str, value: str) -> str | None:
    tag = soup.find("meta", attrs={attr: value})
    if tag is None:
        return None
    content = tag.get("content")
    return content.strip() if content and content.strip() else None


def _extract_title(soup: BeautifulSoup) -> str | None:
    og_title = _meta_content(soup, "property", "og:title")
    if og_title:
        return og_title
    if soup.title and soup.title.string:
        stripped = soup.title.string.strip()
        return stripped or None
    return None


def _extract_publisher(soup: BeautifulSoup) -> str | None:
    for attr, value in _PUBLISHER_META_CANDIDATES:
        found = _meta_content(soup, attr, value)
        if found:
            return found
    return None


def _extract_published_at(soup: BeautifulSoup) -> str | None:
    for attr, value in _PUBLISHED_AT_META_CANDIDATES:
        found = _meta_content(soup, attr, value)
        if found:
            return found
    return None


def extract_page_content(html_bytes: bytes, *, max_text_length_chars: int) -> PageContent:
    """Best-effort extraction — never raises on malformed HTML (BeautifulSoup
    itself is lenient by design); a page with no discoverable title/publisher/
    date simply yields None for those fields, and extracted_text may be "" if
    the page has no body text at all (the caller treats "" the same as "no
    claims detected", the same pattern OCR's NO_TEXT_FOUND uses)."""
    soup = BeautifulSoup(html_bytes, "html.parser")

    for tag_name in _TAGS_TO_STRIP:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    body = soup.body or soup
    text = body.get_text(separator=" ", strip=True)
    text = " ".join(text.split())  # collapse repeated whitespace

    return PageContent(
        title=_extract_title(soup),
        extracted_text=text[:max_text_length_chars],
        publisher=_extract_publisher(soup),
        published_at=_extract_published_at(soup),
    )
