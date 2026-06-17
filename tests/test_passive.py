"""Tests for the passive (archived-URL) endpoint layer."""

from orbis.analysis.analyzer import build_passive_results
from orbis.config import ScopeConfig
from orbis.crawler import passive
from orbis.crawler.passive import fetch_wayback_urls
from orbis.crawler.scope import Scope


def _scope(domains: list[str] | None = None) -> Scope:
    return Scope(ScopeConfig(include_domains=domains or ["ex.com"]))


class TestBuildPassiveResults:
    def test_api_urls_recorded_as_passive_endpoints(self) -> None:
        urls = [
            "https://ex.com/api/v1/courses/15/",
            "https://ex.com/api/v1/courses/16/",
            "https://ex.com/api/v1/courses/220/",
            "https://ex.com/api/v1/board/boards/",
        ]
        eps, seeds = build_passive_results(urls, _scope())
        templates = {e.path_template for e in eps}
        # numeric ids collapse via templatize_path -> one /courses/{id} row
        assert "/api/v1/courses/{id}" in templates
        assert "/api/v1/board/boards" in templates
        assert all(e.source == "passive" for e in eps)
        assert all(e.method == "GET" for e in eps)
        assert all(e.discovered_via == "archive" for e in eps)
        assert seeds == []

    def test_query_params_extracted(self) -> None:
        urls = ["https://ex.com/api/v1/career/positions/?offset=0&limit=24&search=x"]
        eps, _ = build_passive_results(urls, _scope())
        assert len(eps) == 1
        names = {p.name for p in eps[0].params.values()}
        assert {"offset", "limit", "search"} <= names

    def test_pages_become_seeds_assets_dropped(self) -> None:
        urls = [
            "https://ex.com/about",
            "https://ex.com/blog/hello",
            "https://ex.com/static/app.js",
            "https://ex.com/logo.png",
            "https://ex.com/api/users",
        ]
        eps, seeds = build_passive_results(urls, _scope())
        assert "https://ex.com/about" in seeds
        assert "https://ex.com/blog/hello" in seeds
        assert all(".js" not in s and ".png" not in s for s in seeds)
        assert {e.path_template for e in eps} == {"/api/users"}

    def test_out_of_scope_dropped(self) -> None:
        urls = ["https://other.com/api/x", "https://ex.com/api/y"]
        eps, seeds = build_passive_results(urls, _scope(["ex.com"]))
        assert {e.host for e in eps} == {"ex.com"}
        assert all("other.com" not in s for s in seeds)


class FakeResp:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def read(self) -> bytes:
        return self._data

    def __enter__(self) -> "FakeResp":
        return self

    def __exit__(self, *_a) -> bool:
        return False


class TestFetchWayback:
    def test_empty_host(self) -> None:
        assert fetch_wayback_urls("") == []

    def test_parses_and_dedups(self, monkeypatch) -> None:
        text = b"https://ex.com/a\nhttps://ex.com/b\nhttps://ex.com/a\n\n"
        monkeypatch.setattr(passive.urllib.request, "urlopen",
                            lambda *a, **k: FakeResp(text))
        assert fetch_wayback_urls("ex.com") == [
            "https://ex.com/a", "https://ex.com/b",
        ]

    def test_network_error_returns_empty(self, monkeypatch) -> None:
        def boom(*_a, **_k):
            raise OSError("no network")
        monkeypatch.setattr(passive.urllib.request, "urlopen", boom)
        assert fetch_wayback_urls("ex.com") == []
