import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Observation, Scan, open_db


class ProbeLogFailure:
    def __init__(self, file, stage, failure):
        self.file = file
        self.stage = stage
        self.failure = failure
        self.probe_writes = 0
        self.fail_flush = False

    def write(self, text):
        if json.loads(text)["event"] == "probe_end":
            self.probe_writes += 1
            if self.probe_writes == 1:
                if self.stage == "partial_write":
                    self.file.write(text[:len(text) // 2])
                    raise self.failure
                if self.stage == "before_write":
                    raise self.failure
                self.fail_flush = True
        return self.file.write(text)

    def flush(self):
        if self.fail_flush:
            self.fail_flush = False
            if self.stage == "after_flush":
                self.file.flush()
            raise self.failure
        self.file.flush()

    def close(self):
        self.file.close()


@pytest.mark.parametrize("stage", ["before_write", "partial_write", "before_flush", "after_flush"])
def test_probe_log_failure_is_not_retried_as_success(tmp_path, monkeypatch, stage):
    failure = OSError("private-log-failure")
    wrappers = []
    original_log = runner.ScanLog

    def log_factory(*args):
        trace = original_log(*args)
        wrapper = ProbeLogFailure(trace._file, stage, failure)
        wrappers.append(wrapper)
        trace._file = wrapper
        return trace

    monkeypatch.setattr(runner, "ScanLog", log_factory)
    browser = SimpleNamespace(new_context=AsyncMock(return_value=object()), close=AsyncMock())
    playwright = SimpleNamespace(chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
                                 stop=AsyncMock())
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=SimpleNamespace(
        start=AsyncMock(return_value=playwright),
    )))
    config = ScanConfig(target="http://localhost/")
    config.limits.max_pages = 0
    config.limits.probe_max_requests = 0
    database = tmp_path / "scan.db"

    with pytest.raises(OSError) as raised:
        asyncio.run(runner.run_scan(config, db_path=str(database), wayback=False))

    assert raised.value is failure
    assert wrappers[0].probe_writes == 1
    assert wrappers[0].file.closed
    browser.close.assert_awaited_once_with()
    playwright.stop.assert_awaited_once_with()
    engine = open_db(database)
    try:
        with Session(engine) as session:
            scan = session.get(Scan, 1)
            record = session.exec(select(Observation).where(Observation.kind == "scan")).one()
            terminal = json.loads(record.details_json)
            assert scan.finished_at is not None
            assert scan.pages_crawled == scan.endpoints_found == 0
            assert terminal["status"] == "error"
            assert terminal["stop_reason"] == "exception"
            assert terminal["error"] == "OSError"
            assert terminal["totals"].get("probe_end", 0) == 0
            assert "private-" not in record.details_json
    finally:
        engine.dispose()
