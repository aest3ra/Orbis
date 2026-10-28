"""BFS crawler: visits pages, passes raw captures to Analyzer."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from playwright.async_api import async_playwright
from sqlalchemy import func
from sqlmodel import Session, select

from orbis.analysis.analyzer import analyze, build_wayback_results
from orbis.config import ScanConfig
from orbis.crawler.browser import capture_page
from orbis.crawler.interactor import ExplorationState
from orbis.crawler.frontier import Frontier
from orbis.crawler.wayback import fetch_wayback_urls
from orbis.crawler.probe import probe_candidates
from orbis.crawler.scope import Scope
from orbis.storage.db import Endpoint, Observation, open_db
from orbis.telemetry import ScanLog, clean, safe_url
from orbis.storage.repo import (
    create_scan,
    finish_scan,
    list_unprobed_endpoints,
    save_endpoints,
    set_probe_result,
)

log = logging.getLogger("orbis.crawler")


async def run_scan(
    config: ScanConfig,
    *,
    db_path: str,
    headless: bool = True,
    wayback: bool = True,
) -> int:
    for path in config.interaction.upload_files:
        if not path.is_file():
            raise ValueError(f"Upload file does not exist: {path}")
    scope = Scope(config.scope, config.target)
    auth_path: str | None = None
    if config.auth.storage_state_path:
        auth_path = str(config.auth.storage_state_path)

    engine = open_db(db_path)
    try:
        with Session(engine) as session:
            scan_id = create_scan(session, config.target, auth_path)
        trace = ScanLog(db_path, scan_id)
        try:
            log.info("scan=%d diagnostics=%s", scan_id, trace.path)
            trace.emit("scan_start", target=config.target, limits=config.limits.model_dump(),
                       authenticated=auth_path is not None, wayback=wayback)
            outcome = {"status": "error", "stop_reason": "exception"}
            try:
                summary = await _crawl_scan(config, engine, scan_id, scope, auth_path,
                                            db_path=db_path, headless=headless, wayback=wayback, trace=trace)
                outcome = {"status": "completed", **summary}
            except asyncio.CancelledError:
                outcome = {"status": "cancelled", "stop_reason": "cancelled"}
                raise
            except BaseException as exc:
                outcome["error"] = type(exc).__name__
                raise
            finally:
                # However the crawl ended, record it so the results collected so far stay usable.
                try:
                    _record_scan_end(engine, scan_id, trace, outcome, config.target)
                except Exception:
                    if outcome["status"] == "completed":
                        raise
                    log.exception("scan=%d: the scan end could not be recorded", scan_id)
        finally:
            trace.close()
    finally:
        engine.dispose()
    return scan_id


def _record_scan_end(engine, scan_id: int, trace: ScanLog, outcome: dict, target: str) -> None:
    with Session(engine) as session:
        endpoints = session.exec(
            select(func.count()).select_from(Endpoint).where(Endpoint.scan_id == scan_id)
        ).one()
    totals = dict(trace.totals)
    outcome.setdefault("pages", totals.get("pages_crawled", 0))
    outcome["endpoints"] = endpoints
    record = trace.emit("scan_end", **outcome, totals=totals)
    with Session(engine) as session:
        session.add(Observation(scan_id=scan_id, page_url=safe_url(target), kind="scan",
                                details_json=json.dumps(clean(record))))
        finish_scan(session, scan_id, outcome["pages"], endpoints)
    log.info("scan=%d status=%s stop=%s elapsed_ms=%.1f", scan_id, outcome["status"],
             outcome["stop_reason"], record["elapsed_ms"])


async def _close(closing, what: str) -> None:
    """Best-effort teardown: a failure here must not hide the scan's own outcome."""
    try:
        await closing
    except Exception:
        log.warning("%s failed", what, exc_info=True)


