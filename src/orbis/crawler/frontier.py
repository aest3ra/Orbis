"""URL frontier: dedup, template-based visit cap, novelty-based priority."""

from __future__ import annotations

import heapq
from itertools import count
from urllib.parse import urlparse

from orbis.crawler.scope import Scope
from orbis.crawler.slug import DEFAULT_SLUG_THRESHOLD, SlugDetector
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
        slug_threshold: int = DEFAULT_SLUG_THRESHOLD,
    ) -> None:
        self._heap: list[tuple[int, int, FrontierItem]] = []
        self._counter = count()
        self._seen: set[str] = set()
        self._template_visits: dict[tuple[str, str], int] = {}
        self._scope = scope
        self._cap = max_per_template
        self._max_depth = max_depth
        self._detector = SlugDetector(slug_threshold)
        # Templates frozen by diminishing returns — further members are dropped.
        self._saturated: set[tuple[str, str]] = set()

    def enqueue(self, url: str, depth: int = 0) -> bool:
        url = _normalize(url)
        if not url or url in self._seen:
            return False
        if self._max_depth is not None and depth > self._max_depth:
            return False
        if not self._scope.allows(url) or not is_safe_url(url):
            return False
        parsed = urlparse(url)
        host = parsed.hostname or ""
        path = parsed.path or "/"
        # Feed cardinality detection even if this URL ends up rejected — seeing
        # the value still counts toward "this position is high-cardinality".
        self._detector.observe(host, path)
        tkey = (host, self._detector.template(host, path))
        if tkey in self._saturated:
            return False
        if self._template_visits.get(tkey, 0) >= self._cap:
            return False
        self._seen.add(url)
        self._template_visits[tkey] = self._template_visits.get(tkey, 0) + 1
        heapq.heappush(
            self._heap,
            (self._calc_priority(tkey), next(self._counter), FrontierItem(url, depth)),
        )
        return True

    def pop(self) -> FrontierItem | None:
        if not self._heap:
            return None
        _, _, item = heapq.heappop(self._heap)
        return item

    def template_key(self, url: str) -> tuple[str, str]:
        """Current (host, template) for a URL — used by the crawler to track
        per-template novelty for diminishing-returns saturation."""
        parsed = urlparse(_normalize(url))
        host = parsed.hostname or ""
        return host, self._detector.template(host, parsed.path or "/")

    def saturate(self, tkey: tuple[str, str]) -> None:
        """Freeze a template: drop its queued members and reject future ones.

        Called when extra visits to this template stopped yielding new
        endpoints — the remaining siblings are assumed redundant.
        """
        self._saturated.add(tkey)
        kept = [e for e in self._heap if self.template_key(e[2].url) != tkey]
        if len(kept) != len(self._heap):
            self._heap = kept
            heapq.heapify(self._heap)

    @property
    def size(self) -> int:
        return len(self._heap)

    # ------------------------------------------------------------------
    # Priority: novelty-based (no keyword heuristics)
    #
    # First visit to a template gets the highest priority (0).
    # Repeated visits to the same template are progressively deprioritized.
    # This ensures the crawler explores structurally diverse paths first,
    # regardless of whether they contain "/api/", "/admin/", etc.
    # ------------------------------------------------------------------

    def _calc_priority(self, tkey: tuple[str, str]) -> int:
        visits = self._template_visits.get(tkey, 0)
        if visits <= 1:
            return 0                            # first of this template
        return min(10 + visits * 10, 90)        # 2nd→30, 3rd→40, ...


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
