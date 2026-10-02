import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlmodel import Session, select

from orbis.config import LLMConfig, LimitsConfig, ScanConfig
from orbis.crawler import runner
from orbis.storage.db import Observation, open_db
from orbis.telemetry import ScanLog


TEXTS = (
    "한글 café 日本語 العربية 😀 👩‍💻",
    "quotes: \" \\ controls: \x00\n\t",
    "Show more " + "a" * 149 + "\ud83d",
    "\udc00",
    "한글\ud800😀\udfff",
)


@pytest.mark.parametrize("text", TEXTS, ids=["unicode", "controls", "truncated-emoji", "low-surrogate", "mixed-surrogates"])
def test_jsonl_preserves_unicode_and_surrogates_with_redaction(tmp_path, text):
    trace = ScanLog(str(tmp_path / "scan.db"), 1)
    try:
        trace.emit("scan_start")
        trace.emit("action_start", page_id=1, action_id=1, label=text,
                   nested={text: [text]}, headers={"Cookie": "private-cookie"},
                   target="http://user:private-password@localhost/?token=private-token#private-fragment")
        trace.emit("action_end", page_id=1, action_id=1, status="executed", label=text)
        trace.emit("scan_end", status="completed", totals=dict(trace.totals))
    finally:
        trace.close()

    encoded = trace.path.read_text(encoding="utf-8")
    rows = [json.loads(line) for line in encoded.splitlines()]
    assert [row["sequence"] for row in rows] == [1, 2, 3, 4]
    assert rows[1]["label"] == rows[2]["label"] == text
    assert rows[1]["nested"] == {text: [text]}
    assert rows[1]["headers"] == "[redacted]"
    assert "private-" not in encoded
    assert rows[-1]["totals"]["action_start"] == rows[-1]["totals"]["action_end"] == 1


@pytest.mark.parametrize("text", TEXTS, ids=["unicode", "controls", "truncated-emoji", "low-surrogate", "mixed-surrogates"])
@pytest.mark.parametrize("kind", ["action", "llm"])
def test_sqlite_diagnostic_observations_preserve_unicode_and_surrogates(tmp_path, monkeypatch, text, kind):
    record = {"kind": "button", "observation": "action", "label": text, "status": "executed",
              "nested": {text: [text]}, "headers": {"Cookie": "private-cookie"}}
    request = SimpleNamespace(dispose=AsyncMock())
    browser = SimpleNamespace(new_context=AsyncMock(return_value=object()), close=AsyncMock())
    playwright = SimpleNamespace(
        chromium=SimpleNamespace(launch=AsyncMock(return_value=browser)),
        request=SimpleNamespace(new_context=AsyncMock(return_value=request)), stop=AsyncMock(),
    )
    monkeypatch.setattr(runner, "async_playwright", Mock(return_value=SimpleNamespace(
        start=AsyncMock(return_value=playwright),
    )))
    monkeypatch.setattr(runner, "load_api_key", Mock(return_value="unused-local-test-key"))
    monkeypatch.setattr(runner, "QwenPlanner", Mock(return_value=SimpleNamespace(records=[record])))
    monkeypatch.setattr(runner, "capture_page", AsyncMock(return_value=SimpleNamespace(
        page_url="http://localhost/", final_url="http://localhost/", error=None,
        observations=[record] if kind == "action" else [],
    )))
    monkeypatch.setattr(runner, "analyze", Mock(return_value=SimpleNamespace(frontier_urls=[], endpoints=[])))
    config = ScanConfig(target="http://localhost/", limits=LimitsConfig(max_pages=1, probe_max_requests=0),
                        llm=LLMConfig(enabled=kind == "llm"))
    database = tmp_path / "scan.db"

    assert asyncio.run(runner.run_scan(config, db_path=str(database), wayback=False)) == 1

    engine = open_db(database)
    try:
        with Session(engine) as session:
            observation = session.exec(select(Observation).where(Observation.kind == kind)).one()
            stored = observation.details_json
            decoded = json.loads(stored)
            assert decoded["label"] == text
            assert decoded["nested"] == {text: [text]}
            assert decoded["headers"] == "[redacted]"
            assert "private-" not in stored
            terminal = session.exec(select(Observation).where(Observation.kind == "scan")).one()
            assert json.loads(terminal.details_json)["status"] == "completed"
    finally:
        engine.dispose()
