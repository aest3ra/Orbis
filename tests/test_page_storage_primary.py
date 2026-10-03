import asyncio
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.exc import OperationalError, ProgrammingError
from sqlmodel import Session, select

from orbis.analysis.analyzer import AnalysisResult, NormalizedEndpoint
from orbis.config import LimitsConfig, ScanConfig
from orbis.crawler import runner
from orbis.crawler.browser import PageCapture
from orbis.crawler.probe import ProbeResults
from orbis.storage.db import Endpoint, Observation, Scan, open_db


@pytest.fixture
def page_storage(tmp_path, monkeypatch):
    state = SimpleNamespace(
        database=tmp_path / "scan.db", capture_status="completed", query_fails=False,
        disconnect=False, body_error=None, close_error=None, primary=None, cleanup=None,
        chain=None, sessions=0, dropped=False, trace=None, engine=None, dispose_calls=0,
        close_calls=[], action=None,
    )

    def writer_db(path):
        engine = open_db(path)
        state.engine = engine
        dispose = engine.dispose

        def counted_dispose():
            state.dispose_calls += 1
            return dispose()

        engine.dispose = counted_dispose
        return engine

    class PageSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.sessions += 1
            self.index = state.sessions

        def exec(self, statement, *args, **kwargs):
            if self.index != 3 or not (state.query_fails or state.body_error is not None):
                return super().exec(statement, *args, **kwargs)
            raw = self.connection().connection.driver_connection
            if state.query_fails:
                connection = sqlite3.connect(state.database)
                try:
                    connection.execute("DROP TABLE endpoint")
                    connection.commit()
                finally:
                    connection.close()
                state.dropped = True
            try:
                if state.body_error is not None:
                    raise state.body_error
                return super().exec(statement, *args, **kwargs)
            except BaseException as exc:
                state.primary = exc
                state.chain = (exc.__cause__, exc.__context__, exc.__suppress_context__)
                if state.disconnect:
                    raw.close()
                raise

        def close(self):
            state.close_calls.append(self.index)
            try:
                super().close()
                if self.index == 3 and state.close_error is not None:
                    raise state.close_error
            except BaseException as exc:
                if self.index == 3:
                    state.cleanup = exc
                raise
            finally:
                if self.index == 3 and state.dropped:
                    Endpoint.__table__.create(state.engine)

    class PageLog(runner.ScanLog):
        def __init__(self, *args):
            super().__init__(*args)
            state.trace = self

    async def capture(context, url, *, exploration, **kwargs):
        if state.capture_status == "failed":
            raise RuntimeError("private-capture-content")
        state.action = {
            "kind": "button", "observation": "action", "label": "offline action",
            "status": "executed", "action_id": 1, "page_id": exploration.page_id,
        }
        exploration.event("action_start", action_id=1, kind="button", label="offline action")
        exploration.event("action_end", **{key: value for key, value in state.action.items() if key != "page_id"})
        return PageCapture(
            page_url=url, final_url=url, observations=[state.action],
            error="TimeoutError" if state.capture_status == "partial" else None,
        )

    state.browser = SimpleNamespace(new_context=AsyncMock(return_value=object()), close=AsyncMock())
    state.playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=state.browser)), stop=AsyncMock(),
    )
    state.capture = AsyncMock(side_effect=capture)
    state.probe = AsyncMock(return_value=ProbeResults())
    endpoint = NormalizedEndpoint(
        method="GET", host="localhost", path_template="/api/one",
        sample_url="http://localhost/api/one", route_kind="application_api",
    )
    monkeypatch.setattr(runner, "open_db", writer_db)
    monkeypatch.setattr(runner, "Session", PageSession)
    monkeypatch.setattr(runner, "ScanLog", PageLog)
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=SimpleNamespace(
        start=AsyncMock(return_value=state.playwright))))
    monkeypatch.setattr(runner, "capture_page", state.capture)
    monkeypatch.setattr(runner, "analyze", Mock(return_value=AnalysisResult([], [endpoint])))
    monkeypatch.setattr(runner, "probe_candidates", state.probe)
    return state


def run_page(state):
    config = ScanConfig(target="http://localhost/", limits=LimitsConfig(
        max_pages=1, rate_limit_rps=0, probe_max_requests=0))
    error = None
    try:
        asyncio.run(runner.run_scan(config, db_path=str(state.database), wayback=False))
    except BaseException as exc:
        error = exc
    state.capture.assert_awaited_once()
    state.browser.close.assert_awaited_once_with()
    state.playwright.stop.assert_awaited_once_with()
    assert state.dispose_calls == 1
    assert state.close_calls == list(range(1, state.sessions + 1))
    return error


