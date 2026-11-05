"""Command-line interface."""
import argparse
import asyncio
import logging
import sys
import time
from urllib.parse import urlsplit

import httpx

from crawler.crawler import Crawler, CrawlStats, OffDomainRedirect
from crawler.robots import RobotsChecker

_UA = "crawler/0.1 (single-domain async crawler; python-httpx)"


def _format_summary(s: CrawlStats, elapsed: float, concurrency: int) -> str:
    pps = s.crawled / elapsed if elapsed > 0 else 0.0
    skipped_parts = [f"{k}={v}" for k, v in sorted(s.skipped.items()) if v > 0]
    skipped_str = ", ".join(skipped_parts) if skipped_parts else "none"
    fields = [
        f"crawled={s.crawled}",
        f"failed={s.failed}",
        f"redirected={s.redirected}",
        f"skipped({skipped_str})",
        f"discovered={s.discovered}",
        "retries=0",
        f"redirect_hops={s.redirect_hops}",
        f"elapsed={elapsed:.1f}s",
        f"pages/sec={pps:.1f}",
        f"concurrency={concurrency}",
    ]
    return " ".join(fields)


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="crawler",
        description="Single-domain async web crawler",
    )
    p.add_argument("base_url", help="Base URL to crawl")
    p.add_argument(
        "--concurrency", type=int, default=10, metavar="N",
        help="Max concurrent requests (default: 10)",
    )
    p.add_argument(
        "--max-pages", type=int, default=10_000, metavar="N",
        help="Max pages to schedule (default: 10000)",
    )
    p.add_argument(
        "--timeout", type=float, default=10.0, metavar="SECS",
        help="Per-request timeout in seconds (default: 10)",
    )
    return p


async def _run(base_url: str, concurrency: int, max_pages: int, timeout: float) -> int:
    limits = httpx.Limits(
        max_connections=concurrency,
        max_keepalive_connections=concurrency,
    )
    parsed = urlsplit(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    robots = RobotsChecker()
    async with httpx.AsyncClient(
        limits=limits,
        http2=False,
        headers={"User-Agent": _UA},
        follow_redirects=False,
    ) as client:
        await robots.load(client, origin)

        crawler = Crawler(
            client=client,
            base_url=base_url,
            concurrency=concurrency,
            max_pages=max_pages,
            robots=robots,
            out=sys.stdout,
            timeout=timeout,
        )

        t0 = time.monotonic()
        try:
            await crawler.run()
        except OffDomainRedirect as exc:
            print(str(exc), file=sys.stderr)
            return 2

        elapsed = time.monotonic() - t0
        print(_format_summary(crawler.stats, elapsed, concurrency), file=sys.stderr)
        return 0


def main() -> None:
    logging.basicConfig(
        level=logging.WARNING,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    args = _build_parser().parse_args()
    sys.exit(asyncio.run(_run(
        base_url=args.base_url,
        concurrency=args.concurrency,
        max_pages=args.max_pages,
        timeout=args.timeout,
    )))
