"""Tests for orbis.analysis.analyzer — URL extraction and inline URL filtering."""

import pytest

from orbis.analysis.analyzer import _extract_url, analyze
from orbis.config import ScopeConfig
from orbis.crawler.browser import DomElement, PageCapture
from orbis.crawler.scope import Scope


def _scope(domains: list[str] | None = None) -> Scope:
    return Scope(ScopeConfig(include_domains=domains or ["example.com"]))


# ---------------------------------------------------------------------------
# _extract_url: href / action / src priority
# ---------------------------------------------------------------------------

class TestExtractUrlPriority:
    def test_href_basic(self) -> None:
        elem = DomElement(tag="a", attributes={"href": "/about"})
        assert _extract_url(elem) == "/about"

    def test_href_empty_string_is_valid(self) -> None:
        """href="" means 'current page' — must NOT fall through to action."""
        elem = DomElement(tag="a", attributes={"href": "", "action": "/other"})
        assert _extract_url(elem) == ""

    def test_action_when_no_href(self) -> None:
        elem = DomElement(tag="form", attributes={"action": "/submit"})
        assert _extract_url(elem) == "/submit"

    def test_action_empty_string_is_valid(self) -> None:
        elem = DomElement(tag="form", attributes={"action": ""})
        assert _extract_url(elem) == ""

    def test_href_takes_priority_over_action(self) -> None:
        elem = DomElement(tag="a", attributes={"href": "/link", "action": "/form"})
        assert _extract_url(elem) == "/link"

    def test_src_fallback(self) -> None:
        elem = DomElement(tag="iframe", attributes={"src": "/embed"})
        assert _extract_url(elem) == "/embed"

    def test_src_not_checked_when_href_present(self) -> None:
        elem = DomElement(tag="a", attributes={"href": "/link", "src": "/img"})
        assert _extract_url(elem) == "/link"

    def test_no_extractable_url(self) -> None:
        elem = DomElement(tag="div", attributes={"class": "container"})
        assert _extract_url(elem) is None


# ---------------------------------------------------------------------------
# _extract_url: meta tag handling
# ---------------------------------------------------------------------------

class TestExtractUrlMeta:
    def test_og_url(self) -> None:
        elem = DomElement(tag="meta", attributes={"name": "og:url", "content": "https://example.com/page"})
        assert _extract_url(elem) == "https://example.com/page"

    def test_og_image(self) -> None:
        elem = DomElement(tag="meta", attributes={"name": "og:image", "content": "https://cdn.example.com/img.jpg"})
        assert _extract_url(elem) == "https://cdn.example.com/img.jpg"

    def test_api_base_url(self) -> None:
        elem = DomElement(tag="meta", attributes={"name": "api-base-url", "content": "https://api.example.com"})
        assert _extract_url(elem) == "https://api.example.com"

    def test_refresh_redirect(self) -> None:
        elem = DomElement(tag="meta", attributes={"name": "refresh", "content": "5;URL=https://example.com/new"})
        assert _extract_url(elem) == "https://example.com/new"

    def test_refresh_lowercase_url(self) -> None:
        elem = DomElement(tag="meta", attributes={"name": "refresh", "content": "0;url=/redirect"})
        assert _extract_url(elem) == "/redirect"

    def test_meta_irrelevant_name_ignored(self) -> None:
        elem = DomElement(tag="meta", attributes={"name": "description", "content": "Some text"})
        assert _extract_url(elem) is None

    def test_meta_empty_content(self) -> None:
        elem = DomElement(tag="meta", attributes={"name": "og:url", "content": ""})
        # Empty content is technically a valid (empty) URL — return it
        assert _extract_url(elem) == ""


# ---------------------------------------------------------------------------
# _extract_url: hidden input — name-based whitelist
# ---------------------------------------------------------------------------

