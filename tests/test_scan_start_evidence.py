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
