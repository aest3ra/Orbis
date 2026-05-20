"""URL path templatization: collapse high-cardinality segments."""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"\d+$")
_UUID = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_LONG_HEX = re.compile(r"[0-9a-fA-F]{16,}$")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}$")
_SLUG = re.compile(r"^\d+-.{8,}$")
_EMBEDDED_NUM = re.compile(r"\d{4,}")


def templatize_path(path: str) -> str:
    if not path:
        return path
    return "/".join(_replace(s) if s else s for s in path.split("/"))


def _replace(seg: str) -> str:
    if _UUID.match(seg):
        return "{uuid}"
    if _DATE.match(seg):
        return "{date}"
    if _SLUG.match(seg):
        return "{slug}"
    if _LONG_HEX.match(seg):
        return "{hash}"
    if _NUMERIC.match(seg):
        return "{id}"
    return _EMBEDDED_NUM.sub("{n}", seg)
