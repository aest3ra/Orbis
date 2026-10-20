import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError
from playwright._impl._cdp_session import CDPSession
from playwright._impl._connection import ChannelOwner, Connection

from orbis.config import ScanConfig
from orbis.crawler import browser, interactor
from orbis.crawler.interactor import ExplorationState
from orbis.crawler.scope import Scope
from orbis.telemetry import ScanLog


@pytest.fixture
def callback_capture(tmp_path):
    state = SimpleNamespace(
        phase="goto", event="request_observed", fault_stage="write", failure=OSError("private diagnostic fault"),
        primary_mode=None, primary=None, raised=None, result=None, attempts=[], messages=[], raw=[],
        evaluations=0, closed=0, detached=0, trace=None,
        cleanup_stage=None, cleanup_failure=None,
    )

    async def execute():
        loop = asyncio.get_running_loop()
        transport = SimpleNamespace(on_error_future=loop.create_future())
        connection = Connection(None, lambda *args: None, transport, loop)
        page_channel = ChannelOwner(connection, "Page", "offline-page", {})
        client = CDPSession(connection, "CDPSession", "offline-cdp", {})
        original_detach = client.detach

        async def detach():
            await original_detach()
            if state.cleanup_stage == "detach":
                raise state.cleanup_failure

        client.detach = detach
        trace = ScanLog(str(tmp_path / "scan.db"), 1)
        state.trace = trace
        original_file = trace._file
        original_emit = trace.emit
        injected = False
        flush_failure = False

        class File:
            def write(self, data):
                nonlocal injected, flush_failure
                if json.loads(data)["event"] == state.event and state.failure is not None and not injected:
                    injected = True
                    if state.fault_stage == "write":
                        raise state.failure
                    flush_failure = True
                return original_file.write(data)

            def flush(self):
                nonlocal flush_failure
                if flush_failure:
                    flush_failure = False
                    original_file.flush()
                    raise state.failure
                return original_file.flush()

            def close(self):
                original_file.close()

        def emit(name, **record):
            state.attempts.append(name)
            return original_emit(name, **record)

        trace._file = File()
        exploration = ExplorationState(page_id=1, emit=emit)

        def dispatch(method, params):
            # Exercise Playwright's real event/error delivery, without starting its transport.
            connection.dispatch({"guid": "offline-cdp", "method": "event",
                                 "params": {"method": method, "params": params}})

        def responses():
            if state.event == "request_loading_failed":
                for _ in range(2):
                    dispatch("Network.loadingFailed", {"requestId": "observed", "errorText": "net::ERR_FAILED"})
            for status in (200, 200, 503):
                dispatch("Network.responseReceived", {"requestId": "observed", "response": {
                    "status": status, "headers": {"generation": str(status)}, "mimeType": "application/json",
                }})

        def send(message):
            state.messages.append(message["method"])
            if message["method"] == "detach":
                state.detached += 1
                if state.phase == "detach":
                    responses()
            result = [] if message["method"] == "evaluate" else {}
            loop.call_soon(connection.dispatch, {"id": message["id"], "result": {"value": result}})

        transport.send = send
        entered = asyncio.Event()

        async def goto(*args, **kwargs):
            dispatch("Network.requestWillBeSent", {"requestId": "observed", "type": "Fetch",
                     "request": {"method": "GET", "url": "http://localhost/data?token=private-query"}})
            if state.phase == "goto":
                responses()
            entered.set()
            if state.primary_mode == "error":
                state.primary = RuntimeError("private primary failure")
                raise state.primary
            if state.primary_mode == "cancelled":
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError as exc:
                    state.primary = exc
                    raise

        async def evaluate(expression):
            state.evaluations += 1
            result = await page_channel._channel.send("evaluate", None, {"expression": expression})
            if state.phase == "dom" and state.evaluations == 1:
                responses()
            return result

        async def close():
            state.closed += 1
            if state.phase == "close":
                responses()
            if state.cleanup_stage == "close":
                raise state.cleanup_failure

        page = SimpleNamespace(url="http://localhost/", add_init_script=AsyncMock(), on=lambda *args: None,
                               goto=goto, evaluate=evaluate, close=close)
        context = SimpleNamespace(new_page=AsyncMock(return_value=page),
                                  new_cdp_session=AsyncMock(return_value=client))
        client.on("Network.responseReceived", lambda record: state.raw.append(record["response"]))
        task = asyncio.create_task(browser.capture_page(
            context, page.url, exploration=exploration, max_scrolls=0, settle_ms=0,
        ))
        try:
            if state.primary_mode == "cancelled":
                await entered.wait()
                task.cancel("caller cancellation")
            try:
                state.result = await task
            except BaseException as exc:
                state.raised = exc
            state.cancelled = task.cancelled()
            state.connection_error = connection._error
            state.request_counts = exploration.request_counts
        finally:
            trace.close()
            connection.playwright_future.cancel()
            transport.on_error_future.cancel()
        state.rows = [json.loads(line) for line in trace.path.read_text().splitlines()]
        assert not [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]

    state.execute = lambda: asyncio.run(execute())
    return state


@pytest.mark.parametrize("phase", ["goto", "dom", "detach", "close"])
def test_successful_dispatch_preserves_observations_and_request_order(callback_capture, phase):
    state = callback_capture
    state.phase, state.failure = phase, None
    state.execute()
    assert state.raised is None
    assert state.result.error is None
    assert state.result.network_events[0].status == 503
    assert state.result.network_events[0].response_headers == {"generation": "503"}
    assert state.evaluations == 3
    assert state.messages == ["send", "evaluate", "evaluate", "evaluate", "detach"]
    assert state.closed == state.detached == 1
    assert sum(state.request_counts.values()) == 1
