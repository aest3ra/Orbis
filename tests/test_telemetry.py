import asyncio
import json
import stat

import pytest
from sqlmodel import Session, select

from orbis.config import ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Observation, open_db
from orbis.telemetry import ScanLog


def test_log_is_flushed_redacted_and_never_overwritten(tmp_path):
    database = str(tmp_path / "scan.db")
    trace = ScanLog(database, 1)
    try:
        trace.emit("scan_start", target="https://user:private-pass@example.com/path?token=private-token#private-fragment",
                   headers={"Cookie": "private-cookie"}, decision={"value": "private-input"})
        text = trace.path.read_text()
        for private in ("private-pass", "private-token", "private-fragment", "private-cookie", "private-input"):
            assert private not in text
        row = json.loads(text)
        assert row["sequence"] == 1 and row["scan_id"] == 1
        assert row["timestamp"] and row["elapsed_ms"] >= 0
        assert "example.com/path?token=" in row["target"]
        assert stat.S_IMODE(trace.path.stat().st_mode) == 0o600
        second = ScanLog(database, 1)
        second.close()
        assert second.path != trace.path
        trace.emit("scan_end", status="completed")
        assert [json.loads(line)["sequence"] for line in trace.path.read_text().splitlines()] == [1, 2]
    finally:
        trace.close()


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
def test_scan_failure_and_cancellation_have_terminal_evidence(tmp_path, monkeypatch, failure):
    async def broken(*args, **kwargs):
        kwargs["trace"].emit("action_start", page_id=1, action_id=1)
        raise failure("private-exception-content")

    monkeypatch.setattr(runner, "_crawl_scan", broken)
    database = tmp_path / "scan.db"
    with pytest.raises(failure):
        asyncio.run(runner.run_scan(ScanConfig(target="https://example.com"), db_path=str(database), wayback=False))
    rows = [json.loads(line) for line in next(tmp_path.glob("*.jsonl")).read_text().splitlines()]
    assert [r["event"] for r in rows] == ["scan_start", "action_start", "scan_end"]
    assert rows[-1]["status"] == ("cancelled" if failure is asyncio.CancelledError else "error")
    assert "private-exception-content" not in json.dumps(rows)
    engine = open_db(database)
    try:
        with Session(engine) as session:
            record = session.exec(select(Observation).where(Observation.kind == "scan")).one()
            assert json.loads(record.details_json)["status"] == rows[-1]["status"]
    finally:
        engine.dispose()
