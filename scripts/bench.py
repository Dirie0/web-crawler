#!/usr/bin/env python3
"""Concurrency benchmark for the crawler.

Spins up a local stdlib HTTP server with 500 linked pages (75 ms delay per
response), then runs the crawler at --concurrency 1, 5, 10, 20, 50, 100 and
prints a pages/sec table to stdout.

Usage:
    python scripts/bench.py

Requires the package to be installed in the active virtualenv:
    pip install -e .
"""
import http.server
import random
import subprocess
import sys
import threading
import time
from html import escape

NUM_PAGES = 500
DELAY_S = 0.075  # 75 ms per response
LINKS_PER_PAGE = 8
CONCURRENCIES = [1, 5, 10, 20, 50, 100]


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

def _build_page_html(i: int, targets: list[str]) -> bytes:
    hrefs = "".join(
        f'<li><a href="{escape(t)}">{escape(t)}</a></li>' for t in targets
    )
    return (
        f"<!doctype html><html><body>"
        f"<h1>Page {i}</h1><ul>{hrefs}</ul>"
        f"</body></html>"
    ).encode()


def _build_index_html(num: int) -> bytes:
    hrefs = "".join(
        f'<li><a href="/page/{i}">/page/{i}</a></li>' for i in range(num)
    )
    return (
        f"<!doctype html><html><body><h1>Index</h1><ul>{hrefs}</ul></body></html>"
    ).encode()


def _prerender(num: int, links_per: int) -> tuple[bytes, list[bytes]]:
    rng = random.Random(42)
    all_paths = [f"/page/{i}" for i in range(num)]
    pages: list[bytes] = []
    for i in range(num):
        candidates = all_paths[:i] + all_paths[i + 1 :]
        targets = rng.sample(candidates, min(links_per, len(candidates)))
        pages.append(_build_page_html(i, targets))
    index = _build_index_html(num)
    return index, pages


class _Handler(http.server.BaseHTTPRequestHandler):
    index_html: bytes = b""
    pages: list[bytes] = []

    def log_message(self, fmt: str, *args: object) -> None:
        pass  # silence request logs

    def do_GET(self) -> None:
        time.sleep(DELAY_S)
        path = self.path.split("?")[0]
        if path in ("/", ""):
            body = self.__class__.index_html
        elif path.startswith("/page/"):
            try:
                idx = int(path[len("/page/"):])
                body = self.__class__.pages[idx]
            except (ValueError, IndexError):
                self.send_error(404)
                return
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _start_server() -> tuple["http.server.ThreadingHTTPServer", int]:
    index, pages = _prerender(NUM_PAGES, LINKS_PER_PAGE)
    _Handler.index_html = index
    _Handler.pages = pages

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    port: int = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, port


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run_crawler(port: int, concurrency: int) -> str | None:
    result = subprocess.run(
        [
            sys.executable, "-m", "crawler",
            f"http://127.0.0.1:{port}/",
            "--concurrency", str(concurrency),
            "--max-pages", str(NUM_PAGES + 50),
            "--timeout", "30",
        ],
        capture_output=True,
        text=True,
        timeout=600,
    )
    lines = [ln.strip() for ln in result.stderr.splitlines() if ln.strip()]
    return lines[-1] if lines else None


def _parse_tokens(line: str) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for token in line.split():
        if "=" in token:
            k, _, v = token.partition("=")
            tokens[k] = v
    return tokens


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print(
        f"Building {NUM_PAGES} pages with {LINKS_PER_PAGE} links each "
        f"({DELAY_S * 1000:.0f} ms delay) …",
        flush=True,
    )
    server, port = _start_server()
    print(f"Server listening on http://127.0.0.1:{port}/\n", flush=True)

    rows: list[tuple[int, dict[str, str]]] = []

    for c in CONCURRENCIES:
        print(f"  concurrency={c:3d} … ", end="", flush=True)
        line = _run_crawler(port, c)
        if line is None:
            print("FAILED (no stderr output)")
            continue
        t = _parse_tokens(line)
        pps = t.get("pages/sec", "?")
        elapsed = t.get("elapsed", "?")
        crawled = t.get("crawled", "?")
        print(f"crawled={crawled}  elapsed={elapsed}  pages/sec={pps}")
        rows.append((c, t))

    server.shutdown()

    print()
    print("=" * 56)
    print(f"{'concurrency':>12}  {'crawled':>8}  {'elapsed':>10}  {'pages/sec':>10}")
    print("-" * 56)
    for c, t in rows:
        print(
            f"{c:>12}  "
            f"{t.get('crawled', '?'):>8}  "
            f"{t.get('elapsed', '?'):>10}  "
            f"{t.get('pages/sec', '?'):>10}"
        )
    print("=" * 56)


if __name__ == "__main__":
    main()
