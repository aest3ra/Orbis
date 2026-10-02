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


@pytest.mark.parametrize("event", ["request_observed", "request_loading_failed"])
@pytest.mark.parametrize("phase", ["goto", "dom", "detach", "close"])
@pytest.mark.parametrize("fault_stage", ["write", "flush"])
def test_actual_dispatch_diagnostic_failure_is_retained_without_reemission(callback_capture, event, phase, fault_stage):
    state = callback_capture
    state.phase, state.event, state.fault_stage = phase, event, fault_stage
    if phase in {"detach", "close"} and event == "request_observed":
        state.event = "request_status_updated"
    state.execute()

    assert state.raised is state.failure
    assert state.result is None
    assert state.attempts.count(state.event) == 1
    assert state.connection_error is None
    assert state.closed == state.detached == 1
    assert state.raw[-1]["status"] == 503
    assert sum(state.request_counts.values()) == 1
    if phase in {"goto", "dom"}:
        assert state.evaluations == (phase == "dom")
    assert len([row for row in state.rows if row["event"] == state.event]) == (fault_stage == "flush")


@pytest.mark.parametrize("error_type", [TimeoutError, PlaywrightError])
@pytest.mark.parametrize("phase", ["goto", "detach", "close"])
def test_diagnostic_error_types_are_not_handled_as_capture_errors(callback_capture, error_type, phase):
    state = callback_capture
    state.phase = phase
    state.event = "request_observed" if phase == "goto" else "request_status_updated"
    state.failure = error_type("private diagnostic failure")
    state.execute()
    assert state.raised is state.failure
    assert state.result is None
    assert state.connection_error is None


@pytest.mark.parametrize("primary", ["error", "cancelled"])
@pytest.mark.parametrize("phase", ["goto", "detach", "close"])
def test_original_capture_failure_precedes_ordinary_callback_diagnostic(callback_capture, primary, phase):
    state = callback_capture
    state.phase, state.primary_mode = phase, primary
    state.event = "request_observed" if phase == "goto" else "request_status_updated"
    state.execute()
    assert state.raised is state.primary
    assert state.raised.__notes__ == ["HTTP diagnostic failed: OSError"]
    assert state.cancelled == (primary == "cancelled")
    if primary == "cancelled":
        assert state.raised.args == ("caller cancellation",)
    assert state.closed == state.detached == 1


@pytest.mark.parametrize("stage", ["detach", "close"])
def test_already_propagated_callback_interruption_does_not_replace_new_cleanup_interruption(callback_capture, stage):
    class Interruption(BaseException):
        pass

    state = callback_capture
    state.failure = Interruption("earlier diagnostic interruption")
    state.cleanup_stage = stage
    state.cleanup_failure = Interruption("new cleanup interruption")
    state.execute()
    assert state.raised is state.cleanup_failure
    assert state.raised.__notes__ == ["HTTP diagnostic failed: Interruption"]
    assert state.closed == state.detached == 1


@pytest.mark.parametrize("phase,stage", [("detach", "detach"), ("detach", "close"), ("close", "close")])
@pytest.mark.parametrize("interrupt", [False, True])
def test_callback_failure_precedes_ordinary_cleanup_but_yields_to_new_interruption(callback_capture, phase, stage, interrupt):
    class Interruption(BaseException):
        pass

    state = callback_capture
    state.phase, state.event = phase, "request_status_updated"
    state.failure = Interruption("earlier diagnostic") if interrupt else OSError("earlier diagnostic")
    state.cleanup_stage = stage
    state.cleanup_failure = Interruption("new cleanup") if interrupt else RuntimeError("new cleanup")
    state.execute()
    if interrupt:
        assert state.raised is state.cleanup_failure
        assert state.raised.__notes__ == ["HTTP diagnostic failed: Interruption"]
    else:
        assert state.raised is state.failure
        label = "CDP detach" if stage == "detach" else "Page close"
        assert state.raised.__notes__ == [f"{label} cleanup failed: RuntimeError"]
    assert state.closed == state.detached == 1
    assert state.connection_error is None


def test_new_caller_cancellation_precedes_earlier_callback_interruption(callback_capture):
    class Interruption(BaseException):
        pass

    state = callback_capture
    state.failure = Interruption("earlier diagnostic interruption")
    state.primary_mode = "cancelled"
    state.execute()
    assert state.raised is state.primary
    assert state.cancelled
    assert state.raised.args == ("caller cancellation",)
    assert state.raised.__notes__ == ["HTTP diagnostic failed: Interruption"]


def test_new_callback_interruption_precedes_earlier_cleanup_interruption(callback_capture):
    class Interruption(BaseException):
        pass

    state = callback_capture
    state.phase, state.event = "close", "request_status_updated"
    state.failure = Interruption("new diagnostic interruption")
    state.cleanup_stage = "detach"
    state.cleanup_failure = Interruption("earlier cleanup interruption")
    state.execute()
    assert state.raised is state.failure
    assert state.closed == state.detached == 1
    assert state.connection_error is None


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


