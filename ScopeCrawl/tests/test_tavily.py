from __future__ import annotations

import json

import httpx
import pytest
import respx

from scopecrawl.content import ExtractError
from scopecrawl.models import SearchRequest
from scopecrawl.provider import SearchError
from scopecrawl.tavily import (
    TAVILY_ENDPOINT,
    TAVILY_EXTRACT_ENDPOINT,
    TavilyExtractor,
    TavilyProvider,
)
from scopecrawl.utils import country_from_location
from tests.conftest import make_settings


def _provider(sleep=None) -> TavilyProvider:
    return TavilyProvider(
        make_settings(),
        api_key="tvly-test",
        sleep=sleep or (lambda _: None),
    )


def _response(results: list[dict]) -> dict:
    return {"query": "q", "results": results, "request_id": "req-1"}


@respx.mock
def test_search_sends_required_language_parameters():
    route = respx.post(TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_response(
                [
                    {
                        "url": "https://example.com/a",
                        "title": "t",
                        "content": "snippet",
                        "score": 0.8,
                        "published_date": "Mon, 10 Nov 2026 12:00:00 GMT",
                    }
                ]
            ),
        )
    )
    response = _provider().search(SearchRequest(query="埃及 沙尘暴", language="zh", max_results=10))

    body = json.loads(route.calls[0].request.content)
    assert body["language"] == "zh-cn"
    assert body["filter_by_language"] is True
    assert body["max_results"] == 10
    assert "sort_by" not in body
    assert "include_domains" not in body
    assert "include_raw_content" not in body
    assert route.calls[0].request.headers["authorization"] == "Bearer tvly-test"

    assert response.results[0].url == "https://example.com/a"
    assert response.results[0].published_at == "2026-11-10T12:00:00+00:00"
    # Tavily 的 content 是短摘要，不能透传给提取阶段。
    assert response.results[0].content == ""


@pytest.mark.parametrize(
    ("language", "expected"),
    [("zh", "zh-cn"), ("en", "en"), ("ar", "ar"), ("ar-EG", "ar")],
)
@respx.mock
def test_language_mapping(language, expected):
    route = respx.post(TAVILY_ENDPOINT).mock(return_value=httpx.Response(200, json=_response([])))
    _provider().search(SearchRequest(query="q", language=language))
    body = json.loads(route.calls[0].request.content)
    assert body["language"] == expected
    assert body["filter_by_language"] is True


@respx.mock
def test_country_only_mapped_for_general_topic():
    route = respx.post(TAVILY_ENDPOINT).mock(return_value=httpx.Response(200, json=_response([])))
    _provider().search(SearchRequest(query="q", language="en", country="EG"))
    body = json.loads(route.calls[0].request.content)
    assert body["country"] == "egypt"

    _provider().search(SearchRequest(query="q", language="en", country="ZZ"))
    body = json.loads(route.calls[1].request.content)
    assert "country" not in body


@respx.mock
def test_country_map_includes_china():
    route = respx.post(TAVILY_ENDPOINT).mock(return_value=httpx.Response(200, json=_response([])))
    _provider().search(SearchRequest(query="q", language="zh", country="CN"))
    body = json.loads(route.calls[0].request.content)
    assert body["country"] == "china"


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("中国", "CN"),
        ("China", "CN"),
        ("埃及", "EG"),
        ("Egypt", "EG"),
        ("美国", "US"),
        ("沙特阿拉伯", "SA"),
        ("阿联酋", "AE"),
        ("成都", None),
        ("australia", None),
        (None, None),
        ("", None),
    ],
)
def test_country_from_location_maps_names(location, expected):
    assert country_from_location(location) == expected


@respx.mock
def test_country_omitted_for_news_topic():
    settings = make_settings(
        providers={"tavily": {"search": {"topic": "news", "rate_limit_qps": 1000}}}
    )
    route = respx.post(TAVILY_ENDPOINT).mock(return_value=httpx.Response(200, json=_response([])))
    TavilyProvider(settings, api_key="k").search(
        SearchRequest(query="q", language="en", country="EG")
    )
    body = json.loads(route.calls[0].request.content)
    assert "country" not in body
    assert body["topic"] == "news"


@respx.mock
def test_time_range():
    route = respx.post(TAVILY_ENDPOINT).mock(return_value=httpx.Response(200, json=_response([])))
    _provider().search(
        SearchRequest(
            query="q",
            language="en",
            time_range="week",
        )
    )
    body = json.loads(route.calls[0].request.content)
    assert body["time_range"] == "week"


@respx.mock
def test_duplicate_urls_removed_within_response():
    respx.post(TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_response(
                [
                    {"url": "https://example.com/a", "title": "1", "content": ""},
                    {"url": "http://example.com/a/#x", "title": "2", "content": ""},
                    {"url": "https://example.com/b", "title": "3", "content": ""},
                ]
            ),
        )
    )
    response = _provider().search(SearchRequest(query="q", language="en"))
    assert [item.url for item in response.results] == [
        "https://example.com/a",
        "https://example.com/b",
    ]
    assert [item.title for item in response.results] == ["1", "3"]


@respx.mock
def test_retry_on_429_then_success():
    delays: list[float] = []
    route = respx.post(TAVILY_ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "2"}),
            httpx.Response(200, json=_response([])),
        ]
    )
    response = _provider(sleep=delays.append).search(SearchRequest(query="q", language="en"))
    assert route.call_count == 2
    assert delays == [2.0]
    assert response.results == []


