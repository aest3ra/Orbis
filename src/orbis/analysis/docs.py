"""API documentation page endpoint extraction.

Detects API doc pages (Swagger UI, Redoc, apiDoc, etc.) and extracts
METHOD /path endpoint patterns from their HTML content.

No external dependencies — pure HTML text in, StaticEndpointRef list out.
"""

from __future__ import annotations

import json
import re
from html import unescape
from html.parser import HTMLParser

from orbis.analysis.js_static import StaticEndpointRef

# --- Patterns ---

_METHOD_PATH_RE = re.compile(
    r"""\b(?P<method>GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\s+"""
    r"""(?P<path>/[A-Za-z0-9][^\s<>"'`(){}]*)""",
    re.IGNORECASE,
)
_CODE_PATH_RE = re.compile(
    r"""<code[^>]*>\s*(?P<path>/[^<\s]+)\s*</code>""",
    re.IGNORECASE,
)
_COLON_PLACEHOLDER_RE = re.compile(r"/:[A-Za-z_][\w-]*")

# --- Detection markers ---

_DOC_URL_MARKERS = (
    "/api-doc",
    "/apidoc",
    "/docs",
    "/doc",
)
_STRONG_DOC_TEXT_MARKERS = (
    "api documentation",
    "api reference",
    "api docs",
    "http client testing service",
)

# --- Noise filters ---

_STATIC_PREFIXES = (
    "/assets/", "/css/", "/fonts/", "/img/", "/images/",
    "/js/", "/static/", "/vendor/",
)
_DOC_PREFIXES = (
    "/apidoc/", "/api-doc/", "/docs/",
)
_STATIC_SUFFIXES = (
    ".css", ".gif", ".ico", ".jpg", ".jpeg", ".js", ".map",
    ".png", ".svg", ".ttf", ".woff", ".woff2",
)


class _DocsHTMLParser(HTMLParser):
    """Extract form actions and link hrefs from HTML."""

    def __init__(self) -> None:
        super().__init__()
        self.refs: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr = {name.lower(): value or "" for name, value in attrs}
        if tag == "form":
            self.refs.append((attr.get("method", "GET"), attr.get("action", "")))
        elif tag == "a":
            self.refs.append(("GET", attr.get("href", "")))


def extract_doc_endpoints(body: str, base_url: str) -> list[StaticEndpointRef]:
    """Extract API endpoints from an API documentation page.

    Returns empty list if the page doesn't look like API documentation.
    The base_url is used only for detection heuristics (URL marker check),
    not for URL resolution — the caller handles that.
    """
    if not _looks_like_api_docs(body, base_url):
        return []

    seen: set[tuple[str, str]] = set()
    refs: list[StaticEndpointRef] = []

    for method, raw_path in _iter_documented_paths(body):
        key = (method, raw_path)
        if key in seen:
            continue
        seen.add(key)
        refs.append(StaticEndpointRef(method=method, raw_url=raw_path))

    return refs


def _looks_like_api_docs(body: str, base_url: str) -> bool:
    """Heuristic: does this look like an API documentation page?"""
    lowered_url = base_url.lower()
    if any(marker in lowered_url for marker in _DOC_URL_MARKERS):
        return True

    lowered = body[:20_000].lower()
    if any(marker in lowered for marker in _STRONG_DOC_TEXT_MARKERS):
        return True

    # apiDoc define() format
    if "define(" in lowered and '"api"' in lowered and '"url"' in lowered:
        return True

    # At least 2 METHOD /path patterns
    method_examples = 0
    for match in _METHOD_PATH_RE.finditer(body[:20_000]):
        if _looks_documented_endpoint_path(_sanitize_path(match.group("path"))):
            method_examples += 1
            if method_examples >= 2:
                return True

    return False


def _iter_documented_paths(body: str):
    """Yield (METHOD, path) pairs from HTML documentation."""
    # HTML forms and links
    parser = _DocsHTMLParser()
    parser.feed(body)
    for method, raw in parser.refs:
        path = _sanitize_path(raw)
        if _looks_documented_endpoint_path(path):
            yield method.upper(), path

    # METHOD /path text patterns
    for match in _METHOD_PATH_RE.finditer(body):
        path = _sanitize_path(match.group("path"))
        if _looks_documented_endpoint_path(path):
            yield match.group("method").upper(), path

    # <code>/path</code> tags
    for match in _CODE_PATH_RE.finditer(body):
        path = _sanitize_path(match.group("path"))
        if _looks_documented_endpoint_path(path):
            yield "GET", path

    # apiDoc define() format
    yield from _iter_apidoc_data_paths(body)


def _iter_apidoc_data_paths(body: str):
    """Extract endpoints from apiDoc define() format."""
    if '"api"' not in body or '"url"' not in body or not body.lstrip().startswith("define("):
        return
    payload = body.strip()
    if payload.startswith("define("):
        payload = payload[len("define("):]
    if payload.endswith(");"):
        payload = payload[:-2]
    elif payload.endswith(")"):
        payload = payload[:-1]
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return
    for item in data.get("api", []):
        if not isinstance(item, dict):
            continue
        method = str(item.get("type") or "GET").upper()
        raw = str(item.get("url") or "")
        path = _sanitize_path("/" + raw.lstrip("/"))
        if method and _looks_documented_endpoint_path(path):
            yield method, path


def _sanitize_path(raw: str) -> str:
    raw = unescape(raw.strip())
    if not raw or raw.startswith("#"):
        return ""
    raw = raw.split("#", 1)[0]
    raw = _COLON_PLACEHOLDER_RE.sub("/{id}", raw)
    return raw.rstrip(".,;:)")


def _looks_documented_endpoint_path(path: str) -> bool:
    if not path.startswith("/") or path == "/":
        return False
    lowered = path.lower()
    if lowered.startswith(_STATIC_PREFIXES) or lowered.startswith(_DOC_PREFIXES):
        return False
    if lowered.endswith(_STATIC_SUFFIXES):
        return False
    if "://" in lowered or lowered.startswith("//"):
        return False
    if any(char in path for char in "<>"):
        return False
    return True
