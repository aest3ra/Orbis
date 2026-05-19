"""URL frontier: dedup, template-based visit cap, priority ordering."""

from __future__ import annotations

import heapq
from itertools import count
from urllib.parse import urlparse

from orbis.analysis.url import templatize_path
from orbis.crawler.scope import Scope
from orbis.safety import is_safe_url


class FrontierItem:
    __slots__ = ("url", "depth")

    def __init__(self, url: str, depth: int = 0) -> None:
        self.url = url
        self.depth = depth


class Frontier:
    def __init__(
        self,
        scope: Scope,
        max_per_template: int = 5,
        max_depth: int | None = None,
    ) -> None:
        self._heap: list[tuple[int, int, FrontierItem]] = []
        self._counter = count()
        self._seen: set[str] = set()
        self._template_visits: dict[tuple[str, str], int] = {}
        self._scope = scope
        self._cap = max_per_template
        self._max_depth = max_depth

    def enqueue(self, url: str, depth: int = 0) -> bool:
        url = _normalize(url)
        if not url or url in self._seen:
            return False
        if self._max_depth is not None and depth > self._max_depth:
            return False
        if not self._scope.allows(url) or not is_safe_url(url):
            return False
        tkey = _template_key(url)
        if self._template_visits.get(tkey, 0) >= self._cap:
            return False
        self._seen.add(url)
        self._template_visits[tkey] = self._template_visits.get(tkey, 0) + 1
        heapq.heappush(
            self._heap,
            (_priority(url), next(self._counter), FrontierItem(url, depth)),
        )
        return True

    def pop(self) -> FrontierItem | None:
        if not self._heap:
            return None
        _, _, item = heapq.heappop(self._heap)
        return item

    @property
    def size(self) -> int:
        return len(self._heap)


def _normalize(url: str) -> str:
    parsed = urlparse(url)
    fragment = parsed.fragment
    if fragment.startswith("/") or fragment.startswith("!/"):
        return url
    path = parsed.path
    if path != "/" and path.endswith("/"):
        parsed = parsed._replace(path=path.rstrip("/"))
    if not parsed.path:
        parsed = parsed._replace(path="/")
    if fragment and not fragment.startswith("/") and not fragment.startswith("!/"):
        parsed = parsed._replace(fragment="")
    return parsed.geturl()


def _template_key(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    return parsed.hostname or "", templatize_path(parsed.path or "/")


_API_MARKERS = ("/api/", "/rest/", "/graphql")
_HIGH_VALUE = ("admin", "account", "profile", "setting", "dashboard", "login", "search")


def _priority(url: str) -> int:
    path = urlparse(url).path.lower()
    if any(m in path for m in _API_MARKERS):
        return 0
    if any(kw in path for kw in _HIGH_VALUE):
        return 10
    return 30


