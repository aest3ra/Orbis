"""Core analyzer: takes raw PageCapture, returns frontier URLs + endpoints.

This is the single judgment layer. Capture observes, Analyzer decides.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

from orbis.crawler.browser import DomElement, NetworkEvent, PageCapture
from orbis.crawler.scope import Scope
from orbis.analysis.classifier import API_MARKER, ASSET_SUFFIXES, classify
from orbis.analysis.params import extract_params, infer_type
from orbis.analysis.url import templatize_path

log = logging.getLogger("orbis.analyzer")

MAX_SAMPLES = 5

# --- Inline URL noise filters (classification belongs in Analyzer, not Capture) ---
_INLINE_NOISE = re.compile(
    r"\.(js|css|png|jpg|jpeg|gif|svg|ico|woff2?|ttf|eot|map|webp|avif|webmanifest)(\?|$)",
    re.I,
)
_INLINE_SKIP = re.compile(r"^/(_next/static|static/|__webpack|node_modules)")

# --- Hidden input name whitelist for URL extraction ---
# Split name by common separators (_, -, .) and check segments against whitelist.
# Using segment matching instead of \b because \b treats _ as a word character,
# which would miss common patterns like "redirect_url".
_URL_INPUT_KEYWORDS = frozenset({
    "url", "redirect", "next", "return", "callback", "goto",
    "continue", "dest", "link", "href", "endpoint", "uri",
    "path", "target", "referer", "referrer", "back",
})
_NAME_SPLIT = re.compile(r"[-_.\[\]]+")


@dataclass
class NormalizedParam:
    location: str
    name: str
    type_inferred: str
    sample_values: list[str] = field(default_factory=list)
    seen_count: int = 0


@dataclass
class NormalizedEndpoint:
    method: str
    host: str
    path_template: str
    sample_url: str
    route_kind: str
    seen_count: int = 1
    source: str = "dynamic"           # dynamic | static_js | static_openapi | static_docs
    discovered_via: str | None = None  # None = passive load; else interaction label
    params: dict[tuple[str, str], NormalizedParam] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    frontier_urls: list[str]
    endpoints: list[NormalizedEndpoint]


def analyze(capture: PageCapture, scope: Scope) -> AnalysisResult:
    """Classify traffic, extract links, normalize endpoints."""
    frontier_urls: list[str] = []
    endpoints: dict[tuple[str, str, str], NormalizedEndpoint] = {}

    base_url = capture.final_url
    for elem in capture.dom_elements:
        if elem.tag == "base" and elem.attributes.get("href"):
            base_url = urljoin(capture.final_url, elem.attributes["href"])
            break

    # --- Phase 1: Dynamic endpoint discovery (network events) ---

    for event in capture.network_events:
        if not scope.allows(event.url):
            continue
        kind = classify(event)
        if kind in ("asset", "telemetry", "security_challenge", "frontend_data"):
            continue
        if kind == "page_route":
            frontier_urls.append(event.url)
        if kind == "application_api":
            _accumulate(endpoints, event, kind)

    for elem in capture.dom_elements:
        url = _extract_url(elem)
        if url is None:
            continue
        if _INLINE_NOISE.search(url) or _INLINE_SKIP.search(url):
            continue  # images/fonts/css/js/manifests are assets, never crawlable pages
        absolute = urljoin(base_url, url)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https") and scope.allows(absolute):
            frontier_urls.append(absolute)

    for url in capture.inline_urls:
        if _INLINE_NOISE.search(url) or _INLINE_SKIP.search(url):
            continue
        absolute = urljoin(base_url, url)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https") and scope.allows(absolute):
            frontier_urls.append(absolute)

    # --- Phase 1-B: Static analysis of selective bodies ---

    _analyze_selective_bodies(capture, scope, base_url, endpoints)

    # --- Deduplicate frontier URLs ---

    seen: set[str] = set()
    unique: list[str] = []
    for url in frontier_urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    return AnalysisResult(frontier_urls=unique, endpoints=list(endpoints.values()))


def _extract_url(elem: DomElement) -> str | None:
    """Extract a navigable URL from a DOM element based on its tag and attributes.

    Priority: href → action → src → meta content → hidden input value.
    Uses None-check (not truthiness) so that href="" is preserved as a valid
    value meaning "current page".
    """
    url = elem.attributes.get("href")
    if url is None:
        url = elem.attributes.get("action")
    if url is not None:
        return url
    url = elem.attributes.get("src")
    if url is not None:
        return url
    if elem.tag == "meta":
        name = elem.attributes.get("name", "")
        content = elem.attributes.get("content", "")
        if name in ("og:url", "og:image", "og:video", "og:audio", "api-base-url"):
            return content
        if name == "refresh":
            idx = content.lower().find("url=")
            if idx >= 0:
                return content[idx + 4:].strip()
    if elem.tag == "input":
        input_name = elem.attributes.get("name", "").lower()
        value = elem.attributes.get("value", "")
        # Only extract from inputs whose name suggests a URL purpose.
        # Prevents false positives from CSRF tokens, base64 values, etc.
        # Uses segment-based matching: "redirect_url" → {"redirect","url"} → match.
        name_parts = set(_NAME_SPLIT.split(input_name)) if input_name else set()
        if (
            name_parts & _URL_INPUT_KEYWORDS
            and value.startswith(("/", "http://", "https://"))
        ):
            return value
    return None


def _accumulate(
    eps: dict[tuple[str, str, str], NormalizedEndpoint],
    event: NetworkEvent,
    kind: str,
) -> None:
    parsed = urlparse(event.url)
    host = parsed.hostname or ""
    path = (parsed.path or "/").rstrip("/") or "/"
    template = templatize_path(path)
    key = (event.method, host, template)

    ep = eps.get(key)
    if ep is None:
        ep = NormalizedEndpoint(
            method=event.method,
            host=host,
            path_template=template,
            sample_url=event.url,
            route_kind=kind,
            source="dynamic",
            discovered_via=event.triggered_by,
        )
        eps[key] = ep
    else:
        ep.seen_count += 1
        # Passive reachability wins: if this endpoint is ever seen on plain
        # load, drop any interaction tag — you don't need the click to reach it.
        if event.triggered_by is None:
            ep.discovered_via = None

    for location, name, value in extract_params(event):
        pkey = (location, name)
        param = ep.params.get(pkey)
        if param is None:
            param = NormalizedParam(
                location=location, name=name, type_inferred=infer_type(value),
            )
            ep.params[pkey] = param
        param.seen_count += 1
        if value not in param.sample_values and len(param.sample_values) < MAX_SAMPLES:
            param.sample_values.append(value)


# --- Phase 1-B: Static analysis helpers ---

# Source priority: lower number = higher priority.
# dynamic wins over all static sources (it's actually observed traffic).
# OpenAPI is highest static priority (explicit param type/location info).
_SOURCE_PRIORITY: dict[str, int] = {
    "dynamic": 0,
    "static_openapi": 1,
    "static_docs": 2,
    "static_js": 3,
    "passive": 4,   # archived/unverified — lowest; any live sighting overrides
}


def _resolve_static_url(raw_url: str, base_url: str) -> str | None:
    """Resolve a URL extracted from JS/static source against the page URL.

    JS body relative URLs resolve against the PAGE URL (not the JS file URL),
    because `fetch("/api/x")` in browser resolves against `document.baseURI`.

    Rules:
    - Absolute URL (http/https) → return as-is
    - Absolute path (/api/x) → resolve against page origin
    - Relative path (./foo, ../bar) → discard (can't resolve without JS context)
    """
    if raw_url.startswith(("http://", "https://")):
        return raw_url
    if raw_url.startswith("/"):
        return urljoin(base_url, raw_url)
    return None


def _merge_endpoint(
    eps: dict[tuple[str, str, str], NormalizedEndpoint],
    method: str,
    absolute_url: str,
    *,
    source: str,
    params: list[tuple[str, str, str]] | None = None,
) -> None:
    """Merge a statically-discovered endpoint into the endpoints dict.

    Merge rules:
    - If key exists with source="dynamic": keep dynamic, union new parameters
    - If key exists with lower-priority source: replace source, union parameters
    - New key: add as application_api
    """
    parsed = urlparse(absolute_url)
    host = parsed.hostname or ""
    path = (parsed.path or "/").rstrip("/") or "/"
    template = templatize_path(path)
    key = (method, host, template)

    ep = eps.get(key)
    if ep is None:
        # New endpoint
        ep = NormalizedEndpoint(
            method=method,
            host=host,
            path_template=template,
            sample_url=absolute_url,
            route_kind="application_api",
            source=source,
        )
        eps[key] = ep
    else:
        # Existing: apply source priority
        existing_priority = _SOURCE_PRIORITY.get(ep.source, 99)
        new_priority = _SOURCE_PRIORITY.get(source, 99)
        if new_priority < existing_priority:
            ep.source = source
            ep.sample_url = absolute_url

    # Union parameters
    if params:
        for location, name, ptype in params:
            pkey = (location, name)
            if pkey not in ep.params:
                ep.params[pkey] = NormalizedParam(
                    location=location,
                    name=name,
                    type_inferred=ptype,
                    seen_count=1,
                )


def _analyze_selective_bodies(
    capture: PageCapture,
    scope: Scope,
    base_url: str,
    endpoints: dict[tuple[str, str, str], NormalizedEndpoint],
) -> None:
    """Analyze selectively-collected bodies for static endpoint discovery."""
    # Lazy imports to avoid circular dependencies and keep startup fast
    from orbis.analysis.js_static import extract_js_endpoints
    from orbis.analysis.openapi import parse_openapi_spec
    from orbis.analysis.docs import extract_doc_endpoints

    for cb in capture.selective_bodies:
        try:
            if cb.kind == "js":
                # JS body: resolve against PAGE URL (not JS file URL)
                for ref in extract_js_endpoints(cb.body):
                    absolute = _resolve_static_url(ref.raw_url, base_url)
                    if absolute and scope.allows(absolute):
                        _merge_endpoint(
                            endpoints, ref.method, absolute,
                            source="static_js",
                        )

            elif cb.kind == "openapi_json":
                for ep in parse_openapi_spec(cb.body, base_url):
                    absolute = urljoin(base_url, ep.path_template)
                    if scope.allows(absolute):
                        _merge_endpoint(
                            endpoints, ep.method, absolute,
                            source="static_openapi",
                            params=ep.parameters,
                        )

            elif cb.kind == "doc_html":
                # Doc HTML: resolve against DOC URL (not page URL)
                for ref in extract_doc_endpoints(cb.body, cb.url):
                    absolute = urljoin(cb.url, ref.raw_url)
                    if scope.allows(absolute):
                        _merge_endpoint(
                            endpoints, ref.method, absolute,
                            source="static_docs",
                        )

        except Exception:
            log.debug("static analysis failed for %s (%s)", cb.url, cb.kind,
                      exc_info=True)


# --- Passive sources (archived URLs) ---


def build_passive_results(
    urls: list[str], scope: Scope,
) -> tuple[list[NormalizedEndpoint], list[str]]:
    """Split archived URLs into recorded API endpoints + page seeds to crawl.

    API-marked URLs are recorded directly as source="passive" (unverified)
    endpoints with their query params; page-like URLs become frontier seeds;
    assets are dropped. Endpoints are keyed/templatized here, so thousands of
    archived /courses/15, /courses/16 ... collapse to /courses/{id} before
    storage. Returns (endpoints, seed_urls).
    """
    endpoints: dict[tuple[str, str, str], NormalizedEndpoint] = {}
    seeds: list[str] = []
    seen_seeds: set[str] = set()
    for raw in urls:
        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https") or not scope.allows(raw):
            continue
        path = parsed.path or "/"
        if path.lower().endswith(ASSET_SUFFIXES):
            continue
        if API_MARKER.search(path):
            _accumulate_passive(endpoints, parsed)
        elif raw not in seen_seeds:
            seen_seeds.add(raw)
            seeds.append(raw)
    return list(endpoints.values()), seeds


def _accumulate_passive(
    eps: dict[tuple[str, str, str], NormalizedEndpoint],
    parsed,
) -> None:
    host = parsed.hostname or ""
    path = (parsed.path or "/").rstrip("/") or "/"
    template = templatize_path(path)
    key = ("GET", host, template)
    ep = eps.get(key)
    if ep is None:
        ep = NormalizedEndpoint(
            method="GET",
            host=host,
            path_template=template,
            sample_url=parsed.geturl(),
            route_kind="application_api",
            source="passive",
            discovered_via="archive",
        )
        eps[key] = ep
    else:
        ep.seen_count += 1

    for name, values in parse_qs(parsed.query).items():
        pkey = ("query", name)
        param = ep.params.get(pkey)
        if param is None:
            param = NormalizedParam(
                location="query", name=name,
                type_inferred=infer_type(values[0] if values else ""),
            )
            ep.params[pkey] = param
        param.seen_count += 1
        for v in values:
            if v and v not in param.sample_values and len(param.sample_values) < MAX_SAMPLES:
                param.sample_values.append(v)
