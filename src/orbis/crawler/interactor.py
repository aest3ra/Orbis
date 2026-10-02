"""Observe the current screen again after each action and explore its workflows."""

from __future__ import annotations

import asyncio
import hashlib
import json
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
from orbis.crawler.planner import QwenPlanner
from orbis.crawler.scope import Scope
from orbis.safety import is_dangerous_url, is_download_url, is_safe_url, text_has_danger_keyword
from orbis.telemetry import clean, timestamp

MAX_ACTIONS_PER_PAGE = 20     # total actions across all re-discovery rounds
MAX_PAGINATION_REPEATS = 4    # repeat clicks on one "load more"/"next" while it keeps firing
MAX_SCROLL_ACTIONS_PER_URL = 3
__all__ = ["run_safe_interactions"]

_VALUE = "test"
_WRITE_VALUE = "Orbis exploration test content"
_CLICK_TIMEOUT_MS = 1_500
_SETTLE_MS = 400
_MIN_OBSERVATION_MS = 1_000
_SETTLE_TIMEOUT_MS = 2_000
_SETTLE_POLL_MS = 50
_SEARCH_RE = re.compile(r"(^q$|search|query|keyword|filter|find|lookup|term)", re.I)
_BUTTON_RE = re.compile(
    r"(tab|modal|accordion|collapse|load[\s_-]*more|show[\s_-]*more|"
    r"next|search|filter|더보기|다음|검색|필터)",
    re.I,
)
# Buttons worth clicking repeatedly to walk through pages of results.
# Strict subset of _BUTTON_RE (tabs/modals are one-shot, paginators repeat).
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

_FORM_JS = """
(form, value) => {
    const fields = Array.from(form.querySelectorAll("input,textarea")).filter((el) => {
        const type = (el.getAttribute("type") || "text").toLowerCase();
        return !el.disabled && !el.readOnly && !["hidden","file","password","submit","button","reset","checkbox","radio"].includes(type);
    });
    for (const el of fields) {
        el.focus();
        el.value = value;
        el.dispatchEvent(new Event("input", {bubbles: true}));
        el.dispatchEvent(new Event("change", {bubbles: true}));
    }
    if (form.requestSubmit) form.requestSubmit();
    else form.submit();
    return true;
}
"""

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
    acted: set[tuple[str, str]] = field(default_factory=set)
    records: list[dict] = field(default_factory=list)
    scrolls: dict[str, int] = field(default_factory=dict)
    current_action: dict | None = None
    page_id: int = 0
    emit: Callable[..., dict] | None = None
    action_counts: dict[str, int] = field(default_factory=dict)
    seen_request_routes: set[tuple[str, str, str]] = field(default_factory=set)
    request_counts: dict[tuple[str, str, str], int] = field(default_factory=dict)
    download_counts: dict[str, int] = field(default_factory=dict)

    def event(self, event: str, **details) -> None:
        if self.emit is not None:
            self.emit(event, page_id=self.page_id, **details)


