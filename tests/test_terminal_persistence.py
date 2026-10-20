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
