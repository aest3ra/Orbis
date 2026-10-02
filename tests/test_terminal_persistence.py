import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Endpoint, Observation, Scan, open_db


class FailingTerminalFile:
    def __init__(self, file, stage, failure):
        self.file = file
        self.stage = stage
        self.failure = failure

    def write(self, text):
        if self.stage == "write":
            raise self.failure
        return self.file.write(text)

    def flush(self):
        if self.stage == "flush":
            raise self.failure
        return self.file.flush()

    def close(self):
        self.file.close()


def install_crawl(monkeypatch, primary, log_error, stage):
    async def crawl(config, engine, scan_id, *args, trace, **kwargs):
        with Session(engine) as session:
            session.add(Endpoint(scan_id=scan_id, method="GET", host="localhost",
                                 path_template="/api/one", sample_url="http://localhost/api/one",
                                 route_kind="application_api"))
            session.commit()
        trace.emit("page_start", page_id=1)
        trace.emit("page_end", page_id=1, status="completed")
        if stage is not None:
            trace._file = FailingTerminalFile(trace._file, stage, log_error)
        if primary is not None:
            raise primary
        return {"stop_reason": "max_pages", "pages": 1, "endpoints": 999}

    monkeypatch.setattr(runner, "_crawl_scan", crawl)


def read_result(database):
    engine = open_db(database)
    try:
        with Session(engine) as session:
            scan = session.get(Scan, 1)
            rows = session.exec(select(Observation).where(Observation.kind == "scan")).all()
            records = [json.loads(row.details_json) for row in rows]
            return scan, records
    finally:
        engine.dispose()


