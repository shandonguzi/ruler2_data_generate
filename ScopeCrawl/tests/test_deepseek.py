from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import respx

from scopecrawl.content import ExtractError
from scopecrawl.deepseek import DeepSeekProvider
from scopecrawl.models import SearchRequest
from scopecrawl.search import SearchError
from tests.conftest import make_settings

SEARCH_ENDPOINT = "https://api.deepseek.com/v1/tools/search"
FETCH_ENDPOINT = "https://api.deepseek.com/v1/tools/fetch"


def _provider(settings=None, **kwargs) -> DeepSeekProvider:
    return DeepSeekProvider(
        settings or make_settings(provider="deepseek"),
        api_key="ds-test",
        sleep=kwargs.pop("sleep", lambda _: None),
        **kwargs,
    )


def _results(*items: dict) -> dict:
    return {"results": list(items)}


@respx.mock
def test_search_appends_language_and_ignores_country():
    route = respx.post(SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_results(
                {
                    "url": "https://example.com/a",
                    "title": "标题",
                    "date": "2026-06-01T00:00:00+00:00",
                }
            ),
        )
    )
    response = _provider().search(
        SearchRequest(query="埃及 沙尘暴", language="ar", country="EG", max_results=10)
    )

    body = json.loads(route.calls[0].request.content)
    assert body["query"] == "埃及 沙尘暴 lang:ar"
    assert body["count"] == 10
    assert body["need_content"] is False
    assert "country" not in body
    assert route.calls[0].request.headers["authorization"] == "Bearer ds-test"
    assert response.results[0].published_at == "2026-06-01T00:00:00+00:00"


@respx.mock
def test_search_appends_after_for_time_range():
    route = respx.post(SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_results())
    )
    _provider().search(
        SearchRequest(query="沙尘暴", language="zh", time_range="week")
    )
    body = json.loads(route.calls[0].request.content)
    expected = (datetime.now(UTC) - timedelta(days=7)).date().isoformat()
    assert body["query"] == f"沙尘暴 lang:zh after:{expected}"


@respx.mock
def test_search_truncates_query_to_128_chars():
    route = respx.post(SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_results())
    )
    _provider().search(SearchRequest(query="x" * 200, language="en"))
    body = json.loads(route.calls[0].request.content)
    assert len(body["query"]) <= 128
    assert body["query"].endswith("lang:en")


@respx.mock
def test_search_dedupes_urls():
    route = respx.post(SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_results(
                {"url": "https://example.com/a?utm_source=x", "title": "a"},
                {"url": "https://example.com/a", "title": "b"},
            ),
        )
    )
    response = _provider().search(SearchRequest(query="q", language="en"))
    assert len(response.results) == 1
    assert route.call_count == 1


@respx.mock
def test_search_sends_need_content_from_settings():
    route = respx.post(SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(200, json=_results())
    )
    settings = make_settings(
        provider="deepseek",
        providers={"deepseek": {"search": {"need_content": True}}},
    )
    _provider(settings=settings).search(SearchRequest(query="q", language="en"))
    body = json.loads(route.calls[0].request.content)
    assert body["need_content"] is True


@respx.mock
def test_search_maps_content_and_normalizes_empty_values():
    respx.post(SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_results(
                {"url": "https://example.com/a", "content": "  正文  "},
                {"url": "https://example.com/b", "content": None},
                {"url": "https://example.com/c", "content": 123},
                {"url": "https://example.com/d", "content": "   "},
            ),
        )
    )
    response = _provider().search(SearchRequest(query="q", language="en"))
    assert [result.content for result in response.results] == ["正文", "", "", ""]


@respx.mock
def test_search_keeps_longest_content_for_duplicate_urls():
    respx.post(SEARCH_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_results(
                {"url": "https://example.com/a?utm_source=x", "content": ""},
                {"url": "https://example.com/a", "content": "长正文"},
                {"url": "http://example.com/a", "content": "短"},
            ),
        )
    )
    response = _provider().search(SearchRequest(query="q", language="en"))
    assert len(response.results) == 1
    assert response.results[0].content == "长正文"


@pytest.mark.parametrize("status", [401, 402])
@respx.mock
def test_search_auth_and_quota_are_fatal(status):
    respx.post(SEARCH_ENDPOINT).mock(return_value=httpx.Response(status))
    with pytest.raises(SearchError) as excinfo:
        _provider().search(SearchRequest(query="q", language="en"))
    assert excinfo.value.status_code == status


@respx.mock
def test_search_retries_429_then_succeeds():
    delays: list[float] = []
    route = respx.post(SEARCH_ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json=_results()),
        ]
    )
    _provider(sleep=delays.append).search(SearchRequest(query="q", language="en"))
    assert delays == [2.0]
    assert route.call_count == 2


@respx.mock
def test_extract_batch_maps_success_and_preserves_order():
    def handler(request):
        url = json.loads(request.content)["url"]
        return httpx.Response(200, json={"title": url, "content": f"content {url}"})

    route = respx.post(FETCH_ENDPOINT).mock(side_effect=handler)
    results = _provider().extract_batch(
        ["https://example.com/a", "https://example.com/b", "https://example.com/a"]
    )
    assert [item.raw_content for item in results] == [
        "content https://example.com/a",
        "content https://example.com/b",
        "content https://example.com/a",
    ]
    assert [item.final_url for item in results] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/a",
    ]
    assert route.call_count == 2


@respx.mock
def test_extract_empty_content():
    respx.post(FETCH_ENDPOINT).mock(
        return_value=httpx.Response(200, json={"title": "t", "content": " "})
    )
    result = _provider().extract_batch(["https://example.com/a"])[0]
    assert result.error_type == "empty"


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(403, "blocked"), (404, "failed"), (400, "invalid_url"), (413, "http_error")],
)
@respx.mock
def test_extract_non_fatal_errors(status, error_type):
    respx.post(FETCH_ENDPOINT).mock(return_value=httpx.Response(status))
    result = _provider().extract_batch(["https://example.com/a"])[0]
    assert result.error_type == error_type


@pytest.mark.parametrize("status", [401, 402])
@respx.mock
def test_extract_auth_and_quota_are_fatal(status):
    respx.post(FETCH_ENDPOINT).mock(return_value=httpx.Response(status))
    with pytest.raises(ExtractError) as excinfo:
        _provider().extract_batch(["https://example.com/a"])
    assert excinfo.value.fatal is True


@respx.mock
def test_extract_retries_429_then_succeeds():
    delays: list[float] = []
    route = respx.post(FETCH_ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json={"title": "t", "content": "ok"}),
        ]
    )
    result = _provider(sleep=delays.append).extract_batch(
        ["https://example.com/a"]
    )[0]
    assert result.raw_content == "ok"
    assert delays == [2.0]
    assert route.call_count == 2


@respx.mock
def test_extract_network_error_is_per_url_failure():
    settings = make_settings(
        provider="deepseek",
        providers={"deepseek": {"extract": {"max_retries": 0}}},
    )
    respx.post(FETCH_ENDPOINT).mock(side_effect=httpx.ConnectError("boom"))
    result = _provider(settings).extract_batch(["https://example.com/a"])[0]
    assert result.error_type == "network_error"
