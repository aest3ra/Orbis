"""Classify network events into route kinds."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from orbis.crawler.browser import NetworkEvent

RouteKind = str

API_MARKER = re.compile(r"/(?:api|rest|graphql|gql)(?:[-_/]|$)", re.I)

ASSET_SUFFIXES = (
    ".js", ".css", ".map", ".png", ".jpg", ".jpeg", ".gif",
    ".svg", ".ico", ".webp", ".woff", ".woff2", ".ttf", ".eot",
)
STATIC_SUFFIXES = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".zip", ".csv", ".txt",
)
TELEMETRY_HOSTS = (
    "google-analytics", "googletagmanager", "sentry.io",
    "segment.io", "amplitude", "mixpanel", "hotjar", "datadoghq",
)
TELEMETRY_PATHS = ("/beacon", "/collect", "/analytics", "/rum", "/pixel")
SECURITY_MARKERS = ("/cdn-cgi/", "/akam/", "challenge-platform", "cf_chl_")


def classify(event: NetworkEvent) -> RouteKind:
    parsed = urlparse(event.url)
    path = (parsed.path or "/").lower()
    mime = (event.response_mime or "").lower()
    host = (parsed.hostname or "").lower()
    rtype = event.resource_type

    if rtype in ("WebSocket", "EventSource") or parsed.scheme in ("ws", "wss"):
        return "websocket"
    if any(m in path for m in SECURITY_MARKERS):
        return "security_challenge"
    if any(h in host for h in TELEMETRY_HOSTS) or any(p in path for p in TELEMETRY_PATHS):
        return "telemetry"
    if path.endswith(ASSET_SUFFIXES):
        return "asset"
    if path.endswith(STATIC_SUFFIXES):
        return "static_file"
    if _is_frontend_data(path, mime):
        return "frontend_data"
    if _is_api(event, path, mime, rtype):
        return "application_api"
    if _is_page_route(path, mime, rtype):
        return "page_route"
    return "unknown"


def _is_api(ev: NetworkEvent, path: str, mime: str, rtype: str) -> bool:
    if rtype == "Document":
        return False
    if rtype not in ("XHR", "Fetch"):
        return False
    if API_MARKER.search(path):
        return True
    if "json" in mime:
        return True
    return ev.method not in ("GET", "HEAD")


def _is_page_route(path: str, mime: str, rtype: str) -> bool:
    if rtype == "Document":
        return True
    if "html" in mime:
        return True
    if path.endswith((".php", ".jsp", ".asp", ".aspx")):
        return True
    last = path.rsplit("/", 1)[-1]
    return "/" in path and "." not in last


def _is_frontend_data(path: str, mime: str) -> bool:
    if not (path.endswith(".json") or "json" in mime):
        return False
    return (
        path.startswith("/_next/data/")
        or path.startswith("/page-data/")
        or path.endswith("/page-data.json")
    )
