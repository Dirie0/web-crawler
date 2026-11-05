"""Data models."""
from dataclasses import dataclass
from enum import Enum, auto


class FetchStatus(Enum):
    OK = auto()                   # HTML body available
    FAILED = auto()               # Unrecoverable HTTP/network error
    NON_HTML = auto()             # Content-Type not text/html
    REDIRECT_OFF_DOMAIN = auto()  # Hop left scope_host
    REDIRECT_LOOP = auto()        # >5 hops or cycle in this chain
    # Target URL was already in the global seen set; original counts as redirected
    REDIRECT_SEEN = auto()


@dataclass
class FetchOutcome:
    status: FetchStatus
    original_url: str
    final_url: str
    html: str | None = None
    redirect_hops: int = 0
