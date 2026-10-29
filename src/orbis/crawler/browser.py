"""Per-page capture via Playwright + CDP. Returns raw data only — no judgment."""

from __future__ import annotations

import asyncio
import logging
import hashlib
import time
from pathlib import Path
from uuid import uuid4
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from orbis.analysis.url import templatize_path
from orbis.config import InteractionConfig
from orbis.crawler.interactor import ExplorationState, _upload_files, run_safe_interactions
from orbis.crawler.scope import Scope
from orbis.telemetry import safe_url, timestamp

log = logging.getLogger("orbis.capture")

CLEANUP_TIMEOUT_SEC = 2.0
DOWNLOAD_HASH_CHUNK_SIZE = 256 * 1024
STATIC_JS_BODY_LIMIT = 5 * 1024 * 1024

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
    source: str = "cdp"
    triggered_by: str | None = None   # None = page load; else interaction label
    action_id: int | None = None
    loading_error: str | None = None


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
    observations: list[dict] = field(default_factory=list)


async def capture_page(
    context: BrowserContext,
    url: str,
    *,
    scope: Scope | None = None,
    max_scrolls: int = 3,
    nav_timeout_ms: int = 20_000,
    settle_ms: int = 2_000,
    interaction: InteractionConfig | None = None,
    exploration: ExplorationState | None = None,
    max_actions: int = 20,
    download_dir: Path | None = None,
    deadline: float | None = None,
) -> PageCapture:
    captured: dict[str, NetworkEvent] = {}
    result = PageCapture(page_url=url, final_url=url)
    state = exploration if exploration is not None else ExplorationState()
    record_start = len(state.records)
    fetched_rids: set[str] = set()
    download_tasks: list[asyncio.Task] = []
    reported_requests: dict[str, int | None] = {}
    page = None
    client = None
    closing = False
    main_frame_id: str | None = None
    # requestId -> method of the first hop, so a redirect after a POST is never cut short
    origin_methods: dict[str, str] = {}
    # requestId -> real status of a page load that was kept from replacing the screen
    blocked_navigations: dict[str, int] = {}
    pause_tasks: set[asyncio.Task] = set()

    async def downloaded(item, action: dict, network_event: NetworkEvent | None, has_network_evidence: bool) -> None:
        started = time.monotonic()
        partial_path = None
        record = {"kind": "download", "url": safe_url(item.url), "status": "pending",
                  "action_id": action.get("action_id"), "page_id": state.page_id, "started_at": timestamp()}
        result.observations.append(record)
        state.event("download_start", **{k: v for k, v in record.items() if k != "page_id"})
        try:
            if download_dir is None or (scope is not None and not scope.allows(item.url)):
                await item.cancel()
                record["status"] = "cancelled" if download_dir is None else "out_of_scope"
                return
            download_dir.mkdir(parents=True, exist_ok=True)
            identifier = hashlib.sha256(item.url.encode()).hexdigest()[:16]
            path = download_dir / f"{identifier}-{uuid4().hex}.bin"
            partial_path = path.with_suffix(".part")
            await item.save_as(partial_path)
            hasher = hashlib.sha256()
            size = 0
            with partial_path.open("rb") as content:
                while chunk := content.read(DOWNLOAD_HASH_CHUNK_SIZE):
                    hasher.update(chunk)
                    size += len(chunk)
            digest = hasher.hexdigest()
            partial_path.replace(path)
            partial_path = None
            record.update(status="saved", path=str(path), sha256=digest, size=size)
            state.download_counts[digest] = state.download_counts.get(digest, 0) + 1
            record["content_seen_count"] = state.download_counts[digest]
            if network_event is not None:
                network_event.resource_type = "Download"
                record.update(method=network_event.method, method_evidence="network_request")
            elif not has_network_evidence:
                # Chromium's download attribute bypasses page network events.
                # A completed anchor download supplies navigation evidence, not a status code.
                if action.get("kind") == "link" and action.get("nav_url") == item.url and action.get("method", "GET") == "GET":
                    rid = f"download:{identifier}:{len(captured)}"
                    captured[rid] = NetworkEvent(rid, "GET", item.url, "Download", source="download_link",
                                                 triggered_by=action.get("label"), action_id=action.get("action_id"))
                    record.update(method="GET", method_evidence="anchor_navigation")
        except (PlaywrightError, OSError) as exc:
            record.update(status="failed", error=type(exc).__name__)
        except asyncio.CancelledError:
            record.update(status="interrupted", error="cancelled")
            raise
        finally:
            if partial_path is not None:
                try:
                    partial_path.unlink(missing_ok=True)
                except OSError as exc:
                    record.update(cleanup_error=type(exc).__name__, partial_path=str(partial_path))
            record.update(duration_ms=round((time.monotonic() - started) * 1000, 1), finished_at=timestamp())
            state.event("download_end", **{k: v for k, v in record.items() if k != "page_id"})

    def on_download(item) -> None:
        if closing:
            return
        # Saving may finish after later actions have requested the same URL.
        action = dict(state.current_action or {})
        matching = [event for event in captured.values() if event.url == item.url and event.source == "cdp"]
        same_action = [event for event in matching if event.action_id == action.get("action_id")]
        network_event = same_action[0] if len(same_action) == 1 else None
        download_tasks.append(asyncio.create_task(downloaded(item, action, network_event, bool(matching))))

    async def choose_files(chooser, action_id: int | None) -> None:
        files = []
        if interaction is not None and (scope is None or scope.allows(page.url)):
            files = _upload_files({"accept": await chooser.element.get_attribute("accept") or "",
                                   "multiple": chooser.is_multiple()}, interaction)
        await chooser.set_files([str(path) for path in files])
        state.event("file_selection", action_id=action_id, file_count=len(files))

    def on_filechooser(chooser) -> None:
        if closing:
            return
        download_tasks.append(asyncio.create_task(choose_files(chooser, (state.current_action or {}).get("action_id"))))

    def on_request(event: dict) -> None:
        req = event["request"]
        origin_methods.setdefault(event["requestId"], req["method"])
        captured[event["requestId"]] = NetworkEvent(
            request_id=event["requestId"], method=req["method"], url=req["url"],
            resource_type=event.get("type", "Other"),
            request_headers=req.get("headers") or {}, post_data=req.get("postData"),
            action_id=(state.current_action or {}).get("action_id"),
        )

    def on_response(event: dict) -> None:
        cap = captured.get(event["requestId"])
        if cap is not None:
            resp = event["response"]
            cap.status = blocked_navigations.get(event["requestId"], resp["status"])
            cap.response_headers = resp.get("headers") or {}
            cap.response_mime = resp.get("mimeType")
            if relevant(cap):
                report_request(cap)

    def on_failed(event: dict) -> None:
        cap = captured.get(event["requestId"])
        if cap is not None and event["requestId"] not in blocked_navigations:
            error = event.get("errorText", "")
            cap.loading_error = error if error.startswith("net::") and " " not in error else "network_failure"
            if relevant(cap):
                state.event("request_loading_failed", action_id=cap.action_id, request_id=cap.request_id,
                            method=cap.method, url=cap.url, loading_error=cap.loading_error)

    def relevant(event: NetworkEvent) -> bool:
        return (event.resource_type in ("XHR", "Fetch", "Document", "Download", "WebSocket")
                and (scope is None or scope.allows(event.url)))

    def report_request(event: NetworkEvent) -> None:
        name = "request_observed"
        details = {}
        if event.request_id in reported_requests:
            previous_status = reported_requests[event.request_id]
            if previous_status == event.status:
                return
            name = "request_status_updated"
            details["previous_http_status"] = previous_status
        state.event(name, action_id=event.action_id, request_id=event.request_id,
                    method=event.method, url=event.url, resource_type=event.resource_type,
                    http_status=event.status, loading_error=event.loading_error,
                    attribution="action_window" if event.action_id is not None else "outside_action", **details)
        reported_requests[event.request_id] = event.status

    async def resolve_paused(event: dict) -> None:
        request_id = event["requestId"]
        network_id = event.get("networkId") or ""
        # Teardown counts as no running action: nothing is explored any more.
        action = None if closing else state.current_action
        try:
            first_method = origin_methods.get(network_id, event["request"]["method"])
            block = _blocks_navigation(event, action, main_frame_id, first_method)
        except Exception:
            log.debug("navigation check failed", exc_info=True)
            block = False
        try:
            if not block:
                await client.send("Fetch.continueRequest", {"requestId": request_id})
                return
            status = event["responseStatusCode"]
            blocked_navigations[network_id] = status
            if network_id in captured:
                captured[network_id].status = status
            # A 204 answer to a navigation leaves the current document in place.
            await client.send("Fetch.fulfillRequest", {"requestId": request_id, "responseCode": 204})
            state.event("navigation_blocked", action_id=action.get("action_id"),
                        url=event["request"]["url"], http_status=status)
        except Exception:
            # The page may be closing; an unresolved pause dies with it.
            log.debug("paused document request not resolved", exc_info=True)

    def on_paused(event: dict) -> None:
        task = asyncio.create_task(resolve_paused(event))
        pause_tasks.add(task)
        task.add_done_callback(pause_tasks.discard)

    async def observe() -> None:
        elements = await _collect_dom(page)
        base = page.url
        for element in elements:
            if element.tag == "base" and element.attributes.get("href"):
                base = urljoin(base, element.attributes["href"])
                break
        for element in elements:
            for attribute in ("href", "src", "action"):
                if attribute in element.attributes:
                    element.attributes[attribute] = urljoin(base, element.attributes[attribute] or "")
        result.dom_elements = _merge_dom_elements(result.dom_elements, elements)
        seen_inline = set(result.inline_urls)
        for item in await _collect_inline_data(page):
            absolute = urljoin(base, item)
            if absolute not in seen_inline:
                seen_inline.add(absolute)
                result.inline_urls.append(absolute)
        bodies, fetched = await _fill_selective_bodies(client, captured, fetched_rids)
        fetched_rids.update(fetched)
        result.selective_bodies.extend(bodies)
        events = [event for event in captured.values() if relevant(event)]
        if state.current_action is not None:
            action_id = state.current_action["action_id"]
            events = [event for event in events if event.action_id == action_id]
            routes = {_route(event) for event in events}
            state.records[-1].update(
                requests=[{"method": event.method, "path": urlparse(event.url).path, "status": event.status,
                           "loading_error": event.loading_error} for event in events[:12]],
                request_count=len(events), requests_truncated=len(events) > 12,
                http_error_count=sum(event.status is not None and event.status >= 400 for event in events),
                network_failure_count=sum(event.loading_error is not None and event.resource_type != "Download" for event in events),
                http_status_counts={str(status) if status is not None else "unknown": sum(event.status == status for event in events)
                                    for status in {event.status for event in events}},
                new_request_routes=len(routes - state.seen_request_routes), attribution="action_window",
                application_outcome="unverified",
            )
        state.seen_request_routes.update(_route(event) for event in events)

    try:
        async with asyncio.timeout_at(deadline) as capture_timeout:
            page = await context.new_page()
            await page.add_init_script(INIT_SCRIPT)
            page.on("download", on_download)
            page.on("filechooser", on_filechooser)
            client = await context.new_cdp_session(page)
            await client.send("Network.enable")
            client.on("Network.requestWillBeSent", on_request)
            client.on("Network.responseReceived", on_response)
            client.on("Network.loadingFailed", on_failed)
            main_frame_id = (await client.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
            client.on("Fetch.requestPaused", on_paused)
            await client.send("Fetch.enable", {"patterns": [{"resourceType": "Document", "requestStage": "Response"}]})
            try:
                if deadline is not None:
                    nav_timeout_ms = max(1, min(nav_timeout_ms, int((deadline - time.monotonic() - 0.1) * 1000)))
                await page.goto(url, timeout=nav_timeout_ms, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                result.error = type(exc).__name__
            await asyncio.sleep(settle_ms / 1000)
            for _ in range(max_scrolls):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                await asyncio.sleep(0.5)
            await observe()
            if scope is not None:
                tracker = _DeltaTracker(captured)
                await run_safe_interactions(
                    page, scope, tracker=tracker, options=interaction, state=state,
                    max_actions=max_actions, observe=observe, deadline=deadline,
                )
            seen_keys = {(event.method, event.url) for event in captured.values()}
            for event in await _collect_init_script_events(page, page.url, seen_keys):
                captured[event.request_id] = event
            if download_tasks:
                await asyncio.gather(*download_tasks)
    except TimeoutError as exc:
        result.error = "ScanDeadlineExceeded" if capture_timeout.expired() else type(exc).__name__
    except PlaywrightError as exc:
        result.error = type(exc).__name__
    finally:
        # Keep what was captured, whatever stopped the page; then release the page.
        closing = True
        if page is not None:
            result.final_url = page.url
        result.network_events = list(captured.values())
        result.observations.extend({"observation": "action", **record} for record in state.records[record_start:])
        for task in download_tasks:
            task.cancel()
        if download_tasks:
            for outcome in await asyncio.gather(*download_tasks, return_exceptions=True):
                if isinstance(outcome, Exception):
                    log.warning("page task failed: %s", type(outcome).__name__)
                    result.error = result.error or type(outcome).__name__
        for event in captured.values():
            if relevant(event) and event.request_id not in reported_requests:
                report_request(event)
        await _release_page(client, page, result)
    return result


def _blocks_navigation(event: dict, action: dict | None, main_frame_id: str | None, first_method: str) -> bool:
    """Whether a paused document response would replace the screen an action is exploring.

    ``event`` is a Fetch.requestPaused event at the response stage, ``action`` the
    running action or None, and ``first_method`` the method of the request that
    started the redirect chain.

    Only a main-frame GET chain that answers with an HTML page qualifies. Its URL is
    already a captured Document request, which the analyzer hands to the
    frontier, so the page is visited later on its own instead of costing an
    action here plus a Back. Redirect hops, downloads, other content types,
    subframes, chains that started with a non-GET request and the Back action
    itself load normally. Client-side route changes never issue a document
    request and are not affected.
    """
    if action is None or action.get("kind") == "back":
        return False
    if event.get("frameId") != main_frame_id or first_method != "GET":
        return False
    status = event.get("responseStatusCode")
    if status is None or 300 <= status < 400:
        return False
    headers = {item["name"].lower(): item["value"] for item in event.get("responseHeaders") or []}
    if "attachment" in headers.get("content-disposition", "").lower():
        return False
    return "html" in headers.get("content-type", "").lower()


async def _release_page(client, page: Page | None, result: PageCapture) -> None:
    """Detach the CDP session and close the page, each within a bounded wait.

    A hung teardown is reported on the capture rather than raised: the page's
    evidence is already in ``result`` and a stuck close must not lose it.
    """
    for what, close in (("CDP detach", client.detach if client is not None else None),
                        ("page close", page.close if page is not None else None)):
        if close is None:
            continue
        try:
            async with asyncio.timeout(CLEANUP_TIMEOUT_SEC):
                await close()
        except TimeoutError:
            log.warning("%s timed out", what)
            result.error = result.error or "CaptureCleanupTimeout"
        except Exception as exc:
            log.warning("%s failed: %s", what, type(exc).__name__)


def _route(event: NetworkEvent) -> tuple[str, str, str]:
    """Route identity for novelty: templatized so /uploads/<ts>-file.zip and
    /items/<id> count as one route instead of a new one on every request."""
    parsed = urlparse(event.url)
    return event.method, parsed.netloc, templatize_path(parsed.path or "/")


def _merge_dom_elements(
    first: list[DomElement],
    second: list[DomElement],
) -> list[DomElement]:
    merged: list[DomElement] = []
    seen: set[tuple[str, tuple[tuple[str, str], ...], str]] = set()
    for elem in first + second:
        key = (
            elem.tag,
            tuple(sorted((str(k), str(v)) for k, v in elem.attributes.items())),
            elem.text,
        )
        if key in seen:
            continue
        seen.add(key)
        merged.append(elem)
    return merged


class _DeltaTracker:
    """Attributes network requests to the interaction that triggered them.

    Lives in the capture layer because it owns the live ``captured`` dict.
    The interactor stays ignorant of network internals — it only brackets
    each action with begin()/commit(); the diff and tagging happen here.
    """

    def __init__(self, captured: dict[str, NetworkEvent]) -> None:
        self._captured = captured
        self._before: set[str] = set()

    def begin(self) -> None:
        self._before = set(self._captured)

    def commit(self, tag: str) -> int:
        """Tag requests that appeared since begin(); return how many fired.

        The count lets the interactor decide whether a pagination button is
        still productive (keep clicking) or has run dry (stop).
        """
        fired = 0
        for rid in set(self._captured) - self._before:
            self._captured[rid].triggered_by = tag
            fired += 1
        return fired


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


async def _fill_selective_bodies(
    client,
    captured: dict[str, NetworkEvent],
    skip_rids: set[str] | None = None,
) -> tuple[list[CapturedBody], set[str]]:
    """Collect JavaScript bodies once per request for static endpoint discovery."""
    bodies: list[CapturedBody] = []
    fetched_rids: set[str] = set()
    for rid, ev in list(captured.items()):
        if skip_rids and rid in skip_rids:
            continue
        if ev.status is None or ev.source == "init_script":
            continue
        mime = ev.response_mime or ""
        if "javascript" not in mime.lower() and "ecmascript" not in mime.lower():
            continue
        try:
            res = await client.send("Network.getResponseBody", {"requestId": rid})
            fetched_rids.add(rid)
            body_text = res.get("body", "") or ""
            if body_text:
                truncated = len(body_text) > STATIC_JS_BODY_LIMIT
                if truncated:
                    body_text = body_text[:STATIC_JS_BODY_LIMIT]
                bodies.append(CapturedBody(
                    url=ev.url,
                    body=body_text,
                    mime=mime,
                    truncated=truncated,
                ))
        except Exception:
            log.debug("selective body fetch failed for %s", ev.url)
    return bodies, fetched_rids