async def run_safe_interactions(
    page: Page, scope: Scope, *, tracker: Any = None,
    options: InteractionConfig | None = None,
    state: ExplorationState | None = None,
    planner: QwenPlanner | None = None,
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
    while attempts < max_actions and count < max_actions:
        if not scope.allows(page.url) or not _action_time_left(deadline):
            stop_reason = "out_of_scope" if not scope.allows(page.url) else "deadline"
            break
        candidates = await _collect_candidates(page)
        fingerprint = _state_fingerprint(candidates)
        fresh = [
            c for c in candidates
            if (page.url, _candidate_key(c)) not in state.acted
            and _is_candidate_safe(c, scope, page.url, options)
            and (c["kind"] != "scroll" or state.scrolls.get(page.url, 0) < MAX_SCROLL_ACTIONS_PER_URL)
        ]
        if not fresh:
            if not back_stack:
                stop_reason = "no_fresh_candidates"
                break
            back_stack.pop()
            candidate = {"kind": "back", "selector": "", "label": "Back"}
        else:
            fresh.sort(key=_priority)
            candidate = dict(fresh[0])
            decision_started = time.monotonic()
            selection = "rules"
            if planner is not None and (deadline is None or
                    deadline - time.monotonic() > planner.config.timeout_sec * 2 + 5):
                decision = await planner.choose(page.url, fresh, state.records)
                if decision is not None:
                    selection = "llm"
                    candidate = dict(fresh[decision.candidate_id])
                    if decision.value is not None and candidate["kind"] in ("input", "select"):
                        candidate["value"] = decision.value
            if candidate["kind"] == "file":
                candidate["files"] = [str(p) for p in _upload_files(candidate, options)]
            if options.allow_write and candidate["kind"] == "input":
                candidate.setdefault("value", _input_value(candidate))
            state.acted.add((page.url, _candidate_key(candidate)))
        before_url = page.url
        if candidate["kind"] == "scroll":
            state.scrolls[page.url] = state.scrolls.get(page.url, 0) + 1
        attempts += 1
        action_id = len(state.records) + 1
        candidate["action_id"] = action_id
        action_key = hashlib.sha256((before_url + _candidate_key(candidate)).encode()).hexdigest()[:16]
        state.action_counts[action_key] = state.action_counts.get(action_key, 0) + 1
        record = {"action_id": action_id, "page_id": state.page_id, "action_key": action_key,
                  "started_at": timestamp(), "kind": candidate["kind"],
                  "label": candidate.get("label", candidate.get("text", "")),
                  "before_url": before_url, "state": fingerprint, "status": "started", "executions": 0,
                  "attempt_number": state.action_counts[action_key], "candidate_count": len(candidates),
                  "fresh_candidates": len(fresh), "selection": selection if fresh else "backtrack",
                  "decision_ms": round((time.monotonic() - decision_started) * 1000, 1) if fresh else 0}
        state.records.append(record)
        state.event("action_start", **{key: value for key, value in record.items() if key != "page_id"})
        started = time.monotonic()
        state.current_action = candidate
        primary_error: BaseException | None = None
        try:
            executed = await _act(page, candidate, scope, tracker, max_actions - count, deadline)
            count += executed
            record.update(status="executed" if executed else "failed", executions=executed)
            if page.url != before_url and candidate["kind"] != "back":
                back_stack.append(before_url)
            if observe is not None:
                await observe()
            after_state = _state_fingerprint(await _collect_candidates(page))
            record.update(after_state=after_state, ui_state_changed=fingerprint != after_state)
            record["observed_progress"] = bool(record["ui_state_changed"] or page.url != before_url
                                                or record.get("new_request_routes", 0))
        except asyncio.CancelledError as exc:
            primary_error = exc
            record.update(status="interrupted", failure_reason="cancelled")
            raise
        except Exception as exc:
            primary_error = exc
            record.update(status="error", failure_reason=type(exc).__name__)
            raise
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            try:
                record.update(after_url=page.url, duration_ms=round((time.monotonic() - started) * 1000, 1),
                              finished_at=timestamp(), wait_outcomes=candidate.get("wait_outcomes", []))
                if candidate.get("failure_reason"):
                    record.setdefault("failure_reason", candidate["failure_reason"])
                record.update(clean(record))
                state.event("action_end", **{key: value for key, value in record.items() if key != "page_id"})
            except BaseException as exc:
                if isinstance(exc, Exception) and primary_error is not None:
                    primary_error.add_note(f"Action finalization failed: {type(exc).__name__}")
                else:
                    raise
            finally:
                state.current_action = None
    state.event("exploration_end", url=page.url, stop_reason=stop_reason, attempts=attempts, executions=count)
    return count


def _action_time_left(deadline: float | None) -> bool:
    return deadline is None or deadline - time.monotonic() > (_CLICK_TIMEOUT_MS + _SETTLE_TIMEOUT_MS) / 1000


def _candidate_key(candidate: dict) -> str:
    return json.dumps([candidate.get(name, "") for name in
                       ("kind", "key", "name", "label", "nav_url", "form_action", "context")]
                      + ([candidate.get("selector", "")] if "key" not in candidate else []))


def _state_fingerprint(candidates: list[dict]) -> str:
    return hashlib.sha256(json.dumps([
        (_candidate_key(c), c.get("expanded", ""), bool(c.get("filled"))) for c in candidates
    ], ensure_ascii=False).encode()).hexdigest()[:16]


def _priority(candidate: dict) -> int:
    kind = candidate["kind"]
    if kind == "link" and (candidate.get("download") or is_download_url(candidate.get("nav_url", ""))):
        return -1
    offset = 10 if candidate.get("navigation") else 0
    if kind in ("input", "select", "check"):
        return offset
    if kind == "file":
        return offset + 1
    if kind == "form":
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
        if not await _perform_and_settle(page, candidate):
            break
        done += 1
        fired = None
        if tracker is not None:
            fired = tracker.commit(
                str(candidate.get("kind") or ""),
                str(candidate.get("selector") or ""),
                # clean human label; fall back to broad safety text
                str(candidate.get("label") or candidate.get("text") or ""),
            )
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


def _has_structural_button_signal(candidate: dict[str, Any]) -> bool:
    role = str(candidate.get("role") or "").lower()
    tag = str(candidate.get("tag") or "").lower()
    if role == "tab" or tag == "summary":
        return True
    return any(
        str(candidate.get(key) or "")
        for key in ("toggle", "controls", "expanded")
    )


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
    if candidate.get("has_hidden_submit") or (candidate.get("has_file") and not options.allow_write):
        return False
    if candidate.get("filled"):
        return False

    action = _abs(candidate.get("action") or "", base_url)
    form_action = _abs(candidate.get("form_action") or base_url, base_url)
    if not _safe_url(form_action, scope):
        return False
    if kind == "scroll":
        return True
    if kind == "link":
        if not options.allow_write and _WRITE_RE.search(candidate.get("label", candidate.get("text", ""))):
            return False
        target = candidate.get("nav_url", "")
        return (urlparse(target).scheme in ("http", "https") and scope.allows(target)
                and not is_dangerous_url(target)
                and (options.allow_write or _method(candidate, "method") == "GET"))
    if kind == "file":
        return options.allow_write and bool(_upload_files(candidate, options))
    if kind in ("select", "check"):
        return options.allow_write or bool(_SEARCH_RE.search(text))

    if kind == "form":
        return options.allow_write or _method(candidate, "form_method") == "GET"
    if kind == "input":
        if options.allow_write:
            return True
        if not _SEARCH_RE.search(text):
            return False
        return not candidate.get("form_method") or (
            _method(candidate, "form_method") == "GET" and _safe_url(form_action, scope)
        )
    if kind == "button":
        if _UPLOAD_RE.search(text) and not options.upload_files:
            return False
        if not (options.allow_write or _BUTTON_RE.search(text) or _CLOSE_RE.search(candidate.get("label", ""))
                or _has_structural_button_signal(candidate) or candidate.get("nav_url")):
            return False
        if candidate.get("nav_url") and not _safe_url(candidate["nav_url"], scope):
            return False
        if not options.allow_write and _WRITE_RE.search(text):
            return False
        if not options.allow_write and candidate.get("method") and _method(candidate, "method") != "GET":
            return False
        if action and not _safe_url(action, scope):
            return False
        if options.allow_write:
            return True
        if _is_submit(candidate):
            return _method(candidate, "form_method") == "GET" and _safe_url(form_action, scope)
        return not candidate.get("form_method") or _method(candidate, "form_method") == "GET"
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
        if kind == "form":
            await page.eval_on_selector(selector, _FORM_JS, candidate.get("value", _VALUE))
            return True
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
    page.on("request", started)
    page.on("requestfinished", finished)
    page.on("requestfailed", finished)
    try:
        await page.evaluate(_WATCH_DOM_JS)
        if not await _perform(page, candidate):
            return False
        wait_started = time.monotonic()
        outcome = await _settle(page, network_quiet)
        candidate.setdefault("wait_outcomes", []).append({"reason": outcome, "duration_ms": round((time.monotonic() - wait_started) * 1000, 1)})
        return True
    except PlaywrightError as exc:
        candidate["failure_reason"] = "action_timeout" if isinstance(exc, PlaywrightTimeoutError) else "browser_error"
        return False
    finally:
        page.remove_listener("request", started)
        page.remove_listener("requestfinished", finished)
        page.remove_listener("requestfailed", finished)
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


def _method(candidate: dict[str, Any], key: str) -> str:
    return str(candidate.get(key) or "GET").upper()


def _is_submit(candidate: dict[str, Any]) -> bool:
    return candidate.get("button_type") in ("", "submit") and bool(candidate.get("form_method"))
