"""Tests for orbis.crawler.frontier — URL frontier queue."""

import pytest

from orbis.config import ScopeConfig
from orbis.crawler.frontier import Frontier, _normalize, _priority
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


class TestPriority:
    def test_api_paths_highest_priority(self) -> None:
        assert _priority("https://example.com/api/users") < \
            _priority("https://example.com/about")

    def test_high_value_mid_priority(self) -> None:
        p_admin = _priority("https://example.com/admin")
        p_api = _priority("https://example.com/api/v1")
        p_general = _priority("https://example.com/about")
        assert p_api < p_admin < p_general

    def test_general_lowest_priority(self) -> None:
        assert _priority("https://example.com/about") == 30


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

    def test_priority_ordering(self) -> None:
        """API paths should come out before general paths."""
        f = Frontier(_scope())
        f.enqueue("https://example.com/about")
        f.enqueue("https://example.com/api/users")
        first = f.pop()
        assert first is not None
        assert "/api/" in first.url

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
