# Crawler spec (condensed from the final plan)

## Goal
CLI: `crawler <base_url>`. For every page on that exact host, print the page URL and every URL found on it. 3-hour budget. Judged on design, structure, tests, explanation. No Scrapy/Playwright.

## Assumptions
- Server-rendered HTML; no JS rendering, no meta-refresh.
- Scope = exact hostname of base URL. Subdomains and www variants are out.
- Base URL redirecting to another host: exit code 2, message "base URL redirects to <host>, re-run with that URL". Same-host redirect (http->https) is followed.
- "URLs on a page" = href of every `<a>`, deduped per page in first-seen order. Out-of-scope links are listed but never fetched.
- Only http/https. mailto:, tel:, javascript: ignored.
- Non-HTML responses: not parsed, not printed, counted `non_html`. Failed pages not printed.
- Fragments ignored, query strings significant.
- robots.txt honoured (RFC 9309). Concurrency cap (default 10) is NOT a rate limit; Crawl-delay not read.
- One process, seen set in memory, no auth.

## Guarantees (each must be provable by a test)
- Scheduling: each normalised URL scheduled at most once; retries are the only repeat requests.
- Scope: nothing outside the scope host is ever requested, including redirect hops.
- Completeness: discovered = crawled + failed + redirected + skipped. Skip reasons only: robots, non_html, redirect_off_domain, redirect_loop, cap_reached. No catch-all.
- Resources: in-flight requests (MockTransport high-water mark) never exceed --concurrency.

## Link checks (every link, in order)
1. Resolve against the page's final URL (or `<base href>`), drop fragment, lowercase scheme+host, drop default ports.
2. Scheme http/https else drop.
3. Host == scope host else print, never fetch, count out_of_scope.
4. Not in `seen` else ignore.
5. Claim in `seen`, then enqueue (only way a URL enters the queue).

## Redirects
- Manual, one hop at a time, scope-check before each request. 301/302/303/307/308 treated the same.
- Relative Location joined with urljoin. Missing Location = failed. Off-domain hop = never requested, `redirect_off_domain`. Loop (per-chain set) or >5 hops = `redirect_loop`.
- Target already in `seen` = not fetched; original counted `redirected` (its owner prints it). Target unseen = claim BEFORE awaiting the request.
- Page reached via redirect: print final URL, mark original seen too, resolve links against final URL.
- The check and the claim are one block with no await between them.

## robots.txt
- Fetch once with httpx before workers start, from the resolved base URL origin; feed `RobotFileParser.parse(lines)`. Never call `.read()`.
- 200 parse; any 4xx allow all; 5xx or network failure disallow all + stderr warning; redirects up to 5 hops then apply final status; >5 hops = allow all.
- Disallowed URL counted `robots`. One policy per host (documented simplification).

## Retry and errors
- Retry once on timeout, connection error, 502/503/504. Short fixed backoff; honour Retry-After capped ~5s.
- Not retried: 404, 410, other 4xx, 500, non-HTML.
- Retries counted separately, do not consume --max-pages.
- Workers catch httpx.HTTPError and parser errors; a bad page = failed, worker survives.
- Streamed request: read headers first, only parse text/html, cap body size.

## max-pages
Counts scheduled URLs (claims on `seen`). Default 10,000. At cap: further in-scope links are marked seen and counted `cap_reached`. Redirect hops and retries don't consume it.

## Concurrency rules
- asyncio.Queue + N workers, `queue.join()` for termination, unbounded queue.
- Children enqueued before `task_done()`, which is in `finally`.
- Counters are plain `+= 1` (safe, single thread). One shared AsyncClient, pool size matches --concurrency.

## Output
One block per page, built as one string and written in one call, flushed:
```
https://example.com/about
  https://example.com/
  https://example.com/team
  https://blog.example.com/
```
Final URL on its own line, links indented. stdout = pages only. Summary to stderr: crawled, failed, redirected, skipped by reason, discovered, retries, redirect hops, elapsed, pages/sec, concurrency.

## Architecture
```
src/crawler/{__init__,cli,crawler,fetcher,parser,urls,robots,models}.py
tests/{test_urls,test_parser,test_robots,test_fetcher,test_crawler_e2e}.py
pyproject.toml  README.md  .github/workflows/ci.yml
```
- urls.py: `normalise()`, `is_in_scope()`
- parser.py: `extract_links(html, base_url) -> list[str]` (selectolax)
- fetcher.py: timeout, retry, manual redirects, content-type check
- robots.py: `RobotsChecker`
- models.py: `PageResult`, `CrawlStats`
- crawler.py: queue, workers, termination, --max-pages
- cli.py: argparse (--concurrency, --max-pages, --timeout), exit codes
