"""Crawler orchestration."""
import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from typing import IO
from urllib.parse import urlsplit

import httpx

from crawler.fetcher import fetch
from crawler.models import FetchStatus
from crawler.parser import extract_links
from crawler.robots import RobotsChecker
from crawler.urls import is_in_scope, normalise


class OffDomainRedirect(Exception):
    def __init__(self, target_host: str) -> None:
        self.target_host = target_host
        super().__init__(f"base URL redirects to {target_host}, re-run with that URL")


@dataclass
class CrawlStats:
    discovered: int = 0
    crawled: int = 0
    failed: int = 0
    out_of_scope: int = 0
    redirected: int = 0
    redirect_hops: int = 0
    skipped: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def skipped_total(self) -> int:
        return sum(self.skipped.values())

    def equation_holds(self) -> bool:
        return self.discovered == self.crawled + self.failed + self.redirected + self.skipped_total


class Crawler:
    def __init__(
        self,
        client: httpx.AsyncClient,
        base_url: str,
        concurrency: int,
        max_pages: int,
        robots: RobotsChecker,
        out: IO[str],
        timeout: float = 10.0,
    ) -> None:
        self._client = client
        self._base_url = base_url
        self._concurrency = concurrency
        self._max_pages = max_pages
        self._robots = robots
        self._out = out
        self._timeout = timeout

        self._seen: set[str] = set()
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self.stats = CrawlStats()
        self._off_domain_error: OffDomainRedirect | None = None
        self._scope_host: str = ""
        self._base: str = ""

    def _claim(self, url: str) -> bool:
        if url in self._seen:
            return False
        self._seen.add(url)
        self.stats.discovered += 1
        return True

    async def run(self) -> None:
        base = normalise(self._base_url)
        if base is None:
            raise ValueError(f"Invalid base URL: {self._base_url}")
        self._base = base
        self._scope_host = urlsplit(base).hostname or ""

        self._claim(base)
        self._queue.put_nowait(base)

        workers = [asyncio.create_task(self._worker()) for _ in range(self._concurrency)]
        await self._queue.join()
        for w in workers:
            w.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

        if self._off_domain_error:
            raise self._off_domain_error

    async def _worker(self) -> None:
        while True:
            url = await self._queue.get()
            try:
                await self._process(url)
            finally:
                self._queue.task_done()

    async def _process(self, url: str) -> None:
        if not self._robots.allowed(url):
            self.stats.skipped["robots"] += 1
            return

        outcome = await fetch(
            url, self._client, self._scope_host, self._claim, timeout=self._timeout
        )

        if url == self._base and outcome.status is FetchStatus.REDIRECT_OFF_DOMAIN:
            hostname = urlsplit(outcome.final_url).hostname or outcome.final_url
            self._off_domain_error = OffDomainRedirect(hostname)
            return

        self.stats.redirect_hops += outcome.redirect_hops

        if outcome.status is FetchStatus.FAILED:
            self.stats.redirected += outcome.redirect_hops
            self.stats.failed += 1
            return
        if outcome.status is FetchStatus.NON_HTML:
            self.stats.redirected += outcome.redirect_hops
            self.stats.skipped["non_html"] += 1
            return
        if outcome.status is FetchStatus.REDIRECT_OFF_DOMAIN:
            self.stats.redirected += outcome.redirect_hops
            self.stats.skipped["redirect_off_domain"] += 1
            return
        if outcome.status is FetchStatus.REDIRECT_LOOP:
            self.stats.redirected += outcome.redirect_hops
            self.stats.skipped["redirect_loop"] += 1
            return
        if outcome.status is FetchStatus.REDIRECT_SEEN:
            # redirect_hops intermediate hops + the original both answered 3xx.
            self.stats.redirected += outcome.redirect_hops + 1
            return

        # FetchStatus.OK — redirect_hops intermediates answered 3xx; final URL is crawled.
        self.stats.redirected += outcome.redirect_hops
        self.stats.crawled += 1
        assert outcome.html is not None
        links = extract_links(outcome.html, outcome.final_url)

        block = outcome.final_url + "\n" + "".join(f"  {link}\n" for link in links)
        self._out.write(block)
        self._out.flush()

        for link in links:
            if not is_in_scope(link, self._scope_host):
                self.stats.out_of_scope += 1
                continue
            if self._claim(link):
                if self.stats.discovered <= self._max_pages:
                    self._queue.put_nowait(link)
                else:
                    self.stats.skipped["cap_reached"] += 1
