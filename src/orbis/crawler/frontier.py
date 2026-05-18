"""URL frontier: dedup, template-based visit cap, priority ordering."""

from __future__ import annotations

from collections import deque
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
    def __init__(self, scope: Scope, max_per_template: int = 5) -> None:
        self._queue: deque[FrontierItem] = deque()
        self._seen: set[str] = set()
        self._template_visits: dict[tuple[str, str], int] = {}
        self._scope = scope
        self._cap = max_per_template

    def enqueue(self, url: str, depth: int = 0) -> bool:
        url = _normalize(url)
        if not url or url in self._seen:
            return False
        if not self._scope.allows(url) or not is_safe_url(url):
            return False
        tkey = _template_key(url)
        if self._template_visits.get(tkey, 0) >= self._cap:
            return False
        self._seen.add(url)
        self._template_visits[tkey] = self._template_visits.get(tkey, 0) + 1
        _insert_by_priority(self._queue, FrontierItem(url, depth))
        return True

    def pop(self) -> FrontierItem | None:
        return self._queue.popleft() if self._queue else None

    @property
    def size(self) -> int:
        return len(self._queue)


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


def _insert_by_priority(queue: deque[FrontierItem], item: FrontierItem) -> None:
    p = _priority(item.url)
    for i, existing in enumerate(queue):
        if p < _priority(existing.url):
            queue.insert(i, item)
            return
    queue.append(item)
