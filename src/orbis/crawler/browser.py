"""Per-page capture via Playwright + CDP. Returns raw data only — no judgment."""

from __future__ import annotations

import asyncio
import logging
import hashlib
import time
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, Error as PlaywrightError, Page

from orbis.config import InteractionConfig
from orbis.crawler.interactor import ExplorationState, _upload_files, run_safe_interactions
from orbis.crawler.planner import QwenPlanner
from orbis.crawler.scope import Scope
from orbis.telemetry import safe_url, timestamp

log = logging.getLogger("orbis.capture")

BODY_LIMIT = 512 * 1024
JS_BODY_LIMIT = 2 * 1024 * 1024
CLEANUP_TIMEOUT_SEC = 2.0
DOWNLOAD_HASH_CHUNK_SIZE = 256 * 1024

# --- Phase 1-B: Selective body collection limits ---
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
    kind: str         # "js"
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
    triggered_by: str | None = None   # None = page load; else interaction label
    action_id: int | None = None
    loading_error: str | None = None
    route_seen_count: int = 0


@dataclass
class InteractionRecord:
    """One interaction and the network requests it triggered (delta tracking)."""
    action: str                       # "click" | "form" | "input"
    selector: str
    label: str
    triggered: list[tuple[str, str]]  # [(method, url), ...]


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
    interactions: list[InteractionRecord] = field(default_factory=list)
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
    collect_bodies: bool = False,
    interaction: InteractionConfig | None = None,
    exploration: ExplorationState | None = None,
    planner: QwenPlanner | None = None,
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
    download_finalization_errors: list[Exception] = []
    task_diagnostic_errors: list[BaseException] = []
    reported_requests: dict[str, int | None] = {}
    page = None
    client = None
    tracker = None
    primary_error: BaseException | None = None
    handled_error: BaseException | None = None
    http_diagnostic_error: BaseException | None = None
    http_diagnostic_pending = False
    closing = False

    def emit_task_diagnostic(name: str, **details) -> None:
        try:
            state.event(name, **details)
        except BaseException as exc:
            task_diagnostic_errors.append(exc)
            raise

    def check_http_diagnostics() -> None:
        nonlocal http_diagnostic_pending
        if http_diagnostic_error is not None:
            http_diagnostic_pending = False
            raise http_diagnostic_error

    def emit_http_diagnostic(name: str, **details) -> bool:
        nonlocal http_diagnostic_error, http_diagnostic_pending
        if http_diagnostic_error is not None:
            return False
        try:
            state.event(name, **details)
        except BaseException as exc:
            # Playwright otherwise defers callback errors to an unrelated API call.
            http_diagnostic_error = exc
            http_diagnostic_pending = True
            return False
        return True

    async def downloaded(item, action: dict, network_event: NetworkEvent | None, has_network_evidence: bool) -> None:
        started = time.monotonic()
        partial_path = None
        download_error: BaseException | None = None
        record = {"kind": "download", "url": safe_url(item.url), "status": "pending",
                  "action_id": action.get("action_id"), "page_id": state.page_id, "started_at": timestamp()}
        result.observations.append(record)
        emit_task_diagnostic("download_start", **{k: v for k, v in record.items() if k != "page_id"})
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
        except PlaywrightError as exc:
            record.update(status="failed", error=type(exc).__name__)
        except asyncio.CancelledError as exc:
            download_error = exc
            record.update(status="interrupted", error="cancelled")
            raise
        except OSError as exc:
            record.update(status="failed", error=type(exc).__name__)
        except BaseException as exc:
            download_error = exc
            record.update(status="failed", error=type(exc).__name__)
            raise
        finally:
            if partial_path is not None:
                try:
                    partial_path.unlink(missing_ok=True)
                except OSError as exc:
                    record.update(cleanup_error=type(exc).__name__, partial_path=str(partial_path))
            record.update(duration_ms=round((time.monotonic() - started) * 1000, 1), finished_at=timestamp())
            try:
                emit_task_diagnostic("download_end", **{k: v for k, v in record.items() if k != "page_id"})
            except Exception as exc:
                if download_error is None:
                    raise
                # gather does not preserve notes on cancelled child exceptions.
                download_finalization_errors.append(exc)
                download_error.add_note(f"Download finalization failed: {type(exc).__name__}")

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
        if interaction is not None and interaction.allow_write and (scope is None or scope.allows(page.url)):
            files = _upload_files({"accept": await chooser.element.get_attribute("accept") or "",
                                   "multiple": chooser.is_multiple()}, interaction)
        await chooser.set_files([str(path) for path in files])
        emit_task_diagnostic("file_selection", action_id=action_id, file_count=len(files))

    def on_filechooser(chooser) -> None:
        if closing:
            return
        download_tasks.append(asyncio.create_task(choose_files(chooser, (state.current_action or {}).get("action_id"))))

    def on_request(event: dict) -> None:
        req = event["request"]
        captured[event["requestId"]] = NetworkEvent(
            request_id=event["requestId"], method=req["method"], url=req["url"],
            resource_type=event.get("type", "Other"),
            request_headers=req.get("headers") or {}, post_data=req.get("postData"),
            action_id=(state.current_action or {}).get("action_id"),
        )
        cap = captured[event["requestId"]]
        if relevant(cap):
            parsed = urlparse(cap.url)
            key = (cap.method, parsed.netloc, parsed.path)
            state.request_counts[key] = state.request_counts.get(key, 0) + 1
            cap.route_seen_count = state.request_counts[key]

    def on_response(event: dict) -> None:
        cap = captured.get(event["requestId"])
        if cap is not None:
            resp = event["response"]
            cap.status = resp["status"]
            cap.response_headers = resp.get("headers") or {}
            cap.response_mime = resp.get("mimeType")
            if relevant(cap):
                report_request(cap)

    def on_failed(event: dict) -> None:
        cap = captured.get(event["requestId"])
        if cap is not None:
            error = event.get("errorText", "")
            cap.loading_error = error if error.startswith("net::") and " " not in error else "network_failure"
            if relevant(cap):
                emit_http_diagnostic("request_loading_failed", action_id=cap.action_id, request_id=cap.request_id,
                                     method=cap.method, url=cap.url, loading_error=cap.loading_error)

    def relevant(event: NetworkEvent) -> bool:
        return (event.resource_type in ("XHR", "Fetch", "Document", "Download", "WebSocket")
                and (scope is None or scope.allows(event.url)))

    def report_request(event: NetworkEvent) -> None:
        if http_diagnostic_error is not None:
            return
        name = "request_observed"
        details = {}
        if event.request_id in reported_requests:
            previous_status = reported_requests[event.request_id]
            if previous_status == event.status:
                return
            name = "request_status_updated"
            details["previous_http_status"] = previous_status
        if emit_http_diagnostic(name, action_id=event.action_id, request_id=event.request_id,
                               method=event.method, url=event.url, resource_type=event.resource_type,
                               http_status=event.status, loading_error=event.loading_error,
                               route_seen_count=event.route_seen_count,
                               attribution="action_window" if event.action_id is not None else "outside_action", **details):
            reported_requests[event.request_id] = event.status

    async def observe() -> None:
        check_http_diagnostics()
        elements = await _collect_dom(page)
        check_http_diagnostics()
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
        result.inline_urls = _merge_strings(result.inline_urls, [
            urljoin(base, item) for item in await _collect_inline_data(page)
        ])
        check_http_diagnostics()
        bodies, fetched = await _fill_selective_bodies(
            client, captured, scope, fetched_rids, check_diagnostics=check_http_diagnostics,
        )
        check_http_diagnostics()
        fetched_rids.update(fetched)
        result.selective_bodies.extend(bodies)
        events = [event for event in captured.values() if relevant(event)]
        if state.current_action is not None:
            action_id = state.current_action["action_id"]
            events = [event for event in events if event.action_id == action_id]
            routes = {(event.method, urlparse(event.url).netloc, urlparse(event.url).path) for event in events}
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
        state.seen_request_routes.update((event.method, urlparse(event.url).netloc, urlparse(event.url).path) for event in events)

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
            try:
                if deadline is not None:
                    nav_timeout_ms = max(1, min(nav_timeout_ms, int((deadline - time.monotonic() - 0.1) * 1000)))
                await page.goto(url, timeout=nav_timeout_ms, wait_until="domcontentloaded")
            except PlaywrightError as exc:
                result.error = type(exc).__name__
            check_http_diagnostics()
            await asyncio.sleep(settle_ms / 1000)
            check_http_diagnostics()
            for _ in range(max_scrolls):
                await page.evaluate("window.scrollBy(0, window.innerHeight)")
                check_http_diagnostics()
                await asyncio.sleep(0.5)
                check_http_diagnostics()
            await observe()
            if scope is not None:
                tracker = _DeltaTracker(captured)
                await run_safe_interactions(
                    page, scope, tracker=tracker, options=interaction, state=state,
                    planner=planner, max_actions=max_actions, observe=observe, deadline=deadline,
                    check_diagnostics=check_http_diagnostics,
                )
                check_http_diagnostics()
                result.interactions = tracker.records
            seen_keys = {(event.method, event.url) for event in captured.values()}
            for event in await _collect_init_script_events(page, page.url, seen_keys):
                captured[event.request_id] = event
            check_http_diagnostics()
            if collect_bodies:
                await _fill_bodies(client, captured, skip_rids=fetched_rids, check_diagnostics=check_http_diagnostics)
            check_http_diagnostics()
            if download_tasks:
                await asyncio.gather(*download_tasks)
            check_http_diagnostics()
    except TimeoutError as exc:
        if exc is http_diagnostic_error or any(exc is error for error in task_diagnostic_errors):
            primary_error = exc
            raise
        handled_error = exc
        result.error = "ScanDeadlineExceeded" if capture_timeout.expired() else type(exc).__name__
    except PlaywrightError as exc:
        if exc is http_diagnostic_error or any(exc is error for error in task_diagnostic_errors):
            primary_error = exc
            raise
        handled_error = exc
        result.error = type(exc).__name__
    except BaseException as exc:
        if not isinstance(exc, Exception):
            http_diagnostic_pending = False
        primary_error = exc
        raise
    finally:
        closing = True
        try:
            if page is not None:
                result.final_url = page.url
            result.network_events = list(captured.values())
            result.observations.extend({"observation": "action", **record} for record in state.records[record_start:])
            for task in download_tasks:
                if not task.done():
                    task.cancel()
            if download_tasks:
                await asyncio.gather(*download_tasks, return_exceptions=True)
            failures = []
            for task in download_tasks:
                if not task.cancelled():
                    error = task.exception()
                    if error is not None and error is not primary_error and error is not handled_error:
                        failures.append((error, "Page task failed"))
            failures.extend((error, "Download finalization failed") for error in download_finalization_errors)
            if failures and primary_error is None and handled_error is None:
                error = failures[0][0]
                if isinstance(error, (TimeoutError, PlaywrightError)) and not any(error is diagnostic for diagnostic in task_diagnostic_errors):
                    handled_error = error
                    result.error = type(error).__name__
                    failures = failures[1:]
            if failures:
                failure_target = primary_error
                if failure_target is None and handled_error is None:
                    failure_target = failures[0][0]
                if failure_target is not None:
                    for error, label in failures:
                        note = f"{label}: {type(error).__name__}"
                        if error is not failure_target and note not in getattr(failure_target, "__notes__", []):
                            failure_target.add_note(note)
                    if primary_error is None:
                        raise failure_target
                else:
                    result.observations.append({"kind": "capture_cleanup", "errors": [
                        type(error).__name__ for error, _ in failures
                    ]})
            for event in captured.values():
                if relevant(event) and event.request_id not in reported_requests:
                    report_request(event)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                http_diagnostic_pending = False
            if isinstance(exc, Exception) and primary_error is not None:
                primary_error.add_note(f"Capture finalization failed: {type(exc).__name__}")
            else:
                primary_error = exc
                raise
        finally:
            try:
                if client is not None:
                    try:
                        async with asyncio.timeout(CLEANUP_TIMEOUT_SEC):
                            await client.detach()
                    except BaseException as exc:
                        if not isinstance(exc, Exception):
                            http_diagnostic_pending = False
                        if isinstance(exc, Exception) and primary_error is not None:
                            primary_error.add_note(f"CDP detach cleanup failed: {type(exc).__name__}")
                        elif isinstance(exc, Exception) and http_diagnostic_error is not None:
                            http_diagnostic_error.add_note(f"CDP detach cleanup failed: {type(exc).__name__}")
                        elif isinstance(exc, (PlaywrightError, TimeoutError)):
                            log.warning("CDP detach cleanup failed: %s", type(exc).__name__)
                            if isinstance(exc, TimeoutError):
                                result.error = result.error or "CaptureCleanupTimeout"
                        else:
                            primary_error = exc
                            raise
            finally:
                try:
                    if page is not None:
                        try:
                            async with asyncio.timeout(CLEANUP_TIMEOUT_SEC):
                                await page.close()
                        except BaseException as exc:
                            if not isinstance(exc, Exception):
                                http_diagnostic_pending = False
                            if isinstance(exc, Exception) and primary_error is not None:
                                primary_error.add_note(f"Page close cleanup failed: {type(exc).__name__}")
                            elif isinstance(exc, Exception) and http_diagnostic_error is not None:
                                http_diagnostic_error.add_note(f"Page close cleanup failed: {type(exc).__name__}")
                            elif isinstance(exc, (PlaywrightError, TimeoutError)):
                                log.warning("Page close cleanup failed: %s", type(exc).__name__)
                                if isinstance(exc, TimeoutError):
                                    result.error = result.error or "CaptureCleanupTimeout"
                            else:
                                primary_error = exc
                                raise
                finally:
                    if http_diagnostic_error is not None and http_diagnostic_error is not primary_error:
                        if primary_error is None or (
                            http_diagnostic_pending and not isinstance(http_diagnostic_error, Exception)
                        ):
                            raise http_diagnostic_error
                        primary_error.add_note(f"HTTP diagnostic failed: {type(http_diagnostic_error).__name__}")
    return result


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


