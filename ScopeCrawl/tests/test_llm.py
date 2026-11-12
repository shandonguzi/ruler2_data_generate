from __future__ import annotations

import json

import httpx2
import openai
import pytest

from scopecrawl.llm import LLMClient, LLMError
from scopecrawl.models import LLMSettings

BASE_URL = "https://llm.example/v1/"
MESSAGES = [{"role": "user", "content": "hello"}]


def _completion(content: str) -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "created": 0,
        "model": "test-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


def _client(sleep=None, responses=None, calls=None) -> LLMClient:
    queue = list(responses or [])
    recorded = calls if calls is not None else []

    def handler(request: httpx2.Request) -> httpx2.Response:
        recorded.append(json.loads(request.content))
        if queue:
            item = queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return httpx2.Response(200, json=_completion("ok"))

    openai_client = openai.OpenAI(
        api_key="test-key",
        base_url=BASE_URL,
        max_retries=0,
        http_client=httpx2.Client(transport=httpx2.MockTransport(handler)),
    )
    return LLMClient(
        LLMSettings(max_retries=2, timeout_seconds=5),
        model="test-model",
        api_key="test-key",
        client=openai_client,
        sleep=sleep or (lambda _: None),
    )


def test_complete_sends_no_generation_limits():
    calls: list[dict] = []
    client = _client(responses=[httpx2.Response(200, json=_completion('["a"]'))], calls=calls)
    assert client.complete(MESSAGES) == '["a"]'

    body = calls[0]
    assert body["model"] == "test-model"
    assert body["messages"] == MESSAGES
    assert "temperature" not in body
    assert "max_tokens" not in body


def test_complete_retries_rate_limit():
    delays: list[float] = []
    client = _client(
        sleep=delays.append,
        responses=[
            httpx2.Response(429, json={"error": {"message": "slow down"}}),
            httpx2.Response(200, json=_completion("ok")),
        ],
    )
    assert client.complete(MESSAGES) == "ok"
    assert delays == [1]


def test_complete_raises_after_retry_budget():
    client = _client(
        responses=[
            httpx2.Response(429, json={"error": {"message": "slow down"}}),
            httpx2.Response(429, json={"error": {"message": "slow down"}}),
            httpx2.Response(429, json={"error": {"message": "slow down"}}),
        ]
    )
    with pytest.raises(LLMError):
        client.complete(MESSAGES)


def test_json_mode_downgrades_on_400():
    calls: list[dict] = []
    client = _client(
        calls=calls,
        responses=[
            httpx2.Response(
                400,
                json={
                    "error": {
                        "message": "response_format unsupported",
                        "type": "invalid_request_error",
                        "param": None,
                        "code": None,
                    }
                },
            ),
            httpx2.Response(200, json=_completion('{"ok": true}')),
        ],
    )
    assert client.complete(MESSAGES, json_mode=True) == '{"ok": true}'
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in calls[1]


def test_non_retryable_status_raises_immediately():
    calls: list[dict] = []
    client = _client(
        calls=calls,
        responses=[
            httpx2.Response(
                401,
                json={"error": {"message": "bad key", "type": "auth_error"}},
            )
        ],
    )
    with pytest.raises(LLMError):
        client.complete(MESSAGES)
    assert len(calls) == 1


def test_missing_api_key_raises():
    with pytest.raises(LLMError, match="API Key"):
        LLMClient(LLMSettings(), model="m", api_key=None)


def test_missing_model_raises():
    with pytest.raises(LLMError, match="模型名"):
        LLMClient(LLMSettings(), model=None, api_key="k")
