"""Active endpoint probing.

Probe is verification-only: it sends safe in-scope GET requests for endpoints
that were not observed live and records reachability from the response status.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
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


def classify_probe(status: int | None) -> str:
    if status is None:
        return "failed"
    if status in (404, 410):
        return "failed"
    return "verified"


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
            # Free the server-side response buffer immediately; we only need
            # the status, and up to probe_max_requests responses would otherwise
            # accumulate in the driver until the context closes.
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
) -> list[tuple[int, str, int | None]]:
    results: list[tuple[int, str, int | None]] = []
    budget = max(0, int(getattr(limits, "probe_max_requests", 0)))
    timeout_sec = float(getattr(limits, "probe_timeout_sec", 10))
    rps = float(getattr(limits, "rate_limit_rps", 0.0))
    rate_delay = 0.0 if rps <= 0 else 1.0 / rps
    last_req_at = 0.0
    sent = 0
    skipped = 0

    for endpoint_id, url in candidates:
        if not probe_target_ok(url, scope):
            skipped += 1
            continue
        if sent >= budget:
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
                break
            result = ProbeResult(None, error=type(exc).__name__)

        status = classify_probe(result.status)
        results.append((endpoint_id, status, result.status))

    log.info(
        "probe candidates: sent=%d skipped=%d budget=%d results=%d",
        sent, skipped, budget, len(results),
    )
    return results
