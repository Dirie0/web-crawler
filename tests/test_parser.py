"""Tests for HTML parsing."""
from crawler.parser import extract_links


class TestExtractLinks:
    def test_basic_absolute_link(self):
        html = '<a href="http://example.com/about">About</a>'
        assert extract_links(html, "http://example.com/") == ["http://example.com/about"]

    def test_relative_link_resolved(self):
        html = '<a href="/contact">Contact</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/contact"]

    def test_dotdot_resolved(self):
        html = '<a href="../pricing">Pricing</a>'
        result = extract_links(html, "http://example.com/docs/guide/")
        assert result == ["http://example.com/docs/pricing"]

    def test_fragment_stripped(self):
        html = '<a href="/about#team">About</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/about"]

    def test_dedup_first_seen_order(self):
        html = '''
        <a href="/a">A</a>
        <a href="/b">B</a>
        <a href="/a">A again</a>
        '''
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/a", "http://example.com/b"]

    def test_skips_empty_href(self):
        html = '<a href="">Empty</a><a href="/page">Page</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/page"]

    def test_skips_mailto(self):
        html = '<a href="mailto:user@example.com">Mail</a>'
        result = extract_links(html, "http://example.com/")
        assert result == []

    def test_skips_javascript(self):
        html = '<a href="javascript:void(0)">Click</a>'
        result = extract_links(html, "http://example.com/")
        assert result == []

    def test_base_href_honoured(self):
        html = '<base href="http://example.com/docs/"><a href="guide">Guide</a>'
        result = extract_links(html, "http://other.com/")
        assert result == ["http://example.com/docs/guide"]

    def test_out_of_scope_links_included(self):
        # extract_links returns all normalised URLs, scope filtering is not its job
        html = '<a href="http://other.com/page">Other</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://other.com/page"]

    def test_query_strings_kept(self):
        html = '<a href="/search?q=hello">Search</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/search?q=hello"]

    def test_tolerates_malformed_html(self):
        html = "<html><body><a href='/ok'>OK</a><b unclosed"
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/ok"]

    def test_ignores_non_anchor_tags(self):
        html = '<img src="/image.png"><link href="/style.css"><a href="/page">Page</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/page"]

    def test_no_links(self):
        html = "<html><body><p>No links here</p></body></html>"
        result = extract_links(html, "http://example.com/")
        assert result == []

    def test_anchor_without_href(self):
        html = '<a name="section">Anchor</a><a href="/page">Page</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/page"]

    def test_trailing_slash_preserved(self):
        html = '<a href="http://example.com/about/">About</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/about/"]

    def test_base_href_relative(self):
        # <base href> can itself be relative to base_url
        html = '<base href="/docs/"><a href="guide">Guide</a>'
        result = extract_links(html, "http://example.com/")
        assert result == ["http://example.com/docs/guide"]
