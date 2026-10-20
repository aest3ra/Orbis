import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest

from orbis.config import LimitsConfig, ScanConfig
from orbis.crawler import runner
from orbis.crawler.probe import ProbeResults
from orbis.crawler.scope import Scope


@pytest.fixture
def scan_resources(monkeypatch):
    engine = Mock()
    trace = Mock(path="unused.jsonl", totals={})
    trace.emit.return_value = {"elapsed_ms": 0}
    crawl = AsyncMock(return_value={"stop_reason": "frontier_exhausted"})
    monkeypatch.setattr(runner, "open_db", Mock(return_value=engine))
    monkeypatch.setattr(runner, "Session", MagicMock())
    runner.Session.return_value.__enter__.return_value.exec.return_value.one.return_value = 0
    monkeypatch.setattr(runner, "create_scan", Mock(return_value=17))
    monkeypatch.setattr(runner, "ScanLog", Mock(return_value=trace))
    monkeypatch.setattr(runner, "_crawl_scan", crawl)
    return engine, trace, crawl


@pytest.mark.parametrize("stage", ["construction", "first_write"])
def test_scan_log_startup_failure_releases_created_resources(scan_resources, stage):
    engine, trace, crawl = scan_resources
    failure = OSError("synthetic log startup failure")
    if stage == "construction":
        runner.ScanLog.side_effect = failure
    else:
        trace.emit.side_effect = failure

    with pytest.raises(OSError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path="unused.db", wayback=False))

    assert raised.value is failure
    crawl.assert_not_awaited()
    engine.dispose.assert_called_once_with()
    if stage == "first_write":
        trace.close.assert_called_once_with()
    else:
        trace.close.assert_not_called()


def test_scan_log_close_failure_still_disposes_database(scan_resources):
    engine, trace, crawl = scan_resources
    failure = OSError("synthetic log close failure")
    trace.close.side_effect = failure

    with pytest.raises(OSError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path="unused.db", wayback=False))

    assert raised.value is failure
    crawl.assert_awaited_once()
    trace.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()
    assert trace.emit.call_args_list[-1].kwargs["status"] == "completed"


@pytest.mark.parametrize("failure", [None, RuntimeError("synthetic crawl failure"), asyncio.CancelledError()])
def test_scan_cleanup_preserves_result_and_crawl_failure(scan_resources, failure):
    engine, trace, crawl = scan_resources
    crawl.side_effect = failure
    scan = runner.run_scan(ScanConfig(target="http://localhost/"), db_path="unused.db", wayback=False)
    if failure is None:
        assert asyncio.run(scan) == 17
        status = "completed"
    else:
        with pytest.raises(type(failure)) as raised:
            asyncio.run(scan)
        assert raised.value is failure
        status = "cancelled" if isinstance(failure, asyncio.CancelledError) else "error"

    trace.close.assert_called_once_with()
    engine.dispose.assert_called_once_with()
    assert trace.emit.call_args_list[-1].kwargs["status"] == status


@pytest.fixture
def crawl_resources(monkeypatch):
    request = SimpleNamespace(dispose=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=object()), close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
        request=SimpleNamespace(new_context=AsyncMock(return_value=request)),
        stop=AsyncMock(),
    )
    manager = SimpleNamespace(start=AsyncMock(return_value=playwright))
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=manager))
    monkeypatch.setattr(runner, "Session", MagicMock())
    monkeypatch.setattr(runner, "QwenPlanner", Mock(return_value=SimpleNamespace(records=[])))
    monkeypatch.setattr(runner, "list_unverified_endpoints", Mock(return_value=[]))
    monkeypatch.setattr(runner, "probe_candidates", AsyncMock(return_value=ProbeResults()))
    return request, browser


@pytest.mark.parametrize("failure_type", [None, RuntimeError])
def test_fresh_cancellation_during_driver_stop_propagates(crawl_resources, failure_type):
    request, browser = crawl_resources
    playwright = runner.async_playwright.return_value.start.return_value
    trace = Mock(emit=Mock(side_effect=failure_type("primary failure") if failure_type else None))
    config = ScanConfig(target="http://localhost/", limits=LimitsConfig(max_pages=1 if failure_type else 0))

    async def exercise():
        entered = asyncio.Event()

        async def blocked():
            entered.set()
            await asyncio.Event().wait()

        playwright.stop.side_effect = blocked
        task = asyncio.create_task(runner._crawl_scan(
            config, Mock(), 17, Scope(config.scope), None, None,
            db_path="unused.db", headless=True, wayback=False, trace=trace,
        ))
        await asyncio.wait_for(entered.wait(), timeout=0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=0.5)

    asyncio.run(exercise())
    playwright.stop.assert_awaited_once_with()
    browser.close.assert_awaited_once_with()


@pytest.mark.parametrize("stop_fails", [False, True])
def test_driver_stop_runs_once_after_normal_body(crawl_resources, monkeypatch, caplog, stop_fails):
    """Driver teardown runs exactly once; a failing stop is logged and does not
    turn a completed crawl into an error."""
    request, browser = crawl_resources
    playwright = runner.async_playwright.return_value.start.return_value
    if stop_fails:
        playwright.stop.side_effect = OSError("driver stop failure")
    monkeypatch.setattr(runner, "collapse_scan_endpoints", Mock(return_value=0))
    config = ScanConfig(target="http://localhost/", limits=LimitsConfig(max_pages=0))
    scan = runner._crawl_scan(
        config, Mock(), 17, Scope(config.scope), None, None,
        db_path="unused.db", headless=True, wayback=False, trace=Mock(),
    )

    assert asyncio.run(scan)["stop_reason"] == "max_pages"

    runner.collapse_scan_endpoints.assert_called_once()
    playwright.stop.assert_awaited_once_with()
    browser.close.assert_awaited_once_with()
    request.dispose.assert_not_awaited()
    assert ("Playwright stop failed" in caplog.text) == stop_fails


def test_llm_dispose_failure_still_closes_browser(crawl_resources, monkeypatch, caplog):
    request, browser = crawl_resources
    request.dispose.side_effect = RuntimeError("synthetic request disposal failure")
    monkeypatch.setattr(runner, "collapse_scan_endpoints", Mock(return_value=0))
    config = ScanConfig(target="http://localhost/", limits=LimitsConfig(max_pages=0))

    summary = asyncio.run(runner._crawl_scan(
        config, Mock(), 17, Scope(config.scope), None, "synthetic-key",
        db_path="unused.db", headless=True, wayback=False, trace=Mock(),
    ))

    assert summary["stop_reason"] == "max_pages"
    assert "LLM request context disposal failed" in caplog.text
    request.dispose.assert_awaited_once_with()
    browser.close.assert_awaited_once_with()