async def _crawl_scan(
    config: ScanConfig, engine, scan_id: int, scope: Scope, auth_path: str | None,
    *, db_path: str, headless: bool, wayback: bool, trace: ScanLog,
) -> dict:
    frontier = Frontier(
        scope,
        config.limits.max_visits_per_template,
        max_depth=config.limits.max_depth,
        slug_threshold=config.limits.slug_threshold,
    )
    frontier.enqueue(config.target)

    limits = config.limits
    deadline = time.monotonic() + limits.max_duration_sec
    rate_delay = 0.0 if limits.rate_limit_rps <= 0 else 1.0 / limits.rate_limit_rps
    pages = 0
    total_added = 0
    last_req_at = 0.0
    exploration = ExplorationState(emit=trace.emit)
    stop_reason = "max_pages"
    # Per-template count of consecutive visits that produced no new endpoint.
    zero_streak: dict[tuple[str, str], int] = {}
    wayback_seeds: list[str] = []
    wayback_seed_idx = 0

    # Wayback layer: pull archived URLs and record API-marked ones as unprobed
    # endpoints. Page-like archived URLs are kept as backfill: only one is
    # admitted when the live frontier is empty, so archived pages cannot crowd
    # out live discovery or consume template caps before live links appear.
    if wayback:
        wayback_eps = []
        for host in config.scope.include_domains:
            urls = await asyncio.to_thread(fetch_wayback_urls, host, limit=limits.wayback_max_urls)
            eps, seeds = build_wayback_results(urls, scope)
            wayback_eps.extend(eps)
            wayback_seeds.extend(seeds)
        if wayback_eps:
            with Session(engine) as session:
                added, _ = save_endpoints(session, scan_id, wayback_eps)
                total_added += added
        log.info(
            "wayback: %d API endpoints recorded, %d page seeds available",
            len(wayback_eps), len(wayback_seeds),
        )

    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.launch(headless=headless)
        try:
            ctx_kw: dict = {}
            if auth_path:
                ctx_kw["storage_state"] = auth_path
            context = await browser.new_context(**ctx_kw)
            while pages < limits.max_pages:
                wayback_seed_idx = _enqueue_wayback_backfill(
                    frontier, wayback_seeds, wayback_seed_idx,
                )
                if frontier.size == 0:
                    stop_reason = "frontier_exhausted"
                    break

                if time.monotonic() >= deadline:
                    stop_reason = "max_duration"
                    log.warning("timeout: %ds limit reached", limits.max_duration_sec)
                    break

                wait = (last_req_at + rate_delay) - time.monotonic()
                if wait > 0:
                    try:
                        async with asyncio.timeout_at(deadline):
                            await asyncio.sleep(wait)
                    except TimeoutError:
                        stop_reason = "max_duration"
                        break
                if time.monotonic() >= deadline:
                    stop_reason = "max_duration"
                    break

                item = frontier.pop()
                if item is None:
                    stop_reason = "frontier_exhausted"
                    break
                last_req_at = time.monotonic()

                page_started = time.monotonic()
                exploration.page_id = pages + 1
                trace.emit("page_start", page_id=exploration.page_id, url=item.url, depth=item.depth)
                try:
                    capture = await capture_page(
                        context,
                        item.url,
                        scope=scope,
                        max_scrolls=limits.max_scrolls_per_page,
                        interaction=config.interaction,
                        exploration=exploration,
                        max_actions=limits.max_actions_per_page,
                        download_dir=Path(db_path).resolve().parent / "downloads" / str(scan_id),
                        deadline=deadline,
                    )
                except asyncio.CancelledError:
                    trace.emit("page_end", page_id=exploration.page_id, url=item.url, status="interrupted",
                               error="cancelled", duration_ms=round((time.monotonic() - page_started) * 1000, 1))
                    raise
                except Exception as exc:
                    pages += 1
                    log.warning(
                        "[%d/%d] FAIL %s (%s)",
                        pages, limits.max_pages, safe_url(item.url), type(exc).__name__,
                    )
                    trace.emit("page_end", page_id=exploration.page_id, url=item.url, status="failed",
                               error=type(exc).__name__, duration_ms=round((time.monotonic() - page_started) * 1000, 1))
                    continue

                pages += 1

                # Analyzer: all judgment happens here
                result = analyze(capture, scope)

                for url in result.frontier_urls:
                    frontier.enqueue(url, depth=item.depth + 1)

                added = 0
                if result.endpoints:
                    with Session(engine) as session:
                        added, _ = save_endpoints(session, scan_id, result.endpoints)
                        total_added += added

                # Diminishing returns: when repeated visits to one template stop
                # producing new endpoints, freeze it so its remaining siblings are
                # not crawled. Unique pages (visited once) never reach the streak.
                tkey = frontier.template_key(item.url)
                if added > 0:
                    zero_streak.pop(tkey, None)
                else:
                    zero_streak[tkey] = zero_streak.get(tkey, 0) + 1
                    if zero_streak[tkey] >= limits.template_saturation:
                        frontier.saturate(tkey)

                api_n = len(result.endpoints)
                link_n = len(result.frontier_urls)
                actions = [row for row in capture.observations if row.get("observation") == "action"]
                page_record = trace.emit(
                    "page_end", page_id=exploration.page_id, url=item.url, final_url=capture.final_url,
                    status="partial" if capture.error else "completed", error=capture.error,
                    duration_ms=round((time.monotonic() - page_started) * 1000, 1),
                    endpoints_observed=api_n, endpoints_new=added, frontier_candidates=link_n, queue=frontier.size,
                    actions=len(actions), actions_failed=sum(row["status"] == "failed" for row in actions),
                    actions_with_http_errors=sum(bool(row.get("http_error_count")) for row in actions),
                )
                err = f" err={capture.error}" if capture.error else ""
                log.info(
                    "[%d/%d] %s -> endpoints_observed=%d endpoints_new=%d links=%d queue=%d duration_ms=%.1f%s",
                    pages, limits.max_pages, safe_url(item.url),
                    api_n, added, link_n, frontier.size, page_record["duration_ms"], err,
                )

            if time.monotonic() >= deadline:
                stop_reason = "max_duration"
            if await _probe_unobserved(context, engine, scan_id, scope, limits, deadline, trace):
                stop_reason = "max_duration"
        finally:
            await _close(browser.close(), "browser close")
    finally:
        await _close(pw.stop(), "Playwright stop")

    return {"stop_reason": stop_reason, "pages": pages, "endpoints": total_added,
            "queue_remaining": frontier.size}