def stored(state):
    engine = open_db(state.database)
    try:
        with Session(engine) as session:
            scan = session.exec(select(Scan)).one()
            endpoints = session.exec(select(Endpoint)).all()
            observations = session.exec(select(Observation)).all()
            records = [(row.kind, json.loads(row.details_json)) for row in observations]
    finally:
        engine.dispose()
    rows = [json.loads(line) for line in state.trace.path.read_text().splitlines()]
    terminal = rows[-1]
    assert [record for kind, record in records if kind == "scan"] == [terminal]
    assert scan.finished_at is not None
    assert scan.pages_crawled == terminal["pages"] == terminal["totals"].get("pages_crawled", 0)
    assert scan.endpoints_found == terminal["endpoints"] == len(endpoints)
    return scan, records, rows


@pytest.mark.parametrize("status", ["completed", "partial", "failed"])
def test_page_storage_normal_status_and_evidence_are_unchanged(page_storage, status):
    state = page_storage
    state.capture_status = status
    assert run_page(state) is None
    scan, records, rows = stored(state)
    page_ends = [row for row in rows if row["event"] == "page_end"]
    assert [row["status"] for row in page_ends] == [status]
    assert scan.pages_crawled == 1
    assert rows[-1]["status"] == "completed"
    assert [record for kind, record in records if kind == "page"] == ([] if status == "failed" else page_ends)
    assert [record for kind, record in records if kind == "action"] == ([] if status == "failed" else [state.action])
    assert scan.endpoints_found == (0 if status == "failed" else 1)
    state.probe.assert_awaited_once()


@pytest.mark.parametrize("status", ["completed", "partial"])
@pytest.mark.parametrize("disconnect", [False, True])
def test_real_endpoint_query_error_survives_sqlite_rollback_failure(page_storage, status, disconnect):
    state = page_storage
    state.capture_status = status
    state.query_fails = True
    state.disconnect = disconnect
    raised = run_page(state)
    assert raised is state.primary
    assert isinstance(raised, OperationalError)
    assert isinstance(raised.orig, sqlite3.OperationalError)
    assert (raised.__cause__, raised.__context__, raised.__suppress_context__) == state.chain
    if disconnect:
        assert isinstance(state.cleanup, ProgrammingError)
        assert isinstance(state.cleanup.orig, sqlite3.ProgrammingError)
        assert raised.__notes__ == ["Database session close failed: ProgrammingError"]
    else:
        assert state.cleanup is None
        assert not getattr(raised, "__notes__", [])
    scan, records, rows = stored(state)
    assert rows[-1]["error"] == "OperationalError"
    assert rows[-1]["status"] == "error"
    assert scan.pages_crawled == scan.endpoints_found == 0
    assert [record for kind, record in records if kind == "action"] == [state.action]
    assert not [record for kind, record in records if kind == "page"]
    assert not [row for row in rows if row["event"] == "page_end"]
    state.probe.assert_not_awaited()


@pytest.mark.parametrize("body_type", [RuntimeError, asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_page_storage_preserves_body_error_and_chain_over_ordinary_close_error(page_storage, body_type):
    state = page_storage
    cause = ValueError("private-cause-content")
    state.body_error = body_type("private-body-content")
    state.body_error.__cause__ = cause
    state.body_error.__context__ = cause
    state.close_error = OSError("private-cleanup-content")
    raised = run_page(state)
    assert raised is state.body_error
    assert (raised.__cause__, raised.__context__, raised.__suppress_context__) == state.chain
    assert raised.__notes__ == ["Database session close failed: OSError"]
    _, _, rows = stored(state)
    assert rows[-1]["status"] == ("cancelled" if body_type is asyncio.CancelledError else "error")
    assert "private-" not in json.dumps(rows)
    state.probe.assert_not_awaited()


@pytest.mark.parametrize("query_fails", [False, True])
@pytest.mark.parametrize("close_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_new_page_storage_close_interruption_propagates(page_storage, query_fails, close_type):
    state = page_storage
    state.query_fails = query_fails
    state.close_error = close_type("private-cleanup-content")
    assert run_page(state) is state.close_error
    scan, _, rows = stored(state)
    assert rows[-1]["status"] == ("cancelled" if close_type is asyncio.CancelledError else "error")
    assert scan.endpoints_found == (0 if query_fails else 1)
    state.probe.assert_not_awaited()


def test_page_storage_close_error_after_commit_is_not_hidden(page_storage):
    state = page_storage
    state.close_error = OSError("private-cleanup-content")
    assert run_page(state) is state.close_error
    scan, _, rows = stored(state)
    assert state.primary is None
    assert rows[-1]["error"] == "OSError"
    assert scan.pages_crawled == 0
    assert scan.endpoints_found == 1
    state.probe.assert_not_awaited()
