import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from orbis.config import ScanConfig
from orbis.crawler import browser
from orbis.crawler.interactor import ExplorationState
from orbis.telemetry import ScanLog, clean


SCHEMES = ("http", "https", "HTTP", "HTTPS", "HtTp", "hTtPs", "ws", "wss", "WS", "WSS", "Ws", "wSs")
PRIVATE_MARKERS = ("synthetic-password", "synthetic-token", "synthetic-fragment")


@pytest.mark.parametrize("scheme", SCHEMES)
@pytest.mark.parametrize("position", ("target", "nested", "embedded"))
def test_clean_redacts_url_secrets_regardless_of_scheme_case(scheme, position):
    url = f"{scheme}://user:synthetic-password@example.test/page?token=synthetic-token#synthetic-fragment"
    safe = f"{scheme.lower()}://example.test/page?token=%5Bredacted%5D"
    if position == "target":
        value, expected = {"target": url}, {"target": safe}
    elif position == "nested":
        value = {"details": [{"urls": (url,)}]}
        expected = {"details": [{"urls": [safe]}]}
    else:
        value, expected = f"before {url} after", f"before {safe} after"

    actual = clean(value)

    assert all(marker not in json.dumps(actual) for marker in PRIVATE_MARKERS)
    assert actual == expected


def test_scan_log_redacts_original_target_with_mixed_case_scheme(tmp_path):
    config = ScanConfig(
        target="hTtPs://user:synthetic-password@example.test/page?token=synthetic-token#synthetic-fragment"
    )
    trace = ScanLog(str(tmp_path / "scan.db"), 1)
    try:
        record = trace.emit("scan_start", target=config.target)
    finally:
        trace.close()

    text = trace.path.read_text(encoding="utf-8")
    assert all(marker not in text for marker in PRIVATE_MARKERS)
    assert json.loads(text) == record
    assert record["target"] == "https://example.test/page?token=%5Bredacted%5D"


@pytest.mark.parametrize("scheme", ("ws", "wss", "Ws", "WsS"))
def test_captured_websocket_url_is_redacted_only_in_diagnostics(tmp_path, monkeypatch, scheme):
    url = f"{scheme}://localhost/feed?token=synthetic-token&empty="
    client = SimpleNamespace(send=AsyncMock(), on=Mock(), detach=AsyncMock())
    page = SimpleNamespace(
        url="http://localhost/", add_init_script=AsyncMock(), on=Mock(),
        goto=AsyncMock(), close=AsyncMock(),
        evaluate=AsyncMock(return_value=[{"src": "ws", "method": "GET", "url": url}]),
    )
    context = SimpleNamespace(new_page=AsyncMock(return_value=page),
                              new_cdp_session=AsyncMock(return_value=client))
    monkeypatch.setattr(browser, "_collect_dom", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_collect_inline_data", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_fill_selective_bodies", AsyncMock(return_value=([], set())))
    trace = ScanLog(str(tmp_path / "scan.db"), 1)
    state = ExplorationState(emit=trace.emit)

    async def capture():
        return await asyncio.wait_for(browser.capture_page(
            context, page.url, max_scrolls=0, settle_ms=0, exploration=state,
        ), timeout=1)

    try:
        result = asyncio.run(capture())
    finally:
        trace.close()

    event, = result.network_events
    assert event.url == url
    assert event.resource_type == "WebSocket"
    rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
    record, = rows
    assert record["event"] == "request_observed"
    assert record["url"] == f"{scheme.lower()}://localhost/feed?token=%5Bredacted%5D&empty=%5Bredacted%5D"
    assert "synthetic-token" not in trace.path.read_text()
    assert trace.totals == {"request_observed": 1}
    assert trace._file.closed
    client.detach.assert_awaited_once_with()
    page.close.assert_awaited_once_with()
