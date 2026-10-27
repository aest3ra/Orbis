"""Static API endpoint discovery from JavaScript text.

One rule: string literals that carry an API path marker (/api, /rest, /graphql,
...). Call-pattern matching (fetch/axios/XHR/jQuery/this.http) and the
expression evaluator behind it were measured on the six-target baseline
(benchmarks/RESULTS.md, 2026-10-27): ~700 lines produced two real endpoints,
both plain literals that this scan finds as well. Bundled code builds its URLs
from aliased clients and service objects that no offline text scan resolves;
dynamic capture is the layer that finds those.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from orbis.analysis.api_markers import API_URL_RE, contains_api_marker

# Max endpoints per JS file to prevent explosion
MAX_REFS_PER_FILE = 200

# Route-pattern syntax means the literal is a router definition (`/api/users/:id`,
# `/api/v3/*`, `(/projects/:id)`) or a template literal cut at `${` (`/api/users/$`):
# a prefix or a pattern, not a request path.
_ROUTE_PATTERN_RE = re.compile(r"[*$()]|/:[A-Za-z_]")
# A path naming a source or asset module is a bundler reference, not a route.
_SOURCE_SUFFIXES = (".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".vue", ".svelte",
                    ".map", ".css", ".scss", ".less")


@dataclass(frozen=True)
class StaticEndpointRef:
    """A single API endpoint reference extracted from JS text."""
    method: str     # always GET: a literal carries no method
    raw_url: str    # extracted URL (relative or absolute)


def extract_js_endpoints(body: str) -> list[StaticEndpointRef]:
    """Return the API-marked URL literals in ``body``, deduplicated, in order.

    URLs are not resolved; the caller resolves relative ones against the page URL.
    """
    if not contains_api_marker(body):
        return []
    refs: list[StaticEndpointRef] = []
    seen: set[str] = set()
    for match in API_URL_RE.finditer(body):
        url = match.group("url").rstrip(".,;:")
        if url in seen or not _is_request_path(url):
            continue
        seen.add(url)
        refs.append(StaticEndpointRef(method="GET", raw_url=url))
        if len(refs) >= MAX_REFS_PER_FILE:
            break
    return refs


def _is_request_path(url: str) -> bool:
    path = urlsplit(url).path if url.startswith(("http://", "https://")) else url.split("?", 1)[0]
    if _ROUTE_PATTERN_RE.search(path):
        return False
    return not path.rsplit("/", 1)[-1].lower().endswith(_SOURCE_SUFFIXES)
