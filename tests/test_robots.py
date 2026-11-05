"""Tests for robots.txt handling."""
import httpx
import pytest

from crawler.robots import RobotsChecker

pytestmark = pytest.mark.asyncio

ORIGIN = "http://example.com"
ROBOTS_URL = "http://example.com/robots.txt"

DISALLOW_ALL = "User-agent: *\nDisallow: /\n"
ALLOW_ALL_BODY = "User-agent: *\nDisallow:\n"
DISALLOW_ADMIN = "User-agent: *\nDisallow: /admin/\n"


def make_client(*responses):
    """MockTransport backed by a fixed sequence of (status, headers, body) tuples."""
    items = list(responses)
    index = [0]

    def handler(request):
        if index[0] >= len(items):
            raise AssertionError(f"Unexpected request to {request.url}")
        item = items[index[0]]
        index[0] += 1
        if isinstance(item, BaseException):
            raise item
        status, headers, body = item
        return httpx.Response(status, headers=headers, content=body.encode())

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def robots_resp(body=DISALLOW_ALL, status=200):
    return status, {"content-type": "text/plain"}, body


def redir(code, location):
    return code, {"location": location}, ""


class TestStatusPolicies:
    async def test_200_parses_disallow(self):
        async with make_client(robots_resp(DISALLOW_ALL)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is False

    async def test_200_parses_allow(self):
        async with make_client(robots_resp(ALLOW_ALL_BODY)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is True

    async def test_404_allows_all(self):
        async with make_client(robots_resp(status=404)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is True

    async def test_401_allows_all(self):
        async with make_client(robots_resp(status=401)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/secret") is True

    async def test_403_allows_all(self):
        async with make_client(robots_resp(status=403)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/secret") is True

    async def test_500_disallows_all(self, capsys):
        async with make_client(robots_resp(status=500)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is False
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower() or "robots" in captured.err.lower()

    async def test_503_disallows_all(self, capsys):
        async with make_client(robots_resp(status=503)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is False

    async def test_network_error_disallows_all(self, capsys):
        async with make_client(httpx.ConnectError("refused")) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is False
        captured = capsys.readouterr()
        assert "warning" in captured.err.lower() or "robots" in captured.err.lower()


class TestDisallowRespected:
    async def test_disallowed_path_blocked(self):
        async with make_client(robots_resp(DISALLOW_ADMIN)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/admin/settings") is False

    async def test_allowed_path_permitted(self):
        async with make_client(robots_resp(DISALLOW_ADMIN)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/about") is True


class TestRedirects:
    async def test_redirect_followed(self):
        alt = "http://example.com/robots2.txt"
        async with make_client(redir(301, alt), robots_resp(DISALLOW_ALL)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is False

    async def test_five_redirects_followed(self):
        # 5 hops are allowed; the 6th response is the actual robots.txt
        redirs = [redir(301, f"{ORIGIN}/robots{i+1}.txt") for i in range(5)]
        async with make_client(*redirs, robots_resp(DISALLOW_ALL)) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is False

    async def test_more_than_five_redirects_allows_all(self):
        # >5 hops → allow all
        redirs = [redir(301, f"{ORIGIN}/robots{i+1}.txt") for i in range(6)]
        async with make_client(*redirs) as client:
            checker = RobotsChecker()
            await checker.load(client, ORIGIN)
        assert checker.allowed(f"{ORIGIN}/page") is True
