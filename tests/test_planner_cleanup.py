import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Error as PlaywrightError

from orbis.config import LLMConfig
from orbis.crawler import planner as planner_module


class CleanupInterrupt(BaseException):
    pass


@pytest.fixture
def planner_resources(monkeypatch):
    sleep = AsyncMock()
    monkeypatch.setattr(planner_module.asyncio, "sleep", sleep)

    def make(*, status=200, content='{"candidate_id": 0}', body_error=None,
             end_error=None, dispose_error=None, max_calls=2):
        events, order = [], []

        async def body():
            if body_error is not None:
                raise body_error
            return {"choices": [{"message": {"content": content}}]}

        async def dispose():
            order.append("dispose")
            if dispose_error is not None:
                raise dispose_error

        response = SimpleNamespace(status=status, json=AsyncMock(side_effect=body),
                                   dispose=AsyncMock(side_effect=dispose))
        request = SimpleNamespace(post=AsyncMock(return_value=response))

        def emit(event, **record):
            events.append({"event": event, **record})
            order.append(event)
            if event == "llm_end" and end_error is not None:
                raise end_error

        planner = planner_module.QwenPlanner(
            request, LLMConfig(base_url="http://offline.invalid", max_calls=max_calls), emit=emit,
        )

        async def choose():
            return await planner.choose("http://fixture.invalid/", [{"kind": "link"}], [])

        return SimpleNamespace(planner=planner, response=response, request=request,
                               events=events, order=order, choose=choose, sleep=sleep)

    return make


@pytest.mark.parametrize("condition", ["selected", "api_error", "invalid_json", "body_error"])
def test_normal_result_and_owned_response_cleanup_are_unchanged(planner_resources, condition):
    fixture = planner_resources(
        status=401 if condition == "api_error" else 200,
        content="invalid" if condition == "invalid_json" else '{"candidate_id": 0}',
        body_error=PlaywrightError("handled body failure") if condition == "body_error" else None,
    )
    result = asyncio.run(fixture.choose())
    if condition == "selected":
        assert result.candidate_id == 0
    else:
        assert result is None
    fixture.request.post.assert_awaited_once()
    fixture.response.dispose.assert_awaited_once()
    fixture.sleep.assert_not_awaited()
    assert fixture.order == ["llm_start", "llm_end", "dispose"]


@pytest.mark.parametrize("status", [200, 401, 429])
@pytest.mark.parametrize("dispose_fails", [False, True])
def test_terminal_emit_failure_still_disposes_and_does_not_allow_progress(planner_resources, status, dispose_fails):
    failure = OSError("private terminal failure")
    cleanup = PlaywrightError("private cleanup failure") if dispose_fails else None
    fixture = planner_resources(status=status, end_error=failure, dispose_error=cleanup)
    expected = cleanup if status == 429 and dispose_fails else failure
    with pytest.raises(type(expected)) as raised:
        asyncio.run(fixture.choose())
    assert raised.value is expected
    fixture.request.post.assert_awaited_once()
    fixture.response.dispose.assert_awaited_once()
    if status == 429 and dispose_fails:
        assert raised.value.__notes__ == ["LLM finalization failed: OSError"]
        fixture.sleep.assert_not_awaited()
    elif dispose_fails:
        assert raised.value.__notes__ == ["LLM response cleanup failed: Error"]
    else:
        assert getattr(raised.value, "__notes__", []) == []


@pytest.mark.parametrize("condition", ["selected", "api_error", "invalid_json", "body_error"])
@pytest.mark.parametrize("stage", ["emit", "dispose"])
def test_cleanup_failure_cannot_become_success_or_handled_body_fallback(planner_resources, condition, stage):
    failure = PlaywrightError("private cleanup failure")
    fixture = planner_resources(
        status=401 if condition == "api_error" else 200,
        content="invalid" if condition == "invalid_json" else '{"candidate_id": 0}',
        body_error=PlaywrightError("handled body failure") if condition == "body_error" else None,
        end_error=failure if stage == "emit" else None,
        dispose_error=failure if stage == "dispose" else None,
    )
    with pytest.raises(PlaywrightError) as raised:
        asyncio.run(fixture.choose())
    assert raised.value is failure
    fixture.response.dispose.assert_awaited_once()
    fixture.request.post.assert_awaited_once()


@pytest.mark.parametrize("end_fails,dispose_fails", [(False, False), (True, False), (False, True), (True, True)])
def test_unexpected_body_failure_keeps_original_identity(planner_resources, end_fails, dispose_fails):
    failure = RuntimeError("private primary failure")
    fixture = planner_resources(
        body_error=failure, end_error=OSError("private terminal failure") if end_fails else None,
        dispose_error=PlaywrightError("private cleanup failure") if dispose_fails else None,
    )
    with pytest.raises(RuntimeError) as raised:
        asyncio.run(fixture.choose())
    assert raised.value is failure
    expected_notes = (["LLM finalization failed: OSError"] if end_fails else [])
    expected_notes += ["LLM response cleanup failed: Error"] if dispose_fails else []
    assert getattr(failure, "__notes__", []) == expected_notes
    fixture.request.post.assert_awaited_once()
    fixture.response.dispose.assert_awaited_once()


