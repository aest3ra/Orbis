import asyncio
import json

import pytest
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
