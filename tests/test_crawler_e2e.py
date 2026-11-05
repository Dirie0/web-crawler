"""End-to-end crawler tests."""
import io

import httpx
import pytest

from crawler.crawler import Crawler, OffDomainRedirect
from crawler.robots import RobotsChecker

pytestmark = pytest.mark.asyncio

BASE = "http://example.com"

# Minimal robots that allows everything
ALLOW_ALL_ROBOTS = "User-agent: *\nDisallow:\n"


def html(*links):
    anchors = "".join(f'<a href="{link}">{link}</a>' for link in links)
    return f"<html><body>{anchors}</body></html>"


def make_transport(pages: dict[str, tuple[int, str]]):
    """pages maps URL -> (status, body). Content-type text/html for 200s."""
    def handler(request):
        url = str(request.url)
        if url not in pages:
            return httpx.Response(404, content=b"not found")
        status, body = pages[url]
        headers = {"content-type": "text/html"} if status == 200 else {}
        return httpx.Response(status, headers=headers, content=body.encode())
    return httpx.MockTransport(handler)


async def make_robots(client):
    r = RobotsChecker()
    await r.load(client, BASE)
    return r


def page_headers(text: str) -> list[str]:
    """Return lines that are page URL headers (non-indented, non-empty)."""
    return [line for line in text.splitlines() if line and not line.startswith(" ")]


class TestFivePageSmoke:
    """5-page linked site: home->a,b; a->c; b->d. All pages should be crawled."""

    PAGES = {
        f"{BASE}/robots.txt": (200, ALLOW_ALL_ROBOTS),
        f"{BASE}/": (200, html(f"{BASE}/a", f"{BASE}/b")),
        f"{BASE}/a": (200, html(f"{BASE}/c")),
        f"{BASE}/b": (200, html(f"{BASE}/d")),
        f"{BASE}/c": (200, html()),
        f"{BASE}/d": (200, html()),
    }

    async def test_all_pages_printed_once(self):
        out = io.StringIO()
        async with httpx.AsyncClient(transport=make_transport(self.PAGES)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/", concurrency=2, max_pages=100, robots=robots, out=out
            )
            await crawler.run()

        headers = page_headers(out.getvalue())
        for page in [f"{BASE}/", f"{BASE}/a", f"{BASE}/b", f"{BASE}/c", f"{BASE}/d"]:
            assert headers.count(page) == 1, f"{page} not printed exactly once as a page header"

    async def test_completeness_equation(self):
        out = io.StringIO()
        async with httpx.AsyncClient(transport=make_transport(self.PAGES)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/", concurrency=2, max_pages=100, robots=robots, out=out
            )
            await crawler.run()

        s = crawler.stats
        assert s.equation_holds()

    async def test_links_indented(self):
        out = io.StringIO()
        async with httpx.AsyncClient(transport=make_transport(self.PAGES)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/", concurrency=1, max_pages=100, robots=robots, out=out
            )
            await crawler.run()

        text = out.getvalue()
        # Every indented line must start with two spaces
        for line in text.splitlines():
            if line and not line.startswith(("http://", "https://")):
                assert line.startswith("  "), f"Non-indented link line: {repr(line)}"


class TestOffDomainBaseRedirect:
    async def test_raises_off_domain_redirect(self):

        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/":
                return httpx.Response(301, headers={"location": "http://other.com/"}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client,
                f"{BASE}/",
                concurrency=1,
                max_pages=100,
                robots=robots,
                out=io.StringIO(),
            )
            with pytest.raises(OffDomainRedirect):
                await crawler.run()


