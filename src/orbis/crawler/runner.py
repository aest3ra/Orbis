"""BFS crawler: visits pages, passes raw captures to Analyzer."""

from __future__ import annotations

import asyncio
import logging
import time

from playwright.async_api import async_playwright
from sqlmodel import Session

from orbis.analysis.analyzer import analyze
from orbis.config import ScanConfig
from orbis.crawler.browser import capture_page
from orbis.crawler.frontier import Frontier
from orbis.crawler.scope import Scope
from orbis.storage.db import open_db
from orbis.storage.repo import (
    collapse_scan_endpoints,
    create_scan,
    finish_scan,
    save_endpoints,
)

log = logging.getLogger("orbis.crawler")


async def run_scan(
    config: ScanConfig,
    *,
    db_path: str,
    headless: bool = True,
    js_analysis: bool = True,
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
    rate_delay = 1.0 / limits.rate_limit_rps
    pages = 0
    total_added = 0
    last_req_at = 0.0
    # Per-template count of consecutive visits that produced no new endpoint.
    zero_streak: dict[tuple[str, str], int] = {}

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx_kw: dict = {}
        if auth_path:
            ctx_kw["storage_state"] = auth_path
        context = await browser.new_context(**ctx_kw)

        while frontier.size > 0 and pages < limits.max_pages:
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
                    js_analysis=js_analysis,
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
