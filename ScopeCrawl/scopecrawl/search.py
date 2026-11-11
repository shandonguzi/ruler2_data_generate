"""统一搜索接口与 Tavily provider。"""

from __future__ import annotations

import json
import logging
import re
import time
import unicodedata
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any

import httpx

from .models import SearchRequest, SearchResponse, SearchResult, SearchSettings
from .utils import RateLimiter, normalize_url, to_iso, url_host

logger = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
TAVILY_MAX_RESULTS = 20

LANGUAGE_MAP = {
    "zh": "zh-cn",
    "en": "en",
    "ar": "ar",
    "ar-EG": "ar",
}

COUNTRY_MAP = {
    "CN": "china",
    "EG": "egypt",
    "SA": "saudi arabia",
    "AE": "united arab emirates",
    "US": "united states",
}

# 只映射国家级别名，不猜城市；未命中时不传 country。
LOCATION_COUNTRY_MAP = {
    "中国": "CN",
    "china": "CN",
    "cn": "CN",
    "中国大陆": "CN",
    "中华人民共和国": "CN",
    "الصين": "CN",
    "埃及": "EG",
    "egypt": "EG",
    "مصر": "EG",
    "沙特": "SA",
    "沙特阿拉伯": "SA",
    "saudi arabia": "SA",
    "ksa": "SA",
    "السعودية": "SA",
    "阿联酋": "AE",
    "阿拉伯联合酋长国": "AE",
    "uae": "AE",
    "united arab emirates": "AE",
    "الإمارات": "AE",
    "美国": "US",
    "usa": "US",
    "united states": "US",
}


def country_from_location(location: str | None) -> str | None:
    """把地点名确定性映射为 ISO alpha-2 国家码；无法确定时返回 None。"""
    text = _normalize_location(location)
    if not text:
        return None
    if text in LOCATION_COUNTRY_MAP:
        return LOCATION_COUNTRY_MAP[text]

    best: tuple[int, str] | None = None
    for alias, code in LOCATION_COUNTRY_MAP.items():
        if not _contains_location_alias(text, alias):
            continue
        if best is None or len(alias) > best[0]:
            best = (len(alias), code)
    return best[1] if best else None


def _normalize_location(value: str | None) -> str:
    text = unicodedata.normalize("NFKC", value or "").casefold()
    return re.sub(r"\s+", " ", text).strip()


def _contains_location_alias(text: str, alias: str) -> bool:
    if alias.isascii():
        pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        return re.search(pattern, text) is not None
    return alias in text


class SearchError(Exception):
    def __init__(
        self,
        provider: str,
        status_code: int | None,
        retryable: bool,
        message: str,
    ) -> None:
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code
        self.retryable = retryable
        self.message = message

    def __str__(self) -> str:
        prefix = f"{self.provider}"
        if self.status_code is not None:
            prefix += f" HTTP {self.status_code}"
        return f"{prefix}: {self.message}"


class SearchProvider(ABC):
    name: str

    @abstractmethod
    def search(self, request: SearchRequest) -> SearchResponse:
        """执行一次搜索。失败时抛出 SearchError。"""


