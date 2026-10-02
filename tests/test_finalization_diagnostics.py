import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, text
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Endpoint, Observation, Scan, open_db


@pytest.fixture
def finalization(tmp_path, monkeypatch):
    state = SimpleNamespace(
        database=tmp_path / "scan.db", mode="success", metadata_fails=False,
        crawl_mode="completed", entered=asyncio.Event(), crawl_calls=0,
        primary=None, database_error=None, metadata_error=None, raised=None,
        commit_returned=False, after_commit_seen=False, sessions=0,
        trace=None, no_trace=False, log_event=None, log_stage="write", attempts=[],
        log_error=OSError("private-log-content"), database_fault=OSError("private-database-content"),
    )

    class DatabaseSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.sessions += 1
            self.index = state.sessions
            if self.index == 3:
                def after_commit(session):
                    state.after_commit_seen = True
                    if state.mode == "commit_after":
                        raise state.database_fault
                event.listen(self, "after_commit", after_commit)

        def exec(self, *args, **kwargs):
            if self.index == 2 and state.metadata_fails:
                try:
                    return super().exec(text("SELECT * FROM absent_finalization_metadata_table"))
                except Exception as exc:
                    state.metadata_error = exc
                    raise
            return super().exec(*args, **kwargs)

        def commit(self):
            if self.index != 3:
                return super().commit()
            if state.mode == "commit_before":
                self.add(Observation(scan_id=1, page_url="http://localhost/", kind="scan", details_json=None))
            try:
                result = super().commit()
            except Exception as exc:
                state.database_error = exc
                raise
            state.commit_returned = True
            return result

        def close(self):
            super().close()
            if self.index == 3 and state.mode == "close_only":
                state.database_error = state.database_fault
                raise state.database_fault

    class FailingFile:
        def __init__(self, file):
            self.file = file

        def write(self, value):
            if state.log_stage == "write":
                raise state.log_error
            return self.file.write(value)

        def flush(self):
            if state.log_stage == "flush":
                raise state.log_error
            return self.file.flush()

    class DiagnosticLog(runner.ScanLog):
        def __init__(self, *args):
            if state.no_trace:
                raise state.log_error
            super().__init__(*args)
            state.trace = self

        def emit(self, name, **fields):
            state.attempts.append(name)
            original_file = self._file
            if name == state.log_event:
                self._file = FailingFile(original_file)
            try:
                return super().emit(name, **fields)
            finally:
                self._file = original_file

    async def crawl(config, engine, scan_id, *args, trace, **kwargs):
        state.crawl_calls += 1
        with Session(engine) as session:
            session.add(Endpoint(scan_id=scan_id, method="GET", host="localhost", path_template="/api/one",
                                 sample_url="http://localhost/api/one", route_kind="application_api"))
            session.commit()
        trace.emit("page_start", page_id=1)
        trace.emit("page_end", page_id=1, status="completed")
        if state.crawl_mode == "error":
            state.primary = RuntimeError("private-crawl-content")
            raise state.primary
        if state.crawl_mode == "cancelled":
            state.entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                state.primary = exc
                raise
        return {"pages": 1, "stop_reason": "max_pages"}

    monkeypatch.setattr(runner, "Session", DatabaseSession)
    monkeypatch.setattr(runner, "ScanLog", DiagnosticLog)
    monkeypatch.setattr(runner, "_crawl_scan", crawl)
    return state


def execute(state):
    async def exercise():
        scan = runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(state.database), wayback=False)
        if state.crawl_mode == "cancelled":
            scan = asyncio.create_task(scan)
            await state.entered.wait()
            scan.cancel("external stop")
        try:
            state.result = await scan
        except BaseException as exc:
            state.raised = exc

    asyncio.run(exercise())


def evidence(state):
    logs = [json.loads(line) for line in state.trace.path.read_text().splitlines()] if state.trace else []
    engine = open_db(state.database)
    try:
        with Session(engine) as session:
            scan = session.get(Scan, 1)
            records = [json.loads(row.details_json) for row in session.exec(select(Observation)).all()]
            endpoints = session.exec(select(Endpoint)).all()
    finally:
        engine.dispose()
    return logs, scan, records, endpoints


