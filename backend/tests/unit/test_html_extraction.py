from app.web.html_extraction import extract_page_content


class TestTitleExtraction:
    def test_prefers_og_title_over_title_tag(self):
        html = b'<html><head><title>Fallback</title><meta property="og:title" content="Real Title"></head><body></body></html>'
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.title == "Real Title"

    def test_falls_back_to_title_tag(self):
        html = b"<html><head><title>Plain Title</title></head><body></body></html>"
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.title == "Plain Title"

    def test_missing_title_is_none(self):
        html = b"<html><head></head><body><p>text</p></body></html>"
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.title is None


class TestPublisherAndDateExtraction:
    def test_og_site_name_is_publisher(self):
        html = b'<html><head><meta property="og:site_name" content="Example News"></head><body></body></html>'
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.publisher == "Example News"

    def test_article_published_time_is_extracted(self):
        html = (
            b'<html><head><meta property="article:published_time" content="2026-08-01T10:00:00Z">'
            b"</head><body></body></html>"
        )
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.published_at == "2026-08-01T10:00:00Z"

    def test_missing_publisher_and_date_are_none(self):
        html = b"<html><head></head><body><p>text</p></body></html>"
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.publisher is None
        assert result.published_at is None


class TestBodyTextExtraction:
    def test_script_and_style_tags_are_stripped(self):
        html = (
            b"<html><body><script>malicious();</script><style>.x{color:red}</style>"
            b"<p>The real claim text.</p></body></html>"
        )
        result = extract_page_content(html, max_text_length_chars=5000)
        assert "malicious" not in result.extracted_text
        assert "color:red" not in result.extracted_text
        assert "The real claim text." in result.extracted_text

    def test_whitespace_is_collapsed(self):
        html = b"<html><body><p>Hello   \n\n   world.</p></body></html>"
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.extracted_text == "Hello world."

    def test_text_is_truncated_to_max_length(self):
        html = b"<html><body><p>" + b"a" * 100 + b"</p></body></html>"
        result = extract_page_content(html, max_text_length_chars=10)
        assert len(result.extracted_text) == 10

    def test_page_with_no_body_text_yields_empty_string(self):
        html = b"<html><head><title>Empty</title></head><body></body></html>"
        result = extract_page_content(html, max_text_length_chars=5000)
        assert result.extracted_text == ""

    def test_malformed_html_never_raises(self):
        html = b"<html><body><p>Unclosed paragraph <div>nested weirdly</p></body>"
        result = extract_page_content(html, max_text_length_chars=5000)
        assert "Unclosed paragraph" in result.extracted_text
