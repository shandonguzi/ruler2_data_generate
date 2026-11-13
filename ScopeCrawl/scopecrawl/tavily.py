"""Tavily Provider：搜索与正文提取。"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import Any

import httpx

from .content import (
    ExtractError,
    chunk_urls,
    dedupe_inputs,
    expand_outcomes,
    failure_outcomes,
    is_http_url,
)
from .models import (
    ExtractedContent,
    SearchRequest,
    SearchResponse,
    SearchResult,
    Settings,
    TavilyExtractSettings,
)
from .provider import Provider, SearchError
from .utils import RateLimiter, normalize_url, response_body, retry_after, to_iso

logger = logging.getLogger(__name__)

TAVILY_ENDPOINT = "https://api.tavily.com/search"
TAVILY_MAX_RESULTS = 20
TAVILY_EXTRACT_ENDPOINT = "https://api.tavily.com/extract"
TAVILY_MAX_BATCH_SIZE = 20

# Tavily 的搜索语言码；与 prompts 使用的语言展示名分开维护。
TAVILY_LANGUAGE_CODES = {
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


class TavilyProvider(Provider):
    name = "tavily"
    extractor_name = "tavily-extract"

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        cfg = settings.providers.tavily
        resolved_key = api_key or (
            settings.tavily_api_key.get_secret_value() if settings.tavily_api_key else None
        )
        if client is None and not resolved_key:
            raise SearchError(self.name, None, "缺少 Tavily API Key")
        self._search = cfg.search
        self.extract_batch_size = cfg.extract.batch_size
        self._api_key = resolved_key or ""
        self._sleep = sleep
        self._limiter = RateLimiter(cfg.search.rate_limit_qps)
        self._client = client or httpx.Client(
            timeout=cfg.search.timeout_seconds,
            follow_redirects=False,
        )
        self._extractor = TavilyExtractor(
            cfg.extract,
            api_key=resolved_key,
            client=client,
            sleep=sleep,
        )

    def _extract_unique(self, urls: list[str]) -> list[ExtractedContent]:
        return self._extractor.extract_unique(urls)

    def search(self, request: SearchRequest) -> SearchResponse:
        payload = self._build_payload(request)
        started = time.monotonic()
        data = self._post(payload)
        latency_ms = int((time.monotonic() - started) * 1000)

        results = self._map_results(data, request.max_results)
        logger.info(
            "搜索完成：query=%r results=%d latency=%dms",
            request.query,
            len(results),
            latency_ms,
        )
        if not results:
            logger.warning(
                "search_empty：query=%r language=%s",
                request.query,
                request.language,
            )
        return SearchResponse(results=results)

    def _build_payload(self, request: SearchRequest) -> dict[str, Any]:
        cfg = self._search
        max_results = min(request.max_results, TAVILY_MAX_RESULTS)
        payload: dict[str, Any] = {
            "query": request.query,
            "search_depth": cfg.search_depth,
            "topic": cfg.topic,
            "max_results": max_results,
            "language": TAVILY_LANGUAGE_CODES[request.language],
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

        country_code = request.country
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

        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = self._search
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
                raise SearchError(self.name, None, f"网络错误：{exc}") from exc

            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    raise SearchError(self.name, status, "响应不是合法 JSON") from exc
                if not isinstance(data, dict):
                    raise SearchError(self.name, status, "响应结构异常")
                return data

            if status == 400:
                raise SearchError(
                    self.name,
                    status,
                    f"搜索参数错误（language/filter_by_language）：{response_body(response)}",
                )
            if status in (401, 403):
                raise SearchError(self.name, status, "鉴权失败，请检查 API Key")
            if status == 429 or status >= 500:
                if attempt < cfg.max_retries:
                    self._sleep(retry_after(response, default=2**attempt))
                    attempt += 1
                    continue
                raise SearchError(self.name, status, f"服务端错误：{response_body(response)}")
            raise SearchError(self.name, status, f"请求失败：{response_body(response)}")

    def _map_results(self, data: dict[str, Any], max_results: int) -> list[SearchResult]:
        raw_items = data.get("results")
        if not isinstance(raw_items, list):
            return []

        seen: set[str] = set()
        results: list[SearchResult] = []

        for item in raw_items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            key = normalize_url(url)
            if key in seen:
                continue
            seen.add(key)

            raw_published = item.get("published_date") or item.get("published_at")
            published_at = to_iso(raw_published) if isinstance(raw_published, str) else None
            if published_at is None and raw_published:
                published_at = str(raw_published)
            results.append(
                SearchResult(
                    url=url,
                    title=str(item.get("title") or ""),
                    published_at=published_at,
                )
            )
            if len(results) >= max_results:
                break
        return results


class TavilyExtractor:
    """Tavily /extract 单批客户端。切块与并发由 pipeline 控制。"""

    def __init__(
        self,
        settings: TavilyExtractSettings,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if client is None and not api_key:
            raise ExtractError("auth_error", "缺少 Tavily API Key", fatal=True)
        self.settings = settings
        self._api_key = api_key or ""
        self._sleep = sleep
        self._limiter = RateLimiter(settings.rate_limit_qps)
        self._client = client or httpx.Client(
            timeout=settings.timeout_seconds + 30,
            follow_redirects=False,
        )

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
            self.extract_unique(unique_urls),
        )

    def extract_unique(self, urls: list[str]) -> list[ExtractedContent]:
        """提取已去重的 URL，返回与输入等长的结果。"""
        if not urls:
            return []
        if len(urls) > TAVILY_MAX_BATCH_SIZE:
            outcomes: list[ExtractedContent] = []
            for chunk in chunk_urls(urls, TAVILY_MAX_BATCH_SIZE):
                outcomes.extend(self._request(chunk))
            return outcomes
        return self._request(urls)

    def _request(self, urls: list[str]) -> list[ExtractedContent]:
        payload = {
            "urls": urls,
            "extract_depth": self.settings.depth,
            "format": self.settings.format,
            "timeout": int(self.settings.timeout_seconds),
            "include_images": False,
            "include_favicon": False,
        }
        try:
            data = self._post(payload)
        except ExtractError as exc:
            if exc.fatal:
                raise
            return failure_outcomes(
                urls,
                exc.error_type,
                exc.message,
                failed=exc.failed,
            )
        return self._map_results(urls, data)

    def _post(self, payload: dict) -> dict:
        cfg = self.settings
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        attempt = 0

        while True:
            self._limiter.wait()
            try:
                response = self._client.post(
                    TAVILY_EXTRACT_ENDPOINT, json=payload, headers=headers
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < cfg.max_retries:
                    self._sleep(2**attempt)
                    attempt += 1
                    continue
                raise ExtractError("network_error", f"网络错误：{exc}") from exc

            status = response.status_code
            if status == 200:
                try:
                    data = response.json()
                except ValueError as exc:
                    if attempt < cfg.max_retries:
                        self._sleep(2**attempt)
                        attempt += 1
                        continue
                    raise ExtractError("protocol_error", "响应不是合法 JSON") from exc
                if not _valid_payload(data):
                    if attempt < cfg.max_retries:
                        self._sleep(2**attempt)
                        attempt += 1
                        continue
                    raise ExtractError("protocol_error", "响应结构异常")
                return data

            if status == 400:
                raise ExtractError(
                    "invalid_batch",
                    f"提取参数错误：{response_body(response)[:200]}",
                    failed=_failed_from_response(response),
                )
            if status in (401, 403):
                raise ExtractError(
                    "auth_error", "鉴权失败，请检查 Tavily API Key", fatal=True
                )
            if status in (432, 433):
                raise ExtractError(
                    "quota_exceeded", "Tavily 额度或套餐上限", fatal=True
                )
            if status == 429 or status >= 500:
                if attempt < cfg.max_retries:
                    self._sleep(retry_after(response, default=2**attempt))
                    attempt += 1
                    continue
                raise ExtractError(
                    "service_error",
                    f"服务端错误：{response_body(response)[:200]}",
                )
            raise ExtractError(
                "http_error", f"HTTP {status}：{response_body(response)[:200]}"
            )

    def _map_results(self, urls: list[str], data: dict) -> list[ExtractedContent]:
        by_url: dict[str, dict] = {}
        by_normalized: dict[str, dict] = {}
        for item in data.get("results") or []:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "").strip()
            if not raw_url:
                continue
            by_url[raw_url] = item
            normalized = normalize_url(raw_url)
            if normalized:
                by_normalized[normalized] = item

        failed: dict[str, str] = {}
        failed_normalized: dict[str, str] = {}
        for item in data.get("failed_results") or []:
            if not isinstance(item, dict):
                continue
            raw_url = str(item.get("url") or "").strip()
            if not raw_url:
                continue
            error = str(item.get("error") or "提取失败")
            failed[raw_url] = error
            normalized = normalize_url(raw_url)
            if normalized:
                failed_normalized[normalized] = error

        outcomes: list[ExtractedContent] = []
        for raw_url in urls:
            normalized = normalize_url(raw_url) or raw_url
            item = by_url.get(raw_url) or by_normalized.get(normalized)
            if item is not None:
                content = item.get("raw_content")
                raw_content = content if isinstance(content, str) else ""
                final_url = str(item.get("url") or "").strip()
                if not is_http_url(final_url):
                    final_url = raw_url
                if not raw_content.strip():
                    outcomes.append(
                        ExtractedContent(
                            url=raw_url,
                            final_url=final_url,
                            error_type="empty",
                            error="正文为空",
                        )
                    )
                else:
                    outcomes.append(
                        ExtractedContent(
                            url=raw_url,
                            final_url=final_url,
                            raw_content=raw_content,
                        )
                    )
                continue
            error = failed.get(raw_url) or failed_normalized.get(normalized)
            outcomes.append(
                ExtractedContent(
                    url=raw_url,
                    error_type="failed",
                    error=error or "提取结果缺失",
                )
            )
        return outcomes


def _valid_payload(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    results = data.get("results", [])
    failed = data.get("failed_results", [])
    return isinstance(results, list) and isinstance(failed, list)


def _failed_from_response(response: httpx.Response) -> list[tuple[str, str]]:
    try:
        data = response.json()
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    detail = data.get("detail")
    if isinstance(detail, dict):
        data = detail
    items = data.get("failed_results")
    if not isinstance(items, list):
        return []
    failed: list[tuple[str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if url:
            failed.append((url, str(item.get("error") or "URL 未通过校验")))
    return failed
