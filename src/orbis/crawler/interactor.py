"""Observe the current screen again after each action and explore its workflows."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import mimetypes
import re
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

from playwright.async_api import Error as PlaywrightError, Page, Request, TimeoutError as PlaywrightTimeoutError

from orbis.config import InteractionConfig
from orbis.crawler.scope import Scope
from orbis.safety import is_dangerous_url, is_download_url, is_safe_url, text_has_danger_keyword
from orbis.telemetry import clean, timestamp

MAX_ACTIONS_PER_PAGE = 20     # total actions across all re-discovery rounds
MAX_PAGINATION_REPEATS = 4    # repeat clicks on one "load more"/"next" while it keeps firing
MAX_SCROLL_ACTIONS_PER_URL = 3
# Executions of one candidate on one URL across screen states. A control that
# keeps producing new request routes stays eligible; one that stopped is capped
# so open/close toggles and repeated uploads cannot drain the page budget.
MAX_REPEATS_PER_CANDIDATE = 2
# Site-wide UI is judged by what it yields, not by how often it appears: the
# controls one action reveals form a widget, and a widget that produced no new
# request route on this many consecutive URLs is ordered after the page's own
# controls. It is never excluded, so a widget that routes differently on some
# page still gets its turn once the page-specific controls are done.
DEMOTE_AFTER_DRY_URLS = 2
__all__ = ["run_safe_interactions"]

log = logging.getLogger("orbis.interactor")

_VALUE = "test"
_WRITE_VALUE = "Orbis exploration test content"
_CLICK_TIMEOUT_MS = 1_500
_SETTLE_MS = 400
_MIN_OBSERVATION_MS = 1_000
_SETTLE_TIMEOUT_MS = 2_000
_SETTLE_POLL_MS = 50
_SEARCH_RE = re.compile(r"(^q$|search|query|keyword|filter|find|lookup|term)", re.I)
# Buttons worth clicking repeatedly to walk through pages of results.
_PAGINATION_RE = re.compile(
    r"(load[\s_-]*more|show[\s_-]*more|next|더보기|다음)", re.I,
)
_EXTRA_DANGER = frozenset({
    "billing", "checkout", "invite", "payment", "purchase", "결제", "초대",
})
_AUTH_RE = re.compile(r"(?:^|[\s/_-])(login|log[ -]?in|signin|sign[ -]?in|signup|sign[ -]?up|register)(?:$|[\s/?_-])|로그인|회원가입", re.I)
_WRITE_RE = re.compile(r"\b(create|compose|new|publish|submit|save|reply|write|upload)\b|작성|등록|저장|답글|첨부", re.I)
_CLOSE_RE = re.compile(r"^(close|cancel|back|닫기|취소|뒤로)$", re.I)
_UPLOAD_RE = re.compile(r"upload|attach|업로드|첨부", re.I)
_SUBMIT_RE = re.compile(r"^(submit|publish|post|save|등록|게시|저장)(?:\s|$)", re.I)

_COLLECT_JS = Path(__file__).with_name("elements.js").read_text(encoding="utf-8")

_WATCH_DOM_JS = """
() => {
    window.__orbisInteractionActivity?.observer.disconnect();
    const state = {lastMutation: performance.now()};
    state.observer = new MutationObserver(() => { state.lastMutation = performance.now(); });
    state.observer.observe(document, {subtree: true, childList: true, attributes: true, characterData: true});
    window.__orbisInteractionActivity = state;
}
"""

_DOM_QUIET_JS = """
() => window.__orbisInteractionActivity
    ? performance.now() - window.__orbisInteractionActivity.lastMutation
    : null
