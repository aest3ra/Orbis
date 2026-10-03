import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.crawler.probe import probe_candidates
from orbis.crawler.scope import Scope
from orbis.storage.db import Endpoint, open_db
from orbis.storage.repo import create_scan


class FakeRequest:
    def __init__(self):
        self.outcomes = [200, None]
        self.calls = []
        self.responses = []
        self.blocked = asyncio.Event()

    async def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if outcome is None:
            self.blocked.set()
            await asyncio.Event().wait()
        response = SimpleNamespace(status=outcome, dispose=AsyncMock())
        self.responses.append(response)
        return response


@pytest.fixture
def probe_scan(tmp_path, monkeypatch):
    db_path = str(tmp_path / "scan.db")
    engine = open_db(db_path)
    with Session(engine) as session:
        scan_id = create_scan(session, "http://localhost/")
        for name in ("alpha", "beta"):
            session.add(Endpoint(
                scan_id=scan_id, method="GET", host="localhost", path_template=f"/api/{name}",
                sample_url=f"http://localhost/api/{name}", route_kind="api",
                source="static_js", probe_status="unverified",
            ))
        session.commit()
    request = FakeRequest()
    context = SimpleNamespace(request=request)
    browser = SimpleNamespace(new_context=AsyncMock(return_value=context), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
                                 stop=AsyncMock())
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=SimpleNamespace(
        start=AsyncMock(return_value=playwright),
    )))
    config = ScanConfig(target="http://localhost/")
    config.limits.max_pages = 0
    config.limits.max_duration_sec = 5
    config.limits.rate_limit_rps = 0
    trace = SimpleNamespace(emit=Mock(side_effect=lambda event, **fields: fields))
    yield SimpleNamespace(engine=engine, scan_id=scan_id, db_path=db_path, config=config,
                          context=context, request=request, browser=browser,
                          playwright=playwright, trace=trace)
    engine.dispose()


async def crawl(resources):
    return await runner._crawl_scan(
        resources.config, resources.engine, resources.scan_id, Scope(resources.config.scope), None, None,
        db_path=resources.db_path, headless=True, wayback=False, trace=resources.trace,
    )


def stored_results(resources):
    with Session(resources.engine) as session:
        rows = session.exec(select(Endpoint).order_by(Endpoint.id)).all()
        return [(row.probe_status, row.probe_code) for row in rows]


def probe_events(resources):
    return [call.kwargs for call in resources.trace.emit.call_args_list if call.args[0] == "probe_end"]


@pytest.mark.parametrize("mode", ["deadline", "cancelled"])
@pytest.mark.parametrize("status,classification", [(200, "verified"), (404, "failed")])
def test_completed_result_survives_later_interruption(probe_scan, mode, status, classification):
    resources = probe_scan
    resources.request.outcomes[0] = status
    if mode == "deadline":
        resources.config.limits.max_duration_sec = 0.08

    async def exercise():
        task = asyncio.create_task(crawl(resources))
        await asyncio.wait_for(resources.request.blocked.wait(), timeout=1)
        assert stored_results(resources) == [(classification, status), ("unverified", None)]
        if mode == "cancelled":
            task.cancel("external cancellation")
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            assert raised.value.args == ("external cancellation",)
        else:
            result = await asyncio.wait_for(task, timeout=1)
            assert result["stop_reason"] == "max_duration"

    asyncio.run(exercise())

    assert stored_results(resources) == [(classification, status), ("unverified", None)]
    assert len(resources.request.calls) == 2
    resources.request.responses[0].dispose.assert_awaited_once_with()
    event, = probe_events(resources)
    assert event["stop_reason"] == mode
    assert event["verified"] == (classification == "verified")
    assert event["failed"] == (classification == "failed")
    assert event["persisted"] == 1
    assert "sent" not in event
    assert "remaining" not in event
    resources.browser.close.assert_awaited_once_with()
    resources.playwright.stop.assert_awaited_once_with()


def test_normal_results_are_stored_once(probe_scan, monkeypatch):
    resources = probe_scan
    resources.request.outcomes = [200, 404]
    save = Mock(wraps=runner.set_probe_result)
    monkeypatch.setattr(runner, "set_probe_result", save)

    asyncio.run(crawl(resources))

    assert stored_results(resources) == [("verified", 200), ("failed", 404)]
    assert save.call_count == 2
    event, = probe_events(resources)
    assert event["stop_reason"] == "completed"
    assert event["sent"] == 2
    assert event["verified"] == event["failed"] == 1
    assert event["persisted"] == 2


def test_callback_receives_existing_results_without_changing_return(probe_scan):
    resources = probe_scan
    resources.request.outcomes = [200, 404]
    observed = []

    results = asyncio.run(probe_candidates(
        resources.context, [(1, "http://localhost/api/alpha"), (2, "http://localhost/api/beta")],
        scope=Scope(resources.config.scope), limits=resources.config.limits, on_result=observed.append,
    ))

    assert observed == results == [(1, "verified", 200), (2, "failed", 404)]
    assert results.sent == 2


