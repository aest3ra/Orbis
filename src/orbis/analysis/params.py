"""Extract parameters from network events."""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

from orbis.crawler.browser import NetworkEvent

BROWSER_HEADERS = {
    "accept", "accept-encoding", "accept-language", "cache-control",
    "connection", "host", "origin", "referer", "sec-ch-ua",
    "sec-ch-ua-mobile", "sec-ch-ua-platform", "sec-fetch-dest",
    "sec-fetch-mode", "sec-fetch-site", "upgrade-insecure-requests",
    "user-agent",
}


def extract_params(event: NetworkEvent) -> list[tuple[str, str, str]]:
    """Return [(location, name, value), ...]."""
    params: list[tuple[str, str, str]] = []

    parsed = urlparse(event.url)
    for name, values in parse_qs(parsed.query).items():
        for v in values:
            params.append(("query", name, v))

    for name, value in event.request_headers.items():
        if name.lower() not in BROWSER_HEADERS:
            params.append(("header", name, value))

    if event.post_data:
        params.extend(_parse_body(event.post_data))

    cookie = event.request_headers.get("cookie") or event.request_headers.get("Cookie")
    if cookie:
        for pair in cookie.split(";"):
            if "=" in pair:
                k, _, v = pair.strip().partition("=")
                params.append(("cookie", k.strip(), v.strip()))

    return params


def infer_type(value: str) -> str:
    if not value:
        return "empty"
    if value.lower() in ("true", "false"):
        return "bool"
    try:
        int(value)
        return "int"
    except ValueError:
        pass
    try:
        float(value)
        return "float"
    except ValueError:
        pass
    if value.startswith(("{", "[")):
        try:
            json.loads(value)
            return "json"
        except Exception:
            pass
    return "string"


def _parse_body(body: str) -> list[tuple[str, str, str]]:
    stripped = body.strip()
    if stripped.startswith("{"):
        try:
            data = json.loads(stripped)
            if isinstance(data, dict):
                return [
                    ("body", k, json.dumps(v) if not isinstance(v, str) else v)
                    for k, v in data.items()
                ]
        except Exception:
            pass
    params: list[tuple[str, str, str]] = []
    for name, values in parse_qs(body).items():
        for v in values:
            params.append(("body", name, v))
    return params
