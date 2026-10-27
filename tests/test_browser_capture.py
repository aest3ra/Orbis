"""Tests for browser capture scope boundaries."""

from __future__ import annotations

import asyncio

from orbis.analysis.analyzer import analyze
from orbis.config import ScopeConfig
from orbis.crawler.browser import capture_page
from orbis.crawler.scope import Scope


def _scope(domains: list[str] | None = None) -> Scope:
    return Scope(ScopeConfig(include_domains=domains or ["example.com"]))


class FakeCdpClient:
    def __init__(self) -> None:
        self.handlers = {}
        self.detached = False

    async def send(self, method: str, params: dict | None = None) -> dict:
        assert method == "Network.enable"
        return {}

    def on(self, event_name: str, callback) -> None:
        self.handlers[event_name] = callback

    def emit(self, event_name: str, payload: dict) -> None:
        self.handlers[event_name](payload)

    async def detach(self) -> None:
        self.detached = True


class FakePage:
    def __init__(self) -> None:
        self.url = "https://example.com"
        self.client: FakeCdpClient | None = None
        self.closed = False
        self.dom_records: list[dict] = []
        self.inline_urls: list[str] = []

    async def add_init_script(self, _script: str) -> None:
        return None

    def on(self, _event, _callback):
        pass

    def remove_listener(self, _event, _callback):
        pass

    async def route(self, _pattern: str, _handler) -> None:
        raise AssertionError("capture_page must not install scoped routes")

    async def goto(self, url: str, *, timeout: int, wait_until: str) -> None:
        self.url = url
        assert self.client is not None
        self.client.emit(
            "Network.requestWillBeSent",
            {
                "requestId": "out-of-scope-api",
                "type": "Fetch",
                "request": {
                    "method": "GET",
                    "url": "https://evil.com/api/leak",
                    "headers": {},
                },
            },
        )
        self.client.emit(
            "Network.responseReceived",
            {
                "requestId": "out-of-scope-api",
                "response": {
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "mimeType": "application/json",
                },
            },
        )

    async def evaluate(self, _script: str) -> list:
        if "performance.now() - window.__orbisInteractionActivity" in _script:
            return 1000
        if "window.scrollBy" in _script:
            return []
        if "window.__orbisRequests" in _script:
            return []
        if "MAX_JSON_SIZE" in _script:
            return self.inline_urls
        if "querySelectorAll" in _script:
            return self.dom_records
        return []

    async def close(self) -> None:
        self.closed = True


class FakeContext:
    def __init__(self) -> None:
        self.page = FakePage()
        self.client = FakeCdpClient()

    async def new_page(self) -> FakePage:
        return self.page

    async def new_cdp_session(self, page: FakePage) -> FakeCdpClient:
        page.client = self.client
        return self.client


def test_capture_page_keeps_out_of_scope_browser_subrequests_raw() -> None:
    context = FakeContext()

    result = asyncio.run(
        capture_page(
            context,
            "https://example.com",
            scope=_scope(["example.com"]),
            max_scrolls=0,
            settle_ms=0,
        )
    )

    assert context.page.closed is True
    assert context.client.detached is True
    assert [
        (event.method, event.url, event.status)
        for event in result.network_events
    ] == [("GET", "https://evil.com/api/leak", 200)]


class _DiscoverLocator:
    """Stands in for page.locator(...).first: a visible, enabled button whose click
    fires the interaction requests."""

    def __init__(self, page: "InteractionCapturePage") -> None:
        self.page = page

    @property
    def first(self) -> "_DiscoverLocator":
        return self

    async def is_visible(self, **_kwargs) -> bool:
        return True

    async def is_enabled(self, **_kwargs) -> bool:
        return True

    async def click(self, **_kwargs) -> None:
        await self.page.discover()


class InteractionCapturePage(FakePage):
    def __init__(self) -> None:
        super().__init__()
        self.url = "https://example.com/"
        self.candidates = [
            {"kind": "button", "selector": "#discover", "key": "#discover", "text": "Search",
             "label": "Search", "button_type": "button"}
        ]

    async def goto(self, url: str, *, timeout: int, wait_until: str) -> None:
        self.url = url

    async def evaluate(self, script: str) -> list:
        if "data-orbis-interaction" in script or "formMeta" in script:
            return self.candidates
        return await super().evaluate(script)

    def locator(self, selector: str) -> "_DiscoverLocator":
        assert selector == "#discover"
        return _DiscoverLocator(self)

    async def discover(self) -> None:
        assert self.client is not None
        self.client.emit(
            "Network.requestWillBeSent",
            {
                "requestId": "interaction-api",
                "type": "Fetch",
                "request": {
                    "method": "GET",
                    "url": "https://example.com/api/from-interaction",
                    "headers": {},
                },
            },
        )
        self.client.emit(
            "Network.responseReceived",
            {
                "requestId": "interaction-api",
                "response": {
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "mimeType": "application/json",
                },
            },
        )
        self.client.emit(
            "Network.requestWillBeSent",
            {
                "requestId": "interaction-out-of-scope-api",
                "type": "Fetch",
                "request": {
                    "method": "GET",
                    "url": "https://evil.com/api/leak",
                    "headers": {},
                },
            },
        )
        self.client.emit(
            "Network.responseReceived",
            {
                "requestId": "interaction-out-of-scope-api",
                "response": {
                    "status": 200,
                    "headers": {"content-type": "application/json"},
                    "mimeType": "application/json",
                },
            },
        )
        self.dom_records.append(
            {"tag": "a", "a": {"href": "/from-interaction"}, "t": "Created link"}
        )

    async def wait_for_load_state(self, *_args, **_kwargs) -> None:
        return None

    async def wait_for_timeout(self, *_args, **_kwargs) -> None:
        return None


class InteractionCaptureContext:
    def __init__(self) -> None:
        self.page = InteractionCapturePage()
        self.client = FakeCdpClient()

    async def new_page(self) -> InteractionCapturePage:
        return self.page

    async def new_cdp_session(self, page: InteractionCapturePage) -> FakeCdpClient:
        page.client = self.client
        return self.client


def test_interaction_created_observations_flow_through_capture_and_analyzer() -> None:
    context = InteractionCaptureContext()
    scope = _scope(["example.com"])

    capture = asyncio.run(
        capture_page(
            context,
            "https://example.com/",
            scope=scope,
            max_scrolls=0,
            settle_ms=0,
        )
    )

    raw_urls = {event.url for event in capture.network_events}
    assert "https://example.com/api/from-interaction" in raw_urls
    assert "https://evil.com/api/leak" in raw_urls
    assert any(
        elem.tag == "a" and elem.attributes["href"] == "https://example.com/from-interaction"
        for elem in capture.dom_elements
    )

    result = analyze(capture, scope)

    endpoints = {(ep.source, ep.host, ep.path_template) for ep in result.endpoints}
    assert ("dynamic", "https://example.com:443", "/api/from-interaction") in endpoints
    assert not any(host == "https://evil.com:443" for _source, host, _path in endpoints)
    assert "https://example.com/from-interaction" in result.frontier_urls