@pytest.mark.parametrize("mode", ["commit_before", "commit_after", "close_only"])
def test_database_exception_appends_one_diagnostic_without_claiming_rollback(finalization, mode):
    state = finalization
    state.mode = mode

    execute(state)

    assert state.raised is state.database_error
    assert state.attempts.count("scan_finalization_error") == 1
    logs, scan, records, endpoints = evidence(state)
    assert [row["event"] for row in logs] == [
        "scan_start", "page_start", "page_end", "scan_end", "scan_finalization_error"]
    terminal, diagnostic = logs[-2:]
    assert terminal["status"] == "completed"
    assert diagnostic["stage"] == "database"
    assert diagnostic["error"] == type(state.database_error).__name__
    envelope = {"schema_version", "run_id", "scan_id", "sequence", "timestamp", "elapsed_ms", "event"}
    assert set(diagnostic) == envelope | {"stage", "error"}
    assert "private-" not in json.dumps(diagnostic)
    assert len(endpoints) == 1
    assert state.commit_returned == (mode == "close_only")
    assert state.after_commit_seen == (mode != "commit_before")
    assert (scan.finished_at is not None) == (mode != "commit_before")
    assert records == ([terminal] if mode != "commit_before" else [])


@pytest.mark.parametrize("metadata_fails", [False, True])
def test_successful_database_block_has_no_finalization_error(finalization, metadata_fails):
    state = finalization
    state.metadata_fails = metadata_fails

    execute(state)

    assert state.raised is state.metadata_error
    assert "scan_finalization_error" not in state.attempts
    logs, scan, records, endpoints = evidence(state)
    assert records == [logs[-1]]
    assert logs[-1]["status"] == ("error" if metadata_fails else "completed")
    assert (scan.finished_at is not None) == (not metadata_fails)
    assert len(endpoints) == 1


@pytest.mark.parametrize("crawl_mode", ["completed", "error", "cancelled"])
@pytest.mark.parametrize("metadata_fails", [False, True])
@pytest.mark.parametrize("log_stage", [None, "write", "flush"])
def test_finalization_diagnostic_preserves_first_error(finalization, crawl_mode, metadata_fails, log_stage):
    state = finalization
    state.mode = "commit_before"
    state.crawl_mode = crawl_mode
    state.metadata_fails = metadata_fails
    if log_stage is not None:
        state.log_event = "scan_finalization_error"
        state.log_stage = log_stage

    execute(state)

    assert state.raised is next(error for error in (state.primary, state.metadata_error, state.database_error)
                                if error is not None)
    assert state.attempts.count("scan_finalization_error") == 1
    if log_stage is not None:
        assert "Finalization error logging failed: OSError" in state.raised.__notes__
    assert "private-" not in " ".join(getattr(state.raised, "__notes__", []))
    assert state.trace._file.closed
    logs, scan, records, endpoints = evidence(state)
    assert scan.finished_at is None
    assert records == []
    assert len(endpoints) == 1
    if log_stage == "write":
        assert logs[-1]["event"] == "scan_end"


@pytest.mark.parametrize("log_stage", ["write", "flush"])
def test_unconfirmed_scan_end_does_not_attempt_an_extra_diagnostic(finalization, log_stage):
    state = finalization
    state.mode = "commit_before"
    state.log_event = "scan_end"
    state.log_stage = log_stage

    execute(state)

    assert state.raised is state.log_error
    assert state.database_error is not None
    assert "scan_finalization_error" not in state.attempts
    logs, scan, records, _ = evidence(state)
    assert any(row["event"] == "scan_end" for row in logs) == (log_stage == "flush")
    assert scan.finished_at is None
    assert records == []


def test_missing_trace_does_not_attempt_an_extra_diagnostic(finalization):
    state = finalization
    state.mode = "commit_before"
    state.no_trace = True

    execute(state)

    assert state.raised is state.log_error
    assert state.database_error is not None
    assert state.crawl_calls == 0
    assert state.attempts == []
    assert state.trace is None


@pytest.mark.parametrize("crawl_mode", ["completed", "error"])
@pytest.mark.parametrize("interruption_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_new_diagnostic_interruption_propagates(finalization, crawl_mode, interruption_type):
    state = finalization
    state.mode = "commit_before"
    state.crawl_mode = crawl_mode
    state.log_event = "scan_finalization_error"
    state.log_error = interruption_type("private-interruption-content")

    execute(state)

    assert state.raised is state.log_error
    assert state.attempts.count("scan_finalization_error") == 1
    assert state.trace._file.closed
