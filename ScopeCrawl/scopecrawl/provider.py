"""统一搜索 + 提取 Provider 抽象与工厂。"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Callable

import httpx

from .content import dedupe_inputs, expand_outcomes
from .models import ExtractedContent, SearchRequest, SearchResponse, Settings


class Provider(ABC):
    """向上层同时提供搜索与正文提取能力；一个实例只对应一个引擎。"""

    name: str
    extractor_name: str
    extract_batch_size: int

    @abstractmethod
    def search(self, request: SearchRequest) -> SearchResponse:
        """执行一次搜索。失败时抛出 SearchError。"""

    def extract_batch(self, urls: list[str]) -> list[ExtractedContent]:
        """提取一批 URL，返回与输入等长、按输入顺序排列的结果。"""
        keys, unique_urls, key_to_unique = dedupe_inputs(urls)
        if not unique_urls:
            return []
        return expand_outcomes(
            urls,
            keys,
            key_to_unique,
            unique_urls,
            self._extract_unique(unique_urls),
        )

    @abstractmethod
    def _extract_unique(self, urls: list[str]) -> list[ExtractedContent]:
        """提取已去重的 URL，返回与输入等长的结果。"""


def create_provider(
    settings: Settings,
    *,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> Provider:
    """按顶层 provider 选择器构造搜索与提取共用的同一引擎实例。"""
    if settings.provider == "tavily":
        from .search import TavilyProvider

        return TavilyProvider(settings, client=client, sleep=sleep)

    from .deepseek import DeepSeekProvider

    return DeepSeekProvider(settings, client=client, sleep=sleep)
