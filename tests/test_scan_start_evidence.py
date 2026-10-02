import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Observation, Scan, open_db


class FirstWriteFailure:
    def __init__(self, file, stage, failure):
        self.file = file
        self.stage = stage
        self.failure = failure
        self.failed = False

    def write(self, value):
        if self.stage == "write" and not self.failed:
            self.failed = True
            raise self.failure
        return self.file.write(value)

    def flush(self):
        if self.stage == "flush" and not self.failed:
            self.failed = True
            raise self.failure
        self.file.flush()

    def close(self):
        self.file.close()


def install_start_failure(monkeypatch, stage, failure):
    original = runner.ScanLog
    traces = []

    def log_factory(*args):
        if stage == "creation":
            raise failure
        trace = original(*args)
        traces.append(trace)
        trace._file = FirstWriteFailure(trace._file, stage, failure)
        return trace

    monkeypatch.setattr(runner, "ScanLog", log_factory)
    crawl = AsyncMock()
    monkeypatch.setattr(runner, "_crawl_scan", crawl)
    return crawl, traces


def read_result(database):
    engine = open_db(database)
    try:
        with Session(engine) as session:
            scan = session.get(Scan, 1)
            rows = session.exec(select(Observation).where(Observation.kind == "scan")).all()
            return scan, [json.loads(row.details_json) for row in rows]
    finally:
        engine.dispose()


@pytest.mark.parametrize("stage", ["creation", "write", "flush"])
@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
def test_start_failure_finishes_created_scan(tmp_path, monkeypatch, stage, failure_type):
    failure = failure_type("private-start-content")
    crawl, traces = install_start_failure(monkeypatch, stage, failure)
    database = tmp_path / "scan.db"

    with pytest.raises(failure_type) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is failure
    crawl.assert_not_awaited()
    scan, records = read_result(database)
    assert scan.finished_at is not None
    assert scan.pages_crawled == scan.endpoints_found == 0
    record, = records
    assert record["event"] == "scan_end"
    assert record["status"] == ("cancelled" if failure_type is asyncio.CancelledError else "error")
    assert record["failure_stage"] == ("log_creation" if stage == "creation" else "scan_start")
    assert record["pages"] == record["endpoints"] == 0
    assert "private-" not in json.dumps(record)
    if stage == "creation":
        assert not {"run_id", "sequence", "timestamp", "elapsed_ms", "schema_version"}.intersection(record)
        assert list(tmp_path.glob("*.jsonl")) == []
    else:
        assert traces[0]._file.file.closed
        rows = [json.loads(line) for line in traces[0].path.read_text().splitlines()]
        assert rows[-1] == record
        assert len(rows) == (2 if stage == "flush" else 1)


@pytest.mark.parametrize("failure_type", [OSError, asyncio.CancelledError])
def test_database_finalization_failure_preserves_start_error(tmp_path, monkeypatch, failure_type):
    failure = failure_type("private-start-content")
    secondary = ValueError("private-database-content")
    crawl, _ = install_start_failure(monkeypatch, "creation", failure)
    finish = Mock(side_effect=secondary)
    monkeypatch.setattr(runner, "finish_scan", finish)
    database = tmp_path / "scan.db"

    with pytest.raises(failure_type) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is failure
    crawl.assert_not_awaited()
    finish.assert_called_once()
    assert failure.__notes__ == ["Terminal diagnostics failed: ValueError"]
    scan, records = read_result(database)
    assert scan.finished_at is None
    assert records == []


def test_scan_creation_failure_does_not_fabricate_terminal_record(tmp_path, monkeypatch):
    failure = OSError("private-scan-creation-content")
    monkeypatch.setattr(runner, "create_scan", Mock(side_effect=failure))
    trace = Mock()
    monkeypatch.setattr(runner, "ScanLog", trace)
    database = tmp_path / "scan.db"

    with pytest.raises(OSError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is failure
    trace.assert_not_called()
    scan, records = read_result(database)
    assert scan is None
    assert records == []


def test_start_and_terminal_write_failures_still_leave_database_evidence(tmp_path, monkeypatch):
    failure = OSError("private-log-content")
    monkeypatch.setattr(runner.ScanLog, "emit", Mock(side_effect=failure))
    crawl = AsyncMock()
    monkeypatch.setattr(runner, "_crawl_scan", crawl)
    database = tmp_path / "scan.db"

    with pytest.raises(OSError) as raised:
        asyncio.run(runner.run_scan(ScanConfig(target="http://localhost/"), db_path=str(database), wayback=False))

    assert raised.value is failure
    crawl.assert_not_awaited()
    scan, records = read_result(database)
    assert scan.finished_at is not None
    record, = records
    assert record["failure_stage"] == "scan_start"
    assert record["log_write_error"] == "OSError"
    assert record["status"] == "error"
    assert record["pages"] == record["endpoints"] == 0
    assert not {"run_id", "sequence", "timestamp", "elapsed_ms", "schema_version"}.intersection(record)
    assert "private-" not in json.dumps(record)
    assert next(tmp_path.glob("*.jsonl")).read_text() == ""
