# python-developer-test

# Zego

## About Us

At Zego, we understand that traditional motor insurance holds good drivers back.
It's too complicated, too expensive, and it doesn't reflect how well you actually drive.
Since 2016, we have been on a mission to change that by offering the lowest priced insurance for good drivers.
From van drivers and gig economy workers to everyday car drivers, our customers are the driving force behind everything we do. We've sold tens of millions of policies and raised over $200 million in funding. And we're only just getting started.

---

## Solution: Single-Domain Async Web Crawler

A Python CLI that crawls a single domain concurrently, printing each discovered page URL followed by all links found on that page. It respects `robots.txt`, follows redirects within scope, and enforces a formal accounting invariant on every URL it encounters.

---

## Table of Contents

- [Quick Start](#quick-start)
- [Usage](#usage)
- [Output Format](#output-format)
- [Architecture](#architecture)
- [Design Decisions and Trade-offs](#design-decisions-and-trade-offs)
- [Edge Cases](#edge-cases)
- [Testing](#testing)
- [CI](#ci)
- [Tooling](#tooling)
- [Future Extensions](#future-extensions)

---

## Quick Start

**Requirements:** Python 3.11+

```bash
git clone <repository-url>
cd <project-directory>

python3 -m venv .venv
source .venv/bin/activate

python3 -m pip install -e ".[dev]"
python3 crawler https://example.com
```
## Usage

```
crawler [-h] [--concurrency N] [--max-pages N] [--timeout SECS] base_url
```

| Flag | Default | Description |
|---|---|---|
| `base_url` | _(required)_ | The URL to start crawling from |
| `--concurrency` | `10` | Maximum number of concurrent in-flight HTTP requests |
| `--max-pages` | `10000` | Maximum number of URLs to schedule (see [cap semantics](#max-pages-cap-semantics)) |
| `--timeout` | `10.0` | Per-request timeout in seconds |

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | Crawl completed normally |
| `2` | The base URL permanently redirected off-domain — rerun with the redirect target |

The crawl summary is written to `stderr` and looks like:

```
crawled=42 failed=1 redirected=3 skipped(robots=2, cap_reached=0) discovered=48 retries=0 redirect_hops=5 elapsed=3.2s pages/sec=13.1 concurrency=10
```

---

## Output Format

Each crawled page is printed as a block to `stdout`:

```
https://example.com/
  https://example.com/about
  https://example.com/contact
  https://www.other.com/external-link

https://example.com/about
  https://example.com/
```

- The first line of each block is the final URL of the page (after any in-scope redirects).
- Each indented line is a link found on that page, normalised but **not** filtered by scope — out-of-scope links are shown as found, they just are never crawled.
- Links are deduplicated per page in first-seen order; fragments are stripped.

---

## Architecture

The codebase is split into six modules with a single responsibility each:

```
src/crawler/
├── cli.py        — argparse entry point, asyncio.run, summary formatting
├── crawler.py    — orchestration: worker pool, queue, stats
├── fetcher.py    — HTTP fetching, manual redirect following, retry logic
├── models.py     — FetchStatus enum, FetchOutcome dataclass
├── parser.py     — HTML link extraction via selectolax
├── robots.py     — robots.txt fetch, parse, and allow/deny queries
└── urls.py       — URL normalisation and exact-host scope checking
```

### Worker pool

`Crawler.run()` seeds an `asyncio.Queue` with the base URL then spawns `--concurrency` coroutine workers that each loop `await queue.get() → _process() → task_done()`. When the queue drains (`queue.join()`) all workers are cancelled. This is the standard bounded-producer pattern for async Python: it caps in-flight requests at `--concurrency` with zero threads and no external semaphore.

### Manual redirect following

`httpx.AsyncClient` is configured with `follow_redirects=False`. The `fetch()` function in `fetcher.py` drives a redirect loop manually. This is intentional — it allows the crawler to:

1. Detect off-domain redirects mid-chain and stop immediately without requesting the foreign URL.
2. Call `claim(target)` **synchronously** (no `await` between the check and the claim) before issuing the next request, preventing two concurrent workers from both fetching the same redirect target.
3. Count each intermediate 3xx URL accurately in the global stats.

### Claim-before-await invariant

The `claim()` callback is a synchronous set-membership check backed by `asyncio`'s cooperative scheduler. Because there is no `await` between `claim(target)` returning `True` and `chain.add(target)`, no other coroutine can interleave. This prevents double-fetching redirect targets without needing a lock.

### Completeness invariant

Every URL added to `seen` is counted in exactly one of these buckets:

```
discovered == crawled + failed + redirected + skipped_total
```

`skipped_total` breaks down into named reasons: `robots`, `non_html`, `redirect_off_domain`, `redirect_loop`, `redirect_seen`, `cap_reached`. The invariant is verified by `CrawlStats.equation_holds()` and asserted in every integration test scenario.

---

## Design Decisions and Trade-offs

### `httpx` over `aiohttp`

`httpx` has a first-class async API, built-in connection pooling, `Limits` for controlling pool size, and excellent `MockTransport` for testing without network access. `aiohttp` is equally capable but requires a separate connector object; `httpx`'s `AsyncClient` is simpler to inject as a dependency and mock.

### `selectolax` over `BeautifulSoup`

`selectolax` wraps the Lexbor C HTML parser. On typical pages it is 10–30× faster than `html.parser` and 5–10× faster than `lxml` for `<a href>` extraction because parsing and CSS selection happen in C with no Python object overhead per node. The trade-off is a less familiar API and no XPath; CSS selectors cover everything needed here.

### HTTP/1.1 only

`http2=False` is set on the client. HTTP/2 multiplexes many requests over a single TCP connection, which is excellent for browsers but can cause head-of-line blocking at the application level when one slow server saturates the single connection. For a crawler that may hit dozens of different pages under varying server latency, independent HTTP/1.1 connections per slot give more predictable throughput. It is a one-line change to enable if benchmarks show a gain.

### Exact hostname scope, no subdomain wildcarding

`is_in_scope()` compares `urlsplit(url).hostname` to the base URL's hostname with `==`. A link to `www.example.com` when crawling `example.com` is treated as out-of-scope. This is conservative but correct: `www.example.com` and `example.com` can serve entirely different content, and the task spec says "single domain… not crawl URLs pointing to other domains **or subdomains**". A `--include-www` flag or regex-based scope rule would be a straightforward extension.

### Retry policy

Only `502`, `503`, `504` and network-level errors (`TimeoutException`, `ConnectError`) trigger a single retry with backoff. `4xx` and `5xx` other than the above are treated as terminal failures without retry. `Retry-After` is honoured but capped at 5 seconds to prevent a single hostile server from blocking the whole crawl. A second failure on the same URL always gives up.

### `robots.txt` compliance

The standard library `urllib.robotparser.RobotFileParser` is used so the parsing semantics exactly match the `robots.txt` spec. The checker is loaded once at startup, before the crawl begins, with an independent HTTP request (never consuming a slot from the worker pool). A 4xx response means allow-all (the convention); a 5xx means disallow-all (conservative, with a warning). Up to 5 redirects on `robots.txt` itself are followed; exceeding that limit allows all (the resource is assumed unreachable).

### `crawl-delay` not enforced

`RobotFileParser` exposes `crawl_delay()` but it is not read. This is a deliberate scope decision: honouring `crawl-delay` requires per-host rate limiting, which is straightforward to add but significantly increases implementation complexity for a task focused on structure and correctness. It is noted as a future extension.

### `max-pages` cap semantics

The cap counts `claim()` calls on `seen`, not completed page downloads. Each redirect hop claims a URL and therefore counts toward the cap. This is the safest interpretation: it bounds total work scheduled, not just work completed, so the crawler cannot accidentally queue thousands of URLs in a redirect chain beyond the cap.

### Output to `sys.stdout`; summary to `sys.stderr`

Page blocks go to `stdout` so the output can be piped (`crawler https://example.com | grep example.com/product`) without mixing in summary statistics. The summary and all warnings go to `stderr`. The `Crawler` class takes an `IO[str]` parameter for its output stream, making it straightforward to test with `io.StringIO` without capturing `sys.stdout`.

---

## Edge Cases

The following are explicitly handled and covered by tests:

| Scenario | Handling |
|---|---|
| Self-redirect (`/a → /a`) | Detected via per-chain `set`; counted as `redirect_loop` |
| Redirect cycle (`/a → /b → /a`) | Same per-chain set detects the revisit |
| Redirect chain > 5 hops | Hard limit; counted as `redirect_loop` |
| Redirect to already-seen URL | `claim()` returns `False`; counted as `redirect_seen` |
| Two chains both redirect to same target | First claim wins; second gets `REDIRECT_SEEN`, not a loop |
| Base URL redirects off-domain | `OffDomainRedirect` exception, exit code 2 |
| Mid-chain redirect leaves scope | Chain aborted; counted as `redirect_off_domain` |
| HTTP → HTTPS upgrade on same host | Followed (hostname still matches scope) |
| Missing `Location` header on 3xx | Treated as a failure |
| `Retry-After: 100` | Capped at 5 s |
| Non-HTML content type | Fetched but not parsed; counted as `non_html` |
| `robots.txt` 404/401/403 | Treated as allow-all (RFC convention) |
| `robots.txt` 5xx | Treated as disallow-all (conservative); warning to stderr |
| `robots.txt` network error | Treated as disallow-all; warning to stderr |
| `robots.txt` > 5 redirects | Treated as allow-all |
| `<base href>` in HTML | Correctly used as the resolution base for relative links |
| Relative `<base href>` | Resolved against the page URL first |
| `mailto:`, `tel:`, `javascript:` hrefs | Silently skipped by `normalise()` |
| Fragment-only links (`#section`) | Fragment stripped; underlying path deduplicated |
| Default port in URL (`https://…:443`) | Stripped during normalisation |
| Malformed HTML | `selectolax`'s Lexbor parser is error-tolerant; extracts what it can |
| Duplicate links on same page | Deduplicated in first-seen order per page |
| `<a>` tags without `href` | Skipped |
| Path case sensitivity | Preserved (path is case-sensitive per RFC 3986; only scheme/host are lowercased) |

---

## Testing

```bash
pytest -q
# 118 passed
```

The test suite requires no network access. All HTTP interactions are exercised via `httpx.MockTransport`, making tests fast, deterministic, and reliable in CI.

### Test modules

| File | What it covers |
|---|---|
| `test_urls.py` | `normalise()` and `is_in_scope()` — 30 unit tests covering scheme normalisation, default port stripping, fragment removal, relative resolution, `../` segments, ignored schemes, and all scope boundary cases |
| `test_parser.py` | `extract_links()` — 16 unit tests covering absolute/relative links, `<base href>`, fragment stripping, deduplication order, `mailto`/`javascript` skip, `<img src>` / `<link href>` exclusion, malformed HTML, and query string preservation |
| `test_robots.py` | `RobotsChecker` — 13 tests covering 200/404/401/403/500/503 responses, network errors, disallow rules, up-to-5 redirect following, and the >5 redirect allow-all fallback |
| `test_fetcher.py` | `fetch()` — 40 tests covering the happy path, all 4xx/5xx variants, retry on 502–504 and network errors, `Retry-After` capping, all redirect status codes (301/302/303/307/308), relative `Location` headers, off-domain redirects, self-redirect loops, N-cycle loops, hop limit, `REDIRECT_SEEN` vs `REDIRECT_LOOP` distinction, the claim-before-await ordering invariant, and accurate `redirect_hops` counts |
| `test_crawler_e2e.py` | `Crawler` integration — 19 tests with full multi-page site fixtures verifying: all pages printed exactly once, correct indentation, the completeness equation in every redirect/loop/robots scenario, `OffDomainRedirect` exception, and summary formatting |

### Completeness equation tests

Every integration test scenario calls `s.equation_holds()` and asserts `True`. This provides a correctness oracle that catches stat-accounting bugs independent of the specific counter values being checked.

---

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request to `main` against Python 3.11 and 3.12:

```
ruff check .      # linting and import ordering
mypy src          # static type checking
pytest -q         # 118 tests, no network required
```

---

## AI Usage and Development Process

I spent some time planning the project before working on code implementation, my intial approach was trying to build a note document with all of my current thought process about what are the functional requirements, non functional reqiurements, assumptions made to reduce the problem into constraints that can be solved. 

I also researched web crawler architectures to get more familiar with bfs/dfs and the optimal approach for this problem, redirects, edge cases.

From this I fed into claude to produce a design document, feeding as much context as possible, this became the basis for how I tackled this problem and its present in crawler-docs to view if curious.



I built the crawler in nine stages (skeleton, URL handling and parsing, fetcher, robots.txt, crawler core, CLI, end-to-end tests, tooling and CI, benchmark and README), after first drafting a written plan covering scope, guarantees, redirect, robots and retry policies, and a seven-module layout. For each stage I wrote pseudocode for the main functions myself and added as much context as I could about what the stage had to do and what it should be tested against. I then pasted that, with the relevant files and the problem, into Claude (chat interface) to fill out the implementation. I reviewed the result, refactored it where I disagreed with the structure or the error handling, and tested the stage before moving on. The early stages were unit-tested first: URL normalisation and exact-host scoping, selectolax link extraction, the manual redirect fetcher with a per-chain loop check, a five-hop limit and a single retry (using httpx.MockTransport), and an RFC 9309 robots checker. The asyncio worker pool came next, where a URL is claimed before it is queued, task_done() sits in a finally, and --max-pages caps scheduled URLs, followed by the CLI, a streamed output format and a stderr summary. End-to-end and stress tests then used the HTTP call log as the oracle to check that cycles terminate, each URL is scheduled at most once, concurrent redirects to one target fetch it once, in-flight requests never exceed --concurrency, and discovered = crawled + failed + redirected + skipped holds. Beyond mocked tests, I ran the crawler against the live crawler-test.com site and compared each run’s summary counters with what I expected in advance. That found two bugs the mocked tests had missed: redirect loops were counted as redirected instead of redirect_loop, and a bare root URL (https://host) was not normalised to /, which would have scheduled the homepage twice. I pasted the failing output back to Claude, reviewed which files the issue lies in and possible fix, implemented code changes and then added regression tests.



## Tooling

| Tool | Role |
|---|---|
| `httpx` | Async HTTP client with connection pool, mock transport |
| `selectolax` (Lexbor) | Fast C-backed HTML parser for link extraction |
| `ruff` | Linting and import sorting |
| `mypy` | Static type checking |
| `pytest` + `pytest-asyncio` | Test runner with async test support |

**IDE:** VS Code with the Pylance extension and the Ruff extension for inline linting.

**AI tooling:** Claude Code (claude.ai/code) was used throughout — for drafting module skeletons, iterating on the redirect accounting model, generating test cases for subtle edge cases (self-redirects, claim ordering, `Retry-After` capping), and reviewing the completeness invariant design. All generated code was reviewed, run, and adjusted manually. The AI was particularly useful for exhaustively enumerating redirect edge cases that are easy to miss in manual design.

---

## Future Extensions

**Multiple domains / distributed crawl**

The current design is single-process, single-domain. For multi-domain crawling the natural extension is a task queue (Celery + Redis, or AWS SQS) where each domain is an independent job. The `Crawler` class is already decoupled from `sys.stdout` and takes an injected `httpx.AsyncClient`, so it can be wrapped in a worker without touching its internals. A REST or gRPC API (FastAPI or gRPC + asyncio) would replace the CLI as the submission interface, with results streamed to object storage or a message bus rather than printed.

**Per-host rate limiting and `crawl-delay`**

Add a per-hostname `asyncio.Semaphore` or token bucket, seeded from `RobotsChecker.crawl_delay()`. The worker pool would acquire the per-host token before fetching and release it after the configured delay.

**Sitemap.xml discovery**

Fetch and parse `/sitemap.xml` (and `Sitemap:` directives from `robots.txt`) before the BFS starts. This surfaces URLs that are reachable but not linked from any crawled page, improving completeness for SPAs or large e-commerce sites.

**Persistent / resumable crawls**

Replace the in-memory `seen` set and queue with a SQLite or Redis-backed store. The crawler could checkpoint progress and resume after a crash or restart without re-crawling already-visited pages.

**Content extraction and structured output**

Add an optional extraction layer (JSON output, structured dataclasses per page) so the crawler can be used as a data pipeline stage rather than just a link printer.

**Subdomain wildcarding**

Add a `--include-subdomains` flag that changes `is_in_scope()` to match `hostname.endswith("." + scope_host) or hostname == scope_host`.

**HTTP/2**

Switch `http2=True` on the `AsyncClient`. Worth benchmarking against HTTP/1.1 on sites that support it — the win depends on the server's willingness to push and the concurrency level.

---

## The Test

> Create a Python app that can be run from the command line that will accept a base URL to crawl the site. For each page it finds, the script will print the URL of the page and all the URLs it finds on that page. The crawler will only process that single domain and not crawl URLs pointing to other domains or subdomains.

This solution fulfils the brief. The design prioritises correctness (formal accounting invariant, full edge-case coverage), speed (async worker pool, fast HTML parser), and maintainability (small focused modules, injected dependencies, no globals).