@pytest.mark.parametrize("stage", ["write", "flush"])
@pytest.mark.parametrize("primary_type", [None, RuntimeError, asyncio.CancelledError])
def test_terminal_log_failure_preserves_database_evidence(tmp_path, monkeypatch, stage, primary_type):
    primary = primary_type("private-primary-content") if primary_type else None
    log_error = OSError("private-log-content")
    install_crawl(monkeypatch, primary, log_error, stage)
    database = tmp_path / "scan.db"

    with pytest.raises(primary_type or OSError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is (primary if primary is not None else log_error)
    scan, records = read_result(database)
    assert scan.finished_at is not None
    assert scan.pages_crawled == scan.endpoints_found == 1
    assert len(records) == 1
    record = records[0]
    assert record["event"] == "scan_end"
    assert record["pages"] == record["endpoints"] == 1
    assert record["log_write_error"] == "OSError"
    assert record["status"] == ("cancelled" if primary_type is asyncio.CancelledError else "error")
    assert record["stop_reason"] == ("cancelled" if primary_type is asyncio.CancelledError else "exception")
    if primary_type is not asyncio.CancelledError:
        assert record["error"] == (primary_type.__name__ if primary_type else "OSError")
    assert not {"run_id", "sequence", "timestamp", "elapsed_ms", "schema_version"}.intersection(record)
    assert "private-" not in json.dumps(record)
    if primary is not None:
        assert "OSError" in " ".join(primary.__notes__)
    assert "private-" not in " ".join(getattr(raised.value, "__notes__", []))


@pytest.mark.parametrize("primary_type", [None, RuntimeError, asyncio.CancelledError])
def test_terminal_log_failure_precedes_database_failure(tmp_path, monkeypatch, primary_type):
    primary = primary_type("private-primary-content") if primary_type else None
    log_error = OSError("private-log-content")
    database_error = ValueError("private-database-content")
    install_crawl(monkeypatch, primary, log_error, "write")

    def fail_finish(*args):
        raise database_error

    monkeypatch.setattr(runner, "finish_scan", fail_finish)
    database = tmp_path / "scan.db"
    with pytest.raises(primary_type or OSError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is (primary if primary is not None else log_error)
    notes = " ".join(raised.value.__notes__)
    assert "ValueError" in notes
    if primary is not None:
        assert "OSError" in notes
    assert "private-" not in notes
    scan, records = read_result(database)
    assert scan.finished_at is None
    assert records == []


@pytest.mark.parametrize("primary_type", [None, RuntimeError, asyncio.CancelledError])
@pytest.mark.parametrize("database_fails", [False, True])
def test_count_error_precedes_later_diagnostic_failures(tmp_path, monkeypatch, primary_type, database_fails):
    primary = primary_type("private-primary-content") if primary_type else None
    count_error = LookupError("private-count-content")
    log_error = OSError("private-log-content")
    database_error = ValueError("private-database-content")
    install_crawl(monkeypatch, primary, log_error, "write")

    class CountFailureSession(Session):
        def exec(self, *args, **kwargs):
            raise count_error

        def add(self, instance, *args, **kwargs):
            if database_fails and isinstance(instance, Observation):
                raise database_error
            return super().add(instance, *args, **kwargs)

    monkeypatch.setattr(runner, "Session", CountFailureSession)
    database = tmp_path / "scan.db"
    with pytest.raises(primary_type or LookupError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is (primary if primary is not None else count_error)
    notes = " ".join(raised.value.__notes__)
    assert "OSError" in notes
    if primary is not None:
        assert "LookupError" in notes
    if database_fails:
        assert "ValueError" in notes
    assert "private-" not in notes
    scan, records = read_result(database)
    assert scan.finished_at is None
    if database_fails:
        assert records == []
    else:
        assert len(records) == 1
        assert records[0]["metadata_error"] == "LookupError"
        assert records[0]["log_write_error"] == "OSError"
        assert "endpoints" not in records[0]


def test_successful_terminal_record_matches_jsonl_exactly(tmp_path, monkeypatch):
    install_crawl(monkeypatch, None, None, None)
    database = tmp_path / "scan.db"

    assert asyncio.run(runner.run_scan(
        ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False,
    )) == 1

    scan, records = read_result(database)
    terminal = json.loads(next(tmp_path.glob("*.jsonl")).read_text().splitlines()[-1])
    assert records == [terminal]
    assert terminal["status"] == "completed"
    assert terminal["pages"] == terminal["endpoints"] == 1
    assert scan.finished_at is not None
    assert "log_write_error" not in terminal


def install_session_fault(monkeypatch, stage, close_error, *, fail_body=True, body_error=None, disconnect=False):
    state = SimpleNamespace(count=0, body_error=None, close_error=None, close_calls=[], chain=None)
    target = 2 if stage == "count" else 3

    class FaultSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            state.count += 1
            self.index = state.count

        def capture_failure(self, operation):
            try:
                if body_error is not None:
                    raise body_error
                return operation()
            except BaseException as exc:
                state.body_error = exc
                state.chain = (exc.__cause__, exc.__context__, exc.__suppress_context__)
                if disconnect:
                    self.connection().connection.driver_connection.close()
                raise

        def exec(self, *args, **kwargs):
            if fail_body and stage == "count" and self.index == target:
                return self.capture_failure(lambda: super(FaultSession, self).exec(
                    text("SELECT * FROM absent_terminal_count_table")))
            return super().exec(*args, **kwargs)

        def commit(self):
            if fail_body and stage == "commit" and self.index == target:
                self.add(Observation(scan_id=1, page_url="http://localhost/", kind="scan", details_json=None))
                return self.capture_failure(super().commit)
            return super().commit()

        def close(self):
            state.close_calls.append(self.index)
            try:
                super().close()
            except BaseException as exc:
                state.close_error = exc
                raise
            if self.index == target and close_error is not None:
                state.close_error = close_error
                raise close_error

    monkeypatch.setattr(runner, "Session", FaultSession)
    return state


@pytest.mark.parametrize("stage", ["count", "commit"])
@pytest.mark.parametrize("crawl_mode", ["completed", "error", "cancelled"])
def test_database_body_error_survives_session_close_failure(tmp_path, monkeypatch, stage, crawl_mode):
    state = install_session_fault(monkeypatch, stage, OSError("private-close-content"))
    database = tmp_path / "scan.db"
    primary = RuntimeError("private-crawl-content") if crawl_mode == "error" else None

    async def exercise():
        nonlocal primary
        entered = asyncio.Event()

        async def crawl(*args, **kwargs):
            nonlocal primary
            if crawl_mode == "error":
                raise primary
            if crawl_mode == "cancelled":
                entered.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as exc:
                    primary = exc
                    raise
            return {"pages": 0, "stop_reason": "max_pages"}

        monkeypatch.setattr(runner, "_crawl_scan", crawl)
        task = asyncio.create_task(runner.run_scan(
            ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))
        if crawl_mode == "cancelled":
            await entered.wait()
            task.cancel("external stop")
        expected_type = (asyncio.CancelledError if crawl_mode == "cancelled" else
                         RuntimeError if crawl_mode == "error" else
                         OperationalError if stage == "count" else IntegrityError)
        with pytest.raises(expected_type) as raised:
            await task
        assert raised.value is (primary if primary is not None else state.body_error)
        assert task.cancelled() == (crawl_mode == "cancelled")

    asyncio.run(exercise())

    assert state.body_error.__notes__ == ["Terminal database session close failed: OSError"]
    assert state.body_error.__cause__ is state.chain[0]
    assert state.body_error.__context__ is (primary if primary is not None else state.chain[1])
    assert state.body_error.__suppress_context__ == state.chain[2]
    assert state.close_calls == [1, 2, 3]
    scan, records = read_result(database)
    assert scan.finished_at is None
    if stage == "count":
        record, = records
        assert record["metadata_error"] == "OperationalError"
        assert record["status"] == ("cancelled" if crawl_mode == "cancelled" else "error")
        assert "private-" not in json.dumps(record)
    else:
        assert records == []


def test_real_sqlite_rollback_failure_preserves_count_error(tmp_path, monkeypatch):
    state = install_session_fault(monkeypatch, "count", None, disconnect=True)
    install_crawl(monkeypatch, None, None, None)
    database = tmp_path / "scan.db"

    with pytest.raises(OperationalError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is state.body_error
    assert isinstance(state.close_error, ProgrammingError)
    assert state.body_error.__notes__ == ["Terminal database session close failed: ProgrammingError"]
    assert (state.body_error.__cause__, state.body_error.__context__,
            state.body_error.__suppress_context__) == state.chain
    scan, records = read_result(database)
    assert scan.finished_at is None
    assert records[0]["metadata_error"] == "OperationalError"


@pytest.mark.parametrize("stage", ["count", "commit"])
@pytest.mark.parametrize("body_type", [asyncio.CancelledError, KeyboardInterrupt, SystemExit])
def test_terminal_body_interruption_survives_regular_close_failure(tmp_path, monkeypatch, stage, body_type):
    body_error = body_type("private-body-content")
    state = install_session_fault(monkeypatch, stage, OSError("private-close-content"), body_error=body_error)
    install_crawl(monkeypatch, None, None, None)

    with pytest.raises(body_type) as raised:
        asyncio.run(runner.run_scan(
            ScanConfig(target="http://localhost/"), db_path=str(tmp_path / "scan.db"), wayback=False))

    assert raised.value is body_error
    assert body_error.__notes__ == ["Terminal database session close failed: OSError"]
    assert (body_error.__cause__, body_error.__context__, body_error.__suppress_context__) == state.chain


@pytest.mark.parametrize("stage", ["count", "commit"])
def test_session_close_error_after_success_is_not_hidden(tmp_path, monkeypatch, stage):
    close_error = OSError("private-close-content")
    state = install_session_fault(monkeypatch, stage, close_error, fail_body=False)
    install_crawl(monkeypatch, None, None, None)
    database = tmp_path / "scan.db"

    with pytest.raises(OSError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is close_error
    assert state.body_error is None
    scan, records = read_result(database)
    assert (scan.finished_at is not None) == (stage == "commit")
    assert records[0]["status"] == ("completed" if stage == "commit" else "error")


@pytest.mark.parametrize("stage", ["count", "commit"])
@pytest.mark.parametrize("close_type", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("fail_body", [False, True])
def test_fresh_terminal_close_interruption_propagates(tmp_path, monkeypatch, stage, close_type, fail_body):
    close_error = close_type("private-close-content")
    install_session_fault(monkeypatch, stage, close_error, fail_body=fail_body)
    install_crawl(monkeypatch, None, None, None)

    with pytest.raises(close_type) as raised:
        asyncio.run(runner.run_scan(
            ScanConfig(target="http://localhost/"), db_path=str(tmp_path / "scan.db"), wayback=False))

    assert raised.value is close_error


@pytest.mark.parametrize("stage", ["count", "commit"])
def test_terminal_session_entry_failure_preserves_with_contract(tmp_path, monkeypatch, stage):
    target = 2 if stage == "count" else 3
    entry_error = OSError("private-entry-content")
    calls = {"created": 0, "closed": []}

    class EntryFailureSession(Session):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            calls["created"] += 1
            self.index = calls["created"]

        def __enter__(self):
            if self.index == target:
                raise entry_error
            return super().__enter__()

        def close(self):
            calls["closed"].append(self.index)
            return super().close()

    monkeypatch.setattr(runner, "Session", EntryFailureSession)
    install_crawl(monkeypatch, None, None, None)
    with pytest.raises(OSError) as raised:
        asyncio.run(runner.run_scan(
            ScanConfig(target="http://localhost/"), db_path=str(tmp_path / "scan.db"), wayback=False))

    assert raised.value is entry_error
    assert not hasattr(entry_error, "__notes__")
    assert target not in calls["closed"]
