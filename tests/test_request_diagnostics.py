import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from orbis.crawler import browser
from orbis.crawler.interactor import ExplorationState
from orbis.telemetry import ScanLog


@pytest.fixture
def capture_resources(monkeypatch):
    callbacks, logs = {}, []
    client = SimpleNamespace(send=AsyncMock(), detach=AsyncMock(),
                             on=lambda name, callback: callbacks.__setitem__(name, callback))
    page = SimpleNamespace(url="http://localhost/", add_init_script=AsyncMock(),
                           on=lambda *args: None, goto=AsyncMock(), close=AsyncMock())
    context = SimpleNamespace(new_page=AsyncMock(return_value=page),
                              new_cdp_session=AsyncMock(return_value=client))
    state = ExplorationState(page_id=1, emit=lambda name, **record: logs.append({"event": name, **record}))
    monkeypatch.setattr(browser, "_collect_dom", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_collect_inline_data", AsyncMock(return_value=[]))
    monkeypatch.setattr(browser, "_fill_selective_bodies", AsyncMock(return_value=([], set())))
    monkeypatch.setattr(browser, "_collect_init_script_events", AsyncMock(return_value=[]))

    def request(resource_type="Fetch"):
        state.current_action = {"action_id": 7}
        callbacks["Network.requestWillBeSent"]({
            "requestId": "observed", "type": resource_type,
            "request": {"method": "GET", "url": "http://localhost/data?token=private-query"},
        })
        state.current_action = None

    def response(status):
        callbacks["Network.responseReceived"]({
            "requestId": "observed", "response": {
                "status": status, "headers": {"generation": str(status)}, "mimeType": "application/json",
            },
        })

    async def capture(**kwargs):
        return await browser.capture_page(context, page.url, exploration=state, max_scrolls=0, settle_ms=0, **kwargs)

    return SimpleNamespace(context=context, page=page, client=client, state=state, logs=logs,
                           request=request, response=response, capture=capture)


@pytest.mark.parametrize("timing", ["early", "detach"])
@pytest.mark.parametrize("statuses", [(200,), (200, 200), (200, 200, 503, 503), (200, 503, 200)])
def test_response_diagnostics_preserve_status_changes_without_recounting_requests(capture_resources, timing, statuses):
    fixture = capture_resources

    async def goto(*args, **kwargs):
        fixture.request()
        if timing == "early":
            for status in statuses:
                fixture.response(status)

    async def detach():
        await asyncio.sleep(0)
        if timing == "detach":
            fixture.state.current_action = {"action_id": 99}
            for status in statuses:
                fixture.response(status)

    fixture.page.goto.side_effect = goto
    fixture.client.detach.side_effect = detach
    result = asyncio.run(fixture.capture())

    observed = [record for record in fixture.logs if record["event"] == "request_observed"]
    updated = [record for record in fixture.logs if record["event"] == "request_status_updated"]
    assert len(observed) == 1
    expected_statuses = [status for index, status in enumerate(statuses)
                         if index == 0 or status != statuses[index - 1]]
    if timing == "early":
        assert observed[0]["http_status"] == expected_statuses.pop(0)
    else:
        assert observed[0]["http_status"] is None
    previous = observed[0]["http_status"]
    for record, status in zip(updated, expected_statuses, strict=True):
        assert record["previous_http_status"] == previous
        assert record["http_status"] == status
        previous = status
    for record in observed + updated:
        assert record["request_id"] == "observed"
        assert record["page_id"] == 1 and record["action_id"] == 7
        assert record["route_seen_count"] == 1
    assert len(result.network_events) == 1
    assert result.network_events[0].status == statuses[-1]
    assert result.network_events[0].response_headers == {"generation": str(statuses[-1])}
    assert result.network_events[0].response_mime == "application/json"
    assert sum(fixture.state.request_counts.values()) == 1
    fixture.page.close.assert_awaited_once()


def test_irrelevant_response_still_updates_capture_without_request_diagnostics(capture_resources):
    fixture = capture_resources

    async def goto(*args, **kwargs):
        fixture.request("Image")

    async def detach():
        for status in (200, 200, 503):
            fixture.response(status)

    fixture.page.goto.side_effect = goto
    fixture.client.detach.side_effect = detach
    result = asyncio.run(fixture.capture())
    assert fixture.logs == []
    assert fixture.state.request_counts == {}
    assert len(result.network_events) == 1
    assert result.network_events[0].status == 503


@pytest.mark.parametrize("failed_event", ["request_observed", "request_status_updated"])
def test_failed_diagnostic_emission_is_not_marked_reported(capture_resources, failed_event):
    fixture = capture_resources
    failure = OSError("diagnostic emission failure")
    failed = False

    def emit(name, **record):
        nonlocal failed
        if name == failed_event and not failed:
            failed = True
            raise failure
        fixture.logs.append({"event": name, **record})

    fixture.state.emit = emit

    def responses():
        with pytest.raises(OSError) as raised:
            fixture.response(200)
        assert raised.value is failure
        fixture.response(200)
        fixture.response(200)

    async def goto(*args, **kwargs):
        fixture.request()
        if failed_event == "request_observed":
            responses()

    async def detach():
        if failed_event == "request_status_updated":
            responses()

    fixture.page.goto.side_effect = goto
    fixture.client.detach.side_effect = detach
    result = asyncio.run(fixture.capture())
    observed = [record for record in fixture.logs if record["event"] == "request_observed"]
    updated = [record for record in fixture.logs if record["event"] == "request_status_updated"]
    assert failed
    assert len(observed) == 1
    if failed_event == "request_status_updated":
        assert len(updated) == 1
        assert updated[0]["previous_http_status"] is None
        assert updated[0]["http_status"] == 200
    else:
        assert updated == []
    assert len(result.network_events) == 1 and result.network_events[0].status == 200
    assert sum(fixture.state.request_counts.values()) == 1


@pytest.mark.parametrize("primary", ["none", "error", "cancellation"])
def test_status_update_log_failure_preserves_capture_error_policy(capture_resources, primary):
    fixture = capture_resources
    logging_failure = OSError("private logging failure")
    capture_failure = RuntimeError("primary capture failure")

    def emit(name, **record):
        if name == "request_status_updated":
            raise logging_failure
        fixture.logs.append({"event": name, **record})

    fixture.state.emit = emit

    async def exercise():
        entered = asyncio.Event()

        async def goto(*args, **kwargs):
            fixture.request()
            entered.set()
            if primary == "error":
                raise capture_failure
            if primary == "cancellation":
                await asyncio.Event().wait()

        async def detach():
            fixture.response(200)

        fixture.page.goto.side_effect = goto
        fixture.client.detach.side_effect = detach
        task = asyncio.create_task(fixture.capture())
        await asyncio.wait_for(entered.wait(), timeout=1)
        if primary == "cancellation":
            task.cancel("caller cancellation")
            with pytest.raises(asyncio.CancelledError) as raised:
                await task
            assert task.cancelled()
            assert raised.value.args == ("caller cancellation",)
        else:
            expected = logging_failure if primary == "none" else capture_failure
            with pytest.raises(type(expected)) as raised:
                await task
            assert raised.value is expected
        if primary != "none":
            assert raised.value.__notes__ == ["CDP detach cleanup failed: OSError"]
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    fixture.page.close.assert_awaited_once()


def test_scan_log_counts_status_updates_separately_and_redacts_urls(capture_resources, tmp_path):
    fixture = capture_resources
    trace = ScanLog(str(tmp_path / "local.db"), 1)
    fixture.state.emit = trace.emit

    async def goto(*args, **kwargs):
        fixture.request()

    async def detach():
        for status in (200, 200, 503):
            fixture.response(status)

    fixture.page.goto.side_effect = goto
    fixture.client.detach.side_effect = detach
    try:
        trace.emit("scan_start")
        trace.emit("page_start", page_id=1)
        result = asyncio.run(fixture.capture())
        trace.emit("page_end", page_id=1, status="completed")
        trace.emit("scan_end", totals=dict(trace.totals))
    finally:
        trace.close()
    text = trace.path.read_text()
    records = [json.loads(line) for line in text.splitlines()]
    totals = records[-1]["totals"]
    assert totals["request_observed"] == 1
    assert totals["request_status_updated"] == 2
    assert records[-1]["sequence"] == len(records)
    assert "private-query" not in text
    assert result.network_events[0].status == 503
