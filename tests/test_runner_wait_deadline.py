import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from orbis.config import LimitsConfig, ScanConfig
from orbis.crawler import runner
from orbis.crawler.probe import ProbeResults
from orbis.crawler.scope import Scope


@pytest.fixture
def crawl_resources(monkeypatch):
    browser = SimpleNamespace(new_context=AsyncMock(return_value=object()), close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
        stop=AsyncMock(),
    )
    trace = SimpleNamespace(emit=Mock(side_effect=lambda kind, **fields: fields))
    capture = AsyncMock(side_effect=lambda context, url, **kwargs: SimpleNamespace(
        page_url=url, final_url=url, observations=[], error=None,
    ))
    probe = AsyncMock(return_value=ProbeResults())
    rows = Mock(return_value=[])
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=SimpleNamespace(
        start=AsyncMock(return_value=playwright),
    )))
    monkeypatch.setattr(runner, "Session", MagicMock())
    monkeypatch.setattr(runner, "capture_page", capture)
    monkeypatch.setattr(runner, "analyze", Mock(side_effect=[
        SimpleNamespace(frontier_urls=["http://localhost/next"], endpoints=[]),
        SimpleNamespace(frontier_urls=[], endpoints=[]),
    ]))
    monkeypatch.setattr(runner, "list_unverified_endpoints", rows)
    monkeypatch.setattr(runner, "probe_candidates", probe)
    monkeypatch.setattr(runner, "collapse_scan_endpoints", Mock(return_value=0))
    return SimpleNamespace(browser=browser, playwright=playwright, trace=trace,
                           capture=capture, probe=probe, rows=rows)


def config_for(*, pages=2, duration=1, rate=0):
    config = ScanConfig(target="http://localhost/", limits=LimitsConfig(
        max_pages=pages, max_duration_sec=1, rate_limit_rps=rate,
    ))
    config.limits.max_duration_sec = duration
    return config


async def crawl(config, resources):
    return await runner._crawl_scan(
        config, Mock(), 17, Scope(config.scope), None, None,
        db_path="unused.db", headless=True, wayback=False, trace=resources.trace,
    )


def events(resources, kind):
    return [call.kwargs for call in resources.trace.emit.call_args_list if call.args[0] == kind]


def test_rate_wait_stops_at_deadline_without_starting_next_page(crawl_resources):
    config = config_for(duration=0.03, rate=0.1)

    async def exercise():
        return await asyncio.wait_for(crawl(config, crawl_resources), timeout=0.3)

    result = asyncio.run(exercise())

    assert result["stop_reason"] == "max_duration"
    assert result["pages"] == 1
    assert result["queue_remaining"] == 1
    assert crawl_resources.capture.await_count == 1
    assert len(events(crawl_resources, "page_start")) == 1
    assert len(events(crawl_resources, "page_end")) == 1
    crawl_resources.browser.close.assert_awaited_once_with()
    crawl_resources.playwright.stop.assert_awaited_once_with()


def test_rate_wait_within_budget_preserves_normal_page_processing(crawl_resources):
    result = asyncio.run(crawl(config_for(rate=100), crawl_resources))

    assert result["stop_reason"] == "max_pages"
    assert result["pages"] == 2
    assert result["queue_remaining"] == 0
    assert crawl_resources.capture.await_count == 2
    assert len(events(crawl_resources, "page_start")) == 2


def test_expired_budget_does_not_start_a_page(crawl_resources):
    result = asyncio.run(crawl(config_for(duration=0), crawl_resources))

    assert result["stop_reason"] == "max_duration"
    assert result["pages"] == 0
    assert result["queue_remaining"] == 1
    crawl_resources.capture.assert_not_awaited()
    assert events(crawl_resources, "page_start") == []


def test_external_cancellation_during_rate_wait_propagates(crawl_resources):
    async def exercise():
        first_page_done = asyncio.Event()

        def record(kind, **fields):
            if kind == "page_end":
                first_page_done.set()
            return fields

        crawl_resources.trace.emit.side_effect = record
        task = asyncio.create_task(crawl(config_for(duration=60, rate=0.1), crawl_resources))
        await asyncio.wait_for(first_page_done.wait(), timeout=0.3)
        task.cancel("external cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, timeout=0.3)
        assert raised.value.args == ("external cancellation",)

    asyncio.run(exercise())

    assert len(events(crawl_resources, "page_start")) == 1
    assert len(events(crawl_resources, "page_end")) == 1
    crawl_resources.browser.close.assert_awaited_once_with()
    crawl_resources.playwright.stop.assert_awaited_once_with()


def test_probe_deadline_updates_scan_stop_reason(crawl_resources):
    async def blocked(*args, **kwargs):
        await asyncio.Event().wait()

    crawl_resources.probe.side_effect = blocked

    async def exercise():
        return await asyncio.wait_for(crawl(config_for(pages=0, duration=0.03), crawl_resources), timeout=0.3)

    result = asyncio.run(exercise())

    assert result["stop_reason"] == "max_duration"
    assert result["pages"] == 0
    assert events(crawl_resources, "probe_end")[0]["stop_reason"] == "deadline"
    crawl_resources.browser.close.assert_awaited_once_with()
    crawl_resources.playwright.stop.assert_awaited_once_with()


def test_synchronous_probe_preparation_overrun_updates_scan_stop_reason(crawl_resources):
    def slow_listing(*args):
        time.sleep(0.05)
        return []

    crawl_resources.rows.side_effect = slow_listing

    result = asyncio.run(crawl(config_for(pages=0, duration=0.03), crawl_resources))

    assert result["stop_reason"] == "max_duration"
    assert result["pages"] == 0
    assert events(crawl_resources, "probe_end")[0]["stop_reason"] == "completed"


@pytest.mark.parametrize("stage", ["listing", "probe"])
def test_internal_timeout_is_not_a_scan_deadline(crawl_resources, stage):
    failing = crawl_resources.rows if stage == "listing" else crawl_resources.probe
    failing.side_effect = TimeoutError("internal operation failed")

    result = asyncio.run(crawl(config_for(pages=0), crawl_resources))

    assert result["stop_reason"] == "max_pages"
    assert events(crawl_resources, "probe_end")[0]["stop_reason"] == "error"


def test_normal_probe_preserves_scan_stop_reason(crawl_resources):
    result = asyncio.run(crawl(config_for(pages=0), crawl_resources))

    assert result["stop_reason"] == "max_pages"
    assert events(crawl_resources, "probe_end")[0]["stop_reason"] == "completed"


def test_external_cancellation_during_probe_propagates(crawl_resources):
    async def exercise():
        entered = asyncio.Event()

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        crawl_resources.probe.side_effect = blocked
        task = asyncio.create_task(crawl(config_for(pages=0, duration=60), crawl_resources))
        await asyncio.wait_for(entered.wait(), timeout=0.3)
        task.cancel("external cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await asyncio.wait_for(task, timeout=0.3)
        assert raised.value.args == ("external cancellation",)

    asyncio.run(exercise())

    assert events(crawl_resources, "probe_end") == []
    crawl_resources.browser.close.assert_awaited_once_with()
    crawl_resources.playwright.stop.assert_awaited_once_with()
