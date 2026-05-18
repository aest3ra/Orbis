"""BFS crawler: visits pages, passes raw captures to Analyzer."""

from __future__ import annotations

import asyncio
import time

from playwright.async_api import async_playwright
from sqlmodel import Session

from orbis.analysis.analyzer import analyze
from orbis.config import ScanConfig
from orbis.crawler.browser import capture_page
from orbis.crawler.frontier import Frontier
from orbis.crawler.scope import Scope
from orbis.storage.db import open_db
from orbis.storage.repo import create_scan, finish_scan, save_endpoints


async def run_scan(
    config: ScanConfig,
    *,
    db_path: str,
    headless: bool = True,
) -> int:
    scope = Scope(config.scope)
    engine = open_db(db_path)

    auth_path: str | None = None
    if config.auth.type == "storage_state" and config.auth.storage_state_path:
        auth_path = str(config.auth.storage_state_path)

    with Session(engine) as session:
        scan_id = create_scan(session, config.target, auth_path)

    frontier = Frontier(scope, config.limits.max_visits_per_template)
    frontier.enqueue(config.target)

    limits = config.limits
    deadline = time.monotonic() + limits.max_duration_sec
    rate_delay = 1.0 / limits.rate_limit_rps
    pages = 0
    total_added = 0
    last_req_at = 0.0

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        ctx_kw: dict = {}
        if auth_path:
            ctx_kw["storage_state"] = auth_path
        context = await browser.new_context(**ctx_kw)

        while frontier.size > 0 and pages < limits.max_pages:
            if time.monotonic() > deadline:
                print(f"  [timeout] {limits.max_duration_sec}s limit reached")
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
                print(f"  [{pages}/{limits.max_pages}] FAIL {item.url} ({type(exc).__name__})")
                continue

            pages += 1

            # Analyzer: all judgment happens here
            result = analyze(capture, scope)

            for url in result.frontier_urls:
                frontier.enqueue(url, depth=item.depth + 1)

            if result.endpoints:
                with Session(engine) as session:
                    added, _ = save_endpoints(session, scan_id, result.endpoints)
                    total_added += added

            api_n = len(result.endpoints)
            link_n = len(result.frontier_urls)
            err = f" err={capture.error}" if capture.error else ""
            print(
                f"  [{pages}/{limits.max_pages}] {item.url}"
                f" -> api={api_n} links={link_n} queue={frontier.size}{err}"
            )

        await browser.close()

    with Session(engine) as session:
        finish_scan(session, scan_id, pages, total_added)

    return scan_id
