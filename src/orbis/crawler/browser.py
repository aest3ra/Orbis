"""Per-page capture via Playwright + CDP. Returns raw data only — no judgment."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Page

from orbis.crawler.scope import Scope

BODY_LIMIT = 512 * 1024
JS_BODY_LIMIT = 2 * 1024 * 1024

INIT_SCRIPT = """
(() => {
    if (window.__orbisHooked) return;
    window.__orbisHooked = true;
    window.__orbisRequests = [];
    const record = (src, method, url) => {
        try {
            window.__orbisRequests.push({
                src, method: (method || "GET").toUpperCase(),
                url: new URL(String(url), document.baseURI).href,
            });
        } catch (_) {}
    };
    const _fetch = window.fetch;
    if (_fetch) window.fetch = function(input, init) {
        record("fetch", init?.method || input?.method || "GET", input?.url || input);
        return _fetch.apply(this, arguments);
    };
    const _open = XMLHttpRequest?.prototype?.open;
    if (_open) XMLHttpRequest.prototype.open = function(m, u) {
        record("xhr", m, u);
        return _open.apply(this, arguments);
    };
    const _WS = window.WebSocket;
    if (_WS) {
        window.WebSocket = function(url, p) {
            record("ws", "GET", url);
            return p === undefined ? new _WS(url) : new _WS(url, p);
        };
        window.WebSocket.prototype = _WS.prototype;
    }
})();
"""


@dataclass
class NetworkEvent:
    """Raw network request/response pair captured via CDP."""
    request_id: str
    method: str
    url: str
    resource_type: str
    request_headers: dict = field(default_factory=dict)
    post_data: str | None = None
    status: int | None = None
    response_headers: dict = field(default_factory=dict)
    response_mime: str | None = None
    response_body: str | None = None
    body_truncated: bool = False
    source: str = "cdp"


@dataclass
class DomElement:
    """Raw DOM element observed on the page."""
    tag: str
    attributes: dict = field(default_factory=dict)
    text: str = ""


@dataclass
class PageCapture:
    """Raw observation result from a single page visit."""
    page_url: str
    final_url: str
    network_events: list[NetworkEvent] = field(default_factory=list)
    dom_elements: list[DomElement] = field(default_factory=list)
    error: str | None = None


async def capture_page(
    context: BrowserContext,
    url: str,
    *,
    scope: Scope | None = None,
    max_scrolls: int = 3,
    nav_timeout_ms: int = 20_000,
    settle_ms: int = 2_000,
) -> PageCapture:
    """Visit url, capture all network traffic and DOM elements. No filtering."""
    captured: dict[str, NetworkEvent] = {}
    page = await context.new_page()
    await page.add_init_script(INIT_SCRIPT)

    if scope is not None:
        async def handle_route(route):
            await _scoped_route(route, scope)
        await page.route("**/*", handle_route)

    client = await context.new_cdp_session(page)
    await client.send("Network.enable")

    def on_request(event: dict) -> None:
        req = event["request"]
        captured[event["requestId"]] = NetworkEvent(
            request_id=event["requestId"],
            method=req["method"],
            url=req["url"],
            resource_type=event.get("type", "Other"),
            request_headers=req.get("headers") or {},
            post_data=req.get("postData"),
        )

    def on_response(event: dict) -> None:
        cap = captured.get(event["requestId"])
        if cap is None:
            return
        resp = event["response"]
        cap.status = resp["status"]
        cap.response_headers = resp.get("headers") or {}
        cap.response_mime = resp.get("mimeType")

    client.on("Network.requestWillBeSent", on_request)
    client.on("Network.responseReceived", on_response)

    result = PageCapture(page_url=url, final_url=url)

    try:
        await page.goto(url, timeout=nav_timeout_ms, wait_until="domcontentloaded")
        result.final_url = page.url
    except Exception as exc:
        result.error = type(exc).__name__

    await asyncio.sleep(settle_ms / 1000)

    for _ in range(max_scrolls):
        try:
            await page.evaluate("window.scrollBy(0, window.innerHeight)")
            await asyncio.sleep(0.5)
        except Exception:
            break

    result.dom_elements = await _collect_dom(page)

    seen_keys = {(e.method, e.url) for e in captured.values()}
    for ev in await _collect_init_script_events(page, url, seen_keys):
        captured[ev.request_id] = ev

    await _fill_bodies(client, captured)
    result.network_events = list(captured.values())

    with suppress(Exception):
        await client.detach()
    with suppress(Exception):
        await page.close()

    return result


async def _scoped_route(route, scope: Scope) -> None:
    """Allow passive resources (JS/CSS/images) through; block other out-of-scope."""
    url = route.request.url
    rtype = route.request.resource_type
    passthrough = {"script", "stylesheet", "image", "font", "media", "manifest"}
    if not scope.allows(url) and rtype not in passthrough:
        with suppress(Exception):
            await route.abort()
        return
    with suppress(Exception):
        await route.continue_()


async def _collect_dom(page: Page) -> list[DomElement]:
    try:
        raw = await page.evaluate("""() => {
            const out = [];
            for (const a of document.querySelectorAll("a[href]"))
                out.push({tag:"a", a:{href:a.getAttribute("href")}, t:a.textContent?.trim()?.slice(0,100)||""});
            for (const f of document.querySelectorAll("form"))
                out.push({tag:"form", a:{action:f.getAttribute("action")||"", method:f.getAttribute("method")||"GET"}, t:""});
            for (const el of document.querySelectorAll("[routerlink],[data-router-link]"))
                out.push({tag:el.tagName.toLowerCase(), a:{href:el.getAttribute("routerlink")||el.getAttribute("data-router-link")}, t:el.textContent?.trim()?.slice(0,100)||""});
            return out;
        }""")
    except Exception:
        return []
    return [DomElement(tag=r["tag"], attributes=r.get("a", {}), text=r.get("t", "")) for r in raw]


async def _collect_init_script_events(
    page: Page,
    page_url: str,
    seen: set[tuple[str, str]],
) -> list[NetworkEvent]:
    try:
        records = await page.evaluate("() => window.__orbisRequests || []")
    except Exception:
        return []
    rtype_map = {"fetch": "Fetch", "xhr": "XHR", "ws": "WebSocket"}
    events: list[NetworkEvent] = []
    for i, rec in enumerate(records):
        raw_url = rec.get("url")
        if not raw_url:
            continue
        absolute = urljoin(page_url, str(raw_url))
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https", "ws", "wss"):
            continue
        method = str(rec.get("method") or "GET").upper()
        if (method, absolute) in seen:
            continue
        seen.add((method, absolute))
        events.append(NetworkEvent(
            request_id=f"init:{i}",
            method=method,
            url=absolute,
            resource_type=rtype_map.get(rec.get("src", ""), "Other"),
            source="init_script",
        ))
    return events


async def _fill_bodies(client, captured: dict[str, NetworkEvent]) -> None:
    for rid, ev in list(captured.items()):
        if ev.status is None or ev.source == "init_script":
            continue
        mime = (ev.response_mime or "").lower()
        if any(mime.startswith(p) for p in ("image/", "font/", "audio/", "video/")):
            continue
        limit = JS_BODY_LIMIT if "javascript" in mime else BODY_LIMIT
        try:
            res = await client.send("Network.getResponseBody", {"requestId": rid})
            body = res.get("body", "") or ""
            if len(body) > limit:
                ev.response_body = body[:limit]
                ev.body_truncated = True
            else:
                ev.response_body = body
        except Exception:
            pass