@respx.mock
def test_401_is_not_retried():
    route = respx.post(TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(401, json={"error": "bad key"})
    )
    with pytest.raises(SearchError) as excinfo:
        _provider().search(SearchRequest(query="q", language="en"))
    assert excinfo.value.status_code == 401
    assert route.call_count == 1


@respx.mock
def test_400_is_not_retried_and_marked_fatal():
    route = respx.post(TAVILY_ENDPOINT).mock(
        return_value=httpx.Response(400, json={"error": "bad language"})
    )
    with pytest.raises(SearchError) as excinfo:
        _provider().search(SearchRequest(query="q", language="en"))
    assert excinfo.value.status_code == 400
    assert route.call_count == 1


def test_missing_api_key_raises():
    with pytest.raises(SearchError, match="API Key"):
        TavilyProvider(make_settings(), api_key=None)


def _extractor(settings=None, **kwargs) -> TavilyExtractor:
    return TavilyExtractor(
        (settings or make_settings()).providers.tavily.extract,
        api_key="tvly-test",
        **kwargs,
    )


def _extract_response(results: list[dict] | None = None, failed: list[dict] | None = None) -> dict:
    return {"results": results or [], "failed_results": failed or []}


@respx.mock
def test_extract_batch_maps_out_of_order_and_failures():
    route = respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_extract_response(
                results=[
                    {"url": "https://example.com/b", "raw_content": "B"},
                    {"url": "https://example.com/a", "raw_content": "A"},
                ],
                failed=[{"url": "https://example.com/c", "error": "blocked"}],
            ),
        )
    )
    results = _extractor().extract_batch(
        [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]
    )
    assert [item.raw_content for item in results] == ["A", "B", ""]
    assert results[2].error_type == "failed"
    assert results[2].error == "blocked"
    body = json.loads(route.calls[0].request.content)
    assert body["urls"] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert body["extract_depth"] == "basic"
    assert body["format"] == "markdown"
    assert body["include_images"] is False
    assert body["include_favicon"] is False


@respx.mock
def test_extract_batch_dedupes_duplicate_urls():
    route = respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_extract_response(results=[{"url": "https://example.com/a", "raw_content": "A"}]),
        )
    )
    results = _extractor().extract_batch(
        ["https://example.com/a", "http://example.com/a/#frag"]
    )
    assert [item.raw_content for item in results] == ["A", "A"]
    body = json.loads(route.calls[0].request.content)
    assert body["urls"] == ["https://example.com/a"]


@respx.mock
def test_extract_batch_empty_content_and_final_url():
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_extract_response(
                results=[
                    {"url": "https://example.com/a", "raw_content": "  "},
                    {"url": "https://example.com/b", "raw_content": "B"},
                ]
            ),
        )
    )
    results = _extractor().extract_batch(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert results[0].error_type == "empty"
    assert results[0].final_url == "https://example.com/a"
    assert results[1].raw_content == "B"
    assert results[1].final_url == "https://example.com/b"


@respx.mock
def test_extract_batch_400_uses_detail_failed_results():
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "detail": {
                    "failed_results": [
                        {"url": "https://example.com/a", "error": "invalid url"}
                    ]
                }
            },
        )
    )
    results = _extractor().extract_batch(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert results[0].error_type == "invalid_batch"
    assert results[0].error == "invalid url"
    assert results[1].error_type == "invalid_batch"


@pytest.mark.parametrize("status", [401, 432])
@respx.mock
def test_extract_batch_auth_and_quota_are_fatal(status):
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(return_value=httpx.Response(status))
    with pytest.raises(ExtractError) as exc:
        _extractor().extract_batch(["https://example.com/a"])
    assert exc.value.fatal


@respx.mock
def test_extract_batch_retries_429():
    route = respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(
                200,
                json=_extract_response(
                    results=[{"url": "https://example.com/a", "raw_content": "A"}]
                ),
            ),
        ]
    )
    delays: list[float] = []
    results = _extractor(sleep=delays.append).extract_batch(["https://example.com/a"])
    assert results[0].raw_content == "A"
    assert delays == [1.0]
    assert route.call_count == 2


@respx.mock
def test_extract_batch_service_error_returns_failures():
    settings = make_settings(
        providers={"tavily": {"extract": {"max_retries": 0}}}
    )
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(return_value=httpx.Response(503))
    results = _extractor(settings).extract_batch(["https://example.com/a"])
    assert results[0].error_type == "service_error"


@respx.mock
def test_extract_batch_non_json_protocol_error():
    settings = make_settings(
        providers={"tavily": {"extract": {"max_retries": 0}}}
    )
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(200, text="not json")
    )
    results = _extractor(settings).extract_batch(["https://example.com/a"])
    assert results[0].error_type == "protocol_error"


def test_extract_batch_empty_list_makes_no_request():
    assert _extractor().extract_batch([]) == []


@respx.mock
def test_tavily_provider_extract_batch_uses_extractor():
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_extract_response(
                results=[{"url": "https://example.com/a", "raw_content": "A"}]
            ),
        )
    )
    provider = TavilyProvider(make_settings(), api_key="tvly-test")
    results = provider.extract_batch(["https://example.com/a"])
    assert results[0].raw_content == "A"
