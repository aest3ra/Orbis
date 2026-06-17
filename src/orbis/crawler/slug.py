"""Cardinality-based slug detection for crawl-time path templating.

No shape assumptions: a path *position* that takes many distinct observed
values (cumulatively, across the whole crawl) is treated as a ``{slug}``
variable. This generalizes to any site — numeric ids, string slugs, hashes,
non-ASCII titles are all detected the same way, purely by "how many different
values showed up here".

The frontier feeds every seen URL to :meth:`SlugDetector.observe` and keys its
visit-cap on :meth:`SlugDetector.template`, so once ``/board/hello``,
``/board/world``, ... cross the threshold they collapse to ``/board/{slug}``
and the existing per-template cap bounds how many get visited.

Root protection: a single-segment path (``/about``, ``/contact``) is never
slugged — top-level sections are treated as fixed, mirroring the post-scan
collapse's root-level guard. Detection only promotes positions in paths with
at least two segments.
"""

from __future__ import annotations

DEFAULT_SLUG_THRESHOLD = 8
SLUG = "{slug}"


def split_segments(path: str) -> list[str]:
    """``"/board/hello/view"`` -> ``["board", "hello", "view"]``; ``"/"`` -> ``[]``."""
    return [s for s in path.split("/") if s]


class SlugDetector:
    """Learns which path positions are high-cardinality (variable) by counting.

    Host-scoped: rules and counts never cross hosts, so a CDN and the app are
    judged independently. Memory is bounded — each position's value set stops
    growing once it reaches the threshold (that's all we need to know).
    """

    def __init__(self, threshold: int = DEFAULT_SLUG_THRESHOLD) -> None:
        # Below 2 nothing could ever be "high cardinality"; clamp defensively.
        self._threshold = max(2, threshold)
        # (host, segment_count) -> list of learned templates (tuples with SLUG)
        self._rules: dict[tuple[str, int], list[tuple[str, ...]]] = {}
        # context -> bounded set of distinct values seen at the open position
        self._values: dict[tuple, set[str]] = {}

    def template(self, host: str, path: str) -> str:
        """Path with learned variable positions replaced by ``{slug}``."""
        segs = self._apply(host, split_segments(path))
        return "/" + "/".join(segs) if segs else "/"

    def observe(self, host: str, path: str) -> None:
        """Record one seen URL; may promote a position to ``{slug}``."""
        segs = self._apply(host, split_segments(path))
        if len(segs) <= 1:
            return  # root-level pages are never slugged
        for i, seg in enumerate(segs):
            if seg == SLUG:
                continue
            # Context = this path with position i opened up. Other positions
            # use their templated form (SLUG markers already applied), so
            # /shop/{slug}/reviews/a and /shop/{slug}/reviews/b share a context
            # even when the shop differs.
            ctx = (host, len(segs), i,
                   tuple("*" if j == i else s for j, s in enumerate(segs)))
            vals = self._values.get(ctx)
            if vals is None:
                vals = set()
                self._values[ctx] = vals
            if len(vals) >= self._threshold:
                continue
            vals.add(seg)
            if len(vals) >= self._threshold:
                self._add_rule(host, tuple(
                    SLUG if j == i else s for j, s in enumerate(segs)
                ))

    def _apply(self, host: str, segs: list[str]) -> list[str]:
        # Apply learned rules to prefixes shortest-first, so a known parent
        # template (e.g. /shop/{slug}) normalizes the head of a longer path
        # before its deeper positions are matched — that is what makes
        # /shop/1/reviews/x and /shop/2/reviews/y share a deeper context.
        out = list(segs)
        n = len(out)
        for k in range(2, n + 1):
            rules = self._rules.get((host, k))
            if not rules:
                continue
            for rule in rules:
                if all(r == SLUG or r == s for r, s in zip(rule, out[:k])):
                    out[:k] = list(rule)
                    break
        return out

    def _add_rule(self, host: str, rule: tuple[str, ...]) -> None:
        bucket = self._rules.setdefault((host, len(rule)), [])
        if rule not in bucket:
            bucket.append(rule)