class TestRedirectCounters:
    async def test_redirect_hops_accumulated(self):
        # / → /a (1 hop); /a links to /b; /b → /c (1 hop) → total 2 hops
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            if url == f"{BASE}/a":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      content=html(f"{BASE}/b").encode())
            if url == f"{BASE}/b":
                return httpx.Response(301, headers={"location": f"{BASE}/c"}, content=b"")
            if url == f"{BASE}/c":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      content=html().encode())
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/", concurrency=1, max_pages=100, robots=robots, out=io.StringIO()
            )
            await crawler.run()

        assert crawler.stats.redirect_hops == 2
        s = crawler.stats
        assert s.equation_holds()

    async def test_redirected_incremented_for_ok_after_redirect(self):
        # / → 301 → /a (OK): the original URL "/" counts as redirected
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            if url == f"{BASE}/a":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      content=html().encode())
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/", concurrency=1, max_pages=100, robots=robots, out=io.StringIO()
            )
            await crawler.run()

        assert crawler.stats.redirected == 1
        s = crawler.stats
        assert s.equation_holds()

    async def test_redirected_incremented_for_seen_target(self):
        # / → /a (OK, crawled); /a links to /b; /b → /a (already seen) → redirected=2
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            if url == f"{BASE}/a":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      content=html(f"{BASE}/b").encode())
            if url == f"{BASE}/b":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/", concurrency=1, max_pages=100, robots=robots, out=io.StringIO()
            )
            await crawler.run()

        # "/" redirected to /a (OK) and "/b" redirected to already-seen /a
        assert crawler.stats.redirected == 2
        s = crawler.stats
        assert s.equation_holds()


    async def test_three_hop_chain_equation_balances(self):
        # /a → /b → /c → /d(200). Three URLs answer 3xx; one is crawled.
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/a":
                return httpx.Response(302, headers={"location": f"{BASE}/b"}, content=b"")
            if url == f"{BASE}/b":
                return httpx.Response(301, headers={"location": f"{BASE}/c"}, content=b"")
            if url == f"{BASE}/c":
                return httpx.Response(301, headers={"location": f"{BASE}/d"}, content=b"")
            if url == f"{BASE}/d":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      content=html().encode())
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/a", concurrency=1, max_pages=100, robots=robots,
                out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.redirected == 3
        assert s.redirect_hops == 3
        assert s.crawled == 1
        assert s.equation_holds()

    async def test_two_step_loop_equation_balances(self):
        # /a → /b → /a (loop). /a is redirected (hop followed), /b detects the loop.
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/a":
                return httpx.Response(302, headers={"location": f"{BASE}/b"}, content=b"")
            if url == f"{BASE}/b":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/a", concurrency=1, max_pages=100, robots=robots,
                out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.skipped.get("redirect_loop", 0) == 1
        assert s.redirected == 1   # /a's hop to /b was followed
        assert s.crawled == 0
        assert s.equation_holds()


class TestRedirectLoopStats:
    """Redirect-loop detection must produce correct stat counts and satisfy the equation."""

    def _equation(self, s):
        return s.crawled + s.failed + s.redirected + s.skipped_total

    async def test_self_redirect_counts_as_loop(self):
        # /a → /a: the first response detects the loop; nothing is crawled or redirected.
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/a":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/a", concurrency=1, max_pages=100, robots=robots,
                out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.crawled == 0
        assert s.redirected == 0
        assert s.skipped.get("redirect_loop", 0) == 1
        assert s.discovered == 1
        assert s.redirect_hops == 0
        assert s.discovered == self._equation(s)

    async def test_three_cycle_loop_counts(self):
        # /a → /b → /c → /a: two hops followed (redirected=2), loop on third (redirect_loop=1).
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/a":
                return httpx.Response(302, headers={"location": f"{BASE}/b"}, content=b"")
            if url == f"{BASE}/b":
                return httpx.Response(301, headers={"location": f"{BASE}/c"}, content=b"")
            if url == f"{BASE}/c":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/a", concurrency=1, max_pages=100, robots=robots,
                out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.crawled == 0
        assert s.redirected == 2
        assert s.skipped.get("redirect_loop", 0) == 1
        assert s.discovered == 3
        assert s.redirect_hops == 2
        assert s.discovered == self._equation(s)

    async def test_long_chain_exceeds_hop_limit(self):
        # /a → /b → … → /g (7 unique URLs, 5 hops followed, 6th response hits hop limit).
        urls = [f"{BASE}/{chr(ord('a') + i)}" for i in range(7)]

        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            for i, u in enumerate(urls[:-1]):
                if url == u:
                    return httpx.Response(301, headers={"location": urls[i + 1]}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, urls[0], concurrency=1, max_pages=100, robots=robots,
                out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.crawled == 0
        assert s.skipped.get("redirect_loop", 0) == 1
        assert s.redirect_hops == 5
        assert s.discovered == self._equation(s)

    async def test_two_chains_to_same_target_not_loop(self):
        # /a → /c (HTML) and /b → /c. /c claimed by /a's chain.
        # /b's redirect to /c must count as `redirected`, NOT `redirect_loop`.
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      content=html(f"{BASE}/a", f"{BASE}/b").encode())
            if url in (f"{BASE}/a", f"{BASE}/b"):
                return httpx.Response(301, headers={"location": f"{BASE}/c"}, content=b"")
            if url == f"{BASE}/c":
                return httpx.Response(200, headers={"content-type": "text/html"},
                                      content=html().encode())
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/", concurrency=1, max_pages=100, robots=robots,
                out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.skipped.get("redirect_loop", 0) == 0  # /b→/c is REDIRECT_SEEN, never a loop
        assert s.redirected == 2             # /a and /b both redirected
        assert s.crawled == 2               # / and /c crawled
        assert s.discovered == 4            # /, /a, /b, /c
        assert s.discovered == self._equation(s)


