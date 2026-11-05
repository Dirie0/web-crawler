"""Tests for URL normalization and scope checking."""
from crawler.urls import is_in_scope, normalise


class TestNormalise:
    def test_returns_none_for_empty(self):
        assert normalise("") is None

    def test_returns_none_for_mailto(self):
        assert normalise("mailto:user@example.com") is None

    def test_returns_none_for_tel(self):
        assert normalise("tel:+441234567890") is None

    def test_returns_none_for_javascript(self):
        assert normalise("javascript:void(0)") is None

    def test_absolute_http(self):
        assert normalise("http://example.com/about") == "http://example.com/about"

    def test_lowercase_scheme_and_host(self):
        assert normalise("HTTPS://Example.COM:443/Shop") == "https://example.com/Shop"

    def test_drop_default_port_443(self):
        assert normalise("https://example.com:443/path") == "https://example.com/path"

    def test_drop_default_port_80(self):
        assert normalise("http://example.com:80/path") == "http://example.com/path"

    def test_keep_non_default_port(self):
        assert normalise("http://example.com:8080/path") == "http://example.com:8080/path"

    def test_strip_fragment(self):
        assert normalise("http://example.com/about#team") == "http://example.com/about"

    def test_fragment_stripped_keeps_path(self):
        # /about#team and /about should both normalise to /about
        assert normalise("http://example.com/about#team") == normalise("http://example.com/about")

    def test_keep_query_string(self):
        assert normalise("http://example.com/search?q=a") == "http://example.com/search?q=a"

    def test_query_strings_distinct(self):
        a = normalise("http://example.com/search?q=a")
        b = normalise("http://example.com/search?q=b")
        assert a != b

    def test_trailing_slash_preserved(self):
        assert normalise("http://example.com/about/") == "http://example.com/about/"

    def test_resolve_relative_no_base(self):
        # relative URL with no base returns None
        assert normalise("/about") is None

    def test_resolve_relative_with_base(self):
        assert normalise("/about", base="http://example.com/") == "http://example.com/about"

    def test_resolve_dotdot_segments(self):
        result = normalise("../pricing", base="http://example.com/docs/guide/")
        assert result == "http://example.com/docs/pricing"

    def test_resolve_relative_query(self):
        result = normalise("?q=b", base="http://example.com/search")
        assert result == "http://example.com/search?q=b"

    def test_path_case_preserved(self):
        result = normalise("http://example.com/Products/Item")
        assert result == "http://example.com/Products/Item"

    def test_returns_none_for_non_http_scheme(self):
        assert normalise("ftp://example.com/file") is None


class TestIsInScope:
    def test_exact_host_match(self):
        assert is_in_scope("http://example.com/about", "example.com") is True

    def test_different_host(self):
        assert is_in_scope("http://other.com/about", "example.com") is False

    def test_subdomain_is_out_of_scope(self):
        assert is_in_scope("http://sub.example.com/about", "example.com") is False

    def test_www_variant_out_of_scope(self):
        assert is_in_scope("http://www.example.com/about", "example.com") is False

    def test_bare_vs_www_out_of_scope(self):
        assert is_in_scope("http://example.com/about", "www.example.com") is False

    def test_different_tld_out_of_scope(self):
        assert is_in_scope("http://example.org/about", "example.com") is False

    def test_https_in_scope(self):
        assert is_in_scope("https://example.com/page", "example.com") is True

    def test_non_http_scheme_out_of_scope(self):
        assert is_in_scope("ftp://example.com/file", "example.com") is False

    def test_mailto_out_of_scope(self):
        assert is_in_scope("mailto:user@example.com", "example.com") is False
