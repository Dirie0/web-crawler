"""robots.txt handling."""
import sys
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser

import httpx

_AGENT = "*"
_MAX_HOPS = 5


class RobotsChecker:
    def __init__(self) -> None:
        self._parser: RobotFileParser | None = None
        self._allow_all = False

    async def load(self, client: httpx.AsyncClient, origin: str) -> None:
        url = urljoin(origin, "/robots.txt")
        hops = 0
        while True:
            try:
                resp = await client.get(url, follow_redirects=False)
            except httpx.HTTPError as exc:
                print(f"WARNING: robots.txt fetch failed for {origin}: {exc}", file=sys.stderr)
                self._allow_all = False
                return

            if resp.status_code in (301, 302, 303, 307, 308):
                if hops >= _MAX_HOPS:
                    self._allow_all = True
                    return
                location = resp.headers.get("location", "")
                url = urljoin(url, location)
                hops += 1
                continue

            if 400 <= resp.status_code < 500:
                self._allow_all = True
                return

            if resp.status_code >= 500:
                msg = (
                    f"WARNING: robots.txt returned {resp.status_code} for {origin}, "
                    "disallowing all"
                )
                print(msg, file=sys.stderr)
                self._allow_all = False
                return

            # 200
            parser = RobotFileParser()
            parser.parse(resp.text.splitlines())
            self._parser = parser
            return

    def allowed(self, url: str) -> bool:
        if self._allow_all:
            return True
        if self._parser is None:
            return False
        return self._parser.can_fetch(_AGENT, url)
