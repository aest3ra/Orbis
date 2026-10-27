"""Active endpoint probing.

Probe sends safe in-scope GET requests for endpoints that were never observed
live and records the raw outcome: the HTTP status when the server answered, or
no response. It claims nothing about whether the route exists. Servers answer
missing paths in app-specific ways (404, 401 behind an auth wall, 400 under a
GraphQL router), so that judgment belongs to whoever reads probe_code.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlparse

from orbis.crawler.scope import Scope
from orbis.safety import is_safe_url

log = logging.getLogger("orbis.probe")

_UNRESOLVED_PLACEHOLDER = re.compile(
    r"\{(?:id|n|slug|uuid|hash|date)\}|\$\{[^}]*\}"
)
_ACTION_KEYS = {"action", "do", "cmd", "op", "operation"}
_DANGER_VERBS = {
    "delete",
    "remove",
    "destroy",
    "deactivate",
    "cancel",
    "clear",
    "purge",
    "logout",
    "disable",
}
_TOKEN_KEYS = {
    "token",
    "code",
    "key",
    "otp",
    "ticket",
    "signature",
    "sig",
    "nonce",
    "confirm",
}
_STATE_VERB_RE = re.compile(
    r"(?:^|/|[?&=])"
    r"(?:verify|confirm|accept|activate|reset|invite|unsubscribe|optout)\b",
    re.I,
)
_VALUE_SPLIT = re.compile(r"[-_\s]+")


@dataclass
class ProbeResult:
    status: int | None
    error: str | None = None


@dataclass
class ProbeResults:
    """Counts for one probe stage; per-endpoint results go to ``on_result``."""
    responded: int = 0
    no_response: int = 0
    sent: int = 0
    skipped: int = 0
    remaining: int = 0
    stop_reason: str = "completed"


def _has_unresolved_placeholder(url: str) -> bool:
    return bool(_UNRESOLVED_PLACEHOLDER.search(url))


def _dangerous_action_value(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in _DANGER_VERBS:
        return True
    return any(part in _DANGER_VERBS for part in _VALUE_SPLIT.split(normalized))


def _query_is_dangerous(url: str) -> bool:
    parsed = urlparse(url)
    if _STATE_VERB_RE.search(parsed.path or ""):
        return True
    if parsed.query and _STATE_VERB_RE.search(f"?{parsed.query}"):
        return True

    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_l = key.strip().lower()
        if key_l in _TOKEN_KEYS:
            return True
        if key_l in _ACTION_KEYS and _dangerous_action_value(value):
            return True
    return False


def probe_target_ok(url: str | None, scope: Scope) -> bool:
    if not url:
        return False
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if not scope.allows(url):
        return False
    if not is_safe_url(url):
        return False
    if _has_unresolved_placeholder(url):
        return False
    if _query_is_dangerous(url):
        return False
    return True


def _is_target_closed(exc: BaseException) -> bool:
    name = type(exc).__name__
    if name == "TargetClosedError":
        return True
    msg = str(exc)
    return "Target page, context or browser has been closed" in msg


async def probe_endpoint(
    context: Any,
    url: str,
    *,
    timeout_sec: float,
) -> ProbeResult:
    async def _get_status() -> int:
        resp = await context.request.get(
            url,
            max_redirects=0,
            timeout=timeout_sec * 1000,
        )
        try:
            return resp.status
        finally:
            # Free the driver-side response buffer right away; only the status
            # is needed and probe_max_requests responses would otherwise pile up.
            with suppress(Exception):
                await resp.dispose()

    try:
        return ProbeResult(
            status=await asyncio.wait_for(_get_status(), timeout=timeout_sec)
        )
    except Exception as exc:
        if _is_target_closed(exc):
            raise
        return ProbeResult(None, error=type(exc).__name__)


async def probe_candidates(
    context: Any,
    candidates: list[tuple[int, str | None]],
    *,
    scope: Scope,
    limits: Any,
    on_result: Callable[[tuple[int, str, int | None]], None] | None = None,
) -> ProbeResults:
    results: list[tuple[int, str, int | None]] = []
    total = len(candidates)
    budget = max(0, int(getattr(limits, "probe_max_requests", 0)))
    timeout_sec = float(getattr(limits, "probe_timeout_sec", 10))
    rps = float(getattr(limits, "rate_limit_rps", 0.0))
    rate_delay = 0.0 if rps <= 0 else 1.0 / rps
    last_req_at = 0.0
    sent = 0
    skipped = 0
    processed = 0
    stop_reason = "completed"

    for idx, (endpoint_id, url) in enumerate(candidates):
        if not probe_target_ok(url, scope):
            skipped += 1
            processed = idx + 1
            log.debug("probe skip endpoint=%s url=%s", endpoint_id, url)
            continue
        if sent >= budget:
            stop_reason = "budget"
            processed = idx
            break

        wait = (last_req_at + rate_delay) - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        last_req_at = time.monotonic()
        sent += 1

        try:
            result = await probe_endpoint(context, url, timeout_sec=timeout_sec)
        except Exception as exc:
            if _is_target_closed(exc):
                log.warning("probe stopped: browser context closed")
                stop_reason = "context_closed"
                processed = idx + 1
                break
            result = ProbeResult(None, error=type(exc).__name__)

        status = "responded" if result.status is not None else "no_response"
        row = (endpoint_id, status, result.status)
        results.append(row)
        if on_result is not None:
            on_result(row)
        processed = idx + 1
        log.debug(
            "probe result endpoint=%s status=%s code=%s error=%s",
            endpoint_id, status, result.status, result.error,
        )

    remaining = max(0, total - processed)
    log.info(
        "probe candidates: total=%d sent=%d skipped=%d remaining=%d "
        "budget=%d stop=%s",
        total, sent, skipped, remaining, budget, stop_reason,
    )
    return ProbeResults(
        responded=sum(status == "responded" for _, status, _ in results),
        no_response=sum(status == "no_response" for _, status, _ in results),
        sent=sent, skipped=skipped, remaining=remaining, stop_reason=stop_reason,
    )