class TestRobotsSkip:
    async def test_disallowed_url_skipped(self):
        disallow_admin = "User-agent: *\nDisallow: /admin/\n"
        pages = {
            f"{BASE}/robots.txt": (200, disallow_admin),
            f"{BASE}/": (200, html(f"{BASE}/admin/settings", f"{BASE}/about")),
            f"{BASE}/about": (200, html()),
        }

        def handler(request):
            url = str(request.url)
            if url not in pages:
                return httpx.Response(404, content=b"")
            status, body = pages[url]
            ct = "text/plain" if "robots" in url else "text/html"
            return httpx.Response(status, headers={"content-type": ct}, content=body.encode())

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = RobotsChecker()
            await robots.load(client, BASE)
            out = io.StringIO()
            crawler = Crawler(
                client, f"{BASE}/", concurrency=1, max_pages=100, robots=robots, out=out
            )
            await crawler.run()

        assert crawler.stats.skipped.get("robots", 0) == 1
        assert f"{BASE}/admin/settings" not in page_headers(out.getvalue())


class TestRedirectLoopSkipCounter:
    """redirect_loop increments skipped["redirect_loop"]; equation holds in all loop shapes."""

    async def test_self_redirect_skipped_as_loop(self):
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/a":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/a", concurrency=1, max_pages=100, robots=robots, out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.skipped.get("redirect_loop", 0) == 1
        assert s.redirected == 0
        assert s.discovered == 1
        assert s.discovered == s.crawled + s.failed + s.redirected + s.skipped_total

    async def test_three_cycle_skipped_as_loop(self):
        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            if url == f"{BASE}/a":
                return httpx.Response(302, headers={"location": f"{BASE}/b"}, content=b"")
            if url == f"{BASE}/b":
                return httpx.Response(301, headers={"location": f"{BASE}/c"}, content=b"")
            if url == f"{BASE}/c":
                return httpx.Response(301, headers={"location": f"{BASE}/a"}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, f"{BASE}/a", concurrency=1, max_pages=100, robots=robots, out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.redirected == 2
        assert s.skipped.get("redirect_loop", 0) == 1
        assert s.discovered == 3
        assert s.discovered == s.crawled + s.failed + s.redirected + s.skipped_total

    async def test_hop_limit_skipped_as_loop(self):
        # 7 unique URLs: 5 hops followed, 6th response hits the hop limit
        urls = [f"{BASE}/{chr(ord('a') + i)}" for i in range(7)]

        def handler(request):
            url = str(request.url)
            if url == f"{BASE}/robots.txt":
                return httpx.Response(200, headers={"content-type": "text/plain"},
                                      content=ALLOW_ALL_ROBOTS.encode())
            for i, u in enumerate(urls[:-1]):
                if url == u:
                    return httpx.Response(301, headers={"location": urls[i + 1]}, content=b"")
            return httpx.Response(404, content=b"")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            robots = await make_robots(client)
            crawler = Crawler(
                client, urls[0], concurrency=1, max_pages=100, robots=robots, out=io.StringIO()
            )
            await crawler.run()

        s = crawler.stats
        assert s.skipped.get("redirect_loop", 0) == 1
        assert s.discovered == s.crawled + s.failed + s.redirected + s.skipped_total


class TestSummaryFormat:
    """_format_summary produces correctly spaced output with skipped dict."""

    def test_self_redirect_summary_contains_skip_key(self):
        from crawler.cli import _format_summary
        from crawler.crawler import CrawlStats

        s = CrawlStats(discovered=1)
        s.skipped["redirect_loop"] = 1
        summary = _format_summary(s, elapsed=1.0, concurrency=1)

        assert "skipped(redirect_loop=1)" in summary
        tokens = summary.split(" ")
        for token in tokens:
            assert token.count("=") == 1, f"Token {token!r} does not have exactly one '='"
