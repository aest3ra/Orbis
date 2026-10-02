import asyncio
import json

import pytest

from orbis.config import LLMConfig
from orbis.crawler.planner import QwenPlanner, load_api_key


class Response:
    def __init__(self, status, content):
        self.status = status
        self.content = content
        self.disposed = False

    async def json(self):
        return {"choices": [{"message": {"content": self.content}}], "usage": {"total_tokens": 20}}

    async def dispose(self):
        self.disposed = True


class Request:
    def __init__(self, responses):
        self.responses = responses
        self.sent = []

    async def post(self, url, **kwargs):
        self.sent.append((url, kwargs))
        return self.responses[len(self.sent) - 1]


CANDIDATES = [{"kind": "input", "label": "Title", "type": "text", "value": "private existing value",
               "selector": "#private-selector", "nav_url": "https://user:password@example.com/page?token=secret"}]


def test_planner_sends_only_reduced_page_data():
    response = Response(200, '{"candidate_id": 0, "value": "Example title"}')
    request = Request([response])
    planner = QwenPlanner(request, LLMConfig())
    result = asyncio.run(planner.choose("https://user:password@example.com/page?token=secret", CANDIDATES, []))
    assert result.value == "Example title"
    payload = json.dumps(request.sent[0][1]["data"])
    for private in ("password", "token=secret", "private existing value", "#private-selector"):
        assert private not in payload
    assert response.disposed
    assert planner.records[0]["usage"]["total_tokens"] == 20


@pytest.mark.parametrize("content", [
    '{"candidate_id": 999}', '{"candidate_id": "0"}', '{"candidate_id": -1}',
    '{"candidate_id": 0, "script": "fetch(\'/delete\')"}', 'not json',
])
def test_invalid_model_response_cannot_create_an_action(content):
    response = Response(200, content)
    planner = QwenPlanner(Request([response]), LLMConfig())
    assert asyncio.run(planner.choose("https://example.com", CANDIDATES, [])) is None
    assert planner.records[0]["error"]
    assert response.disposed


def test_rate_limit_retry_and_total_call_budget_are_bounded(monkeypatch):
    async def no_wait(_seconds):
        return None

    monkeypatch.setattr("orbis.crawler.planner.asyncio.sleep", no_wait)
    responses = [Response(429, ""), Response(200, '{"candidate_id": 0}')]
    request = Request(responses)
    planner = QwenPlanner(request, LLMConfig(max_calls=2))
    assert asyncio.run(planner.choose("https://example.com", CANDIDATES, [])).candidate_id == 0
    assert asyncio.run(planner.choose("https://example.com", CANDIDATES, [])) is None
    assert len(request.sent) == 2
    assert all(response.disposed for response in responses)


def test_api_failure_falls_back_without_retrying_authentication_errors():
    response = Response(401, "")
    request = Request([response])
    planner = QwenPlanner(request, LLMConfig())
    assert asyncio.run(planner.choose("https://example.com", CANDIDATES, [])) is None
    assert len(request.sent) == 1
    assert planner.records[0]["error"] == "api_response"


def test_key_can_be_read_from_ignored_local_file(monkeypatch, tmp_path):
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text('NVIDIA_API_KEY="local-fixture-key"\n')
    assert load_api_key() == "local-fixture-key"
    monkeypatch.setenv("NVIDIA_API_KEY", "environment-fixture-key")
    assert load_api_key() == "environment-fixture-key"