@pytest.mark.parametrize("end_fails,dispose_fails", [(False, False), (True, False), (False, True), (True, True)])
def test_actual_task_cancel_preserves_message_and_identity(planner_resources, end_fails, dispose_fails):
    fixture = planner_resources(
        end_error=OSError("private terminal failure") if end_fails else None,
        dispose_error=PlaywrightError("private cleanup failure") if dispose_fails else None,
    )

    async def exercise():
        entered = asyncio.Event()
        cancellation = []

        async def blocked_body():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                cancellation.append(exc)
                raise

        fixture.response.json.side_effect = blocked_body
        task = asyncio.create_task(fixture.choose())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel("original cancellation")
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert task.cancelled()
        assert raised.value is cancellation[0]
        assert raised.value.args == ("original cancellation",)
        expected_notes = (["LLM finalization failed: OSError"] if end_fails else [])
        expected_notes += ["LLM response cleanup failed: Error"] if dispose_fails else []
        assert getattr(raised.value, "__notes__", []) == expected_notes
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    fixture.request.post.assert_awaited_once()
    fixture.response.dispose.assert_awaited_once()
    assert fixture.planner.records[0]["outcome"] == "interrupted"


@pytest.mark.parametrize("error_type", [PlaywrightError, RuntimeError, CleanupInterrupt])
@pytest.mark.parametrize("end_fails", [False, True])
def test_retry_disposal_failure_is_not_repeated_or_swallowed(planner_resources, error_type, end_fails):
    failure = error_type("private retry cleanup failure")
    fixture = planner_resources(status=429, dispose_error=failure,
                                end_error=OSError("private terminal failure") if end_fails else None)
    with pytest.raises(error_type) as raised:
        asyncio.run(fixture.choose())
    assert raised.value is failure
    fixture.response.dispose.assert_awaited_once()
    fixture.request.post.assert_awaited_once()
    fixture.sleep.assert_not_awaited()
    assert getattr(failure, "__notes__", []) == (["LLM finalization failed: OSError"] if end_fails else [])


@pytest.mark.parametrize("end_fails", [False, True])
def test_actual_cancel_during_retry_disposal_is_not_disposed_again(planner_resources, end_fails):
    fixture = planner_resources(status=429, end_error=OSError("private terminal failure") if end_fails else None)

    async def exercise():
        entered = asyncio.Event()
        cancellation = []

        async def blocked_dispose():
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError as exc:
                cancellation.append(exc)
                raise

        fixture.response.dispose.side_effect = blocked_dispose
        task = asyncio.create_task(fixture.choose())
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel("retry cancellation")
        # A duplicate dispose would block again; make that call fail promptly.
        fixture.response.dispose.side_effect = RuntimeError("duplicate dispose")
        with pytest.raises(asyncio.CancelledError) as raised:
            await task
        assert raised.value is cancellation[0] and task.cancelled()
        assert raised.value.args == ("retry cancellation",)
        assert getattr(raised.value, "__notes__", []) == (["LLM finalization failed: OSError"] if end_fails else [])
        assert [task for task in asyncio.all_tasks() if task is not asyncio.current_task()] == []

    asyncio.run(exercise())
    fixture.response.dispose.assert_awaited_once()
    fixture.request.post.assert_awaited_once()
    fixture.sleep.assert_not_awaited()


@pytest.mark.parametrize("has_primary", [False, True])
@pytest.mark.parametrize("stage", ["emit", "dispose", "dispose_after_emit_failure"])
def test_new_cleanup_baseexception_takes_precedence(planner_resources, has_primary, stage):
    interrupt = CleanupInterrupt("private new interruption")
    end_error = interrupt if stage == "emit" else OSError("private terminal failure") if stage == "dispose_after_emit_failure" else None
    dispose_error = PlaywrightError("private cleanup failure") if stage == "emit" else interrupt
    fixture = planner_resources(body_error=RuntimeError("private primary failure") if has_primary else None,
                                end_error=end_error, dispose_error=dispose_error)
    with pytest.raises(CleanupInterrupt) as raised:
        asyncio.run(fixture.choose())
    assert raised.value is interrupt
    fixture.response.dispose.assert_awaited_once()
    fixture.request.post.assert_awaited_once()
    assert getattr(interrupt, "__notes__", []) == (["LLM response cleanup failed: Error"] if stage == "emit" else [])


@pytest.mark.parametrize("max_calls", [1, 2])
def test_normal_retry_request_count_and_budget_are_unchanged(planner_resources, max_calls):
    fixture = planner_resources(status=429, max_calls=max_calls)
    second = SimpleNamespace(status=200, json=AsyncMock(return_value={"choices": [{"message": {"content": '{"candidate_id": 0}'}}]}),
                             dispose=AsyncMock())
    fixture.request.post.side_effect = [fixture.response, second]
    result = asyncio.run(fixture.choose())
    if max_calls == 1:
        assert result is None
        second.dispose.assert_not_awaited()
    else:
        assert result.candidate_id == 0
        second.dispose.assert_awaited_once()
    assert fixture.request.post.await_count == max_calls
    assert fixture.planner.calls == max_calls
    fixture.response.dispose.assert_awaited_once()
    fixture.sleep.assert_awaited_once_with(1)
    assert [row["event"] for row in fixture.events] == ["llm_start", "llm_end"] * max_calls
