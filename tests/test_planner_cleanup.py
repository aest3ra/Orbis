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
