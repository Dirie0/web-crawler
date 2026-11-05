"""URL normalization and scope checking."""
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}
_IGNORED_SCHEMES = {"mailto", "tel", "javascript"}


def normalise(url: str, base: str | None = None) -> str | None:
    """Resolve url (optionally relative to base), strip fragment, lowercase scheme+host,
    drop default ports. Returns None for empty, non-http(s), or unresolvable URLs."""
    if not url or not url.strip():
        return None

    if base:
        url = urljoin(base, url)

    parts: SplitResult = urlsplit(url)

    if parts.scheme in _IGNORED_SCHEMES:
        return None

    if parts.scheme not in ("http", "https"):
        return None

    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    port = parts.port

    if port == _DEFAULT_PORTS.get(scheme):
        port = None

    netloc = host if port is None else f"{host}:{port}"

    # urlunsplit preserves path case; fragment is dropped by using empty string
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def is_in_scope(url: str, scope_host: str) -> bool:
    """Return True iff url's scheme is http/https and its hostname exactly matches scope_host."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return False
    return (parts.hostname or "") == scope_host
