import json

import httpx2 as httpx
import pytest
from openai import OpenAI, APIStatusError
from pydantic import ValidationError

from doc2qa.config import SynthesisConfig
from doc2qa.quality import DuplicateResult, JudgeResult
from doc2qa.remote import Remote


def completion(content='{"score": 3}', *, finish_reason="stop", refusal=None):
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "object": "chat.completion",
            "created": 0,
            "model": "test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {"role": "assistant", "content": content, "refusal": refusal},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
    )


def error(status, message):
    return httpx.Response(
        status,
        json={
            "error": {
                "type": "invalid_request_error",
                "code": "invalid_parameter_error",
                "message": message,
            }
        },
    )


@pytest.fixture
def remote(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    value = Remote(SynthesisConfig(judge_model="test"))
    value.client.close()
    yield value
    value.client.close()


def mock_requests(remote, handler):
    requests = []

    def respond(request):
        payload = json.loads(request.content)
        requests.append(payload)
        return handler(payload)

    remote.client = OpenAI(
        api_key="offline-test-key",
        base_url="https://example.invalid/v1",
        max_retries=0,
        http_client=httpx.Client(transport=httpx.MockTransport(respond)),
    )
    return requests


def test_native_schema_still_works_and_accounts_usage(remote):
    requests = mock_requests(remote, lambda _: completion())
    assert remote.structured("Rules", "Payload", JudgeResult).score == 3
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["messages"][0]["content"] == "Rules"
    assert remote.usage["judge_calls"] == 1
    assert remote.usage["prompt_tokens"] == 10
    assert remote.usage["completion_tokens"] == 2


@pytest.mark.parametrize("supported", ["json_object", "text"])
def test_unsupported_formats_fall_back_and_cache_for_all_schemas(remote, supported, caplog):
    def respond(payload):
        mode = payload.get("response_format", {}).get("type", "text")
        if mode == "json_schema" or (mode == "json_object" and supported == "text"):
            return error(400, "This response_format type is unavailable now")
        assert "JSON schema" in payload["messages"][0]["content"]
        assert payload["messages"][1]["content"] == "Payload"
        return completion(
            '{"duplicate": false, "reason": "Different facts"}'
            if "duplicate" in payload["messages"][0]["content"]
            else '{"score": 2}'
        )

    requests = mock_requests(remote, respond)
    assert remote.structured("Rules", "Payload", JudgeResult).score == 2
    first_count = 2 if supported == "json_object" else 3
    assert len(requests) == first_count
    assert remote.structured("Rules", "Payload", DuplicateResult).duplicate is False
    assert len(requests) == first_count + 1
    assert requests[-1].get("response_format", {}).get("type", "text") == supported
    assert remote.usage["judge_calls"] == len(requests)
    assert remote.usage["prompt_tokens"] == 20
    assert remote.usage["completion_tokens"] == 4
    assert "falling back" in caplog.text
    assert "This response_format type is unavailable now" in caplog.text


@pytest.mark.parametrize(
    ("status", "message"),
    [
        (401, "Invalid API key"),
        (429, "Rate limit exceeded"),
        (400, "Unsupported temperature parameter"),
        (400, "Invalid schema for response_format: missing required property"),
    ],
)
def test_other_api_errors_do_not_trigger_format_fallback(remote, status, message, caplog):
    requests = mock_requests(remote, lambda _: error(status, message))
    with pytest.raises(APIStatusError):
        remote.structured("Rules", "Payload", JudgeResult)
    assert len(requests) == 1
    assert message in caplog.text


@pytest.mark.parametrize(
    "content",
    [
        '{"score": 4}',
        '{"score": 3.0}',
        '{"score": true}',
        '{"score": "3"}',
        '{"score": 3, "reason": "extra"}',
        "{}",
        "not JSON",
    ],
)
def test_plain_text_response_is_still_strictly_validated(remote, content):
    remote._response_formats["test"] = "text"
    requests = mock_requests(remote, lambda _: completion(content))
    with pytest.raises(ValidationError):
        remote.structured("Rules", "Payload", JudgeResult)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "options",
    [
        {"content": None},
        {"finish_reason": "length"},
        {"refusal": "Cannot comply"},
    ],
)
def test_plain_text_missing_refused_and_incomplete_outputs_are_errors(remote, options):
    remote._response_formats["test"] = "text"
    mock_requests(remote, lambda _: completion(**options))
    with pytest.raises(ValueError):
        remote.structured("Rules", "Payload", JudgeResult)


@pytest.mark.parametrize("count", [0, 1, 20, 21, 75])
def test_embedding_batches_obey_limit_and_preserve_global_order_and_usage(remote, count):
    def respond(payload):
        batch = payload["input"]
        assert len(batch) <= 20
        return httpx.Response(
            200,
            json={
                "object": "list",
                "model": "embedding-test",
                # Providers may return a batch's vectors out of order.
                "data": [
                    {
                        "object": "embedding",
                        "index": index,
                        "embedding": [float(text.removeprefix("query_")), 1.0],
                    }
                    for index, text in reversed(list(enumerate(batch)))
                ],
                "usage": {"prompt_tokens": len(batch), "total_tokens": len(batch)},
            },
        )

    requests = mock_requests(remote, respond)
    texts = [f"query_{index}" for index in range(count)]
    assert remote.embed(texts, "embedding-test") == [[float(index), 1.0] for index in range(count)]
    expected_sizes = [min(20, count - start) for start in range(0, count, 20)]
    assert [len(request["input"]) for request in requests] == expected_sizes
    assert [text for request in requests for text in request["input"]] == texts
    assert remote.usage["embedding_calls"] == len(expected_sizes)
    assert remote.usage["embedding_tokens"] == count
