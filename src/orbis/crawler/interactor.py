"""Conservative dynamic interactions for already-loaded pages."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Page

from orbis.crawler.scope import Scope
from orbis.safety import is_safe_url, text_has_danger_keyword

MAX_ACTIONS_PER_PAGE = 20     # total actions across all re-discovery rounds
MAX_ROUNDS = 4                # re-discovery passes: click → new UI appears → collect again
MAX_PAGINATION_REPEATS = 4    # repeat clicks on one "load more"/"next" while it keeps firing
__all__ = ["run_safe_interactions"]

_VALUE = "test"
_CLICK_TIMEOUT_MS = 1_500
_SETTLE_MS = 400
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
_NAV_RE = re.compile(
    r"(window\.open|location(?:\.href|\.assign|\.replace)?|"
    r"document\.location|history\.pushState|router\.push|navigate\s*\(|href\s*=)",
    re.I,
)
_EXTRA_DANGER = frozenset({
    "admin", "billing", "checkout", "download", "invite", "payment",
    "permission", "purchase", "role", "upload", "결제", "권한", "초대",
})

_COLLECT_JS = """
() => {
    const out = [];
    const seen = new Set();
    const textOf = (el) => [
        el.textContent, el.getAttribute("aria-label"), el.getAttribute("name"),
        el.getAttribute("id"), el.getAttribute("title"), el.getAttribute("value"),
        el.getAttribute("placeholder"), el.getAttribute("class"),
        el.getAttribute("role"), el.getAttribute("aria-controls"),
        el.getAttribute("aria-expanded"), el.getAttribute("data-toggle"),
        el.getAttribute("data-bs-toggle"), el.getAttribute("data-target"),
        el.getAttribute("data-bs-target"),
        el.getAttribute("data-action"), el.getAttribute("onclick"),
        el.getAttribute("formaction"), el.getAttribute("action"),
        el.getAttribute("data-href"), el.getAttribute("data-url"),
        el.getAttribute("routerlink")
    ].filter(Boolean).join(" ").trim().slice(0, 500);
    const labelOf = (el) => (
        el.getAttribute("aria-label") || (el.textContent || "").trim() ||
        el.getAttribute("value") || el.getAttribute("placeholder") ||
        el.getAttribute("name") || el.getAttribute("title") || ""
    ).trim().replace(/\\s+/g, " ").slice(0, 80);
    const visible = (el) => {
        if (!el || el.disabled || el.hidden || el.closest("[hidden],[aria-hidden='true']")) return false;
        const style = getComputedStyle(el);
        const rect = el.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && rect.width > 0 && rect.height > 0;
    };
    const abs = (url, fallback = "") => {
        try { return url ? new URL(url, document.baseURI).href : fallback; }
        catch (_) { return ""; }
    };
    const formMeta = (form) => form ? {
        form_method: (form.getAttribute("method") || "GET").toUpperCase(),
        form_action: abs(form.getAttribute("action"), document.baseURI),
        form_text: textOf(form),
        has_file: !!form.querySelector("input[type='file']"),
        has_hidden_submit: !!Array.from(form.querySelectorAll("button,input")).find((el) => {
            const type = (el.getAttribute("type") || (el.tagName === "BUTTON" ? "submit" : "")).toLowerCase();
            return type === "submit" && !visible(el);
        })
    } : {};
    const mark = (kind, el, extra = {}) => {
        if (!visible(el) || seen.has(el) || el.closest("a[href]")) return;
        // Reuse an existing mark so the same surviving element keeps a stable
        // selector across re-collections — that is what lets the caller skip
        // elements it already acted on while still picking up newly-revealed ones.
        let key = el.getAttribute("data-orbis-interaction");
        if (!key) {
            key = `orbis-${out.length}-${Math.random().toString(36).slice(2)}`;
            el.setAttribute("data-orbis-interaction", key);
        }
        seen.add(el);
        out.push({kind, selector: `[data-orbis-interaction="${key}"]`, text: textOf(el), label: labelOf(el), ...extra});
    };

    for (const form of document.querySelectorAll("form")) {
        const meta = formMeta(form);
        if (visible(form) && meta.form_method === "GET") mark("form", form, meta);
    }

    for (const el of document.querySelectorAll("input,textarea")) {
        const type = (el.getAttribute("type") || "text").toLowerCase();
        if (["hidden", "file", "password", "submit", "button", "reset", "checkbox", "radio"].includes(type)) continue;
        const form = el.closest("form");
        mark("input", el, {type, ...formMeta(form)});
    }

    const buttonSelector = [
        "button", "input[type='button']", "input[type='submit']",
        "[role='button']", "[role='tab']", "summary",
        "[aria-controls]", "[aria-expanded]", "[data-toggle]", "[data-bs-toggle]"
    ].join(",");
    for (const el of document.querySelectorAll(buttonSelector)) {
        if (el.matches("a,a *,[role='link']")) continue;
        const form = el.closest("form");
        const action = el.getAttribute("formaction") || "";
        const method = (el.getAttribute("formmethod") || "").toUpperCase();
        const buttonType = (el.getAttribute("type") || "").toLowerCase();
        if (form && (buttonType === "submit" || (buttonType === "" && el.tagName === "BUTTON"))) continue;
        mark("button", el, {
            button_type: buttonType,
            action: action ? abs(action) : "",
            nav_url: abs(el.getAttribute("href") || el.getAttribute("data-href") || el.getAttribute("data-url") || el.getAttribute("routerlink") || ""),
            method,
            role: el.getAttribute("role") || "",
            toggle: el.getAttribute("data-bs-toggle") || el.getAttribute("data-toggle") || "",
            ...formMeta(form)
        });
    }
    return out;
}
"""

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


async def run_safe_interactions(
    page: Page, scope: Scope, *, tracker: Any = None,
) -> int:
    """Drive safe interactions, re-discovering UI revealed by earlier clicks.

    Each round re-collects candidates and acts only on ones not seen before;
    a click that opens a modal/tab surfaces new candidates the next round.
    The loop ends when a round reveals nothing new (or a cap is hit).
    """
    if not scope.allows(page.url):
        return 0
    acted: set[str] = set()
    count = 0
    for _round in range(MAX_ROUNDS):
        if count >= MAX_ACTIONS_PER_PAGE or not scope.allows(page.url):
            break
        fresh = [
            c
            for c in await _collect_candidates(page)
            if str(c.get("selector") or "") not in acted
            and _is_candidate_safe(c, scope, page.url)
        ]
        if not fresh:
            break  # nothing new revealed → page is exhausted
        for candidate in fresh:
            if count >= MAX_ACTIONS_PER_PAGE or not scope.allows(page.url):
                break
            acted.add(str(candidate.get("selector") or ""))
            count += await _act(
                page, candidate, scope, tracker, MAX_ACTIONS_PER_PAGE - count,
            )
    return count


async def _act(
    page: Page, candidate: dict[str, Any], scope: Scope, tracker: Any, budget: int,
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
        if tracker is not None:
            tracker.begin()
        if not await _perform(page, candidate):
            break
        done += 1
        await _settle(page)
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


async def _collect_candidates(page: Page) -> list[dict[str, Any]]:
    try:
        raw = await page.evaluate(_COLLECT_JS)
    except Exception:
        return []
    return [
        item
        for item in raw or []
        if isinstance(item, dict) and item.get("kind")
    ]


def _is_candidate_safe(candidate: dict[str, Any], scope: Scope, base_url: str) -> bool:
    kind = candidate.get("kind")
    text = " ".join(
        str(candidate.get(key) or "")
        for key in ("text", "form_text", "role", "toggle", "action", "form_action")
    )
    if text_has_danger_keyword(text, _EXTRA_DANGER):
        return False
    if candidate.get("has_file") or candidate.get("has_hidden_submit"):
        return False

    action = _abs(candidate.get("action") or "", base_url)
    form_action = _abs(candidate.get("form_action") or base_url, base_url)

    if kind == "form":
        return _method(candidate, "form_method") == "GET" and _safe_url(form_action, scope)
    if kind == "input":
        if not _SEARCH_RE.search(text):
            return False
        return not candidate.get("form_method") or (
            _method(candidate, "form_method") == "GET" and _safe_url(form_action, scope)
        )
    if kind == "button":
        if not _BUTTON_RE.search(text):
            return False
        if candidate.get("nav_url") or _NAV_RE.search(text):
            return False
        if candidate.get("method") and _method(candidate, "method") != "GET":
            return False
        if action and not _safe_url(action, scope):
            return False
        if _is_submit(candidate):
            return _method(candidate, "form_method") == "GET" and _safe_url(form_action, scope)
        return not candidate.get("form_method") or _method(candidate, "form_method") == "GET"
    return False


async def _perform(page: Page, candidate: dict[str, Any]) -> bool:
    selector = candidate.get("selector")
    if not selector:
        return False
    try:
        if candidate.get("kind") == "form":
            if hasattr(page, "eval_on_selector"):
                await page.eval_on_selector(selector, _FORM_JS, _VALUE)
            else:
                await _first_locator(page, selector).evaluate(_FORM_JS, _VALUE)
        elif candidate.get("kind") == "input":
            locator = _first_locator(page, selector)
            await locator.fill(_VALUE, timeout=_CLICK_TIMEOUT_MS)
            await locator.press("Enter", timeout=_CLICK_TIMEOUT_MS)
        else:
            locator = _first_locator(page, selector)
            if not await _locator_check(locator, "is_visible") or not await _locator_check(locator, "is_enabled"):
                return False
            await locator.click(timeout=_CLICK_TIMEOUT_MS)
    except Exception:
        return False
    return True


async def _settle(page: Page) -> None:
    # Single wait: networkidle returns as soon as the triggered request drains
    # (or after the timeout ceiling), so no extra fixed sleep on top of it.
    if hasattr(page, "wait_for_load_state"):
        with suppress(Exception):
            await page.wait_for_load_state("networkidle", timeout=_SETTLE_MS)
            return
    if hasattr(page, "wait_for_timeout"):
        with suppress(Exception):
            await page.wait_for_timeout(_SETTLE_MS)
            return
    await asyncio.sleep(_SETTLE_MS / 1000)


async def _locator_check(locator: Any, name: str) -> bool:
    method = getattr(locator, name, None)
    if method is None:
        return True
    try:
        return bool(await method(timeout=500))
    except TypeError:
        return bool(await method())


def _first_locator(page: Page, selector: str) -> Any:
    locator = page.locator(selector)
    first = getattr(locator, "first", None)
    if callable(first):
        return first()
    if first is not None and any(hasattr(first, name) for name in ("click", "fill", "evaluate")):
        return first
    return locator


def _safe_url(url: str, scope: Scope) -> bool:
    return bool(url) and scope.allows(url) and is_safe_url(url)


def _abs(url: str, base_url: str) -> str:
    return urljoin(base_url, url) if url else ""


def _method(candidate: dict[str, Any], key: str) -> str:
    return str(candidate.get(key) or "GET").upper()


def _is_submit(candidate: dict[str, Any]) -> bool:
    return candidate.get("button_type") in ("", "submit") and bool(candidate.get("form_method"))
