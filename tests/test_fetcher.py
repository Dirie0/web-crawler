"""Tests for HTTP fetching."""
import httpx
import pytest

from crawler.fetcher import fetch
from crawler.models import FetchStatus

pytestmark = pytest.mark.asyncio

SCOPE = "example.com"
URL = "http://example.com/page"


def mock_client(*responses):
    """AsyncClient backed by a fixed sequence of (status, headers, body) tuples or exceptions."""
    items = list(responses)
    index = [0]

    def handler(request):
        if index[0] >= len(items):
            raise AssertionError(
                f"Unexpected request to {request.url} — only {len(items)} response(s) queued"
            )
        item = items[index[0]]
        index[0] += 1
        if isinstance(item, BaseException):
            raise item
        status, headers, body = item
        return httpx.Response(status, headers=headers, content=body.encode())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def html_resp(body="<html>ok</html>"):
    return 200, {"content-type": "text/html; charset=utf-8"}, body


def plain_resp():
    return 200, {"content-type": "text/plain"}, "hello"


def redir(code, location):
    return code, {"location": location}, ""


def err(code):
    return code, {}, ""


@pytest.fixture
def claim():
    seen = set()

    def _claim(url):
        if url in seen:
            return False
        seen.add(url)
        return True

    return _claim


class TestHappyPath:
    async def test_ok_returns_html(self, claim):
        async with mock_client(html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK
        assert out.final_url == URL
        assert out.original_url == URL
        assert out.html is not None and "<html>" in out.html

    async def test_non_html(self, claim):
        async with mock_client(plain_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.NON_HTML
        assert out.html is None
        assert out.original_url == URL
        assert out.final_url == URL


class TestErrors:
    async def test_404_is_failed(self, claim):
        async with mock_client(err(404)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED

    async def test_410_is_failed(self, claim):
        async with mock_client(err(410)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED

    async def test_500_is_failed(self, claim):
        async with mock_client(err(500)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED


class TestRetry:
    async def test_502_retried_ok(self, claim):
        async with mock_client(err(502), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_503_retried_ok(self, claim):
        async with mock_client(err(503), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_504_retried_ok(self, claim):
        async with mock_client(err(504), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_502_twice_is_failed(self, claim):
        async with mock_client(err(502), err(502)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED

    async def test_timeout_retried_ok(self, claim):
        async with mock_client(httpx.TimeoutException("t"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_connect_error_retried_ok(self, claim):
        async with mock_client(httpx.ConnectError("refused"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_timeout_twice_is_failed(self, claim):
        async with mock_client(httpx.TimeoutException("t"), httpx.TimeoutException("t")) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED

    async def test_404_not_retried(self, claim):
        # Only one response queued — the AssertionError in mock_client fires if retried
        async with mock_client(err(404)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED

    async def test_500_not_retried(self, claim):
        async with mock_client(err(500)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED

    async def test_retry_after_capped_at_5s(self, claim, monkeypatch):
        slept = []

        async def fake_sleep(n):
            slept.append(n)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        async with mock_client((503, {"retry-after": "100"}, ""), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK
        assert slept == [5.0]

    async def test_retry_after_small_value_honoured(self, claim, monkeypatch):
        slept = []

        async def fake_sleep(n):
            slept.append(n)

        monkeypatch.setattr("asyncio.sleep", fake_sleep)
        async with mock_client((503, {"retry-after": "2"}, ""), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK
        assert slept == [2.0]


class TestRedirectLoopDetection:
    async def test_self_redirect_is_loop(self, claim):
        # /page → /page: the starting URL is in chain, so the first response detects the loop
        async with mock_client(redir(301, URL)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_LOOP
        assert out.redirect_hops == 0  # no hops were followed

    async def test_three_cycle_is_loop(self, claim):
        # /page → /b → /c → /page: two hops followed, loop detected on third response
        b = "http://example.com/b"
        c = "http://example.com/c"
        async with mock_client(redir(301, b), redir(301, c), redir(301, URL)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_LOOP
        assert out.redirect_hops == 2  # /page→/b and /b→/c were followed

    async def test_loop_does_not_fire_for_different_seen_target(self, claim):
        # /page → /other where /other is globally seen but NOT in this chain → REDIRECT_SEEN
        other = "http://example.com/other"
        claim(other)  # pre-claim as a different chain would
        async with mock_client(redir(301, other)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_SEEN

    async def test_long_chain_different_urls_hits_hop_limit(self, claim):
        # 6 unique URLs: five hops are followed, the sixth response hits the limit
        resps = [redir(301, f"http://example.com/p{i + 1}") for i in range(6)]
        async with mock_client(*resps) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_LOOP
        assert out.redirect_hops == 5


class TestRedirects:
    async def test_301_followed(self, claim):
        target = "http://example.com/new"
        async with mock_client(redir(301, target), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK
        assert out.final_url == target
        assert out.original_url == URL

    async def test_302_followed(self, claim):
        async with mock_client(redir(302, "http://example.com/new"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_303_followed(self, claim):
        async with mock_client(redir(303, "http://example.com/new"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_307_followed(self, claim):
        async with mock_client(redir(307, "http://example.com/new"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_308_followed(self, claim):
        async with mock_client(redir(308, "http://example.com/new"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_redirect_off_domain(self, claim):
        async with mock_client(redir(301, "http://other.com/page")) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_OFF_DOMAIN

    async def test_relative_location_resolved(self, claim):
        async with mock_client(redir(302, "/new"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK
        assert out.final_url == "http://example.com/new"

    async def test_missing_location_is_failed(self, claim):
        async with mock_client((301, {}, "")) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.FAILED

    async def test_redirect_loop_gt_5_hops(self, claim):
        # 6 redirect responses → exceeds 5-hop limit
        resps = [redir(301, f"http://example.com/p{i + 1}") for i in range(6)]
        async with mock_client(*resps) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_LOOP

    async def test_redirect_exactly_5_hops_ok(self, claim):
        # 5 hops are allowed; the 6th response is the final HTML page
        resps = [redir(301, f"http://example.com/p{i + 1}") for i in range(5)]
        resps.append(html_resp())
        async with mock_client(*resps) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK

    async def test_redirect_cycle_detected(self, claim):
        # /page → /b → /page is a per-chain cycle
        resps = [
            redir(301, "http://example.com/b"),
            redir(301, URL),
        ]
        async with mock_client(*resps) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_LOOP

    async def test_redirect_target_already_seen(self, claim):
        target = "http://example.com/already"
        claim(target)  # pre-claim as the crawler would
        async with mock_client(redir(301, target)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_SEEN
        assert out.final_url == target
        assert out.original_url == URL

    async def test_redirect_target_claimed_before_hop(self):
        # claim must be called synchronously (no await) before the next hop request
        claimed_before_request = []
        request_count = [0]

        def tracking_claim(url):
            claimed_before_request.append((url, request_count[0]))
            return True

        target = "http://example.com/new"

        def handler(request):
            request_count[0] += 1
            if request_count[0] == 1:
                return httpx.Response(301, headers={"location": target}, content=b"")
            return httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html/>")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await fetch(URL, client, SCOPE, tracking_claim)

        # target must have been claimed before the second request (request_count == 1)
        assert any(url == target and req_count == 1 for url, req_count in claimed_before_request)

    async def test_http_to_https_same_host_followed(self, claim):
        target = "https://example.com/page"
        async with mock_client(redir(301, target), html_resp()) as client:
            out = await fetch("http://example.com/page", client, SCOPE, claim)
        assert out.status == FetchStatus.OK
        assert out.final_url == target


class TestRedirectHops:
    async def test_no_redirect_zero_hops(self, claim):
        async with mock_client(html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.redirect_hops == 0

    async def test_single_redirect_one_hop(self, claim):
        async with mock_client(redir(301, "http://example.com/new"), html_resp()) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.redirect_hops == 1

    async def test_five_hops_ok(self, claim):
        resps = [redir(301, f"http://example.com/p{i + 1}") for i in range(5)]
        resps.append(html_resp())
        async with mock_client(*resps) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.OK
        assert out.redirect_hops == 5

    async def test_redirect_seen_zero_hops(self, claim):
        target = "http://example.com/already"
        claim(target)
        async with mock_client(redir(301, target)) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_SEEN
        assert out.redirect_hops == 0

    async def test_redirect_off_domain_zero_hops(self, claim):
        async with mock_client(redir(301, "http://other.com/page")) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_OFF_DOMAIN
        assert out.redirect_hops == 0

    async def test_loop_cycle_reports_prior_hops(self, claim):
        # /page → /b → /page: one hop is followed before the cycle is detected
        resps = [redir(301, "http://example.com/b"), redir(301, URL)]
        async with mock_client(*resps) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_LOOP
        assert out.redirect_hops == 1

    async def test_loop_at_max_reports_five_hops(self, claim):
        # 6 redirects: five are followed then the sixth hits the hop limit
        resps = [redir(301, f"http://example.com/p{i + 1}") for i in range(6)]
        async with mock_client(*resps) as client:
            out = await fetch(URL, client, SCOPE, claim)
        assert out.status == FetchStatus.REDIRECT_LOOP
        assert out.redirect_hops == 5
