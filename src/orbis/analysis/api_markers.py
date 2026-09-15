"""Shared API path marker rules."""

from __future__ import annotations

import re

API_MARKER_PATTERN = r"(?:api(?:[-_][a-z0-9]+)*|rest|b2b|graphql|gql)"
API_PREFIX_RE = re.compile(rf"/{API_MARKER_PATTERN}(?=[/?#]|$)", re.I)
API_URL_RE = re.compile(
    rf"""(?P<url>"""
    rf"""https?://[^"'`\s<>{{}}\\]+/{API_MARKER_PATTERN}(?=[/?#]|$)[^"'`\s<>{{}}\\]*"""
    rf"""|https?://[^"'`\s<>{{}}\\]+/[^"'`\s<>{{}}\\]*?/{API_MARKER_PATTERN}(?=[/?#]|$)[^"'`\s<>{{}}\\]*"""
    rf"""|/(?!/)[^"'`\s<>{{}}\\]*?/{API_MARKER_PATTERN}(?=[/?#]|$)[^"'`\s<>{{}}\\]*"""
    rf"""|/{API_MARKER_PATTERN}(?=[/?#]|$)[^"'`\s<>{{}}\\]*)""",
    re.I,
)


def contains_api_marker(text: str) -> bool:
    """Cheap prefilter before running heavier static extraction."""
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            "/api", "api/", "api-", "api_",
            "/rest", "rest/",
            "/b2b", "b2b/",
            "/graphql", "graphql",
            "/gql", "gql/",
        )
    )