async def _probe_unobserved(context, engine, scan_id: int, scope: Scope, limits, deadline: float,
                            trace: ScanLog) -> bool:
    """Send safe GETs to endpoints never observed live and store the raw outcome.
    Non-fatal; returns whether the scan deadline was reached while probing."""
    started = time.monotonic()
    counts = {"responded": 0, "no_response": 0, "persisted": 0}

    def persist(row: tuple[int, str, int | None]) -> None:
        endpoint_id, status, code = row
        counts[status] += 1
        with Session(engine) as session:
            set_probe_result(session, endpoint_id, status, code)
            session.commit()
            counts["persisted"] += 1

    def elapsed() -> float:
        return round((time.monotonic() - started) * 1000, 1)

    timeout = asyncio.timeout_at(deadline)
    try:
        with Session(engine) as session:
            rows = list_unprobed_endpoints(session, scan_id)
            candidates = [(ep.id, ep.sample_url) for ep in rows if ep.id is not None and ep.method == "GET"]
        async with timeout:
            results = await probe_candidates(context, candidates, scope=scope, limits=limits, on_result=persist)
    except asyncio.CancelledError:
        trace.emit("probe_end", error="cancelled", stop_reason="cancelled", **counts, duration_ms=elapsed())
        raise
    except Exception as exc:
        trace.emit("probe_end", error=type(exc).__name__,
                   stop_reason="deadline" if timeout.expired() else "error", **counts, duration_ms=elapsed())
        log.warning("probe non-fatal: %s", type(exc).__name__)
    else:
        trace.emit("probe_end", sent=results.sent, responded=results.responded, no_response=results.no_response,
                   skipped=results.skipped, remaining=results.remaining, stop_reason=results.stop_reason,
                   persisted=counts["persisted"], duration_ms=elapsed())
        log.info(
            "probe: %d responded, %d no response (total=%d sent=%d skipped=%d remaining=%d stop=%s)",
            results.responded, results.no_response, len(candidates), results.sent, results.skipped,
            results.remaining, results.stop_reason,
        )
    return timeout.expired() or time.monotonic() >= deadline


def _enqueue_wayback_backfill(
    frontier: Frontier,
    seeds: list[str],
    start_idx: int,
) -> int:
    """Admit one archived page only after the live frontier is empty."""
    idx = start_idx
    while frontier.size == 0 and idx < len(seeds):
        seed = seeds[idx]
        idx += 1
        if frontier.enqueue(seed, depth=0):
            log.debug("wayback seed admitted: %s", safe_url(seed))
            break
    return idx
