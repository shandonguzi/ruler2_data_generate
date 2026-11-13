from __future__ import annotations

import json

import httpx
import pytest
import respx

from scopecrawl.models import SearchRequest
from scopecrawl.search import (
    TAVILY_ENDPOINT,
    SearchError,
    TavilyProvider,
    country_from_location,
)
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
