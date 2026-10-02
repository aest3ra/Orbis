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


def test_llm_dispose_failure_still_closes_browser(monkeypatch):
    failure = RuntimeError("synthetic request disposal failure")
    request = SimpleNamespace(dispose=AsyncMock(side_effect=failure))
    browser = SimpleNamespace(new_context=AsyncMock(return_value=object()), close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
        request=SimpleNamespace(new_context=AsyncMock(return_value=request)),
    )
    manager = MagicMock()
    manager.__aenter__ = AsyncMock(return_value=playwright)
    manager.__aexit__ = AsyncMock(return_value=False)
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=manager))
    monkeypatch.setattr(runner, "Session", MagicMock())
    monkeypatch.setattr(runner, "QwenPlanner", Mock(return_value=SimpleNamespace(records=[])))
    monkeypatch.setattr(runner, "list_unverified_endpoints", Mock(return_value=[]))
    monkeypatch.setattr(runner, "probe_candidates", AsyncMock(return_value=ProbeResults()))
    config = ScanConfig(target="http://localhost/", limits=LimitsConfig(max_pages=0))

    with pytest.raises(RuntimeError) as raised:
        asyncio.run(runner._crawl_scan(
            config, Mock(), 17, Scope(config.scope), None, "synthetic-key",
            db_path="unused.db", headless=True, wayback=False, trace=Mock(),
        ))

    assert raised.value is failure
    request.dispose.assert_awaited_once_with()
    browser.close.assert_awaited_once_with()
