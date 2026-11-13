"""DeepSeek Search + Fetch provider。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from .content import ExtractError
from .models import (
    ExtractedContent,
    SearchRequest,
    SearchResponse,
    SearchResult,
    Settings,
)
from .provider import Provider
from .search import SearchError
from .utils import (
    RateLimiter,
    iter_concurrent,
    normalize_url,
    response_body,
    retry_after,
    to_iso,
)

logger = logging.getLogger(__name__)

DEEPSEEK_SEARCH_PATH = "/v1/tools/search"
DEEPSEEK_FETCH_PATH = "/v1/tools/fetch"
DEEPSEEK_MAX_RESULTS = 50
DEEPSEEK_QUERY_MAX_CHARS = 128

DEEPSEEK_LANGUAGE_CODES = {
    "zh": "zh",
    "en": "en",
    "ar": "ar",
    "ar-EG": "ar",
}

TIME_RANGE_DAYS = {
    "day": 1,
    "week": 7,
    "month": 31,
    "year": 365,
}


class DeepSeekProvider(Provider):
    name = "deepseek"
    extractor_name = "deepseek-fetch"

    def __init__(
        self,
        settings: Settings,
        *,
        api_key: str | None = None,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        cfg = settings.providers.deepseek
        resolved_key = api_key or (
            settings.deepseek_api_key.get_secret_value()
            if settings.deepseek_api_key
            else None
        )
        if client is None and not resolved_key:
            raise SearchError(self.name, None, "缺少 DeepSeek API Key")

        base_url = cfg.base_url.rstrip("/")
        if base_url.endswith("/v1"):
            base_url = base_url[:-3]
        self._search_url = base_url + DEEPSEEK_SEARCH_PATH
        self._fetch_url = base_url + DEEPSEEK_FETCH_PATH
        self._search_cfg = cfg.search
        self._extract_cfg = cfg.extract
        self.extract_batch_size = cfg.extract.batch_size
        self._api_key = resolved_key or ""
        self._sleep = sleep
        self._search_limiter = RateLimiter(cfg.search.rate_limit_qps)
        self._fetch_limiter = RateLimiter(cfg.extract.rate_limit_qps)
        self._client = client or httpx.Client(
            timeout=max(cfg.search.timeout_seconds, cfg.extract.timeout_seconds),
            follow_redirects=False,
        )
        self._country_lock = threading.Lock()
        self._country_warned = False

    def search(self, request: SearchRequest) -> SearchResponse:
        if request.country:
            with self._country_lock:
                if not self._country_warned:
                    logger.warning(
                        "DeepSeek 搜索不支持 country 参数，已忽略：%s",
                        request.country,
                    )
                    self._country_warned = True

        payload = {
            "query": self._build_query(request),
            "count": min(request.max_results, DEEPSEEK_MAX_RESULTS),
            "need_content": self._search_cfg.need_content,
        }
        started = time.monotonic()
        data, response = self._request_json(
            self._search_url,
            payload,
            timeout=self._search_cfg.timeout_seconds,
            limiter=self._search_limiter,
            max_retries=self._search_cfg.max_retries,
        )
        latency_ms = int((time.monotonic() - started) * 1000)
        if data is None:
            raise self._search_error(response)

        results = self._map_results(data, request.max_results)
        logger.info(
            "搜索完成（deepseek）：query=%r results=%d latency=%dms",
            request.query,
            len(results),
            latency_ms,
        )
        if not results:
            logger.warning("search_empty：query=%r language=%s", request.query, request.language)
        return SearchResponse(results=results)

    def _extract_unique(self, urls: list[str]) -> list[ExtractedContent]:
        if not urls:
            return []
        outcomes: list[ExtractedContent | None] = [None] * len(urls)
        for index, future in iter_concurrent(
            urls, self._fetch_one, self._extract_cfg.fetch_concurrency
        ):
            outcomes[index] = future.result()
        return [
            outcome
            or ExtractedContent(
                url=urls[index],
                error_type="missing_result",
                error="提取结果缺失",
            )
            for index, outcome in enumerate(outcomes)
        ]

    def _fetch_one(self, url: str) -> ExtractedContent:
        data, response = self._request_json(
            self._fetch_url,
            {"url": url},
            timeout=self._extract_cfg.timeout_seconds,
            limiter=self._fetch_limiter,
            max_retries=self._extract_cfg.max_retries,
        )
        if data is not None:
            content = data.get("content")
            if not isinstance(content, str) or not content.strip():
                return ExtractedContent(
                    url=url, final_url=url, error_type="empty", error="正文为空"
                )
            return ExtractedContent(url=url, final_url=url, raw_content=content)

        if response is None:
            return ExtractedContent(
                url=url, final_url=url, error_type="network_error", error="网络错误"
            )

        status = response.status_code
        if status == 401:
            raise ExtractError("auth_error", "鉴权失败，请检查 DeepSeek API Key", fatal=True)
        if status == 402:
            raise ExtractError("quota_exceeded", "DeepSeek 账户余额不足", fatal=True)
        if status == 403:
            return ExtractedContent(
                url=url,
                final_url=url,
                error_type="blocked",
                error=f"站点安全策略拒绝：{response_body(response)}",
            )
        if status == 404:
            return ExtractedContent(
                url=url,
                final_url=url,
                error_type="failed",
                error=f"无可提取正文：{response_body(response)}",
            )
        if status == 400:
            return ExtractedContent(
                url=url,
                final_url=url,
                error_type="invalid_url",
                error=f"URL 无效：{response_body(response)}",
            )
        if status == 413:
            return ExtractedContent(
                url=url,
                final_url=url,
                error_type="http_error",
                error=f"请求体过大：{response_body(response)}",
            )
        if status == 200:
            return ExtractedContent(
                url=url,
                final_url=url,
                error_type="protocol_error",
                error="响应不是合法 JSON 或结构异常",
            )
        if status == 408 or status == 429 or status >= 500:
            return ExtractedContent(
                url=url,
                final_url=url,
                error_type="service_error",
                error=f"服务端错误：{response_body(response)}",
            )
        return ExtractedContent(
            url=url,
            final_url=url,
            error_type="http_error",
            error=f"HTTP {status}：{response_body(response)}",
        )

    def _build_query(self, request: SearchRequest) -> str:
        query = request.query.strip()
        operators: list[str] = []
        if "lang:" not in query:
            operators.append(f"lang:{DEEPSEEK_LANGUAGE_CODES[request.language]}")
        if request.time_range and "after:" not in query:
            operators.append(f"after:{_after_date(request.time_range)}")

        suffix = " ".join(operators)
        if not suffix:
            return query[:DEEPSEEK_QUERY_MAX_CHARS]

        limit = DEEPSEEK_QUERY_MAX_CHARS - len(suffix) - 1
        if len(query) > limit:
            logger.warning("DeepSeek 查询超过 %d 字符，已截断：%r", DEEPSEEK_QUERY_MAX_CHARS, query)
            query = query[: max(0, limit)].rstrip()
        return f"{query} {suffix}".strip()

    def _map_results(self, data: dict[str, Any], max_results: int) -> list[SearchResult]:
        raw_items = data.get("results")
        if not isinstance(raw_items, list):
            return []

        seen: dict[str, SearchResult] = {}
        results: list[SearchResult] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url") or "").strip()
            if not url:
                continue
            key = normalize_url(url)
            raw_content = item.get("content")
            content = raw_content.strip() if isinstance(raw_content, str) else ""

            existing = seen.get(key)
            if existing is not None:
                # 同一 URL 的重复条目保留最长非空正文，空正文不覆盖已有正文。
                if len(content) > len(existing.content):
                    existing.content = content
                continue

            raw_date = item.get("date")
            published_at = to_iso(raw_date) if isinstance(raw_date, str) else None
            result = SearchResult(
                url=url,
                title=str(item.get("title") or ""),
                published_at=published_at,
                content=content,
            )
            seen[key] = result
            results.append(result)
            if len(results) >= max_results:
                break
        return results

    def _search_error(self, response: httpx.Response | None) -> SearchError:
        if response is None:
            return SearchError(self.name, None, "网络错误")

        status = response.status_code
        if status == 400:
            return SearchError(self.name, status, f"搜索参数错误：{response_body(response)}")
        if status == 401:
            return SearchError(self.name, status, "鉴权失败，请检查 DeepSeek API Key")
        if status == 402:
            return SearchError(self.name, status, "DeepSeek 账户余额不足")
        if status == 403:
            return SearchError(self.name, status, f"请求被拒绝：{response_body(response)}")
        if status == 200:
            return SearchError(self.name, status, "响应不是合法 JSON 或结构异常")
        return SearchError(self.name, status, f"请求失败：{response_body(response)}")

    def _request_json(
        self,
        url: str,
        payload: dict[str, Any],
        *,
        timeout: float,
        limiter: RateLimiter,
        max_retries: int,
    ) -> tuple[dict[str, Any] | None, httpx.Response | None]:
        """发送请求并处理重试；返回解析后的 JSON 与原始响应。"""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        attempt = 0
        while True:
            limiter.wait()
            try:
                response = self._client.post(
                    url, json=payload, headers=headers, timeout=timeout
                )
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt < max_retries:
                    self._sleep(2**attempt)
                    attempt += 1
                    continue
                return None, None

            status = response.status_code
            if status in (408, 429) or status >= 500:
                if attempt < max_retries:
                    self._sleep(retry_after(response, default=2**attempt))
                    attempt += 1
                    continue
                return None, response

            if status != 200:
                return None, response

            try:
                data = response.json()
            except ValueError:
                if attempt < max_retries:
                    self._sleep(2**attempt)
                    attempt += 1
                    continue
                return None, response

            if isinstance(data, dict):
                return data, response
            if attempt < max_retries:
                self._sleep(2**attempt)
                attempt += 1
                continue
            return None, response


def _after_date(time_range: str) -> str:
    days = TIME_RANGE_DAYS[time_range]
    return (datetime.now(UTC) - timedelta(days=days)).date().isoformat()