class TestExtractUrlHiddenInput:
    @pytest.mark.parametrize("name", [
        "redirect_url", "return_url", "next", "callback_url",
        "goto", "continue_url", "dest", "redirect",
        "target_uri", "back_url", "referrer", "endpoint",
    ])
    def test_url_name_with_path_value(self, name: str) -> None:
        elem = DomElement(tag="input", attributes={"name": name, "value": "/dashboard"})
        assert _extract_url(elem) == "/dashboard"

    @pytest.mark.parametrize("name", [
        "redirect_url", "return_url", "callback",
    ])
    def test_url_name_with_absolute_value(self, name: str) -> None:
        elem = DomElement(tag="input", attributes={"name": name, "value": "https://example.com/back"})
        assert _extract_url(elem) == "https://example.com/back"

    def test_csrf_token_not_extracted(self) -> None:
        """CSRF tokens starting with / must NOT be treated as URLs."""
        elem = DomElement(tag="input", attributes={"name": "csrf_token", "value": "/abc123def456"})
        assert _extract_url(elem) is None

    def test_authenticity_token_not_extracted(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "authenticity_token", "value": "/+xYz123="})
        assert _extract_url(elem) is None

    def test_nonce_not_extracted(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "nonce", "value": "/randomValue"})
        assert _extract_url(elem) is None

    def test_random_hidden_field_not_extracted(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "session_id", "value": "/sess_abc123"})
        assert _extract_url(elem) is None

    def test_url_name_but_non_url_value(self) -> None:
        """Even if name matches, value must look like a URL."""
        elem = DomElement(tag="input", attributes={"name": "redirect_url", "value": "not-a-url"})
        assert _extract_url(elem) is None

    def test_empty_name(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "", "value": "/some-path"})
        assert _extract_url(elem) is None


# ---------------------------------------------------------------------------
# analyze(): inline URL noise filtering
# ---------------------------------------------------------------------------

class TestAnalyzeInlineUrls:
    def _capture(self, inline_urls: list[str]) -> PageCapture:
        return PageCapture(
            page_url="https://example.com",
            final_url="https://example.com",
            inline_urls=inline_urls,
        )

    def test_valid_api_url_passes(self) -> None:
        result = analyze(self._capture(["/api/users"]), _scope())
        assert "https://example.com/api/users" in result.frontier_urls

    def test_js_extension_filtered(self) -> None:
        result = analyze(self._capture(["/static/app.js"]), _scope())
        assert not any("app.js" in u for u in result.frontier_urls)

    def test_css_extension_filtered(self) -> None:
        result = analyze(self._capture(["/styles/main.css"]), _scope())
        assert not any("main.css" in u for u in result.frontier_urls)

    def test_image_extension_filtered(self) -> None:
        result = analyze(self._capture(["/images/logo.png"]), _scope())
        assert len(result.frontier_urls) == 0

    def test_image_with_query_string_filtered(self) -> None:
        result = analyze(self._capture(["/images/photo.jpg?w=200"]), _scope())
        assert len(result.frontier_urls) == 0

    def test_font_extension_filtered(self) -> None:
        result = analyze(self._capture(["/fonts/roboto.woff2"]), _scope())
        assert len(result.frontier_urls) == 0

    def test_webpack_path_filtered(self) -> None:
        result = analyze(self._capture(["/__webpack/chunk-123"]), _scope())
        assert len(result.frontier_urls) == 0

    def test_next_static_filtered(self) -> None:
        result = analyze(self._capture(["/_next/static/chunks/main"]), _scope())
        assert len(result.frontier_urls) == 0

    def test_node_modules_filtered(self) -> None:
        result = analyze(self._capture(["/node_modules/lodash/index"]), _scope())
        assert len(result.frontier_urls) == 0

    def test_mixed_valid_and_noise(self) -> None:
        urls = ["/api/v2/users", "/static/app.js", "/v1/items", "/logo.svg"]
        result = analyze(self._capture(urls), _scope())
        frontier = result.frontier_urls
        assert "https://example.com/api/v2/users" in frontier
        assert "https://example.com/v1/items" in frontier
        assert not any(".js" in u for u in frontier)
        assert not any(".svg" in u for u in frontier)

    def test_absolute_url_not_in_scope_filtered(self) -> None:
        result = analyze(self._capture(["https://other.com/api"]), _scope())
        assert len(result.frontier_urls) == 0

    def test_non_noise_path_passes(self) -> None:
        result = analyze(self._capture(["/v2/checkout/complete"]), _scope())
        assert "https://example.com/v2/checkout/complete" in result.frontier_urls


# ---------------------------------------------------------------------------
# _extract_url: hidden input — segment-based matching (no \b)
# ---------------------------------------------------------------------------

