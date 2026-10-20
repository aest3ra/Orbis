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
