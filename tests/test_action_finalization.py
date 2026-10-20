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
