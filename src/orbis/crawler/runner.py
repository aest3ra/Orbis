"""BFS crawler: visits pages, passes raw captures to Analyzer."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from playwright.async_api import async_playwright
from sqlalchemy import func
from sqlmodel import Session, select

from orbis.analysis.analyzer import analyze, build_wayback_results
from orbis.config import ScanConfig
from orbis.crawler.browser import capture_page
from orbis.crawler.interactor import ExplorationState
from orbis.crawler.planner import QwenPlanner, load_api_key
from orbis.crawler.frontier import Frontier
from orbis.crawler.wayback import fetch_wayback_urls
from orbis.crawler.probe import probe_candidates
from orbis.crawler.scope import Scope
from orbis.storage.db import Endpoint, Observation, open_db
from orbis.telemetry import ScanLog, clean, safe_url
from orbis.storage.repo import (
    collapse_scan_endpoints,
    create_scan,
    finish_scan,
    list_unverified_endpoints,
    save_endpoints,
    set_probe_result,
)

log = logging.getLogger("orbis.crawler")


@contextmanager
def _database_session(engine) -> Iterator[Session]:
    failure: BaseException | None = None
    try:
        with Session(engine) as session:
            try:
                yield session
            except BaseException as exc:
                failure = exc
                raise
    except Exception as exc:
        if failure is None or exc is failure:
            raise
        failure.add_note(f"Database session close failed: {type(exc).__name__}")
    else:
        return
    # Re-raise outside the cleanup handler to retain the original exception chain.
    raise failure


async def run_scan(
    config: ScanConfig,
    *,
    db_path: str,
    headless: bool = True,
    wayback: bool = True,
) -> int:
    api_key = load_api_key() if config.llm.enabled else None
    for path in config.interaction.upload_files:
        if not path.is_file():
            raise ValueError(f"Upload file does not exist: {path}")
    scope = Scope(config.scope)
    engine = open_db(db_path)
    trace = None
    failure: BaseException | None = None
    try:
        auth_path: str | None = None
        if config.auth.type == "storage_state" and config.auth.storage_state_path:
            auth_path = str(config.auth.storage_state_path)

        with Session(engine) as session:
            scan_id = create_scan(session, config.target, auth_path)

        outcome = {"status": "error", "stop_reason": "exception"}
        stage = "log_creation"
        try:
            trace = ScanLog(db_path, scan_id)
            stage = "scan_start"
            log.info("scan=%d diagnostics=%s", scan_id, trace.path)
            trace.emit("scan_start", target=config.target, limits=config.limits.model_dump(),
                       authenticated=auth_path is not None, llm_enabled=config.llm.enabled, wayback=wayback)
            stage = "crawl"
            summary = await _crawl_scan(config, engine, scan_id, scope, auth_path, api_key,
                                        db_path=db_path, headless=headless, wayback=wayback, trace=trace)
            outcome = {"status": "completed", **summary}
            return scan_id
        except asyncio.CancelledError as exc:
            failure = exc
            outcome = {"status": "cancelled", "stop_reason": "cancelled"}
            if stage != "crawl":
                outcome["failure_stage"] = stage
            raise
        except BaseException as exc:
            failure = exc
            outcome["error"] = type(exc).__name__
            if stage != "crawl":
                outcome["failure_stage"] = stage
            raise
        finally:
            try:
                totals = dict(trace.totals) if trace is not None else {}
                outcome.setdefault("pages", totals.get("pages_crawled", 0))
                metadata_error = None
                try:
                    with _database_session(engine) as session:
                        outcome["endpoints"] = session.exec(
                            select(func.count()).select_from(Endpoint).where(Endpoint.scan_id == scan_id)
                        ).one()
                except Exception as exc:
                    metadata_error = exc
                    outcome.pop("endpoints", None)
                    outcome["metadata_error"] = type(exc).__name__
                    if failure is None:
                        outcome.update(status="error", stop_reason="exception", error=type(exc).__name__)
                terminal_error = metadata_error
                terminal_log_emitted = False
                try:
                    if trace is None:
                        record = {"event": "scan_end", **outcome, "totals": totals}
                    else:
                        record = trace.emit("scan_end", **outcome, totals=totals)
                        terminal_log_emitted = True
                except Exception as exc:
                    if terminal_error is None:
                        terminal_error = exc
                    else:
                        preserved = failure if failure is not None else terminal_error
                        preserved.add_note(f"Terminal log write failed: {type(exc).__name__}")
                    if failure is None:
                        outcome.update(status="error", stop_reason="exception", error=type(terminal_error).__name__)
                    # A failed write/flush cannot supply a confirmed JSONL envelope.
                    record = {"event": "scan_end", **outcome, "totals": totals,
                              "log_write_error": type(exc).__name__}
                try:
                    with _database_session(engine) as session:
                        session.add(Observation(scan_id=scan_id, page_url=safe_url(config.target), kind="scan",
                                                details_json=json.dumps(clean(record))))
                        if metadata_error is None:
                            finish_scan(session, scan_id, outcome["pages"], outcome["endpoints"])
                        else:
                            session.commit()
                except Exception as exc:
                    if terminal_error is None:
                        terminal_error = exc
                    else:
                        preserved = failure if failure is not None else terminal_error
                        preserved.add_note(f"Terminal database persistence failed: {type(exc).__name__}")
                    if terminal_log_emitted:
                        try:
                            trace.emit("scan_finalization_error", stage="database", error=type(exc).__name__)
                        except Exception as diagnostic_error:
                            preserved = failure if failure is not None else terminal_error
                            preserved.add_note(f"Finalization error logging failed: {type(diagnostic_error).__name__}")
                if terminal_error is not None:
                    raise terminal_error
                if trace is not None:
                    log.info("scan=%d status=%s stop=%s elapsed_ms=%.1f", scan_id, outcome["status"],
                             outcome["stop_reason"], record["elapsed_ms"])
            except Exception as exc:
                if failure is None:
                    raise
                failure.add_note(f"Terminal diagnostics failed: {type(exc).__name__}")
    except BaseException as exc:
        failure = exc
        raise
    finally:
        try:
            if trace is not None:
                trace.close()
        except BaseException as exc:
            if failure is None or not isinstance(exc, Exception):
                failure = exc
                raise
            failure.add_note(f"Scan log close failed: {type(exc).__name__}")
        finally:
            try:
                engine.dispose()
            except Exception as exc:
                if failure is None:
                    raise
                failure.add_note(f"Database disposal failed: {type(exc).__name__}")


async def _crawl_scan(
    config: ScanConfig, engine, scan_id: int, scope: Scope, auth_path: str | None, api_key: str | None,
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

    # Wayback layer: pull archived URLs and record API-marked ones as unverified
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
    driver_failure: BaseException | None = None
    try:
        browser = await pw.chromium.launch(headless=headless)
        llm_request = None
        probe_cleanup_observations: list[dict[str, int | str]] = []
        failure: BaseException | None = None
        try:
            ctx_kw: dict = {}
            if auth_path:
                ctx_kw["storage_state"] = auth_path
            context = await browser.new_context(**ctx_kw)
            planner = None
            if api_key:
                llm_request = await pw.request.new_context(extra_http_headers={"Authorization": f"Bearer {api_key}"})
                planner = QwenPlanner(llm_request, config.llm, emit=exploration.event)

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
                        planner=planner,
                        max_actions=limits.max_actions_per_page,
                        download_dir=Path(db_path).resolve().parent / "downloads" / str(scan_id),
                        deadline=deadline,
                    )
                except asyncio.CancelledError as exc:
                    try:
                        trace.emit("page_end", page_id=exploration.page_id, url=item.url, status="interrupted",
                                   error="cancelled", duration_ms=round((time.monotonic() - page_started) * 1000, 1))
                    except Exception as diagnostic_error:
                        exc.add_note(f"Page cancellation diagnostics failed: {type(diagnostic_error).__name__}")
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
                with Session(engine) as session:
                    for record in capture.observations:
                        session.add(Observation(scan_id=scan_id, page_url=safe_url(capture.page_url),
                                                kind=record.get("observation", record["kind"]),
                                                details_json=json.dumps(clean(record))))
                    session.commit()

                for url in result.frontier_urls:
                    frontier.enqueue(url, depth=item.depth + 1)

                added = 0
                if result.endpoints:
                    with _database_session(engine) as session:
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
                with Session(engine) as session:
                    session.add(Observation(scan_id=scan_id, page_url=safe_url(item.url), kind="page",
                                            details_json=json.dumps(page_record)))
                    session.commit()
                err = f" err={capture.error}" if capture.error else ""
                log.info(
                    "[%d/%d] %s -> endpoints_observed=%d endpoints_new=%d links=%d queue=%d duration_ms=%.1f%s",
                    pages, limits.max_pages, safe_url(item.url),
                    api_n, added, link_n, frontier.size, page_record["duration_ms"], err,
                )

            if time.monotonic() >= deadline:
                stop_reason = "max_duration"
            probe_started = time.monotonic()
            probe_timeout = asyncio.timeout_at(deadline)
            probe_counts = {"verified": 0, "failed": 0, "persisted": 0}

            def persist_probe_result(row: tuple[int, str, int | None]) -> None:
                endpoint_id, status, code = row
                probe_counts[status] += 1
                with Session(engine) as session:
                    set_probe_result(session, endpoint_id, status, code)
                    session.commit()
                    probe_counts["persisted"] += 1

            try:
                with Session(engine) as session:
                    rows = list_unverified_endpoints(session, scan_id)
                    candidates = [(ep.id, ep.sample_url) for ep in rows if ep.id is not None and ep.method == "GET"]

                async with probe_timeout:
                    results = await probe_candidates(
                        context, candidates, scope=scope, limits=limits,
                        on_result=persist_probe_result,
                        cleanup_observations=probe_cleanup_observations,
                    )

            except asyncio.CancelledError as exc:
                try:
                    trace.emit("probe_end", error="cancelled", stop_reason="cancelled", **probe_counts,
                               duration_ms=round((time.monotonic() - probe_started) * 1000, 1))
                except Exception as diagnostic_error:
                    exc.add_note(f"Probe diagnostics failed: {type(diagnostic_error).__name__}")
                raise
            except Exception as exc:
                diagnostics_failed = False
                try:
                    trace.emit("probe_end", error=type(exc).__name__,
                               stop_reason="deadline" if probe_timeout.expired() else "error", **probe_counts,
                               duration_ms=round((time.monotonic() - probe_started) * 1000, 1))
                except Exception as diagnostic_error:
                    exc.add_note(f"Probe diagnostics failed: {type(diagnostic_error).__name__}")
                    diagnostics_failed = True
                if diagnostics_failed:
                    raise
                log.warning("probe non-fatal: %s", type(exc).__name__)
            else:
                trace.emit("probe_end", sent=results.sent, verified=results.verified, failed=results.failed,
                           skipped=results.skipped, remaining=results.remaining, stop_reason=results.stop_reason,
                           persisted=probe_counts["persisted"],
                           duration_ms=round((time.monotonic() - probe_started) * 1000, 1))
                log.info(
                    "probe: %d verified, %d failed "
                    "(total=%d sent=%d skipped=%d remaining=%d stop=%s)",
                    results.verified,
                    results.failed,
                    len(candidates),
                    results.sent,
                    results.skipped,
                    results.remaining,
                    results.stop_reason,
                )
            finally:
                if probe_timeout.expired() or time.monotonic() >= deadline:
                    stop_reason = "max_duration"

            if planner is not None:
                with Session(engine) as session:
                    for record in planner.records:
                        session.add(Observation(scan_id=scan_id, page_url=safe_url(config.target), kind="llm",
                                                details_json=json.dumps(clean(record))))
                    session.commit()
        except BaseException as exc:
            failure = exc
            raise
        finally:
            try:
                if probe_cleanup_observations:
                    with Session(engine) as session:
                        for record in probe_cleanup_observations:
                            endpoint = session.get(Endpoint, record["endpoint_id"])
                            if endpoint is not None and endpoint.scan_id == scan_id:
                                record.update(method=endpoint.method, host=endpoint.host,
                                              path_template=endpoint.path_template)
                            session.add(Observation(scan_id=scan_id, page_url="", kind="probe_response_cleanup",
                                                    details_json=json.dumps(record)))
                        session.commit()
            except BaseException as exc:
                if failure is None or not isinstance(exc, Exception):
                    failure = exc
                    raise
                failure.add_note(f"Probe cleanup observation persistence failed: {type(exc).__name__}")
            finally:
                try:
                    if llm_request is not None:
                        await llm_request.dispose()
                except BaseException as exc:
                    if failure is None or not isinstance(exc, Exception):
                        failure = exc
                        raise
                    failure.add_note(f"Request context disposal failed: {type(exc).__name__}")
                finally:
                    try:
                        await browser.close()
                    except Exception as exc:
                        if failure is None:
                            raise
                        failure.add_note(f"Browser close failed: {type(exc).__name__}")
    except BaseException as exc:
        driver_failure = exc
        raise
    finally:
        try:
            await pw.stop()
        except Exception as exc:
            if driver_failure is None:
                raise
            driver_failure.add_note(f"Playwright stop failed: {type(exc).__name__}")

    with Session(engine) as session:
        # Same cardinality bar as the frontier: only genuinely high-cardinality
        # sibling sets collapse, so a handful of distinct API resources
        # (/api/forum/{communities,questions,reviews}) are left intact.
        collapsed = collapse_scan_endpoints(
            session, scan_id, threshold=limits.slug_threshold,
        )

    return {"stop_reason": stop_reason, "pages": pages, "endpoints": total_added - collapsed,
            "endpoints_merged": collapsed, "queue_remaining": frontier.size}


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
