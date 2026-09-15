"""Tests for static JavaScript analysis integration in analyzer.py.

Tests _resolve_static_url, _merge_endpoint, and full analyze() with
selective JavaScript bodies.
"""

from orbis.analysis.analyzer import (
    _merge_endpoint,
    _resolve_static_url,
    analyze,
)
from orbis.config import ScopeConfig
from orbis.crawler.browser import CapturedBody, PageCapture
from orbis.crawler.scope import Scope


def _scope(domains: list[str] | None = None) -> Scope:
    return Scope(ScopeConfig(include_domains=domains or ["example.com"]))


# ---------------------------------------------------------------------------
# _resolve_static_url
# ---------------------------------------------------------------------------

class TestResolveStaticUrl:
    def test_absolute_url(self) -> None:
        result = _resolve_static_url(
            "https://api.example.com/api/users",
            "https://example.com/page",
        )
        assert result == "https://api.example.com/api/users"

    def test_absolute_path(self) -> None:
        result = _resolve_static_url(
            "/api/v1/users",
            "https://example.com/app/page",
        )
        assert result == "https://example.com/api/v1/users"

    def test_relative_path_discarded(self) -> None:
        assert _resolve_static_url("../api/users", "https://example.com/page") is None
        assert _resolve_static_url("./api/users", "https://example.com/page") is None
        assert _resolve_static_url("api/users", "https://example.com/page") is None

    def test_cdn_js_resolves_to_page_origin(self) -> None:
        """JS from CDN containing fetch("/api/users") should resolve to page origin."""
        result = _resolve_static_url(
            "/api/users",
            "https://example.com/dashboard",
        )
        # NOT https://cdn.example.com/api/users
        assert result == "https://example.com/api/users"


# ---------------------------------------------------------------------------
# _merge_endpoint
# ---------------------------------------------------------------------------

class TestMergeEndpoint:
    def test_new_endpoint(self) -> None:
        eps: dict = {}
        _merge_endpoint(
            eps, "GET", "https://example.com/api/users",
            source="static_js",
        )
        assert len(eps) == 1
        ep = list(eps.values())[0]
        assert ep.source == "static_js"
        assert ep.route_kind == "application_api"

    def test_dynamic_wins_over_static(self) -> None:
        """Dynamic source should not be overwritten by static."""
        eps: dict = {}
        _merge_endpoint(eps, "GET", "https://example.com/api/users", source="dynamic")
        _merge_endpoint(eps, "GET", "https://example.com/api/users", source="static_js")
        assert len(eps) == 1
        assert list(eps.values())[0].source == "dynamic"

    def test_parameter_union(self) -> None:
        eps: dict = {}
        _merge_endpoint(
            eps, "GET", "https://example.com/api/users",
            source="static_js",
        )
        _merge_endpoint(
            eps, "GET", "https://example.com/api/users",
            source="static_js",
            params=[("query", "page", "integer"), ("query", "limit", "integer")],
        )
        ep = list(eps.values())[0]
        assert ("query", "page") in ep.params
        assert ("query", "limit") in ep.params

    def test_parameter_union_no_overwrite(self) -> None:
        """Existing params should not be overwritten."""
        eps: dict = {}
        _merge_endpoint(
            eps, "GET", "https://example.com/api/users",
            source="dynamic",
            params=[("query", "page", "string")],
        )
        _merge_endpoint(
            eps, "GET", "https://example.com/api/users",
            source="static_js",
            params=[("query", "page", "integer"), ("query", "new", "string")],
        )
        ep = list(eps.values())[0]
        # Existing "page" param not overwritten, new "new" param added
        assert ep.params[("query", "page")].type_inferred == "string"  # original
        assert ("query", "new") in ep.params


# ---------------------------------------------------------------------------
# analyze() with JS selective bodies
# ---------------------------------------------------------------------------

class TestAnalyzeJsStatic:
    def _capture_with_js(self, js_body: str) -> PageCapture:
        return PageCapture(
            page_url="https://example.com",
            final_url="https://example.com",
            selective_bodies=[
                CapturedBody(
                    url="https://cdn.example.com/app.bundle.js",
                    body=js_body,
                    mime="application/javascript",
                    kind="js",
                    truncated=False,
                ),
            ],
        )

    def test_js_fetch_endpoint_discovered(self) -> None:
        js = 'fetch("/api/users")'
        result = analyze(self._capture_with_js(js), _scope())
        assert any(
            ep.path_template == "/api/users" and ep.source == "static_js"
            for ep in result.endpoints
        )

    def test_js_cdn_resolves_to_page_origin(self) -> None:
        """CDN JS with fetch("/api/x") should resolve to example.com, not cdn."""
        js = 'fetch("/api/products")'
        result = analyze(self._capture_with_js(js), _scope())
        assert any(
            ep.host == "example.com" and ep.path_template == "/api/products"
            for ep in result.endpoints
        )

    def test_js_out_of_scope_filtered(self) -> None:
        js = 'fetch("https://other.com/api/users")'
        result = analyze(self._capture_with_js(js), _scope())
        assert not any(ep.host == "other.com" for ep in result.endpoints)

    def test_js_with_no_api_marker(self) -> None:
        js = 'fetch("/users/123")'
        result = analyze(self._capture_with_js(js), _scope())
        assert len(result.endpoints) == 0

    def test_js_multiple_endpoints(self) -> None:
        js = '''
        fetch("/api/users");
        axios.post("/api/orders");
        '''
        result = analyze(self._capture_with_js(js), _scope())
        paths = {ep.path_template for ep in result.endpoints}
        assert "/api/users" in paths
        assert "/api/orders" in paths

    def test_js_error_doesnt_crash(self) -> None:
        """Malformed JS body should not crash analyze()."""
        cap = PageCapture(
            page_url="https://example.com",
            final_url="https://example.com",
            selective_bodies=[
                CapturedBody(
                    url="https://example.com/bad.js",
                    body="",  # empty body
                    mime="application/javascript",
                    kind="js",
                    truncated=False,
                ),
            ],
        )
        result = analyze(cap, _scope())
        assert isinstance(result.endpoints, list)


# ---------------------------------------------------------------------------
# Source priority integration
# ---------------------------------------------------------------------------

class TestSourcePriorityIntegration:
    def test_dynamic_beats_static_js(self) -> None:
        """Dynamic network event + same endpoint in JS → source stays dynamic."""
        from orbis.crawler.browser import NetworkEvent

        cap = PageCapture(
            page_url="https://example.com",
            final_url="https://example.com",
            network_events=[
                NetworkEvent(
                    request_id="1",
                    method="GET",
                    url="https://example.com/api/users",
                    resource_type="Fetch",
                    response_mime="application/json",
                    status=200,
                ),
            ],
            selective_bodies=[
                CapturedBody(
                    url="https://cdn.example.com/app.js",
                    body='fetch("/api/users")',
                    mime="application/javascript",
                    kind="js",
                    truncated=False,
                ),
            ],
        )
        result = analyze(cap, _scope())
        users_eps = [ep for ep in result.endpoints if ep.path_template == "/api/users"]
        assert len(users_eps) == 1
        assert users_eps[0].source == "dynamic"
