import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeoutError

from orbis.config import ScopeConfig
from orbis.crawler import interactor
from orbis.crawler.scope import Scope


class FinalizationInterrupt(BaseException):
    pass


@pytest.fixture
def action_resources(monkeypatch):
    collect = AsyncMock(return_value=[{"kind": "button", "selector": "#tab", "text": "Results tab"}])
    settle = AsyncMock(return_value="quiet")
    monkeypatch.setattr(interactor, "_collect_candidates", collect)
    monkeypatch.setattr(interactor, "_settle", settle)

    def make(*, click_error=None, emit_error=None, failing_event="action_end"):
        events, markers, listeners = [], [], {}
        state = interactor.ExplorationState(page_id=1)
        locator = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                                  is_enabled=AsyncMock(return_value=True),
                                  click=AsyncMock(side_effect=click_error))
        locator.first = locator

        def remove_listener(event, callback):
            assert listeners.pop(event) is callback

        page = SimpleNamespace(
            url="https://example.com/?token=private-query",
            evaluate=AsyncMock(return_value=None), locator=Mock(return_value=locator),
            on=lambda event, callback: listeners.__setitem__(event, callback),
            remove_listener=Mock(side_effect=remove_listener),
        )

        def emit(event, **record):
            events.append({"event": event, **record})
            markers.append((event, None if state.current_action is None else state.current_action["action_id"]))
            if event == failing_event and emit_error is not None:
                raise emit_error

        state.emit = emit

        async def run(observe=None):
            return await interactor.run_safe_interactions(
                page, Scope(ScopeConfig(include_domains=["example.com"])),
                state=state, max_actions=1, observe=observe,
            )

        return SimpleNamespace(run=run, page=page, locator=locator, state=state,
                               events=events, markers=markers, listeners=listeners,
                               collect=collect, settle=settle)

    return make


def assert_finished(fixture):
    assert fixture.state.current_action is None
    assert fixture.markers[:2] == [("action_start", None), ("action_end", 1)]
    assert fixture.listeners == {}
    assert fixture.page.remove_listener.call_count == 3
    fixture.locator.click.assert_awaited_once()
    assert sum(fixture.state.action_counts.values()) == 1
    assert "private-query" not in str([event for event in fixture.events if event["event"] == "action_end"])


@pytest.mark.parametrize("click_fails", [False, True])
def test_normal_action_result_and_terminal_marker_order_are_unchanged(action_resources, click_fails):
    fixture = action_resources(click_error=PlaywrightTimeoutError("private click failure") if click_fails else None)
    assert asyncio.run(fixture.run()) == (0 if click_fails else 1)
    assert_finished(fixture)
    record = fixture.state.records[0]
    assert record["status"] == ("failed" if click_fails else "executed")
    assert record.get("failure_reason") == ("action_timeout" if click_fails else None)
    assert fixture.markers == [("action_start", None), ("action_end", 1), ("exploration_end", None)]
    assert fixture.collect.await_count == 2


@pytest.mark.parametrize("error_type", [OSError, FinalizationInterrupt])
def test_action_end_failure_stops_progress_and_clears_marker(action_resources, error_type):
    failure = error_type("private terminal failure")
    fixture = action_resources(emit_error=failure)
    with pytest.raises(error_type) as raised:
        asyncio.run(fixture.run())
    assert raised.value is failure
    assert_finished(fixture)
    assert [record["event"] for record in fixture.events] == ["action_start", "action_end"]


@pytest.mark.parametrize("body_error_type", [OSError, PlaywrightError, FinalizationInterrupt])
@pytest.mark.parametrize("emit_error_type", [None, OSError, FinalizationInterrupt])
def test_unhandled_body_error_keeps_identity_unless_new_interruption(action_resources, body_error_type, emit_error_type):
    primary = body_error_type("private body failure")
    terminal = emit_error_type("private terminal failure") if emit_error_type else None
    fixture = action_resources(emit_error=terminal)

    async def observe():
        raise primary

    expected = terminal if emit_error_type is FinalizationInterrupt else primary
    with pytest.raises(type(expected)) as raised:
        asyncio.run(fixture.run(observe))
    assert raised.value is expected
    assert_finished(fixture)
    notes = ["Action finalization failed: OSError"] if emit_error_type is OSError else []
    assert getattr(raised.value, "__notes__", []) == notes
    if isinstance(primary, Exception):
        assert fixture.state.records[0]["status"] == "error"
        assert fixture.state.records[0]["failure_reason"] == type(primary).__name__