@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
def test_callback_error_propagates_without_another_request(probe_scan, failure_type):
    resources = probe_scan
    failure = failure_type("private callback failure")
    callback = Mock(side_effect=failure)

    with pytest.raises(failure_type) as raised:
        asyncio.run(probe_candidates(
            resources.context, [(1, "http://localhost/api/alpha"), (2, "http://localhost/api/beta")],
            scope=Scope(resources.config.scope), limits=resources.config.limits, on_result=callback,
        ))

    assert raised.value is failure
    callback.assert_called_once_with((1, "verified", 200))
    assert len(resources.request.calls) == 1


@pytest.mark.parametrize("logging_fails", [False, True])
@pytest.mark.parametrize("stage", ["update", "commit"])
def test_database_error_keeps_earlier_commits_and_original_diagnostic(probe_scan, monkeypatch, logging_fails, stage):
    resources = probe_scan
    resources.request.outcomes = [200, 200]
    failure = OSError("private database failure")
    if stage == "update":
        original_save = runner.set_probe_result

        def fail_second(session, endpoint_id, status, code):
            if endpoint_id == 2:
                raise failure
            original_save(session, endpoint_id, status, code)

        monkeypatch.setattr(runner, "set_probe_result", fail_second)
    else:
        original_commit = Session.commit
        commits = 0

        def fail_second_commit(session):
            nonlocal commits
            commits += 1
            if commits == 2:
                raise failure
            original_commit(session)

        monkeypatch.setattr(Session, "commit", fail_second_commit)
    if logging_fails:
        resources.trace.emit.side_effect = RuntimeError("private diagnostic failure")
        with pytest.raises(OSError) as raised:
            asyncio.run(crawl(resources))
        assert raised.value is failure
        assert failure.__notes__ == ["Probe diagnostics failed: RuntimeError"]
    else:
        asyncio.run(crawl(resources))

    assert stored_results(resources) == [("verified", 200), ("unverified", None)]
    event, = probe_events(resources)
    assert event["stop_reason"] == "error"
    assert event["error"] == "OSError"
    assert event["verified"] == 2
    assert event["persisted"] == 1
    assert "sent" not in event


def test_cancellation_survives_probe_diagnostic_failure(probe_scan):
    resources = probe_scan
    resources.trace.emit.side_effect = OSError("private diagnostic failure")

    async def exercise():
        task = asyncio.create_task(crawl(resources))
        await asyncio.wait_for(resources.request.blocked.wait(), timeout=1)
        task.cancel("external cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert raised.value.args == ("external cancellation",)
        assert raised.value.__notes__ == ["Probe diagnostics failed: OSError"]

    asyncio.run(exercise())

    assert stored_results(resources) == [("verified", 200), ("unverified", None)]
    event, = probe_events(resources)
    assert event["stop_reason"] == "cancelled"
    assert event["persisted"] == 1
    resources.browser.close.assert_awaited_once_with()
    resources.playwright.stop.assert_awaited_once_with()


class DiagnosticInterrupt(BaseException):
    pass


@pytest.mark.parametrize("failure_type", [OSError, TimeoutError, asyncio.CancelledError])
@pytest.mark.parametrize("diagnostic_type", [None, OSError, asyncio.CancelledError, DiagnosticInterrupt])
def test_probe_diagnostics_preserve_original_error_chain(probe_scan, monkeypatch, failure_type, diagnostic_type):
    resources = probe_scan
    failure = failure_type("private probe failure")
    failure.__cause__ = ValueError("private original cause")
    failure.__context__ = LookupError("private original context")
    original_chain = (failure.__cause__, failure.__context__, failure.__suppress_context__)
    monkeypatch.setattr(runner, "set_probe_result", Mock(side_effect=failure))
    diagnostic_error = diagnostic_type("private diagnostic failure") if diagnostic_type else None
    if diagnostic_error is not None:
        resources.trace.emit.side_effect = diagnostic_error

    expected_error = failure if diagnostic_error is not None or isinstance(failure, asyncio.CancelledError) else None
    if diagnostic_error is not None and not isinstance(diagnostic_error, Exception):
        expected_error = diagnostic_error
    if expected_error is None:
        asyncio.run(crawl(resources))
    else:
        with pytest.raises(type(expected_error)) as raised:
            asyncio.run(crawl(resources))
        assert raised.value is expected_error

    assert (failure.__cause__, failure.__context__, failure.__suppress_context__) == original_chain
    expected_notes = ["Probe diagnostics failed: OSError"] if isinstance(diagnostic_error, Exception) else []
    assert getattr(failure, "__notes__", []) == expected_notes
    assert len(resources.request.calls) == 1
    event, = probe_events(resources)
    assert event["error"] == ("cancelled" if isinstance(failure, asyncio.CancelledError) else type(failure).__name__)
    assert event["stop_reason"] == ("cancelled" if isinstance(failure, asyncio.CancelledError) else "error")
    assert event["persisted"] == 0
    assert stored_results(resources) == [("unverified", None), ("unverified", None)]
    resources.browser.close.assert_awaited_once_with()
    resources.playwright.stop.assert_awaited_once_with()