@pytest.mark.parametrize("boundary", ["candidates", "planner", "pagination"])
def test_known_diagnostic_failure_stops_next_interaction(monkeypatch, boundary):
    failure = OSError("diagnostic failure")
    broken = False
    candidate = {"kind": "button", "selector": "#more", "label": "Load more", "text": "Load more"}
    calls = []

    def check():
        if broken:
            raise failure

    async def candidates(page):
        nonlocal broken
        calls.append("candidates")
        broken = boundary == "candidates"
        return [candidate]

    async def choose(*args):
        nonlocal broken
        calls.append("planner")
        broken = True
        return None

    async def perform(*args, **kwargs):
        nonlocal broken
        calls.append("action")
        broken = True
        return True

    monkeypatch.setattr(interactor, "_collect_candidates", candidates)
    monkeypatch.setattr(interactor, "_perform_and_settle", perform)
    page = SimpleNamespace(url="http://localhost/")
    scope = Scope(ScanConfig(target=page.url).scope)
    tracker = SimpleNamespace(begin=lambda: None, commit=lambda *args: 1)
    planner = SimpleNamespace(config=SimpleNamespace(timeout_sec=1), choose=choose) if boundary != "pagination" else None
    with pytest.raises(OSError) as raised:
        asyncio.run(interactor.run_safe_interactions(
            page, scope, tracker=tracker, planner=planner, check_diagnostics=check,
        ))
    assert raised.value is failure
    assert calls == (["candidates"] if boundary == "candidates" else
                     ["candidates", "planner"] if boundary == "planner" else ["candidates", "action"])


@pytest.mark.parametrize("mode", ["diagnostic", "diagnostic_playwright", "cancelled", "handled_playwright", "success"])
@pytest.mark.parametrize("cleanup_type", [OSError, PlaywrightError, BaseException])
def test_observer_disconnect_preserves_primary_and_new_interruptions(monkeypatch, mode, cleanup_type):
    cleanup = cleanup_type("private observer cleanup failure")
    primary = PlaywrightError("private diagnostic failure") if mode == "diagnostic_playwright" else OSError("private diagnostic failure")
    listeners = {}
    entered = None
    seen_primary = None
    cleanup_calls = 0

    async def evaluate(script):
        nonlocal cleanup_calls, seen_primary
        if script == "() => window.__orbisInteractionActivity?.observer.disconnect()":
            cleanup_calls += 1
            raise cleanup
        if mode == "cancelled":
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                seen_primary = exc
                raise
        if mode == "handled_playwright":
            raise PlaywrightError("handled browser failure")

    def check():
        if mode.startswith("diagnostic"):
            raise primary

    page = SimpleNamespace(evaluate=evaluate, on=lambda event, callback: listeners.__setitem__(event, callback),
                           remove_listener=lambda event, callback: listeners.pop(event))
    monkeypatch.setattr(interactor, "_perform", AsyncMock(return_value=True))
    monkeypatch.setattr(interactor, "_settle", AsyncMock(return_value="quiet"))

    async def exercise():
        nonlocal entered
        entered = asyncio.Event()
        task = asyncio.create_task(interactor._perform_and_settle(page, {}, check_diagnostics=check))
        if mode == "cancelled":
            await entered.wait()
            task.cancel("caller cancellation")
        error = None
        result = None
        try:
            result = await task
        except BaseException as exc:
            error = exc
        if cleanup_type is BaseException:
            assert error is cleanup
        elif mode.startswith("diagnostic") or mode == "cancelled":
            assert error is (seen_primary if mode == "cancelled" else primary)
            assert task.cancelled() == (mode == "cancelled")
            if mode == "cancelled":
                assert error.args == ("caller cancellation",)
            if cleanup_type is OSError:
                assert error.__notes__ == ["Observer disconnect cleanup failed: OSError"]
            else:
                assert not getattr(error, "__notes__", [])
        elif cleanup_type is OSError:
            assert error is cleanup
        else:
            assert error is None
            assert result == (mode == "success")
        assert not [item for item in asyncio.all_tasks() if item is not asyncio.current_task()]

    asyncio.run(exercise())
    assert not listeners
    assert cleanup_calls == 1


@pytest.mark.parametrize("diagnostic_type", [OSError, PlaywrightError, BaseException])
@pytest.mark.parametrize("cleanup_type", [RuntimeError, BaseException])
def test_diagnostic_first_observed_during_disconnect_preserves_error_order(monkeypatch, diagnostic_type, cleanup_type):
    diagnostic = diagnostic_type("diagnostic before disconnect failure")
    cleanup = cleanup_type("later disconnect failure")
    broken = False

    async def evaluate(script):
        nonlocal broken
        if script == "() => window.__orbisInteractionActivity?.observer.disconnect()":
            broken = True
            raise cleanup

    def check():
        if broken:
            raise diagnostic

    page = SimpleNamespace(evaluate=evaluate, on=lambda *args: None, remove_listener=lambda *args: None)
    monkeypatch.setattr(interactor, "_perform", AsyncMock(return_value=True))
    monkeypatch.setattr(interactor, "_settle", AsyncMock(return_value="quiet"))
    with pytest.raises(BaseException) as raised:
        asyncio.run(interactor._perform_and_settle(page, {}, check_diagnostics=check))
    if cleanup_type is BaseException:
        assert raised.value is cleanup
    else:
        assert raised.value is diagnostic
        assert raised.value.__notes__ == ["Observer disconnect cleanup failed: RuntimeError"]
