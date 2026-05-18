"""Core analyzer: takes raw PageCapture, returns frontier URLs + endpoints.

This is the single judgment layer. Capture observes, Analyzer decides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from orbis.crawler.browser import NetworkEvent, PageCapture
from orbis.crawler.scope import Scope
from orbis.analysis.classifier import classify
from orbis.analysis.params import extract_params, infer_type
from orbis.analysis.url import templatize_path

MAX_SAMPLES = 5


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
    params: dict[tuple[str, str], NormalizedParam] = field(default_factory=dict)


@dataclass
class AnalysisResult:
    frontier_urls: list[str]
    endpoints: list[NormalizedEndpoint]


def analyze(capture: PageCapture, scope: Scope) -> AnalysisResult:
    """Classify traffic, extract links, normalize endpoints."""
    frontier_urls: list[str] = []
    endpoints: dict[tuple[str, str, str], NormalizedEndpoint] = {}

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
        href = elem.attributes.get("href") or elem.attributes.get("action")
        if not href:
            continue
        absolute = urljoin(capture.final_url, href)
        parsed = urlparse(absolute)
        if parsed.scheme in ("http", "https") and scope.allows(absolute):
            frontier_urls.append(absolute)

    seen: set[str] = set()
    unique: list[str] = []
    for url in frontier_urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    return AnalysisResult(frontier_urls=unique, endpoints=list(endpoints.values()))


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
        )
        eps[key] = ep
    else:
        ep.seen_count += 1

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
