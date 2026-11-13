from __future__ import annotations

import httpx

from scopecrawl.deepseek import DeepSeekProvider
from scopecrawl.provider import Provider, create_provider
from scopecrawl.tavily import TavilyProvider
from tests.conftest import make_settings


def _client() -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    )


def test_create_provider_selects_single_engine():
    with _client() as client:
        tavily = create_provider(make_settings(), client=client)
        assert isinstance(tavily, TavilyProvider)
        assert isinstance(tavily, Provider)
        assert tavily.name == "tavily"
        assert tavily.extractor_name == "tavily-extract"

        deepseek = create_provider(make_settings(provider="deepseek"), client=client)
        assert isinstance(deepseek, DeepSeekProvider)
        assert isinstance(deepseek, Provider)
        assert deepseek.name == "deepseek"
        assert deepseek.extractor_name == "deepseek-fetch"


def test_provider_exposes_search_and_extract():
    with _client() as client:
        provider = create_provider(make_settings(), client=client)
        assert callable(provider.search)
        assert callable(provider.extract_batch)
        assert provider.extract_batch_size == 20
