"""HTTP fetching and retries."""
import asyncio
from typing import Callable

import httpx

from crawler.models import FetchOutcome, FetchStatus
from crawler.urls import is_in_scope, normalise

_RETRY_STATUSES = frozenset({502, 503, 504})
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_MAX_HOPS = 5
_MAX_RETRY_AFTER = 5.0
_BACKOFF = 0.1


async def fetch(
    url: str,
    client: httpx.AsyncClient,
    scope_host: str,
    claim: Callable[[str], bool],
    *,
    timeout: float = 10.0,
) -> FetchOutcome:
    original = url
    chain: set[str] = {url}
    current = url
    hops = 0

    while True:
        resp = await _request_with_retry(client, current, timeout)
        if resp is None:
            return FetchOutcome(FetchStatus.FAILED, original, current, redirect_hops=hops)

        if resp.status_code in _REDIRECT_STATUSES:
            location = resp.headers.get("location")
            if not location:
                return FetchOutcome(FetchStatus.FAILED, original, current, redirect_hops=hops)

            target = normalise(location, base=current)
            if target is None:
                return FetchOutcome(FetchStatus.FAILED, original, current, redirect_hops=hops)

            if not is_in_scope(target, scope_host):
                return FetchOutcome(FetchStatus.REDIRECT_OFF_DOMAIN, original, target, redirect_hops=hops)

            if target in chain or hops >= _MAX_HOPS:
                return FetchOutcome(FetchStatus.REDIRECT_LOOP, original, target, redirect_hops=hops)

            # Claim before awaiting — no await between this check and claim
            if not claim(target):
                return FetchOutcome(FetchStatus.REDIRECT_SEEN, original, target, redirect_hops=hops)

            hops += 1
            chain.add(target)
            current = target
            continue

        if not (200 <= resp.status_code < 300):
            return FetchOutcome(FetchStatus.FAILED, original, current, redirect_hops=hops)

        if "text/html" not in resp.headers.get("content-type", ""):
            return FetchOutcome(FetchStatus.NON_HTML, original, current, redirect_hops=hops)

        return FetchOutcome(FetchStatus.OK, original, current, html=resp.text, redirect_hops=hops)


async def _request_with_retry(
    client: httpx.AsyncClient,
    url: str,
    timeout: float,
) -> httpx.Response | None:
    for attempt in range(2):
        try:
            resp = await client.get(url, follow_redirects=False, timeout=timeout)
        except (httpx.TimeoutException, httpx.ConnectError):
            if attempt == 0:
                await asyncio.sleep(_BACKOFF)
                continue
            return None

        if resp.status_code in _RETRY_STATUSES and attempt == 0:
            retry_after = resp.headers.get("retry-after")
            if retry_after:
                try:
                    delay = min(float(retry_after), _MAX_RETRY_AFTER)
                except ValueError:
                    delay = _BACKOFF
            else:
                delay = _BACKOFF
            await asyncio.sleep(delay)
            continue

        return resp

    return None
