"""Tests for orbis.crawler.frontier — URL frontier queue."""

import pytest

from orbis.config import ScopeConfig
from orbis.crawler.frontier import Frontier, _normalize
from orbis.crawler.scope import Scope


def _scope(domains: list[str] | None = None) -> Scope:
    return Scope(ScopeConfig(include_domains=domains or ["example.com"]))


class TestNormalize:
    def test_strips_trailing_slash(self) -> None:
        assert _normalize("https://example.com/path/") == "https://example.com/path"

    def test_root_keeps_slash(self) -> None:
        assert _normalize("https://example.com/") == "https://example.com/"

    def test_empty_path_gets_slash(self) -> None:
        result = _normalize("https://example.com")
        assert "//" not in result or result.startswith("https://")
        # should have path /
        from urllib.parse import urlparse
        assert urlparse(result).path == "/"

    def test_strips_fragment(self) -> None:
        assert _normalize("https://example.com/page#section") == \
            "https://example.com/page"

    def test_keeps_spa_hash_fragment(self) -> None:
        url = "https://example.com/app#/dashboard"
        assert "/dashboard" in _normalize(url)

    def test_keeps_hashbang_fragment(self) -> None:
        url = "https://example.com/app#!/settings"
        assert "!/settings" in _normalize(url)


class TestNoveltyPriority:
    """Priority is based on template novelty, not URL keywords."""

    def test_all_novel_templates_get_equal_priority(self) -> None:
        """First visit to any template gets priority 0 — FIFO decides order."""
        f = Frontier(_scope())
        f.enqueue("https://example.com/about")
        f.enqueue("https://example.com/api/users")
        f.enqueue("https://example.com/admin/dashboard")
        # All novel → all priority 0 → FIFO order
        first = f.pop()
        second = f.pop()
        third = f.pop()
        assert first is not None and "about" in first.url
        assert second is not None and "api/users" in second.url
        assert third is not None and "admin/dashboard" in third.url

    def test_repeated_template_deprioritized(self) -> None:
        """Second visit to same template goes behind novel templates."""
        f = Frontier(_scope())
        f.enqueue("https://example.com/user/1")    # /user/{id} visits=1 → p=0
        f.enqueue("https://example.com/about")      # /about     visits=1 → p=0
        f.enqueue("https://example.com/user/2")     # /user/{id} visits=2 → p=30
        first = f.pop()
        second = f.pop()
        third = f.pop()
        assert first is not None and "user/1" in first.url   # novel, FIFO first
        assert second is not None and "about" in second.url   # novel, FIFO second
        assert third is not None and "user/2" in third.url    # repeated, deprioritized

    def test_progressive_deprioritization(self) -> None:
        """Each repeat pushes priority further back."""
        f = Frontier(_scope())
        f.enqueue("https://example.com/item/1")   # visits=1 → p=0
        f.enqueue("https://example.com/item/2")   # visits=2 → p=30
        f.enqueue("https://example.com/item/3")   # visits=3 → p=40
        f.enqueue("https://example.com/other")     # visits=1 → p=0
        first = f.pop()
        second = f.pop()
        # Both novel (item/1 and other) come out before repeats
        assert first is not None and "item/1" in first.url
        assert second is not None and "other" in second.url
        third = f.pop()
        fourth = f.pop()
        assert third is not None and "item/2" in third.url
        assert fourth is not None and "item/3" in fourth.url

    def test_priority_capped_at_90(self) -> None:
        """Priority doesn't exceed 90 even with many repeats."""
        f = Frontier(_scope(), max_per_template=20)
        for i in range(15):
            f.enqueue(f"https://example.com/item/{i}")
        # All enqueued — the last ones should have priority capped at 90
        # Just verify they all come out (no crash)
        count = 0
        while f.pop() is not None:
            count += 1
        assert count == 15


class TestFrontierEnqueue:
    def test_basic_enqueue(self) -> None:
        f = Frontier(_scope())
        assert f.enqueue("https://example.com/page") is True
        assert f.size == 1

    def test_dedup(self) -> None:
        f = Frontier(_scope())
        f.enqueue("https://example.com/page")
        assert f.enqueue("https://example.com/page") is False
        assert f.size == 1

    def test_out_of_scope_rejected(self) -> None:
        f = Frontier(_scope(["example.com"]))
        assert f.enqueue("https://evil.com/page") is False

    def test_dangerous_url_rejected(self) -> None:
        f = Frontier(_scope())
        assert f.enqueue("https://example.com/logout") is False

    def test_template_cap(self) -> None:
        f = Frontier(_scope(), max_per_template=2)
        assert f.enqueue("https://example.com/user/1") is True
        assert f.enqueue("https://example.com/user/2") is True
        assert f.enqueue("https://example.com/user/3") is False

    def test_max_depth(self) -> None:
        f = Frontier(_scope(), max_depth=2)
        assert f.enqueue("https://example.com/a", depth=0) is True
        assert f.enqueue("https://example.com/b", depth=2) is True
        assert f.enqueue("https://example.com/c", depth=3) is False

    def test_no_max_depth_allows_deep(self) -> None:
        f = Frontier(_scope(), max_depth=None)
        assert f.enqueue("https://example.com/deep", depth=100) is True

    def test_slug_urls_share_template(self) -> None:
        """Slug URLs collapse to same template, sharing the per-template cap."""
        f = Frontier(_scope(["dreamhack.io"]), max_per_template=2)
        base = "https://dreamhack.io/forum/posts"
        assert f.enqueue(f"{base}/1944-%ED%99%94%EC%9D%B4%ED%8A%B8%ED%96%87%EC%8A%A4%EC%BF%A8-long-title") is True
        assert f.enqueue(f"{base}/1954-bob-vs-something-long-enough") is True
        assert f.enqueue(f"{base}/1950-another-slug-long-enough-text") is False  # cap hit


class TestFrontierPop:
    def test_pop_empty(self) -> None:
        f = Frontier(_scope())
        assert f.pop() is None

    def test_pop_returns_item(self) -> None:
        f = Frontier(_scope())
        f.enqueue("https://example.com/page")
        item = f.pop()
        assert item is not None
        assert "example.com" in item.url
        assert f.size == 0

    def test_fifo_within_same_priority(self) -> None:
        """Items with same priority maintain insertion order."""
        f = Frontier(_scope())
        f.enqueue("https://example.com/page-a")
        f.enqueue("https://example.com/page-b")
        first = f.pop()
        second = f.pop()
        assert first is not None and second is not None
        assert "page-a" in first.url
        assert "page-b" in second.url
