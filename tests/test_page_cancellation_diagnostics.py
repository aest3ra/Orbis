import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, select

from orbis.config import LimitsConfig, ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Observation, Scan, open_db


@pytest.fixture
def page_cancellation(tmp_path, monkeypatch):
    state = SimpleNamespace(
        database=tmp_path / "scan.db", entered=asyncio.Event(), stage=None,
        log_error=OSError("private-log-content"), capture_error=None,
        original=None, observed=None, task=None, trace=None, page_end_attempts=0,
    )
    browser = SimpleNamespace(new_context=AsyncMock(return_value=object()), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
                                 stop=AsyncMock())

    async def blocked_capture(*args, **kwargs):
        state.entered.set()
        if state.capture_error is not None:
            raise state.capture_error
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError as exc:
            state.original = exc
            raise

    class FailingFile:
        def __init__(self, file):
            self.file = file

        def write(self, value):
            if state.stage == "write":
                raise state.log_error
            return self.file.write(value)

        def flush(self):
            if state.stage == "flush":
                raise state.log_error
            return self.file.flush()

    class PageLog(runner.ScanLog):
        def __init__(self, *args):
            super().__init__(*args)
            state.trace = self

        def emit(self, name, **fields):
            original_file = self._file
            if name == "page_end":
                state.page_end_attempts += 1
                if state.stage is not None:
                    self._file = FailingFile(original_file)
            try:
                return super().emit(name, **fields)
            finally:
                self._file = original_file

    state.browser = browser
    state.playwright = playwright
    state.capture = AsyncMock(side_effect=blocked_capture)
    state.probe = AsyncMock(side_effect=AssertionError("probe must not run"))
    state.analyze = Mock(side_effect=AssertionError("analysis must not run"))
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=SimpleNamespace(
        start=AsyncMock(return_value=playwright))))
    monkeypatch.setattr(runner, "capture_page", state.capture)
    monkeypatch.setattr(runner, "probe_candidates", state.probe)
    monkeypatch.setattr(runner, "analyze", state.analyze)
    monkeypatch.setattr(runner, "ScanLog", PageLog)
    return state


def cancel_capture(state):
    async def exercise():
        config = ScanConfig(target="http://localhost/", limits=LimitsConfig(
            max_pages=1, max_duration_sec=60, rate_limit_rps=0))
        state.task = asyncio.create_task(runner.run_scan(
            config, db_path=str(state.database), wayback=False))
        await asyncio.wait_for(state.entered.wait(), timeout=1)
        if state.capture_error is None:
            state.task.cancel("external stop")
        try:
            await state.task
        except BaseException as exc:
            state.observed = exc

    try:
        asyncio.run(exercise())
    except BaseException as exc:
        # asyncio also forwards a child task's KeyboardInterrupt/SystemExit to the runner.
        state.observed = exc
    assert state.task.done()
    if not state.task.cancelled():
        state.task.exception()


def read_evidence(state):
    rows = [json.loads(line) for line in state.trace.path.read_text().splitlines()]
    engine = open_db(state.database)
    try:
        with Session(engine) as session:
            scan = session.get(Scan, 1)
            observations = session.exec(select(Observation).where(Observation.kind == "scan")).all()
            records = [json.loads(row.details_json) for row in observations]
    finally:
        engine.dispose()
    return rows, scan, records


def assert_cleanup(state):
    state.capture.assert_awaited_once()
    state.browser.close.assert_awaited_once_with()
    state.playwright.stop.assert_awaited_once_with()
    state.probe.assert_not_called()
    state.analyze.assert_not_called()
    assert state.page_end_attempts == 1
    assert state.trace._file.closed


@pytest.mark.parametrize("stage", [None, "write", "flush"])
def test_actual_page_cancellation_survives_diagnostic_failure(page_cancellation, stage):
    state = page_cancellation
    state.stage = stage

    cancel_capture(state)

    assert state.observed is state.original
    assert isinstance(state.observed, asyncio.CancelledError)
    assert state.observed.args == ("external stop",)
    assert state.task.cancelled()
    assert_cleanup(state)
    if stage is not None:
        assert state.observed.__notes__ == ["Page cancellation diagnostics failed: OSError"]
    rows, scan, records = read_evidence(state)
    assert rows[-1]["status"] == "cancelled"
    assert records == [rows[-1]]
    assert scan.finished_at is not None
    assert scan.pages_crawled == scan.endpoints_found == 0
    assert "private-" not in json.dumps(records)
    page_ends = [row for row in rows if row["event"] == "page_end"]
    assert [row["sequence"] for row in rows] == ([1, 2, 4] if stage == "write" else [1, 2, 3, 4])
    assert len(page_ends) == (0 if stage == "write" else 1)
    assert rows[-1]["totals"].get("page_end", 0) == (1 if stage is None else 0)


@pytest.mark.parametrize("stage", ["write", "flush"])
@pytest.mark.parametrize("interruption_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_new_page_diagnostic_interruption_takes_precedence(page_cancellation, stage, interruption_type):
    state = page_cancellation
    state.stage = stage
    state.log_error = interruption_type("new diagnostic interruption")

    cancel_capture(state)

    assert state.observed is state.log_error
    assert state.observed is not state.original
    assert state.task.cancelled() == (interruption_type is asyncio.CancelledError)
    assert_cleanup(state)
    rows, scan, records = read_evidence(state)
    assert rows[-1]["status"] == ("cancelled" if interruption_type is asyncio.CancelledError else "error")
    assert records == [rows[-1]]
    assert scan.finished_at is not None


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_page_cancellation_survives_later_cleanup_failures(page_cancellation, stage):
    state = page_cancellation
    state.stage = stage
    state.browser.close.side_effect = RuntimeError("private-browser-content")
    state.playwright.stop.side_effect = ValueError("private-driver-content")

    cancel_capture(state)

    assert state.observed is state.original
    assert state.task.cancelled()
    assert state.observed.__notes__ == [
        "Page cancellation diagnostics failed: OSError",
        "Browser close failed: RuntimeError",
        "Playwright stop failed: ValueError",
    ]
    assert_cleanup(state)
    rows, _, records = read_evidence(state)
    assert records == [rows[-1]]
    assert rows[-1]["status"] == "cancelled"


@pytest.mark.parametrize("stage", ["write", "flush"])
def test_regular_capture_error_logging_behavior_is_unchanged(page_cancellation, stage):
    state = page_cancellation
    state.stage = stage
    state.capture_error = RuntimeError("private-capture-content")

    cancel_capture(state)

    assert state.observed is state.log_error
    assert state.original is None
    assert not state.task.cancelled()
    assert_cleanup(state)
    rows, _, records = read_evidence(state)
    assert records == [rows[-1]]
    assert rows[-1]["status"] == "error"
    assert rows[-1]["error"] == "OSError"