class TestExtractUrlNameSegments:
    """Verify segment-based matching prevents substring false positives."""

    def test_xpath_expression_not_matched(self) -> None:
        """'path' in whitelist must NOT match 'xpath' substring."""
        elem = DomElement(tag="input", attributes={"name": "xpath_expression", "value": "/some/path"})
        assert _extract_url(elem) is None

    def test_multipath_id_not_matched(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "multipath_id", "value": "/data"})
        assert _extract_url(elem) is None

    def test_action_token_not_matched(self) -> None:
        """'action' removed from whitelist — action_token should not match."""
        elem = DomElement(tag="input", attributes={"name": "action_token", "value": "/verify"})
        assert _extract_url(elem) is None

    def test_underscore_separated_url_matches(self) -> None:
        """'redirect_url' → segments {'redirect','url'} — both in whitelist."""
        elem = DomElement(tag="input", attributes={"name": "redirect_url", "value": "/home"})
        assert _extract_url(elem) == "/home"

    def test_hyphen_separated_matches(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "return-url", "value": "/back"})
        assert _extract_url(elem) == "/back"

    def test_dot_separated_matches(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "form.redirect", "value": "/done"})
        assert _extract_url(elem) == "/done"

    def test_single_keyword_matches(self) -> None:
        elem = DomElement(tag="input", attributes={"name": "next", "value": "/step2"})
        assert _extract_url(elem) == "/step2"


# ---------------------------------------------------------------------------
# analyze(): integration tests — full DOM → frontier pipeline
# ---------------------------------------------------------------------------

class TestAnalyzeIntegration:
    """Integration tests exercising analyze() end-to-end with DOM elements."""

    def _capture_with_dom(self, elements: list[DomElement]) -> PageCapture:
        return PageCapture(
            page_url="https://example.com",
            final_url="https://example.com",
            dom_elements=elements,
        )

    def test_href_empty_resolves_to_current_page(self) -> None:
        """href="" should resolve to base URL (current page), not be dropped."""
        cap = self._capture_with_dom([
            DomElement(tag="a", attributes={"href": ""}),
        ])
        result = analyze(cap, _scope())
        # href="" → urljoin("https://example.com", "") → "https://example.com"
        assert "https://example.com" in result.frontier_urls

    def test_action_empty_resolves_to_current_page(self) -> None:
        """action="" (submit to current page) should not be dropped."""
        cap = self._capture_with_dom([
            DomElement(tag="form", attributes={"action": ""}),
        ])
        result = analyze(cap, _scope())
        assert "https://example.com" in result.frontier_urls

    def test_ping_url_not_in_frontier(self) -> None:
        """a[ping] tracking beacons must NOT enter the frontier."""
        cap = self._capture_with_dom([
            DomElement(tag="a-ping", attributes={"ping": "https://example.com/track"}),
        ])
        result = analyze(cap, _scope())
        assert "https://example.com/track" not in result.frontier_urls

    def test_base_href_changes_resolution(self) -> None:
        """<base href> should change relative URL resolution."""
        cap = self._capture_with_dom([
            DomElement(tag="base", attributes={"href": "https://example.com/app/"}),
            DomElement(tag="a", attributes={"href": "settings"}),
        ])
        result = analyze(cap, _scope())
        assert "https://example.com/app/settings" in result.frontier_urls

    def test_hidden_input_csrf_not_in_frontier(self) -> None:
        """CSRF tokens must not leak into frontier as URLs."""
        cap = self._capture_with_dom([
            DomElement(tag="input", attributes={"name": "csrf_token", "value": "/abc123"}),
        ])
        result = analyze(cap, _scope())
        assert len(result.frontier_urls) == 0

    def test_hidden_input_redirect_url_in_frontier(self) -> None:
        cap = self._capture_with_dom([
            DomElement(tag="input", attributes={"name": "redirect_url", "value": "/dashboard"}),
        ])
        result = analyze(cap, _scope())
        assert "https://example.com/dashboard" in result.frontier_urls

    def test_out_of_scope_dom_url_filtered(self) -> None:
        cap = self._capture_with_dom([
            DomElement(tag="a", attributes={"href": "https://evil.com/phish"}),
        ])
        result = analyze(cap, _scope())
        assert len(result.frontier_urls) == 0

    def test_dedup_within_same_capture(self) -> None:
        """Same URL from multiple elements should appear only once."""
        cap = self._capture_with_dom([
            DomElement(tag="a", attributes={"href": "/about"}),
            DomElement(tag="a", attributes={"href": "/about"}),
            DomElement(tag="link", attributes={"href": "/about"}),
        ])
        result = analyze(cap, _scope())
        count = result.frontier_urls.count("https://example.com/about")
        assert count == 1
