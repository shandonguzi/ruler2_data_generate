"""共享测试夹具：配置构造、文档构造、假 LLM/Provider/Extractor。"""

from __future__ import annotations

import copy
import json

import pytest

from scopecrawl.content import plain_length
from scopecrawl.models import ExtractedContent, ExtractedDoc, SearchResponse, SearchResult, Settings
from scopecrawl.provider import Provider

_PROXY_ENV = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


@pytest.fixture(autouse=True)
def _disable_env_proxy(monkeypatch):
    """测试环境可能配置了 SOCKS 代理，避免 httpx 在用例中读取它。"""
    for key in _PROXY_ENV:
        monkeypatch.delenv(key, raising=False)


BASE_CONFIG = {
    "run": {
        "s3": "天气查询",
        "s4": "气象灾害查询",
        "location": "埃及",
        "language": "zh",
        "time_range": None,
    },
    "keywords": {"max_queries": 5},
    "search": {
        "defaults": {"max_results": 10, "country": None},
        "dedupe": {
            "enabled": True,
            "merge_www": False,
            "strip_tracking_params": True,
            "max_urls": None,
        },
    },
    "extract": {
        "concurrency": 2,
        "min_chars": 20,
        "min_paragraphs": 1,
    },
    "providers": {
        "tavily": {
            "search": {"rate_limit_qps": 1000},
            "extract": {"rate_limit_qps": 1000},
        },
        "deepseek": {
            "search": {"rate_limit_qps": 1000},
            "extract": {"rate_limit_qps": 1000},
        },
    },
    "llm": {},
    "review": {"enabled": True},
    "authority": {"enabled": True, "config_path": "config/authority.yaml"},
}


def make_settings(**overrides) -> Settings:
    data = copy.deepcopy(BASE_CONFIG)
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(data.get(key), dict):
            data[key].update(copy.deepcopy(value))
        else:
            data[key] = copy.deepcopy(value)
    return Settings.model_validate(data)


class FakeLLM:
    model_name = "fake-model"

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], bool]] = []

    def complete(self, messages, *, json_mode=False):
        self.calls.append((messages, json_mode))
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


REVIEW_JSON = json.dumps(
    {
        "relevance_score": 0.9,
        "relevance_level": "high",
        "content_richness_score": 0.8,
        "content_richness_level": "high",
        "content_quality_score": 0.75,
        "content_quality_level": "high",
        "content_reason": "信息密集且结构清晰",
        "content_type": "article",
        "matched_scope": "s4",
        "location_match": True,
        "language_match": True,
        "temporal_match": True,
        "flags": [],
        "reason": "正文直接报道埃及沙尘暴预警",
    }
)


class RouterLLM:
    """按 prompt 内容选择关键词或内容复核响应。"""

    model_name = "fake-model"

    def __init__(self, keywords=None):
        self.keywords = keywords or ["关键词一", "关键词二"]
        self.calls: list[tuple[list[dict], bool]] = []

    def complete(self, messages, *, json_mode=False):
        self.calls.append((messages, json_mode))
        content = messages[0]["content"]
        if "search query generator" in content:
            return json.dumps(self.keywords, ensure_ascii=False)
        return REVIEW_JSON


class FakeProvider(Provider):
    name = "tavily"
    extractor_name = "tavily-extract"
    extract_batch_size = 20

    def __init__(self, responses):
        self.responses = responses
        self.requests = []

    def search(self, request):
        self.requests.append(request)
        response = self.responses[request.query]
        if isinstance(response, Exception):
            raise response
        return response

    def _extract_unique(self, urls):
        return [
            ExtractedContent(url=url, error_type="failed", error="no extractor")
            for url in urls
        ]


class CombinedProvider(Provider):
    """测试用：把独立的搜索 fake 与提取 fake 组合成一个统一 provider。"""

    extract_batch_size = 20

    def __init__(self, provider: FakeProvider, extractor):
        self._provider = provider
        self._extractor = extractor
        self.name = provider.name
        self.extractor_name = provider.extractor_name
        self.requests = provider.requests
        self.calls = extractor.calls

    def search(self, request):
        return self._provider.search(request)

    def _extract_unique(self, urls):
        return self._extractor.extract_batch(urls)


class FakeExtractor:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def extract_batch(self, urls):
        self.calls.append(list(urls))
        results = []
        for url in urls:
            response = self.responses.get(url)
            if isinstance(response, ExtractedContent):
                results.append(response)
            elif isinstance(response, Exception):
                raise response
            elif response is None:
                results.append(
                    ExtractedContent(url=url, error_type="failed", error="not found")
                )
            else:
                results.append(
                    ExtractedContent(url=url, final_url=url, raw_content=str(response))
                )
        return results


MARKDOWN_OK = """# Egypt sandstorm warning

Egyptian authorities issued a sandstorm warning for several governorates.
The meteorological authority advised residents to avoid outdoor activities
and to close windows as visibility dropped sharply across the region.

Emergency services said hospitals were placed on alert and schools in
affected areas would close early. The warning remains in effect until the
weather front moves east later this week.

Forecasters expect the dust to clear gradually, but another wave may
arrive next week. Officials urged drivers to reduce speed on desert roads.

See also [the full report](https://example.org/report) and
[live updates](https://example.org/updates).
"""

MARKDOWN_NAV = """[首页](https://example.com/)

[登录](https://example.com/login)

[热门城市](https://example.com/cities)
"""

def make_search_response(
    urls: list[str], contents: dict[str, str] | None = None
) -> SearchResponse:
    contents = contents or {}
    return SearchResponse(
        results=[
            SearchResult(
                url=url,
                title=f"title {index}",
                content=contents.get(url, ""),
            )
            for index, url in enumerate(urls)
        ]
    )


def yaml_config(**overrides) -> dict:
    data = copy.deepcopy(BASE_CONFIG)
    for key, value in overrides.items():
        data[key] = copy.deepcopy(value)
    return data


MARKDOWN = "word " * 300


def make_doc(**overrides) -> ExtractedDoc:
    data = {
        "doc_id": "0123456789abcdef",
        "url": "https://example.com/a",
        "final_url": "https://example.com/a",
        "title": "title",
        "site": "example.com",
        "published_at": "2026-11-01T00:00:00+00:00",
        "fetched_at": "2026-11-10T00:00:00+00:00",
        "lang": "en",
        "markdown": MARKDOWN,
        "matched_queries": [],
        "outbound_links": 2,
        "kept_link_text_ratio": 0.0,
        "extractor": "tavily-extract",
    }
    data.update(overrides)
    data.setdefault("plain_chars", plain_length(data["markdown"]))
    return ExtractedDoc(**data)
