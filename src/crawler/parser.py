"""HTML parsing."""
from urllib.parse import urljoin

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from crawler.urls import normalise


def extract_links(html: str, base_url: str) -> list[str]:
    """Extract and normalise all <a href> links from html.

    Resolves relative URLs against base_url (or <base href> if present),
    strips fragments, dedupes in first-seen order, skips empty/non-http hrefs.
    """
    tree = HTMLParser(html)

    # <base href> overrides the resolution base; it may itself be relative
    base_node = tree.css_first("base[href]")
    if base_node is not None:
        base_href = base_node.attributes.get("href", "")
        if base_href:
            base_url = urljoin(base_url, base_href)

    seen: set[str] = set()
    links: list[str] = []

    for node in tree.css("a[href]"):
        href = node.attributes.get("href", "")
        if not href:
            continue
        normalised = normalise(href, base=base_url)
        if normalised is None:
            continue
        if normalised not in seen:
            seen.add(normalised)
            links.append(normalised)

    return links