class TavilyProvider(SearchProvider):
    name = "tavily"

    def __init__(
        self,
        settings: SearchSettings,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None and not api_key:
            raise SearchError(self.name, None, False, "缺少 Tavily API Key")
        self.settings = settings
        self._api_key = api_key or ""
        self._sleep = sleep
        self._limiter = RateLimiter(settings.tavily.rate_limit_qps)
        self._client = client or httpx.Client(
            timeout=settings.tavily.timeout_seconds,
            follow_redirects=False,
        )

    def search(self, request: SearchRequest) -> SearchResponse:
        payload = self._build_payload(request)
        started = time.monotonic()
        data = self._post(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        results = self._map_results(data, request.max_results)
        request_id = data.get("request_id") if isinstance(data, dict) else None
        logger.info(
            "搜索完成：query=%r results=%d latency=%dms",
            request.query,
            len(results),
            latency_ms,
        )
        if not results:
            logger.warning(
                "search_empty_after_language_filter：query=%r language=%s",
                request.query,
                request.language,
            )
        return SearchResponse(
            query=request.query,
            provider=self.name,
            results=results,
            raw=data if isinstance(data, dict) else {},
            latency_ms=latency_ms,
            request_id=request_id,
        )

    def _build_payload(self, request: SearchRequest) -> dict[str, Any]:
        cfg = self.settings.tavily
        max_results = min(request.max_results, TAVILY_MAX_RESULTS)
        payload: dict[str, Any] = {
            "query": request.query,
            "search_depth": cfg.search_depth,
            "topic": cfg.topic,
            "max_results": max_results,
            "language": LANGUAGE_MAP[request.language],
            "filter_by_language": True,
            "include_published_date": cfg.include_published_date,
            "filter_by_published_date": cfg.filter_by_published_date,
            "include_answer": cfg.include_answer,
            "include_images": cfg.include_images,
            "include_favicon": False,
            "auto_parameters": cfg.auto_parameters,
            "exact_match": cfg.exact_match,
            "safe_search": cfg.safe_search,
        }

        country_code = request.country or self.settings.defaults.country
        if country_code and cfg.topic == "general":
            mapped = COUNTRY_MAP.get(country_code.upper())
            if mapped:
                payload["country"] = mapped
            else:
                logger.warning("未映射的国家码，忽略 country：%s", country_code)
        elif country_code:
            logger.warning("topic=%s 不支持 country，已忽略", cfg.topic)

        if request.time_range:
            payload["time_range"] = request.time_range

        payload.update(request.provider_options)
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self.settings.tavily
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        attempt = 0

        while True:
            self._limiter.wait()
            try:
                response = self._client.post(TAVILY_ENDPOINT, json=payload, headers=headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < cfg.max_retries:
                    self._sleep(2**attempt)
                    attempt += 1
                    continue
                raise SearchError(self.name, None, True, f"网络错误：{exc}") from exc

            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except (ValueError, json.JSONDecodeError) as exc:
                    raise SearchError(self.name, status, True, "响应不是合法 JSON") from exc
                if not isinstance(data, dict):
                    raise SearchError(self.name, status, False, "响应结构异常")
                return data

            if status == 400:
                raise SearchError(
                    self.name,
                    status,
                    False,
                    f"搜索参数错误（language/filter_by_language）：{_body(response)}",
                )
            if status in (401, 403):
                raise SearchError(self.name, status, False, "鉴权失败，请检查 API Key")
            if status == 429 or status >= 500:
                if attempt < cfg.max_retries:
                    self._sleep(_retry_after(response, default=2**attempt))
                    attempt += 1
                    continue
                raise SearchError(self.name, status, True, f"服务端错误：{_body(response)}")
            raise SearchError(self.name, status, False, f"请求失败：{_body(response)}")

    def _map_results(self, data: dict[str, Any], max_results: int) -> list[SearchResult]:
        raw_items = data.get("results")
        if not isinstance(raw_items, list):
            return []

        seen: set[str] = set()
        results: list[SearchResult] = []

        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)

            score = item.get("score")
            raw_published = item.get("published_date") or item.get("published_at")
            published_at = to_iso(raw_published) if isinstance(raw_published, str) else None
            if published_at is None and raw_published:
                published_at = str(raw_published)
            results.append(
                SearchResult(
                    url=url,
                    title=str(item.get("title") or ""),
                    snippet=str(item.get("content") or ""),
                    rank=index + 1,
                    score=float(score) if isinstance(score, (int, float)) else None,
                    domain=url_host(url),
                    published_at=published_at,
                    raw=item,
                )
            )
            if len(results) >= max_results:
                break
        return results


def create_search_provider(
    settings: SearchSettings,
    *,
    api_key: str | None = None,
    client: httpx.Client | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> SearchProvider:
    if settings.provider == "tavily":
        return TavilyProvider(settings, api_key=api_key, client=client, sleep=sleep)
    raise SearchError(settings.provider, None, False, "不支持的搜索 provider")


def _retry_after(response: httpx.Response, *, default: float) -> float:
    value = response.headers.get("retry-after")
    if not value:
        return default
    try:
        return min(float(value), 30.0)
    except ValueError:
        return default


def _body(response: httpx.Response) -> str:
    try:
        return response.text[:300]
    except Exception:  # noqa: BLE001
        return ""
