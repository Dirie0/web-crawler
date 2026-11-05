# Single-domain async web crawler

A command-line crawler that takes a base URL and, for every page it finds on that **one exact host**, prints the page's URL followed by every URL found on it. It is concurrent (asyncio + httpx), respects `robots.txt`, follows redirects only within scope, and accounts for every URL it encounters with a checkable invariant.

**Where to look first (for reviewers)**

| If you want to see… | Look at |
|---|---|
| Correctness guarantees and how they are proved | [Correctness](#correctness) |
| Concurrency and speed decisions | [Concurrency and performance](#concurrency-and-performance) |
| Why each design choice was made, and what I rejected | [Design decisions](#design-decisions-and-options-considered) |
| How it was tested | [Testing](#testing) and [Manual verification](#manual-verification-against-a-live-test-site) |
| How I used AI tools | [AI usage and development process](#ai-usage-and-development-process) |
| What I would do next | [Known limitations](#known-limitations) and [Future work](#future-work) |

## Contents

1. [Quick start](#quick-start)
2. [Usage](#usage)
3. [Example output](#example-output)
4. [How it works](#how-it-works)
5. [Architecture](#architecture)
6. [Correctness](#correctness)
7. [Concurrency and performance](#concurrency-and-performance)
8. [Reliability](#reliability)
9. [Design decisions and options considered](#design-decisions-and-options-considered)
10. [Assumptions](#assumptions)
11. [Edge cases handled](#edge-cases-handled)
12. [Testing](#testing)
13. [Manual verification against a live test site](#manual-verification-against-a-live-test-site)
14. [CI](#ci)
15. [Known limitations](#known-limitations)
16. [Future work](#future-work)
17. [Scaling beyond a command line](#scaling-beyond-a-command-line)
18. [AI usage and development process](#ai-usage-and-development-process)

---

## Quick start

Requires Python 3.11 or newer.

```bash
git clone <repository-url>
cd <project-directory>

python3 -m venv .venv
source .venv/bin/activate

python3 -m pip install -e ".[dev]"

crawler https://example.com            # or: python3 -m crawler https://example.com
```

Run the checks:

```bash
pytest -q          # unit and integration tests, no network needed
ruff check .       # lint and import order
mypy src           # static types
```

## Usage

```
crawler [-h] [--concurrency N] [--max-pages N] [--timeout SECS] base_url
```

| Argument | Default | Meaning |
|---|---|---|
| `base_url` | required | URL to start from. Its exact hostname defines the crawl scope |
| `--concurrency` | 10 | Maximum number of in-flight HTTP requests (also the number of workers) |
| `--max-pages` | 10000 | Maximum number of URLs scheduled. Bounds work and memory on very large or infinite sites |
| `--timeout` | 10.0 | Per-request timeout in seconds |

**Exit codes**

| Code | Meaning |
|---|---|
| 0 | Crawl completed |
| 2 | The base URL redirects to a different host. The crawler does not silently change scope, it tells you which URL to re-run with |

**Streams.** Page blocks go to **stdout**, so output can be piped (`crawler https://example.com | grep /product`). The run summary and all warnings go to **stderr**.

## Example output

Each crawled page is one block: the page's final URL (after any in-scope redirects), then every link found on it, indented.

```
https://crawler-test.com/mobile/separate_desktop_with_different_h1
  https://crawler-test.com/
https://crawler-test.com/mobile/separate_desktop_with_different_links_in
  https://crawler-test.com/
  https://crawler-test.com/mobile/separate_desktop_with_different_links_out
```

- Links are normalised and de-duplicated per page in first-seen order. Fragments are stripped.
- Out-of-scope links (other domains, subdomains) **are listed** so the output is a complete record of the page, but they are **never requested**.
- Failed pages, non-HTML responses and `robots.txt`-disallowed pages are not printed. They are counted in the summary.
- Pages are printed as they finish, so output order is not breadth-first order.

The summary, on stderr:

```
crawled=10 failed=0 redirected=0 skipped(cap_reached=400) discovered=410 retries=0 redirect_hops=0 elapsed=0.6s pages/sec=17.1 concurrency=5
```

### Screenshots

Real runs against [crawler-test.com](https://crawler-test.com), a public site built to exercise crawlers.

**A normal crawl**: page blocks on stdout, summary on stderr.
`crawler https://crawler-test.com/ --max-pages 10 --concurrency 5`

![Normal crawl](screenshot/1.png)

**A three-hop redirect chain**: the final page is printed once, all three hops are counted as `redirected`, and the equation balances (1 + 0 + 3 + 1 = 5).
`crawler https://crawler-test.com/redirects/redirect_3_302 --max-pages 3 --concurrency 1`

![Redirect chain](screenshot/2.png)

**A redirect loop**: stops cleanly and is counted as `redirect_loop`.
`crawler https://crawler-test.com/redirects/infinite_redirect --max-pages 5 --concurrency 1`

![Redirect loop](screenshot/3.png)

**A redirect off the host**: never followed. The base URL case exits with code 2 and names the host to re-run with.
`crawler https://crawler-test.com/redirects/external_redirect --max-pages 5 --concurrency 1; echo "exit=$?"`

![Off-host redirect](screenshot/4.png)

---

## How it works

1. Normalise the base URL. Its hostname becomes the **scope host**.
2. Fetch `robots.txt` once, before any worker starts, and cache the rules.
3. Claim the base URL in the `seen` set and put it on an `asyncio.Queue`.
4. Start `--concurrency` workers. Each worker loops:
   1. take a URL from the queue;
   2. skip it if `robots.txt` disallows it;
   3. fetch it, following redirects **manually**, one hop at a time, checking scope before every hop;
   4. skip non-HTML responses;
   5. extract `<a href>` links, resolve them against the page's final URL (or `<base href>`) and normalise them;
   6. print the page block;
   7. for each in-scope link not yet seen: claim it in `seen`, then enqueue it. Once `--max-pages` claims exist, further in-scope links are counted as `cap_reached`.
5. Children are queued **before** `task_done()` is called, and `task_done()` sits in a `finally`, so `queue.join()` cannot return early or hang.
6. When the queue drains, cancel the workers and print the summary.

Every link passes five checks before it can be queued:

| # | Check | If it fails |
|---|---|---|
| 1 | Resolve against the page's final URL (or `<base href>`), drop the fragment, lowercase scheme and host, drop default ports, empty path becomes `/` | n/a |
| 2 | Scheme is `http` or `https` | Dropped (`mailto:`, `tel:`, `javascript:` are not pages) |
| 3 | Hostname equals the scope host exactly | Listed on the page, never fetched |
| 4 | Not already in `seen` | Ignored |
| 5 | Claim in `seen`, then enqueue | This is the only way a URL enters the queue |

## Architecture

Seven small modules. Pure functions sit at the edges, and all network I/O is isolated in two files.

```
src/crawler/
├── cli.py        argparse, wiring, exit codes, summary formatting
├── crawler.py    Crawler: queue, workers, termination, --max-pages, stats
├── fetcher.py    HTTP fetch: timeout, retry, manual redirects, content-type check   (I/O)
├── robots.py     RobotsChecker: fetch once, RFC 9309 status handling, cache        (I/O)
├── parser.py     extract_links(html, base_url) -> list[str]                        (pure)
├── urls.py       normalise(), is_in_scope()                                        (pure)
└── models.py     FetchStatus, FetchOutcome, CrawlStats
tests/            one test module per source module, plus end-to-end tests
```

Design rules I held to:

- `urls.py` and `parser.py` are pure functions, so they are trivially testable.
- `Crawler` receives an injected `httpx.AsyncClient` and an output stream. Tests use `httpx.MockTransport` and `io.StringIO`, so there is no network and no patching of globals.
- Logging and process-level wiring happen only in `cli.py`.
- No bare `except Exception`. Workers catch `httpx.HTTPError` and parser errors specifically, so one bad page cannot kill a worker.

## Correctness

Every URL the crawler claims is accounted for in **exactly one** outcome:

```
discovered = crawled + failed + redirected + skipped_total
```

`skipped_total` is the sum of **named** reasons only, with no catch-all bucket:
`robots`, `non_html`, `redirect_off_domain`, `redirect_loop`, `redirect_seen`, `cap_reached`.

`CrawlStats.equation_holds()` checks this, and every integration test asserts it. Because redirect chains involve awaits between hops, they are where accounting is easiest to break: each intermediate 3xx URL that was claimed is counted as `redirected`, and a hop that would revisit a URL already in the same chain is counted as `redirect_loop`.

**What the equation proves:** no URL is silently dropped or counted twice, including URLs claimed along redirect chains.
**What it does not prove:** that every real page was found (links inserted by JavaScript are not), or that the extracted links are right. Those are covered by the link-extraction tests and by manual runs against a live site.

Guarantees, each worded so a test can prove it:

| Guarantee | How it is enforced | Proved by |
|---|---|---|
| Each normalised URL is scheduled **at most once**. Deliberate retries are the only repeat requests | Check and claim in `seen` with no `await` between them, including redirect targets | Fetcher tests (claim ordering), e2e call log |
| Nothing outside the exact scope host is ever requested, **including redirect hops** | Scope check before every hop | Off-domain redirect tests, e2e call log |
| Every claimed URL ends in exactly one outcome | Named counters, one increment per outcome | `equation_holds()` in every e2e test |
| In-flight requests never exceed `--concurrency` | One worker per slot, no unbounded task spawning

## Concurrency and performance

Crawling is I/O bound. Throughput is roughly *requests in flight ÷ average response time*, so the aim is to keep as many requests in flight as the target can sensibly take without wasting requests or CPU.

| Technique | Why it helps | Trade-off |
|---|---|---|
| asyncio worker pool on one shared queue | Many requests wait on the network at once on a single thread, with no per-request thread cost | More workers raise load on the target, so the count is capped |
| No level-by-level barriers | A slow page ties up one worker, not the whole crawl | Output order is not by depth |
| One shared `httpx.AsyncClient` with a connection pool | Keep-alive skips the TCP and TLS handshake on most requests | Pool size is set to match the worker count |
| `selectolax` (Lexbor) for parsing | Parsing is the only CPU-heavy step and runs on the event-loop thread, so a fast C parser keeps the loop free | Smaller API than BeautifulSoup. I chose it for speed and did not benchmark parsers |
| Dedup before enqueue | A seen URL is never requested twice, and lookups are constant time | The set lives in memory |
| `robots.txt` fetched once | One request for the whole crawl | Mid-crawl changes are not seen |
| Non-HTML responses are not parsed | No CPU spent on PDFs or images | n/a |
| Per-request timeout and one retry | A hung server cannot pin a worker forever | A slow but valid page may be cut off |
| Output printed as each page finishes | Nothing waits for the crawl to end, and parsed pages are not retained | The frontier still grows (see limitations) |

**Resource bounds.** In-flight requests and in-progress parses are bounded by `--concurrency`. The frontier (queue plus `seen` set) grows with the site and is bounded by `--max-pages`. It holds one normalised string per discovered URL, roughly 100 to 200 bytes each.



## Reliability

- **Timeouts:** every request has a timeout (`--timeout`, default 10 s), so a hung server cannot pin a worker.
- **One retry** on transient failures only: timeouts, connection errors, and 502, 503, 504. A short backoff applies, and `Retry-After` is honoured but capped at 5 seconds so one hostile server cannot stall the crawl. A second failure is final. 404, 410, other 4xx and 500 are not retried.
- **Retries are separate from scheduling.** A URL is scheduled once but its call log may show two requests. Retries are counted on their own and do not consume `--max-pages`.
- **A bad page never kills a worker.** HTTP and parse errors are caught specifically, counted as `failed`, and the run continues.
- **`task_done()` is always in a `finally`**, so termination cannot hang.
- **`robots.txt` failures** follow RFC 9309 (see the table in design decisions): unavailable (4xx) allows all, unreachable (5xx or network error) disallows all with a warning on stderr.

## Design decisions and options considered

| Decision | Choice | Alternative I rejected | Why |
|---|---|---|---|
| Concurrency model | `asyncio` worker pool, shared `Queue`, `queue.join()` | Threads (heavier, GIL contention when parsing); multiprocessing (overkill); level-by-level BFS in batches (one slow page stalls the batch) | Crawling is I/O bound. Thousands of requests can be in flight on one thread at low memory |
| HTTP client | `httpx` async, one shared client, HTTP/1.1 | `aiohttp` | Built-in pooling and `MockTransport` make injection and network-free tests simple. I kept HTTP/1.1 for simplicity and did not benchmark HTTP/2 |
| Parser | `selectolax` (Lexbor) | `bs4` + `lxml` | Faster, and CSS selectors cover everything needed. Less familiar API, no XPath |
| Dedup | In-memory `seen` set, checked and claimed before enqueue | Bloom filter | A Bloom filter's false positive would silently skip a real page. No lock is needed on single-threaded asyncio |
| Queue | Unbounded, growth capped by `--max-pages` | Bounded queue | A bounded queue can deadlock when workers are both the only producers and the only consumers |
| Scope | `urlsplit(url).hostname == scope_host` and scheme is http/https | Registered-domain match | The brief says no other domains **or subdomains**. A `--include-subdomains` flag is a small extension |
| Redirects | Manual, one hop at a time, scope-checked on every hop | `follow_redirects=True` | With auto-follow the request to an off-domain host has already been sent before any check, and hop limits and mid-chain dedup are not controllable. Cost: about 25 more lines and more tests |
| `robots.txt` | Own fetch with RFC 9309 status handling, then `RobotFileParser.parse(lines)` | `RobotFileParser.read()`; `protego` | `.read()` treats 401 and 403 as disallow-all, which contradicts RFC 9309. `protego` has better wildcard support but adds a dependency |
| Load control | Concurrency cap and a descriptive User-Agent | Per-host token-bucket rate limit | Cheap and defensible. It bounds load but is **not** a rate limit, and `Crawl-delay` is not read |
| Output | Stream each page block to stdout as it completes, summary to stderr | Collect everything, then print | Keeps stdout clean for piping, shows progress, and memory does not grow with output |
| `--max-pages` | Counts claims on `seen`, including redirect hops and targets | Count only completed downloads | Bounds scheduled work, so a long redirect chain cannot blow past the cap |
| URL identity | Fragments ignored, query strings significant, path case preserved | Lowercasing paths, dropping query strings | Paths are case-sensitive per RFC 3986. `/search?q=a` and `/search?q=b` are different pages |

`robots.txt` status policy:

| Response | Behaviour |
|---|---|
| 200 | Parse the rules |
| Any 4xx | Allow all |
| 5xx or network failure | Disallow all, with a warning on stderr |
| Redirect | Follow up to 5 hops, then apply the final status. More than 5 hops is treated as unavailable (allow all) |

## Assumptions

The brief leaves some points open. These are my readings, stated so a reviewer can disagree with them.

- **Site type:** a conventional server-rendered HTML site. No JavaScript rendering and no meta-refresh handling, so script-inserted links are not found.
- **"Single domain":** the exact hostname of the base URL. Subdomains and `www.` variants are out of scope.
- **Base URL redirecting to another host:** the crawler exits with code 2 and a message instead of silently changing scope. A same-host redirect (http to https) is followed.
- **"URLs found on a page":** the `href` of every `<a>` element. Images, scripts and stylesheets are not listed.
- **Out-of-scope links** appear in the page's link list but are never fetched.
- **Link schemes:** only `http` and `https`. `mailto:`, `tel:` and `javascript:` are ignored.
- **Non-HTML responses** (PDF, images) are not parsed or printed, and are counted as `non_html`.
- **Politeness:** `robots.txt` is honoured. The concurrency cap bounds load but is not a rate limit, and `Crawl-delay` is not read.
- **Environment:** one process on one machine, with the whole `seen` set in memory. No authentication.

## Edge cases handled

| Scenario | Handling |
|---|---|
| Self-redirect (`/a` to `/a`) | Detected by the per-chain visited set, counted `redirect_loop` |
| Redirect cycle (`/a` to `/b` to `/a`) | Same per-chain set detects the revisit |
| Redirect chain longer than 5 hops | Hard limit, counted `redirect_loop` |
| Redirect to a URL already claimed by another worker | `claim()` returns false, counted `redirect_seen` (its owner prints it) |
| Two chains redirecting to the same target | First claim wins. The second is `redirect_seen`, not a loop |
| Base URL redirects off-host | `OffDomainRedirect`, exit code 2 |
| Mid-chain redirect leaves scope | Chain aborted before the request, counted `redirect_off_domain` |
| HTTP to HTTPS on the same host | Followed (hostname still matches) |
| Missing `Location` on a 3xx | Treated as a failure |
| `Retry-After: 100` | Capped at 5 s |
| Non-HTML content type | Not parsed, counted `non_html` |
| `robots.txt` 404, 401, 403 | Allow all (RFC 9309) |
| `robots.txt` 5xx or network error | Disallow all, with a warning |
| `robots.txt` more than 5 redirects | Allow all |
| `<base href>` (including a relative one) | Used as the resolution base for relative links |
| `mailto:`, `tel:`, `javascript:` | Skipped by `normalise()` |
| Fragment-only or fragment-bearing links | Fragment stripped, path de-duplicated |
| Bare host (`https://host`) vs `https://host/` | Normalised to the same URL |
| Default port (`:443`, `:80`) | Stripped |
| Malformed HTML | Lexbor is error-tolerant, so it extracts what it can |
| Duplicate links on one page | De-duplicated in first-seen order |
| `<a>` without `href` | Skipped |
| Path case | Preserved. Only scheme and host are lowercased |

## Testing

```bash
pytest -q
```


118 tests, none of which touch the network. All HTTP is exercised through `httpx.MockTransport`, so the suite is fast and deterministic.

| File | What it covers |
|---|---|
| `test_urls.py` (30) | `normalise()` and `is_in_scope()`: scheme and host casing, default ports, fragments, relative resolution, `..` segments, ignored schemes, every scope boundary (subdomain, `www`, different TLD) |
| `test_parser.py` (16) | `extract_links()`: absolute and relative links, `<base href>`, fragments, first-seen de-duplication, `mailto:`/`javascript:` skipped, `<img src>` and `<link href>` excluded, malformed HTML, query strings preserved |
| `test_robots.py` (13) | `RobotsChecker`: 200, 404, 401, 403, 500, 503, network errors, disallow rules, redirects followed up to 5 hops, more than 5 allows all |
| `test_fetcher.py` (40) | `fetch()`: happy path, 4xx and 5xx variants, retry on 502 to 504 and network errors, `Retry-After` capping, all five redirect codes, relative `Location`, off-domain redirects, self-redirect loops, N-cycle loops, hop limit, `redirect_seen` vs `redirect_loop`, claim-before-await ordering, accurate `redirect_hops` |
| `test_crawler_e2e.py` (19) | Full multi-page mock sites: every page printed once, correct indentation, the completeness equation in every redirect, loop and robots scenario, `OffDomainRedirect`, summary formatting |

**The equation as an oracle.** Every integration scenario calls `equation_holds()` and asserts it. This catches accounting bugs independently of whichever counter a particular test is looking at.



## Manual verification against a live test site

Automated tests use mocks, so I also ran the crawler against [crawler-test.com](https://crawler-test.com), comparing each run's summary counters with what I expected beforehand. The site changes over time, so the numbers below are from my runs and may differ slightly.

| Command target | What it exercises | Result |
|---|---|---|
| `/redirects/redirect_2` | 2-hop chain | `redirected=2`, `redirect_hops=2`, final page printed once |
| `/redirects/redirect_3_302` | 3-hop chain (`302`, `301`, `301`) | `crawled=1 redirected=3 skipped(cap_reached=1) discovered=5 redirect_hops=3` |
| `/redirects/infinite_redirect` | Self-redirect | Stops immediately, counted `redirect_loop` |
| `/redirects/two_step_redirect_loop_1` | Redirect cycle | Stops, counted `redirect_loop` |
| `/redirects/external_redirect` | Redirect off the host | Exit code 2, message names the host to re-run with, no request sent to it |
| `/` with `--max-pages 10 --concurrency 5` | Ordinary crawl | `crawled=10 skipped(cap_reached=400) discovered=410`, about 17 pages/sec |

**Bugs this found that the mocked tests had missed**

1. **Redirect loops were counted as `redirected`, not `redirect_loop`.** The "already in `seen`" check ran before the per-chain visited-set check, so a loop back to a claimed URL looked like "someone else owns it". Fixed by checking the chain first, then adding regression tests.
2. **A bare root URL (`https://host`) was not normalised to `https://host/`.** The homepage linked to itself in both forms, so it would have been scheduled twice, breaking the at-most-once guarantee. Fixed in `normalise()`, with regression tests.

Both were caught by reading the summary counters against the completeness equation, which is why I keep it as the main oracle.

## CI

GitHub Actions (`.github/workflows/ci.yml`) runs on every push and pull request to `main`, on Python 3.11 and 3.12:

```
ruff check .      # lint and import order
mypy src          # static types
pytest -q         # tests, no network
```

## Known limitations

- **No JavaScript rendering and no meta-refresh handling**, so links added by scripts are not found.
- **The frontier is in memory.** `--max-pages` bounds it, but a very large crawl would need the queue and `seen` set moved to disk or Redis.
- **No rate limit and no `Crawl-delay`.** The concurrency cap bounds simultaneous requests, not requests per second.
- **One `robots.txt` policy per host,** not per origin (scheme, host and port). Relevant only if a site serves different rules over http and https.
- **Weak wildcard support in `robotparser`** for `*` and `$` patterns. `protego` is the drop-in fix.
- **4xx rate limiting (429) is not retried.** It counts as `failed`. Adaptive backoff is listed under future work.
- **No resume.** An interrupted crawl starts again from scratch.
- **No SSRF protection.** The crawler does not block private or loopback addresses, so it should only be pointed at sites you trust.

## Future work

In the order I would build them, with rough effort:

| Item | Problem it solves | How | Effort |
|---|---|---|---|
| Automated concurrency benchmark in CI-friendly form | Speed is measured by hand today | Local mock server with fixed delay, results table generated by a script | ~1 h |
| Resume from the output file | A crash or Ctrl-C throws away all progress | The output (one JSON line per page) is the checkpoint. `--resume` marks every page in it as seen and rebuilds the queue from links found but never crawled | ~1 h |
| Failure ledger and `--retry-failed` | Failed pages are only counted, so they cannot be retried | Write each failure with URL, status or exception, attempts and timestamp, then re-queue only those | ~0.5 h |
| Token-bucket `--rate` | The cap limits requests in flight, not requests per second | A bucket refilled at R tokens per second, used together with the cap. Seed it from `Crawl-delay` | ~0.5 h |
| Adaptive backoff | A server answering 429 or 503 under load gets hammered further | Honour `Retry-After`, halve the concurrency after a burst of 429/503, raise it slowly | ~1 h |
| Per-origin `robots.txt` and `protego` | One policy per host ignores scheme and port differences, and wildcards are weak | One cached policy per origin | ~0.5 h |
| Sitemap seeding | Pages nothing links to are never found | Read `sitemap.xml` (and `Sitemap:` lines in `robots.txt`), seeding the queue through the same scope checks | ~1 h |
| Meta-refresh redirects | Not followed today | Treat `<meta http-equiv="refresh">` as a redirect hop with the same scope checks | ~0.5 h |
| SSRF protection | A crawl could be steered at private addresses | Block private, loopback and link-local ranges after DNS resolution, including on redirect hops | ~1 h |
| Reconciliation report | Counts alone do not show *which* URL is in which bucket | `report.json` listing every in-scope URL discovered with its outcome | ~1 h |
| `--include-subdomains` flag | Some users want subdomains | Change the scope check to `hostname == scope_host or hostname.endswith("." + scope_host)` | ~0.25 h |
| HTTP/2 | May improve throughput on supporting servers | Benchmark `http2=True` against HTTP/1.1 | ~0.5 h |

## Scaling beyond a command line

A CLI runs one crawl in one process and loses everything if it dies, so it does not suit crawling many domains at once. A service version would look like this:

- A client submits a **crawl job** (domain plus options) to an HTTP API (for example FastAPI), which puts it on a durable queue such as SQS or Redis.
- **Stateless workers** pick up jobs. Each domain gets its own frontier, seen set and rate limit, so one slow site cannot starve the others.
- The **frontier moves to Redis or SQS** and the **seen set to a persistent store**, replacing the in-memory structures.
- **Per-host politeness** (rate, backoff, `robots.txt` cache) lives with the domain's frontier.
- **Results** go to one output file per domain in object storage. The domain is the natural unit to shard on.
- The **failure ledger becomes a dead-letter queue**, and the reconciliation report becomes a per-job health check.
- A **job status endpoint** replaces the console summary, and an HTTP or queue-triggered API replaces the CLI as the entry point.

The current code is already close to this shape: `Crawler` takes an injected client and output stream and holds no globals, so it can be wrapped in a worker without changing its internals.

---

## AI usage and development process

This section describes how I actually worked, including where AI was used.

### Planning and research (before any code)

- I wrote a planning note covering functional requirements, non-functional requirements, and the assumptions I made to reduce the problem to something solvable in the time.
- I researched crawler architectures (breadth-first vs depth-first, frontier and dedup strategies), redirect handling and robots.txt behaviour.
- I gave Claude as much of that context as I could and asked it to produce a design document. That document, and the per-stage notes derived from it, are in, planning docs in `claude-docs/` if you want to read them. 

### Implementation workflow

I built the crawler in stages: URL handling and parsing, fetcher, `robots.txt`, crawler core, CLI, tests, tooling and CI, then README. For each stage:

1. I wrote **pseudocode** for the main functions myself, with expected logic, data structures, i/o  with as much context as I could about what the stage had to do.
2. I pasted that, with the relevant files and the problem, into **Claude** chat to fill out the implementation.
3. I **reviewed** the result and **refactored** it where I disagreed with the structure or the error handling.
4. I **ran and checked** it before moving to the next stage.

### Tests

Claude generated the first draft of the unit and integration tests from my list of cases. I reviewed each test and edited where i disagreed.

### Testing Issues

- Ran the crawler against crawler-test.com and compared each summary line with what I expected beforehand (see [Manual verification](#manual-verification-against-a-live-test-site)).
- That found two bugs the mocked tests had missed (redirect loops counted as `redirected`; root URL not normalised to `/`). I pasted the failing output back to Claude, identified which files the problem lay in, reviewed the proposed fix, implemented it and added regression tests.


### Tools

| Tool | Role |
|---|---|
| 
| Claude [chat] | Design discussion, implementation from my pseudocode, debugging, test cases, README review |
| `httpx` | Async HTTP client with connection pool and `MockTransport` |
| `selectolax` (Lexbor) | Fast HTML parsing for link extraction |
| `pytest` + `pytest-asyncio` | Test runner with async support |
| `ruff` | Linting and import order |
| `mypy` | Static type checking |
| GitHub Actions | CI on Python 3.11 and 3.12 |

### Learning resources

[the `httpx` and `asyncio` documentation, RFC 9309 (robots.txt), RFC 3986 (URI syntax and path case sensitivity), the `selectolax` documentation, articles on web crawler architecture, and [crawler-test.com](https://crawler-test.com) as the live verification target.

---

## The brief

> Create a Python app that can be run from the command line that will accept a base URL to crawl the site. For each page it finds, the script will print the URL of the page and all the URLs it finds on that page. The crawler will only process that single domain and not crawl URLs pointing to other domains or subdomains.

The crawler does this: it prints each in-scope page's URL with every link found on it, requests nothing outside the exact base host, and runs its requests concurrently under a fixed cap. The sections above set out the decisions behind that, how correctness is checked, and where its limits are.