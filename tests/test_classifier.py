"""Tests for orbis.analysis.classifier — route classification."""

import pytest

from orbis.analysis.classifier import classify
from orbis.crawler.browser import NetworkEvent


def _event(
    url: str,
    method: str = "GET",
    resource_type: str = "Other",
    response_mime: str | None = None,
    **kw,
) -> NetworkEvent:
    return NetworkEvent(
        request_id="test",
        method=method,
        url=url,
        resource_type=resource_type,
        response_mime=response_mime,
        **kw,
    )


class TestClassifyWebSocket:
    def test_websocket_resource_type(self) -> None:
        ev = _event("https://example.com/ws", resource_type="WebSocket")
        assert classify(ev) == "websocket"

    def test_ws_scheme(self) -> None:
        ev = _event("wss://example.com/feed")
        assert classify(ev) == "websocket"


class TestClassifySecurityChallenge:
    @pytest.mark.parametrize("url", [
        "https://example.com/cdn-cgi/challenge",
        "https://example.com/akam/security",
        "https://example.com/challenge-platform/check",
    ])
    def test_security_markers(self, url: str) -> None:
        assert classify(_event(url)) == "security_challenge"


class TestClassifyTelemetry:
    @pytest.mark.parametrize("url", [
        "https://google-analytics.com/collect",
        "https://sentry.io/api/errors",
        "https://api.segment.io/v1/track",
    ])
    def test_telemetry_by_host(self, url: str) -> None:
        assert classify(_event(url)) == "telemetry"

    @pytest.mark.parametrize("url", [
        "https://example.com/beacon",
        "https://example.com/analytics/event",
        "https://example.com/rum/data",
    ])
    def test_telemetry_by_path(self, url: str) -> None:
        assert classify(_event(url)) == "telemetry"


class TestClassifyAsset:
    @pytest.mark.parametrize("url", [
        "https://example.com/app.js",
        "https://example.com/style.css",
        "https://example.com/logo.png",
        "https://example.com/font.woff2",
    ])
    def test_asset_by_extension(self, url: str) -> None:
        assert classify(_event(url)) == "asset"


class TestClassifyStaticFile:
    @pytest.mark.parametrize("url", [
        "https://example.com/report.pdf",
        "https://example.com/data.csv",
        "https://example.com/doc.docx",
    ])
    def test_static_by_extension(self, url: str) -> None:
        assert classify(_event(url)) == "static_file"


class TestClassifyFrontendData:
    def test_next_data(self) -> None:
        ev = _event(
            "https://example.com/_next/data/abc/page.json",
            response_mime="application/json",
        )
        assert classify(ev) == "frontend_data"

    def test_page_data(self) -> None:
        ev = _event(
            "https://example.com/page-data/index/page-data.json",
            response_mime="application/json",
        )
        assert classify(ev) == "frontend_data"


class TestClassifyApplicationApi:
    def test_xhr_with_api_path(self) -> None:
        ev = _event(
            "https://example.com/api/users",
            resource_type="XHR",
            response_mime="application/json",
        )
        assert classify(ev) == "application_api"

    def test_fetch_with_json_mime(self) -> None:
        ev = _event(
            "https://example.com/data",
            resource_type="Fetch",
            response_mime="application/json",
        )
        assert classify(ev) == "application_api"

    def test_xhr_post_non_json(self) -> None:
        ev = _event(
            "https://example.com/submit",
            method="POST",
            resource_type="XHR",
            response_mime="text/plain",
        )
        assert classify(ev) == "application_api"

    def test_document_type_not_api(self) -> None:
        """Document resource type should be page_route, not API."""
        ev = _event(
            "https://example.com/api/users",
            resource_type="Document",
            response_mime="application/json",
        )
        assert classify(ev) != "application_api"
        assert classify(ev) == "page_route"


class TestClassifyPageRoute:
    def test_document_resource_type(self) -> None:
        ev = _event("https://example.com/about", resource_type="Document")
        assert classify(ev) == "page_route"

    def test_html_mime(self) -> None:
        ev = _event(
            "https://example.com/page",
            resource_type="XHR",
            response_mime="text/html",
        )
        assert classify(ev) == "page_route"

    def test_php_extension(self) -> None:
        ev = _event("https://example.com/index.php")
        assert classify(ev) == "page_route"

    def test_extensionless_path(self) -> None:
        ev = _event("https://example.com/some/path")
        assert classify(ev) == "page_route"
