from app.agent.url_detection import extract_bare_url


class TestBareUrlDetection:
    def test_plain_https_url_is_detected(self):
        assert extract_bare_url("https://example.com/article") == "https://example.com/article"

    def test_plain_http_url_is_detected(self):
        assert extract_bare_url("http://example.com/article") == "http://example.com/article"

    def test_surrounding_whitespace_is_stripped(self):
        assert extract_bare_url("  https://example.com  \n") == "https://example.com"

    def test_url_with_trailing_other_text_is_not_bare(self):
        assert extract_bare_url("Is this true? https://example.com/article") is None

    def test_url_with_leading_other_text_is_not_bare(self):
        assert extract_bare_url("https://example.com/article — is this real?") is None

    def test_plain_claim_text_is_not_a_url(self):
        assert extract_bare_url("The scheme was announced yesterday.") is None

    def test_empty_string_is_not_a_url(self):
        assert extract_bare_url("") is None

    def test_none_is_not_a_url(self):
        assert extract_bare_url(None) is None

    def test_non_http_scheme_is_rejected(self):
        assert extract_bare_url("ftp://example.com/file") is None
        assert extract_bare_url("javascript:alert(1)") is None
        assert extract_bare_url("file:///etc/passwd") is None

    def test_url_with_no_host_is_rejected(self):
        assert extract_bare_url("https://") is None

    def test_url_with_internal_tab_or_newline_is_not_bare(self):
        assert extract_bare_url("https://example.com\nhttps://example.com") is None