@pytest.mark.parametrize("location", ["click", "observe", "observe_after_timeout"])
@pytest.mark.parametrize("emit_error_type", [None, OSError, FinalizationInterrupt])
def test_actual_task_cancellation_preserves_identity_and_final_reason(action_resources, location, emit_error_type):
    terminal = emit_error_type("private terminal failure") if emit_error_type else None
    fixture = action_resources(
        click_error=PlaywrightTimeoutError("private click failure") if location == "observe_after_timeout" else None,
        emit_error=terminal,
    )

    async def exercise():
        entered = asyncio.Event()
        cancellations = []

        async def blocked(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                cancellations.append(exc)
                raise

        if location == "click":
            fixture.locator.click.side_effect = blocked
            observe = None
        else:
            observe = blocked
        task = asyncio.create_task(fixture.run(observe))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel("original caller cancellation")
        expected_type = FinalizationInterrupt if emit_error_type is FinalizationInterrupt else asyncio.CancelledError
        with pytest.raises(expected_type) as raised:
            await task
        if emit_error_type is FinalizationInterrupt:
            assert raised.value is terminal and not task.cancelled()
        else:
            assert raised.value is cancellations[0]
            assert raised.value.args == ("original caller cancellation",)
            assert task.cancelled()
        notes = ["Action finalization failed: OSError"] if emit_error_type is OSError else []
        assert getattr(raised.value, "__notes__", []) == notes
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    assert_finished(fixture)
    assert fixture.state.records[0]["status"] == "interrupted"
    assert fixture.state.records[0]["failure_reason"] == "cancelled"
    assert fixture.events[-1]["failure_reason"] == "cancelled"


@pytest.mark.parametrize("click_fails", [False, True])
@pytest.mark.parametrize("emit_fails", [False, True])
def test_timeout_at_retains_deadline_conversion_and_final_reason(action_resources, click_fails, emit_fails):
    fixture = action_resources(
        click_error=PlaywrightTimeoutError("private click failure") if click_fails else None,
        emit_error=OSError("private terminal failure") if emit_fails else None,
    )

    async def exercise():
        cancellations = []

        async def observe():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                cancellations.append(exc)
                raise

        timeout = asyncio.timeout_at(asyncio.get_running_loop().time() + 0.02)
        with pytest.raises(TimeoutError) as raised:
            async with timeout:
                await fixture.run(observe)
        assert timeout.expired()
        assert raised.value.__cause__ is cancellations[0]
        notes = ["Action finalization failed: OSError"] if emit_fails else []
        assert getattr(cancellations[0], "__notes__", []) == notes
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    assert_finished(fixture)
    assert fixture.state.records[0]["status"] == "interrupted"
    assert fixture.state.records[0]["failure_reason"] == "cancelled"


@pytest.mark.parametrize("emit_fails", [False, True])
def test_prior_click_failure_does_not_overwrite_observation_error(action_resources, emit_fails):
    primary = OSError("private observation failure")
    fixture = action_resources(click_error=PlaywrightTimeoutError("private click failure"),
                               emit_error=OSError("private terminal failure") if emit_fails else None)

    async def observe():
        raise primary

    with pytest.raises(OSError) as raised:
        asyncio.run(fixture.run(observe))
    assert raised.value is primary
    assert_finished(fixture)
    assert fixture.state.records[0]["status"] == "error"
    assert fixture.state.records[0]["failure_reason"] == "OSError"
    assert fixture.events[-1]["failure_reason"] == "OSError"
    assert getattr(primary, "__notes__", []) == (["Action finalization failed: OSError"] if emit_fails else [])


@pytest.mark.parametrize("event", ["action_start", "exploration_end"])
def test_other_diagnostic_failures_keep_existing_propagation(action_resources, event):
    failure = OSError("private diagnostic failure")
    fixture = action_resources(emit_error=failure, failing_event=event)
    with pytest.raises(OSError) as raised:
        asyncio.run(fixture.run())
    assert raised.value is failure
    assert fixture.state.current_action is None
    assert fixture.locator.click.await_count == (0 if event == "action_start" else 1)