def _merge_strings(first: list[str], second: list[str]) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for value in first + second:
        if value in seen:
            continue
        seen.add(value)
        merged.append(value)
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
        self.records: list[InteractionRecord] = []
        self.last_requests: list[NetworkEvent] = []

    def begin(self) -> None:
        self._before = set(self._captured)
        self.last_requests = []

    def commit(self, action: str, selector: str, label: str) -> int:
        """Tag requests that appeared since begin(); return how many fired.

        The count lets the interactor decide whether a pagination button is
        still productive (keep clicking) or has run dry (stop).
        """
        tag = label.strip() or action
        triggered: list[tuple[str, str]] = []
        for rid in set(self._captured) - self._before:
            ev = self._captured[rid]
            self.last_requests.append(ev)
            ev.triggered_by = tag
            triggered.append((ev.method, ev.url))
        if triggered:
            self.records.append(
                InteractionRecord(action, selector, label, triggered)
            )
        return len(triggered)


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
    check_diagnostics: Callable[[], None] | None = None,
) -> None:
    for rid, ev in list(captured.items()):
        if skip_rids and rid in skip_rids:
            continue
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
        if check_diagnostics is not None:
            check_diagnostics()


# --- Phase 1-B: Selective body collection ---


def _classify_selective_body(
    url: str, mime: str, scope: Scope | None,
) -> tuple[str, int] | None:
    """Decide if a response qualifies for selective body collection.

    Returns (kind, size_limit) if it should be collected, None otherwise.
    JS bodies are collected regardless of scope (CDN JS may contain target API paths).
    """
    mime_lower = mime.lower()

    if "javascript" in mime_lower or "ecmascript" in mime_lower:
        return ("js", STATIC_JS_BODY_LIMIT)

    return None


async def _fill_selective_bodies(
    client,
    captured: dict[str, NetworkEvent],
    scope: Scope | None,
    skip_rids: set[str] | None = None,
    check_diagnostics: Callable[[], None] | None = None,
) -> tuple[list[CapturedBody], set[str]]:
    """Collect JavaScript response bodies for static endpoint discovery.

    This runs independently of the collect_bodies flag.

    Returns (bodies, fetched_rids) so the caller can skip these request IDs
    in a subsequent _fill_bodies call to avoid duplicate CDP fetches.
    """
    bodies: list[CapturedBody] = []
    fetched_rids: set[str] = set()
    for rid, ev in list(captured.items()):
        if skip_rids and rid in skip_rids:
            continue
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
            if body_text:
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
        if check_diagnostics is not None:
            check_diagnostics()
    return bodies, fetched_rids
