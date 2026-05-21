"""Per-page capture via Playwright + CDP. Returns raw data only — no judgment."""

from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Page

from orbis.crawler.scope import Scope

log = logging.getLogger("orbis.capture")

BODY_LIMIT = 512 * 1024
JS_BODY_LIMIT = 2 * 1024 * 1024

# --- Phase 1-B: Selective body collection limits ---
STATIC_JS_BODY_LIMIT = 5 * 1024 * 1024
OPENAPI_BODY_LIMIT = 1 * 1024 * 1024
DOCS_BODY_LIMIT = 2 * 1024 * 1024

_OPENAPI_PATH_RE = re.compile(
    r"/(swagger|openapi|api-docs|api_docs)"
    r"|/v[23]/api-docs"
    r"|/\.well-known/openapi",
    re.I,
)
_DOC_URL_RE = re.compile(
    r"/(api-?docs?|apidoc|docs?/api|redoc|swagger-ui)", re.I,
)

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
class CapturedBody:
    """Response body selectively collected for static analysis."""
    url: str          # resource URL
    body: str         # response body text
    mime: str         # response MIME type
    kind: str         # "js" | "openapi_json" | "doc_html"
    truncated: bool   # whether body was truncated by size limit


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
    inline_urls: list[str] = field(default_factory=list)
    selective_bodies: list[CapturedBody] = field(default_factory=list)
    error: str | None = None


