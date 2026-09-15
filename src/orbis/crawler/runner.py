"""BFS crawler: visits pages, passes raw captures to Analyzer."""

from __future__ import annotations

import asyncio
import logging
import time

from playwright.async_api import async_playwright
from sqlmodel import Session

from orbis.analysis.analyzer import analyze, build_wayback_results
from orbis.config import ScanConfig
from orbis.crawler.browser import capture_page
from orbis.crawler.frontier import Frontier
from orbis.crawler.wayback import fetch_wayback_urls
from orbis.crawler.probe import probe_candidates
from orbis.crawler.scope import Scope
from orbis.storage.db import open_db
from orbis.storage.repo import (
    collapse_scan_endpoints,
    create_scan,
    finish_scan,
    list_unverified_endpoints,
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
    scope = Scope(config.scope)
    engine = open_db(db_path)

    auth_path: str | None = None
    if config.auth.type == "storage_state" and config.auth.storage_state_path:
        auth_path = str(config.auth.storage_state_path)

    with Session(engine) as session:
        scan_id = create_scan(session, config.target, auth_path)

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
            urls = fetch_wayback_urls(host, limit=limits.wayback_max_urls)
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

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx_kw: dict = {}
        if auth_path:
            ctx_kw["storage_state"] = auth_path
        context = await browser.new_context(**ctx_kw)

        while pages < limits.max_pages:
            wayback_seed_idx = _enqueue_wayback_backfill(
                frontier, wayback_seeds, wayback_seed_idx,
            )
            if frontier.size == 0:
                break

            if time.monotonic() > deadline:
                log.warning("timeout: %ds limit reached", limits.max_duration_sec)
                break

            item = frontier.pop()
            if item is None:
                break

            wait = (last_req_at + rate_delay) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            last_req_at = time.monotonic()

            try:
                capture = await capture_page(
                    context,
                    item.url,
                    scope=scope,
                    max_scrolls=limits.max_scrolls_per_page,
                )
            except Exception as exc:
                pages += 1
                log.warning(
                    "[%d/%d] FAIL %s (%s)",
                    pages, limits.max_pages, item.url, type(exc).__name__,
                )
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
            err = f" err={capture.error}" if capture.error else ""
            log.info(
                "[%d/%d] %s -> api=%d links=%d queue=%d%s",
                pages, limits.max_pages, item.url,
                api_n, link_n, frontier.size, err,
            )

        try:
            with Session(engine) as session:
                rows = list_unverified_endpoints(session, scan_id)
                candidates = [(ep.id, ep.sample_url) for ep in rows if ep.id is not None]

            results = await probe_candidates(
                context,
                candidates,
                scope=scope,
                limits=limits,
            )

            if results:
                with Session(engine) as session:
                    for endpoint_id, status, code in results:
                        set_probe_result(session, endpoint_id, status, code)
                    session.commit()

            log.info(
                "probe: %d verified, %d failed "
                "(total=%d sent=%d skipped=%d remaining=%d stop=%s)",
                getattr(results, "verified", 0),
                getattr(results, "failed", 0),
                len(candidates),
                getattr(results, "sent", len(results)),
                getattr(results, "skipped", 0),
                getattr(results, "remaining", 0),
                getattr(results, "stop_reason", "completed"),
            )
        except Exception as exc:
            log.warning("probe non-fatal: %s", type(exc).__name__)

        await browser.close()

    with Session(engine) as session:
        # Same cardinality bar as the frontier: only genuinely high-cardinality
        # sibling sets collapse, so a handful of distinct API resources
        # (/api/forum/{communities,questions,reviews}) are left intact.
        collapsed = collapse_scan_endpoints(
            session, scan_id, threshold=limits.slug_threshold,
        )
        finish_scan(session, scan_id, pages, total_added - collapsed)

    return scan_id


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
            log.debug("wayback seed admitted: %s", seed)
            break
    return idx
