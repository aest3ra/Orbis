"""Passive endpoint sources — archived URLs from third-party datasets.

Recon practice treats passive collection as the first coverage layer, merged
with active crawling: archives surface dead/unlinked/deprecated endpoints a
live crawl can never reach. We query the Wayback Machine CDX API (free, no
key) for a host's historical URLs. Failures are swallowed — passive is a
bonus, never a reason to abort a scan.
"""

from __future__ import annotations

import logging
import urllib.parse
import urllib.request

log = logging.getLogger("orbis.passive")

_CDX = "https://web.archive.org/cdx/search/cdx"
_UA = "Mozilla/5.0 (compatible; orbis-asm/1.0)"


def fetch_wayback_urls(host: str, *, limit: int = 5000, timeout: int = 30) -> list[str]:
    """Return distinct historical URLs archived under ``host`` (best effort)."""
    if not host:
        return []
    query = urllib.parse.urlencode({
        "url": host,
        "matchType": "domain",
        "fl": "original",
        "collapse": "urlkey",   # one row per distinct URL key
        "output": "text",
        "limit": str(limit),
    })
    req = urllib.request.Request(f"{_CDX}?{query}", headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "replace")
    except Exception as exc:
        log.warning("wayback fetch failed for %s (%s)", host, type(exc).__name__)
        return []

    urls: list[str] = []
    seen: set[str] = set()
    for line in body.splitlines():
        url = line.strip()
        if url and url not in seen:
            seen.add(url)
            urls.append(url)
    log.info("wayback: %d archived URLs for %s", len(urls), host)
    return urls