async def capture_page(
    context: BrowserContext,
    url: str,
    *,
    scope: Scope | None = None,
    max_scrolls: int = 3,
    nav_timeout_ms: int = 20_000,
    settle_ms: int = 2_000,
    collect_bodies: bool = False,
    js_analysis: bool = True,
) -> PageCapture:
    """Visit url, capture all network traffic and DOM elements"""
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
    result.inline_urls = await _collect_inline_data(page)

    seen_keys = {(e.method, e.url) for e in captured.values()}
    for ev in await _collect_init_script_events(page, url, seen_keys):
        captured[ev.request_id] = ev

    selective_rids: set[str] = set()
    if js_analysis:
        result.selective_bodies, selective_rids = await _fill_selective_bodies(
            client, captured, scope,
        )
    if collect_bodies:
        await _fill_bodies(client, captured, skip_rids=selective_rids)
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
            const TEXT_LIMIT = 100;  // max chars for element text preview
            const out = [];
            const push = (tag, a, t) => out.push({tag, a, t:t||""});
            const txt = (el) => el.textContent?.trim()?.slice(0, TEXT_LIMIT);

            // --- Links ---
            for (const a of document.querySelectorAll("a[href]"))
                push("a", {href:a.getAttribute("href")}, txt(a));
            // --- Forms ---
            for (const f of document.querySelectorAll("form"))
                push("form", {action:f.getAttribute("action")||"", method:f.getAttribute("method")||"GET"});
            // --- SPA router links ---
            for (const el of document.querySelectorAll("[routerlink],[data-router-link],router-link[to],[ng-href]")) {
                const href = el.getAttribute("routerlink")||el.getAttribute("data-router-link")||el.getAttribute("to")||el.getAttribute("ng-href")||"";
                push(el.tagName.toLowerCase(), {href}, txt(el));
            }
            // --- [P2] Resource hints & link metadata ---
            for (const l of document.querySelectorAll('link[rel="preconnect"],link[rel="dns-prefetch"],link[rel="prefetch"],link[rel="preload"],link[rel="modulepreload"],link[rel="manifest"],link[rel="canonical"],link[rel="alternate"],link[rel="sitemap"]'))
                push("link", {href:l.getAttribute("href")||"", rel:l.getAttribute("rel")||""});
            // --- [P3] Meta tags ---
            for (const m of document.querySelectorAll('meta[property^="og:"],meta[name="csrf-token"],meta[name="csrf-param"],meta[name="api-base-url"],meta[http-equiv="refresh"]'))
                push("meta", {name:m.getAttribute("property")||m.getAttribute("name")||m.getAttribute("http-equiv")||"", content:m.getAttribute("content")||""});
            // --- [P4] data-* URL attributes ---
            for (const el of document.querySelectorAll("[data-href],[data-url],[data-src],[data-endpoint],[data-api]")) {
                const url = el.getAttribute("data-href")||el.getAttribute("data-url")||el.getAttribute("data-src")||el.getAttribute("data-endpoint")||el.getAttribute("data-api")||"";
                push(el.tagName.toLowerCase(), {href:url}, txt(el));
            }
            // HTMX
            const hxMap = {"hx-get":"GET","hx-post":"POST","hx-put":"PUT","hx-delete":"DELETE","hx-patch":"PATCH"};
            for (const attr of Object.keys(hxMap)) {
                for (const el of document.querySelectorAll("["+attr+"]"))
                    push(el.tagName.toLowerCase(), {href:el.getAttribute(attr)||"", method:hxMap[attr]}, txt(el));
            }
            // Rails UJS
            for (const el of document.querySelectorAll('[data-remote="true"]'))
                push(el.tagName.toLowerCase(), {href:el.getAttribute("href")||el.getAttribute("action")||"", method:el.getAttribute("data-method")||"GET"}, txt(el));
            // Turbo frames
            for (const el of document.querySelectorAll("turbo-frame[src]"))
                push("turbo-frame", {src:el.getAttribute("src")||""});
            // --- [P5] formaction ---
            for (const btn of document.querySelectorAll("[formaction]"))
                push(btn.tagName.toLowerCase(), {action:btn.getAttribute("formaction")||"", method:btn.getAttribute("formmethod")||""}, txt(btn));
            // Hidden inputs
            for (const inp of document.querySelectorAll("input[type=hidden]"))
                push("input", {name:inp.getAttribute("name")||"", value:inp.getAttribute("value")||""});
            // --- [P6] base ---
            for (const b of document.querySelectorAll("base[href]"))
                push("base", {href:b.getAttribute("href")||""});
            // iframe, source, track, embed, object
            for (const el of document.querySelectorAll("iframe[src],source[src],track[src],embed[src],object[data]"))
                push(el.tagName.toLowerCase(), {src:el.getAttribute("src")||el.getAttribute("data")||""});
            // a[ping] — stored under "ping" key (not "href") so _extract_url
            // won't send tracking beacons into the frontier
            for (const a of document.querySelectorAll("a[ping]"))
                push("a-ping", {ping:a.getAttribute("ping")||""});

            return out;
        }""")
    except Exception:
        log.debug("DOM collection failed", exc_info=True)
        return []
    return [DomElement(tag=r["tag"], attributes=r.get("a", {}), text=r.get("t", "")) for r in raw]


async def _collect_inline_data(page: Page) -> list[str]:
    """Extract URL candidates from inline script JSON state and API-call patterns.

    Returns raw URL-shaped strings. Noise filtering (asset extensions,
    framework internals) is deliberately left to the Analyzer layer.
    """
    try:
        return await page.evaluate("""() => {
            const MAX_JSON_SIZE   = 500000;  // skip JSON scripts larger than 500KB
            const MAX_SCRIPT_SIZE = 500000;  // skip inline scripts larger than 500KB
            const MIN_SCRIPT_SIZE = 20;      // skip trivially short scripts
            const MAX_WALK_DEPTH  = 12;      // max recursion depth for JSON walk
            const MAX_WALK_ITEMS  = 500;     // max items per array/object in JSON walk
            const MAX_URL_LEN     = 2000;    // ignore strings longer than this
            const MAX_URLS        = 500;     // cap total returned URLs

            const urls = new Set();
            const isUrl = (s) => {
                if (typeof s !== "string" || s.length < 2 || s.length > MAX_URL_LEN) return false;
                if (s.startsWith("/") && /^\\/[a-zA-Z0-9@]/.test(s) && !s.startsWith("//")) return true;
                if (/^https?:\\/\\//.test(s)) return true;
                return false;
            };
            const walk = (obj, d) => {
                if (d > MAX_WALK_DEPTH) return;
                if (typeof obj === "string") { if (isUrl(obj)) urls.add(obj); return; }
                if (Array.isArray(obj)) { for (let i = 0; i < Math.min(obj.length, MAX_WALK_ITEMS); i++) walk(obj[i], d+1); return; }
                if (obj && typeof obj === "object") {
                    const v = Object.values(obj);
                    for (let i = 0; i < Math.min(v.length, MAX_WALK_ITEMS); i++) walk(v[i], d+1);
                }
            };

            // Structured JSON: __NEXT_DATA__, application/json, application/ld+json
            const nd = document.getElementById("__NEXT_DATA__");
            if (nd && nd.textContent) try { walk(JSON.parse(nd.textContent), 0); } catch(e) {}
            for (const s of document.querySelectorAll('script[type="application/json"],script[type="application/ld+json"]')) {
                if (s === nd) continue;
                const t = s.textContent || "";
                if (t.length > MAX_JSON_SIZE) continue;
                try { walk(JSON.parse(t), 0); } catch(e) {}
            }

            // Inline scripts: extract URLs from API-call patterns
            for (const s of document.querySelectorAll("script:not([src])")) {
                const t = s.textContent || "";
                if (t.length < MIN_SCRIPT_SIZE || t.length > MAX_SCRIPT_SIZE) continue;
                if (s.type && s.type !== "text/javascript" && s.type !== "module" && s.type !== "") continue;
                // g-flag regexes carry lastIndex state — create fresh per script
                // element to avoid subtle bugs if the inner loop is ever modified
                // (e.g. adding break/limit). Moving these outside would silently
                // break matching from the second script onward in that case.
                const re = [
                    /fetch\\s*\\(\\s*["'`]([^"'`\\s]+)["'`]/g,
                    /axios\\.[a-z]+\\s*\\(\\s*["'`]([^"'`\\s]+)["'`]/g,
                    /\\.open\\s*\\(\\s*["'][A-Z]+["']\\s*,\\s*["'`]([^"'`\\s]+)["'`]/g,
                    /["'`](\\/api\\/[^"'`\\s]{1,200})["'`]/g,
                    /["'`](\\/v\\d+\\/[^"'`\\s]{1,200})["'`]/g,
                ];
                for (const r of re) {
                    let m;
                    while ((m = r.exec(t)) !== null) {
                        if (isUrl(m[1])) urls.add(m[1]);
                    }
                }
            }

            return [...urls].slice(0, MAX_URLS);
        }""")
    except Exception:
        log.debug("Inline data collection failed", exc_info=True)
        return []


async def _collect_init_script_events(
    page: Page,
    page_url: str,
    seen: set[tuple[str, str]],
) -> list[NetworkEvent]:
    try:
        records = await page.evaluate("() => window.__orbisRequests || []")
    except Exception:
        log.debug("Init script event collection failed", exc_info=True)
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


async def _fill_bodies(
    client,
    captured: dict[str, NetworkEvent],
    skip_rids: set[str] | None = None,
) -> None:
    for rid, ev in list(captured.items()):
        if ev.status is None or ev.source == "init_script":
            continue
        if skip_rids and rid in skip_rids:
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


# --- Phase 1-B: Selective body collection ---


def _classify_selective_body(
    url: str, mime: str, scope: Scope | None,
) -> tuple[str, int] | None:
    """Decide if a response qualifies for selective body collection.

    Returns (kind, size_limit) if it should be collected, None otherwise.
    JS bodies are collected regardless of scope (CDN JS may contain target API paths).
    OpenAPI/doc bodies are only collected for in-scope URLs.
    """
    mime_lower = mime.lower()

    # JS/ECMAScript — always collect, scope-independent
    if "javascript" in mime_lower or "ecmascript" in mime_lower:
        return ("js", STATIC_JS_BODY_LIMIT)

    # JSON on OpenAPI well-known paths — scope-dependent
    if "json" in mime_lower:
        path = urlparse(url).path or "/"
        if _OPENAPI_PATH_RE.search(path):
            if scope is None or scope.allows(url):
                return ("openapi_json", OPENAPI_BODY_LIMIT)

    # HTML on API doc URL patterns — scope-dependent
    if "html" in mime_lower:
        path = urlparse(url).path or "/"
        if _DOC_URL_RE.search(path):
            if scope is None or scope.allows(url):
                return ("doc_html", DOCS_BODY_LIMIT)

    return None


async def _fill_selective_bodies(
    client,
    captured: dict[str, NetworkEvent],
    scope: Scope | None,
) -> tuple[list[CapturedBody], set[str]]:
    """Collect response bodies for JS, OpenAPI JSON, and API doc HTML resources.

    This runs independently of the collect_bodies flag — selective bodies
    are always collected when js_analysis is enabled.

    Returns (bodies, fetched_rids) so the caller can skip these request IDs
    in a subsequent _fill_bodies call to avoid duplicate CDP fetches.
    """
    bodies: list[CapturedBody] = []
    fetched_rids: set[str] = set()
    for rid, ev in list(captured.items()):
        if ev.status is None or ev.source == "init_script":
            continue
        mime = ev.response_mime or ""
        classification = _classify_selective_body(ev.url, mime, scope)
        if classification is None:
            continue
        kind, limit = classification
        try:
            res = await client.send("Network.getResponseBody", {"requestId": rid})
            fetched_rids.add(rid)
            body_text = res.get("body", "") or ""
            if not body_text:
                continue
            truncated = len(body_text) > limit
            if truncated:
                body_text = body_text[:limit]
            bodies.append(CapturedBody(
                url=ev.url,
                body=body_text,
                mime=mime,
                kind=kind,
                truncated=truncated,
            ))
        except Exception:
            log.debug("selective body fetch failed for %s", ev.url)
    return bodies, fetched_rids