"""


@dataclass
class ExplorationState:
    acted: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    records: list[dict] = field(default_factory=list)
    scrolls: dict[str, int] = field(default_factory=dict)
    current_action: dict | None = None
    page_id: int = 0
    emit: Callable[..., dict] | None = None
    action_counts: dict[str, int] = field(default_factory=dict)
    seen_request_routes: set[tuple[str, str, str]] = field(default_factory=set)
    download_counts: dict[str, int] = field(default_factory=dict)
    # action key -> completed executions (action_counts also counts failed attempts)
    executed_counts: dict[str, int] = field(default_factory=dict)
    # action key -> whether its latest execution produced new request routes
    productive: dict[str, bool] = field(default_factory=dict)
    # control key -> widget key of the action that first revealed it
    revealed_by: dict[str, str] = field(default_factory=dict)
    # (widget key, url) -> new request routes its controls produced there
    widget_yield: dict[tuple[str, str], int] = field(default_factory=dict)
    # widget key -> consecutive URLs on which it produced nothing new
    widget_dry_urls: dict[str, int] = field(default_factory=dict)

    def event(self, event: str, **details) -> None:
        if self.emit is None:
            return
        try:
            self.emit(event, page_id=self.page_id, **details)
        except Exception:
            # Diagnostics never stop exploration; the DB keeps the primary evidence.
            log.warning("diagnostic event %s was not recorded", event, exc_info=True)


async def run_safe_interactions(
    page: Page, scope: Scope, *, tracker: Any = None,
    options: InteractionConfig | None = None,
    state: ExplorationState | None = None,
    max_actions: int = MAX_ACTIONS_PER_PAGE,
    observe: Callable[[], Awaitable[None]] | None = None,
    deadline: float | None = None,
) -> int:
    if not scope.allows(page.url):
        return 0
    options = options or InteractionConfig()
    state = state or ExplorationState()
    back_stack: list[str] = []
    count = 0
    attempts = 0
    stop_reason = "action_budget"
    previous_candidates: set[str] = set()
    last_widget: str | None = None
    touched: set[tuple[str, str]] = set()
    while attempts < max_actions and count < max_actions:
        if not scope.allows(page.url) or not _action_time_left(deadline):
            stop_reason = "out_of_scope" if not scope.allows(page.url) else "deadline"
            break
        candidates = await _collect_candidates(page)
        if last_widget is not None:
            # Controls that appeared after an action belong to that action's widget.
            for c in candidates:
                if _candidate_key(c) not in previous_candidates:
                    state.revealed_by.setdefault(_control_key(c), last_widget)
        fingerprint = _state_fingerprint(candidates)
        node = (page.url, fingerprint)
        attempted = state.acted.setdefault(node, set())
        fresh = [
            c for c in candidates
            if _candidate_key(c) not in attempted
            and _is_candidate_safe(c, scope, page.url, options)
            and (c["kind"] != "scroll" or state.scrolls.get(page.url, 0) < MAX_SCROLL_ACTIONS_PER_URL)
            and _repeat_allowed(state, page.url, c)
        ]
        if not fresh:
            if not back_stack:
                stop_reason = "no_fresh_candidates"
                break
            back_stack.pop()
            candidate = {"kind": "back", "selector": "", "label": "Back"}
        else:
            # Breadth before repetition: a candidate never executed on this URL
            # outranks a re-execution in a new screen state, whatever their
            # priority classes. Within a class, controls that just appeared go
            # first. Newness never outranks the class itself: rich pages keep
            # producing new low-priority controls (inserted links, revealed
            # menus, toasts) and would otherwise starve an already visible
            # submit control until the page budget is gone.
            fresh.sort(key=lambda c: (_demoted(state, c),
                                      state.executed_counts.get(_action_key(page.url, c), 0) > 0,
                                      _priority(c), _candidate_key(c) in previous_candidates))
            candidate = dict(fresh[0])
            decision_started = time.monotonic()
            selection = "rules"
            if candidate["kind"] == "file":
                candidate["files"] = [str(p) for p in _upload_files(candidate, options)]
            if candidate["kind"] == "input":
                candidate.setdefault("value", _input_value(candidate))
            attempted.add(_candidate_key(candidate))
        previous_candidates = {_candidate_key(c) for c in candidates}
        before_url = page.url
        if candidate["kind"] == "scroll":
            state.scrolls[page.url] = state.scrolls.get(page.url, 0) + 1
        attempts += 1
        action_id = len(state.records) + 1
        candidate["action_id"] = action_id
        action_key = _action_key(before_url, candidate)
        state.action_counts[action_key] = state.action_counts.get(action_key, 0) + 1
        widget = _widget_key(state, candidate)
        record = {"action_id": action_id, "page_id": state.page_id, "action_key": action_key,
                  "started_at": timestamp(), "kind": candidate["kind"],
                  "label": candidate.get("label", candidate.get("text", "")),
                  "widget": widget, "demoted": _demoted(state, candidate),
                  "before_url": before_url, "state": fingerprint, "status": "started", "executions": 0,
                  "attempt_number": state.action_counts[action_key], "candidate_count": len(candidates),
                  "fresh_candidates": len(fresh), "selection": selection if fresh else "backtrack",
                  "decision_ms": round((time.monotonic() - decision_started) * 1000, 1) if fresh else 0}
        state.records.append(record)
        state.event("action_start", **{key: value for key, value in record.items() if key != "page_id"})
        started = time.monotonic()
        state.current_action = candidate
        try:
            executed = await _act(page, candidate, scope, tracker, max_actions - count, deadline)
            count += executed
            if executed:
                # Only completed executions count toward the per-URL cap; a control
                # that was covered or not yet enabled may still work in a later state.
                state.executed_counts[action_key] = state.executed_counts.get(action_key, 0) + 1
            record.update(status="executed" if executed else "failed", executions=executed)
            if page.url != before_url and candidate["kind"] != "back":
                back_stack.append(before_url)
            if observe is not None:
                await observe()
            after_state = _state_fingerprint(await _collect_candidates(page))
            record.update(after_state=after_state, ui_state_changed=fingerprint != after_state)
            record["observed_progress"] = bool(record["ui_state_changed"] or page.url != before_url
                                                or record.get("new_request_routes", 0))
            state.productive[action_key] = bool(record.get("new_request_routes", 0))
            if executed:
                touched.add((widget, before_url))
                state.widget_yield[(widget, before_url)] = (
                    state.widget_yield.get((widget, before_url), 0) + (record.get("new_request_routes") or 0))
                last_widget = widget
        except asyncio.CancelledError:
            record.update(status="interrupted", failure_reason="cancelled")
            raise
        except Exception as exc:
            record.update(status="error", failure_reason=type(exc).__name__)
            raise
        finally:
            record.update(after_url=page.url, duration_ms=round((time.monotonic() - started) * 1000, 1),
                          finished_at=timestamp(), wait_outcomes=candidate.get("wait_outcomes", []))
            if candidate.get("failure_reason"):
                record.setdefault("failure_reason", candidate["failure_reason"])
            record.update(clean(record))
            state.event("action_end", **{key: value for key, value in record.items() if key != "page_id"})
            state.current_action = None
    for widget, url in touched:
        state.widget_dry_urls[widget] = 0 if state.widget_yield.get((widget, url), 0) else (
            state.widget_dry_urls.get(widget, 0) + 1)
    state.event("exploration_end", url=page.url, stop_reason=stop_reason, attempts=attempts, executions=count)
    return count


def _action_time_left(deadline: float | None) -> bool:
    return deadline is None or deadline - time.monotonic() > (_CLICK_TIMEOUT_MS + _SETTLE_TIMEOUT_MS) / 1000


def _candidate_key(candidate: dict) -> str:
    return json.dumps([candidate.get(name, "") for name in
                       ("kind", "key", "name", "label", "nav_url", "form_action", "context")]
                      + ([candidate.get("selector", "")] if "key" not in candidate else []))


def _action_key(url: str, candidate: dict) -> str:
    return hashlib.sha256((url + _candidate_key(candidate)).encode()).hexdigest()[:16]


def _control_key(candidate: dict) -> str:
    """What a control is, independent of the URL and DOM position it was seen at."""
    return json.dumps([candidate.get(name, "") for name in ("kind", "name", "label", "type")], ensure_ascii=False)


def _widget_key(state: ExplorationState, candidate: dict) -> str:
    key = _control_key(candidate)
    return state.revealed_by.get(key, key)


def _demoted(state: ExplorationState, candidate: dict) -> bool:
    return state.widget_dry_urls.get(_widget_key(state, candidate), 0) >= DEMOTE_AFTER_DRY_URLS


def _repeat_allowed(state: ExplorationState, url: str, candidate: dict) -> bool:
    """A candidate may run again on this URL in a new screen state while it keeps
    yielding new request routes; otherwise MAX_REPEATS_PER_CANDIDATE bounds it."""
    key = _action_key(url, candidate)
    return state.executed_counts.get(key, 0) < MAX_REPEATS_PER_CANDIDATE or state.productive.get(key, False)


def _state_fingerprint(candidates: list[dict]) -> str:
    return hashlib.sha256(json.dumps(sorted([
        (_candidate_key(c), c.get("expanded", ""), c.get("selected", ""), bool(c.get("filled")))
        for c in candidates
    ]), ensure_ascii=False).encode()).hexdigest()[:16]


def _priority(candidate: dict) -> int:
    kind = candidate["kind"]
    if kind == "link" and (candidate.get("download") or is_download_url(candidate.get("nav_url", ""))):
        return -1
    offset = 10 if candidate.get("navigation") else 0
    if kind in ("input", "select", "check"):
        return offset
    if kind == "file":
        return offset + 1
    if kind == "button" and _UPLOAD_RE.search(candidate.get("label", "")):
        return offset + 1
    if kind == "button" and _SUBMIT_RE.search(candidate.get("label", "")):
        return offset + 2
    if (kind == "button" or candidate.get("role") == "button") and _WRITE_RE.search(candidate.get("label", "")):
        return offset + 2
    if candidate.get("role") == "button":
        return offset + 3
    if kind == "scroll":
        return 1
    if _CLOSE_RE.search(candidate.get("label", "")):
        return offset + 6
    if kind == "link" and not candidate.get("local_toggle"):
        return offset + (5 if candidate.get("heading") else 6)
    return offset + 3


def _input_value(candidate: dict) -> str:
    values = {"email": "orbis@example.invalid", "url": "https://example.invalid",
              "number": candidate.get("min") or "1", "date": "2026-01-01",
              "datetime-local": "2026-01-01T12:00", "time": "12:00", "tel": "01000000000"}
    default = _WRITE_VALUE if candidate.get("tag") == "textarea" else "Orbis test"
    value = values.get(candidate.get("type"), default)
    length = candidate.get("max_length", -1)
    return value[:length] if length > 0 else value


def _upload_files(candidate: dict, options: InteractionConfig) -> list[Path]:
    accept = [value.strip().lower() for value in candidate.get("accept", "").split(",") if value.strip()]
    files = []
    for path in options.upload_files:
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        if not accept or any(path.suffix.lower() == value or mime == value or
                             (value.endswith("/*") and mime.startswith(value[:-1])) for value in accept):
            files.append(path)
    return files if candidate.get("multiple") else files[:1]


async def _act(
    page: Page, candidate: dict[str, Any], scope: Scope, tracker: Any, budget: int,
    deadline: float | None = None,
) -> int:
    """Perform one candidate, returning the number of actions taken.

    Pagination buttons ("load more"/"next") are clicked repeatedly while the
    tracker confirms each click drained new requests; everything else fires
    once. Without a tracker there is no productivity signal, so even a
    paginator is clicked a single time.
    """
    paginate = tracker is not None and _is_pagination(candidate)
    repeats = min(MAX_PAGINATION_REPEATS if paginate else 1, budget)
    done = 0
    for _ in range(repeats):
        if not _action_time_left(deadline):
            break
        if tracker is not None:
            tracker.begin()
        performed = await _perform_and_settle(page, candidate)
        if not performed:
            break
        done += 1
        fired = None
        if tracker is not None:
            # clean human label; fall back to the broad text, then the kind
            fired = tracker.commit(str(candidate.get("label") or candidate.get("text") or candidate.get("kind") or ""))
        if not scope.allows(page.url):
            break
        if paginate and not fired:
            break  # paginator produced no new traffic → stop walking it
    return done


def _is_pagination(candidate: dict[str, Any]) -> bool:
    if candidate.get("kind") != "button":
        return False
    text = " ".join(
        str(candidate.get(key) or "") for key in ("text", "label", "role")
    )
    return bool(_PAGINATION_RE.search(text))


async def _collect_candidates(page: Page) -> list[dict[str, Any]]:
    raw = await page.evaluate(_COLLECT_JS)
    return [
        item
        for item in raw or []
        if isinstance(item, dict) and item.get("kind")
    ]


def _is_candidate_safe(
    candidate: dict[str, Any], scope: Scope, base_url: str,
    options: InteractionConfig | None = None,
) -> bool:
    options = options or InteractionConfig()
    kind = candidate.get("kind")
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("text", "form_text", "role", "toggle", "action", "form_action")
    )
    if text_has_danger_keyword(text, _EXTRA_DANGER):
        return False
    if candidate.get("authentication") or _AUTH_RE.search(text + " " + candidate.get("nav_url", "")):
        return False
    if candidate.get("filled"):
        return False

    action = _abs(candidate.get("action") or "", base_url)
    form_action = _abs(candidate.get("form_action") or base_url, base_url)
    if not _safe_url(form_action, scope):
        return False
    if kind in ("scroll", "input", "select", "check"):
        return True
    if kind == "link":
        target = candidate.get("nav_url", "")
        return (urlparse(target).scheme in ("http", "https") and scope.allows(target)
                and not is_dangerous_url(target))
    if kind == "file":
        return bool(_upload_files(candidate, options))
    if kind == "button":
        if _UPLOAD_RE.search(text) and not options.upload_files:
            return False
        if candidate.get("nav_url") and not _safe_url(candidate["nav_url"], scope):
            return False
        return not action or _safe_url(action, scope)
    return False


async def _perform(page: Page, candidate: dict[str, Any]) -> bool:
    selector = candidate.get("selector")
    kind = candidate.get("kind")
    if kind == "back":
        await page.go_back(wait_until="domcontentloaded", timeout=_CLICK_TIMEOUT_MS)
        return True
    if kind == "scroll":
        await page.evaluate("() => window.scrollTo(0, document.scrollingElement.scrollHeight)")
        return True
    if not selector:
        candidate["failure_reason"] = "missing_selector"
        return False
    try:
        locator = page.locator(selector).first
        if kind == "input":
            await locator.fill(candidate.get("value", _VALUE), timeout=_CLICK_TIMEOUT_MS)
            if _SEARCH_RE.search(candidate.get("text", "")):
                await locator.press("Enter", timeout=_CLICK_TIMEOUT_MS)
        elif kind == "select":
            choices = candidate.get("options", [])
            value = candidate.get("value") or (choices[0]["value"] if choices else "")
            if value not in {item["value"] for item in choices}:
                candidate["failure_reason"] = "invalid_option"
                return False
            await locator.select_option(value, timeout=_CLICK_TIMEOUT_MS)
        elif kind == "check":
            await locator.check(timeout=_CLICK_TIMEOUT_MS)
        elif kind == "file":
            await locator.set_input_files(candidate["files"], timeout=_CLICK_TIMEOUT_MS)
        else:
            if not await locator.is_visible():
                candidate["failure_reason"] = "not_visible"
                return False
            if not await locator.is_enabled():
                candidate["failure_reason"] = "not_enabled"
                return False
            await locator.click(timeout=_CLICK_TIMEOUT_MS)
    except PlaywrightError as exc:
        candidate["failure_reason"] = "action_timeout" if isinstance(exc, PlaywrightTimeoutError) else "browser_error"
        return False
    return True


async def _perform_and_settle(page: Page, candidate: dict[str, Any]) -> bool:
    pending: set[Request] = set()
    last_network = time.monotonic()

    def started(request: Request) -> None:
        nonlocal last_network
        pending.add(request)
        last_network = time.monotonic()

    def finished(request: Request) -> None:
        nonlocal last_network
        if request in pending:
            pending.discard(request)
            last_network = time.monotonic()

    def network_quiet() -> bool:
        return not pending and (time.monotonic() - last_network) * 1000 >= _SETTLE_MS

    # Subscribe before clicking so even requests that start during the action
    # keep the next discovery round from running ahead of their response.
    listeners = (("request", started), ("requestfinished", finished), ("requestfailed", finished))
    for event, callback in listeners:
        page.on(event, callback)
    try:
        await page.evaluate(_WATCH_DOM_JS)
        if not await _perform(page, candidate):
            return False
        wait_started = time.monotonic()
        outcome = await _settle(page, network_quiet)
        candidate.setdefault("wait_outcomes", []).append(
            {"reason": outcome, "duration_ms": round((time.monotonic() - wait_started) * 1000, 1)})
        return True
    except PlaywrightError as exc:
        candidate["failure_reason"] = "action_timeout" if isinstance(exc, PlaywrightTimeoutError) else "browser_error"
        return False
    finally:
        for event, callback in listeners:
            page.remove_listener(event, callback)
        with suppress(PlaywrightError):
            await page.evaluate("() => window.__orbisInteractionActivity?.observer.disconnect()")


async def _settle(page: Page, network_quiet: Callable[[], bool]) -> str:
    started = time.monotonic()
    # A load state can already be satisfied before a SPA click. Observe fresh
    # network/DOM activity instead; streaming and animations remain bounded.
    with suppress(TimeoutError):
        async with asyncio.timeout(_SETTLE_TIMEOUT_MS / 1000):
            while True:
                await asyncio.sleep(_SETTLE_POLL_MS / 1000)
                try:
                    dom_quiet_ms = await page.evaluate(_DOM_QUIET_JS)
                    if dom_quiet_ms is None:
                        await page.evaluate(_WATCH_DOM_JS)
                        continue
                except PlaywrightError:
                    if page.is_closed():
                        return "page_closed"
                    continue
                elapsed_ms = (time.monotonic() - started) * 1000
                if elapsed_ms >= max(_MIN_OBSERVATION_MS, _SETTLE_MS) and dom_quiet_ms >= _SETTLE_MS and network_quiet():
                    return "quiet"
    return "timeout"


def _safe_url(url: str, scope: Scope) -> bool:
    return bool(url) and scope.allows(url) and is_safe_url(url)


def _abs(url: str, base_url: str) -> str:
    return urljoin(base_url, url) if url else ""
