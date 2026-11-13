"""URL 规范化、去重、哈希、时间、地点与文本辅助函数。"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
import unicodedata
from collections.abc import Callable, Iterable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import httpx

from .models import SearchResult, UrlTask

logger = logging.getLogger(__name__)


def log_stage(name: str) -> None:
    """在控制台打印显著分隔行，标记一个新阶段的开始。"""
    logger.info("================ %s ================", name)


def iter_concurrent(
    items: Iterable[Any],
    fn: Callable[[Any], Any],
    max_workers: int,
) -> Iterator[tuple[int, Future[Any]]]:
    """并发执行 fn(item)，按完成顺序产出 (index, future)；异常由调用方处理。"""
    values = list(items)
    if not values:
        return
    workers = min(max(1, max_workers), len(values))
    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {executor.submit(fn, value): index for index, value in enumerate(values)}
        for future in as_completed(futures):
            yield futures[future], future
    finally:
        # 中断时不能等排队任务跑完：取消未开始的任务并立即返回；在途请求由调用方直接退出进程。
        executor.shutdown(wait=False, cancel_futures=True)


TRACKING_PARAMS = {
    "gclid",
    "fbclid",
    "msclkid",
    "spm",
    "scm",
    "yclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_ga",
    "_gl",
    "ref_src",
}

INDEX_LAST_SEGMENTS = {
    "index.html",
    "index.htm",
    "index.php",
    "default.aspx",
    "default.html",
    "home",
    "homepage",
}

CATEGORY_SEGMENTS = {
    "tag",
    "tags",
    "category",
    "categories",
    "search",
    "list",
    "channel",
    "column",
    "archive",
    "archives",
    "topic",
    "topics",
    "login",
    "signin",
    "signup",
    "register",
    "auth",
}

CHANNEL_SEGMENTS = {
    "news",
    "sports",
    "finance",
    "tech",
    "weather",
    "entertainment",
    "health",
    "education",
    "travel",
    "food",
    "auto",
    "video",
    "photo",
}

SEARCH_QUERY_KEYS = {"q", "wd", "keyword", "keywords", "search", "page", "pn"}

_LIST_SEGMENT_RE = re.compile(
    r"(?:^|_)(?:list|index|channel|column|category|archive)(?:\.[a-z0-9]+)?$"
)


def index_url_reason(url: str) -> str | None:
    """返回 URL 被判为索引页的原因；不是索引页时返回 None。"""
    try:
        parsed = urlsplit(url or "")
    except ValueError:
        return None
    if not parsed.netloc:
        return None

    path = parsed.path or ""
    if path in ("", "/"):
        return "homepage"

    segments = [
        unquote(segment).strip().casefold()
        for segment in path.split("/")
        if segment.strip()
    ]
    if not segments:
        return "homepage"
    if segments[-1] in INDEX_LAST_SEGMENTS:
        return "index_page"
    if _LIST_SEGMENT_RE.search(segments[-1]):
        return "index_page"
    if any(segment in CATEGORY_SEGMENTS for segment in segments):
        return "category_page"
    if segments[-1] in CHANNEL_SEGMENTS:
        return "channel_page"
    if path.endswith("/") and len(segments) == 1:
        return "section_page"

    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if key.casefold() in SEARCH_QUERY_KEYS:
            return "search_page"
    return None


@lru_cache(maxsize=4096)
def normalize_url(
    url: str,
    *,
    merge_www: bool = False,
    strip_tracking: bool = True,
) -> str:
    """把 URL 规范化为去重键（不含 scheme）。

    规则：scheme/host 小写、去 fragment、去默认端口、去跟踪参数、
    查询参数排序、去路径末尾斜杠、忽略 scheme、www 默认不合并、不展开短链。
    无法解析时返回去空白后的原始 URL。
    同一 URL 在检索去重、批次内去重与写入等环节会被反复规范化，缓存结果避免重复解析。
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlsplit(raw)
    except ValueError:
        return raw

    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return raw

    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    port = parsed.port
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"

    path = parsed.path or ""
    if path == "/":
        path = ""
    elif path.endswith("/"):
        path = path.rstrip("/")

    query = parsed.query
    if query:
        pairs = [
            (key, value)
            for key, value in parse_qsl(query, keep_blank_values=True)
            if not (strip_tracking and _is_tracking_param(key))
        ]
        pairs.sort(key=lambda item: (item[0], item[1]))
        query = urlencode(pairs)

    if merge_www and netloc.startswith("www."):
        netloc = netloc[4:]

    key = netloc + path
    if query:
        key += "?" + query
    return key


def _is_tracking_param(name: str) -> bool:
    lowered = name.lower()
    return lowered.startswith("utm_") or lowered in TRACKING_PARAMS


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


def url_host(url: str) -> str:
    """返回小写主机名，失败时返回空串。"""
    try:
        return (urlsplit(url).hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def build_url_task(
    query_index: int,
    query: str,
    result: SearchResult,
    *,
    merge_www: bool = False,
    strip_tracking: bool = True,
) -> UrlTask:
    """把单条搜索结果转换为候选 URL 任务。"""
    return UrlTask(
        url=result.url,
        normalized_url=normalize_url(
            result.url, merge_www=merge_www, strip_tracking=strip_tracking
        ),
        matched_queries=[query],
        first_query_index=query_index,
        title=result.title,
        published_at=result.published_at,
        content=result.content,
    )


def dedupe_results(
    items: Iterable[tuple[int, str, SearchResult]],
    *,
    merge_www: bool = False,
    strip_tracking: bool = True,
) -> list[UrlTask]:
    """合并多个关键词的搜索结果，按首次出现顺序去重。

    items 为 ``(query_index, query, SearchResult)``，按关键词顺序传入。
    """
    seen: dict[str, UrlTask] = {}
    order: list[str] = []

    for query_index, query, result in items:
        new_task = build_url_task(
            query_index,
            query,
            result,
            merge_www=merge_www,
            strip_tracking=strip_tracking,
        )
        normalized = new_task.normalized_url
        if not normalized:
            continue
        task = seen.get(normalized)
        if task is None:
            seen[normalized] = new_task
            order.append(normalized)
            continue
        if query not in task.matched_queries:
            task.matched_queries.append(query)
        if not task.title and result.title:
            task.title = result.title
        if not task.published_at and result.published_at:
            task.published_at = result.published_at
        if len(result.content) > len(task.content):
            task.content = result.content

    return [seen[key] for key in order]


def interleave_by_query(tasks: list[UrlTask], max_urls: int) -> list[UrlTask]:
    """按首次命中的关键词轮转取数，避免靠前的关键词吃满配额。"""
    if len(tasks) <= max_urls:
        return tasks
    buckets: dict[int, list[UrlTask]] = {}
    for task in tasks:
        buckets.setdefault(task.first_query_index, []).append(task)
    queues = [buckets[key] for key in sorted(buckets)]
    picked: list[UrlTask] = []
    for offset in range(max(len(queue) for queue in queues)):
        for queue in queues:
            if offset >= len(queue):
                continue
            picked.append(queue[offset])
            if len(picked) >= max_urls:
                return picked
    return picked


def make_doc_id(normalized_url: str, salt: int = 0) -> str:
    value = normalized_url if salt == 0 else f"{normalized_url}#{salt}"
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def domain_matches(host: str, pattern: str) -> bool:
    """域名匹配：支持精确域名、子域名和 ``*.example.com`` 通配。"""
    rule = (pattern or "").lower().strip().rstrip(".")
    host = (host or "").lower().strip().rstrip(".")
    if not host or not rule:
        return False
    if rule.startswith("*."):
        base = rule[2:]
        return host.endswith("." + base)
    return host == rule or host.endswith("." + rule)


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(value: datetime | str | None) -> str | None:
    """把 datetime 或可解析字符串统一为 UTC ISO 8601；失败返回 None。"""
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = _parse_date_only(text)
            if parsed is None:
                try:
                    parsed = parsedate_to_datetime(text)
                except (TypeError, ValueError):
                    return None
        value = parsed
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_date_only(text: str) -> datetime | None:
    match = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", text)
    if not match:
        return None
    year, month, day = (int(part) for part in match.groups())
    try:
        return datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return None


def visible_length(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


_PLACEHOLDER_RE = re.compile(r"{{\s*(\w+)\s*}}")


def render_template(template: str, values: Mapping[str, object]) -> str:
    """替换 ``{{name}}`` 占位符；缺失变量直接报错，避免把占位符发给模型。"""

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in values:
            raise KeyError(f"prompt 变量缺失：{key}")
        return str(values[key])

    return _PLACEHOLDER_RE.sub(replace, template)


def strip_json_noise(text: str) -> str:
    """去掉 BOM 与 Markdown 代码围栏，返回待解析的 JSON 文本。"""
    value = (text or "").lstrip("﻿").strip()
    if "```" in value:
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", value, re.DOTALL | re.IGNORECASE)
        if blocks:
            value = blocks[0].strip()
    return value


def try_json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def read_prompt(path: str | Path, *, label: str, error_cls: type[Exception]) -> str:
    """读取 prompt 文件；失败时抛出调用方指定的异常类型。"""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as exc:
        raise error_cls(f"无法读取{label}：{path}：{exc}") from exc


def retry_after(response: httpx.Response, *, default: float) -> float:
    value = response.headers.get("retry-after")
    if not value:
        return default
    try:
        return min(float(value), 30.0)
    except ValueError:
        return default


def response_body(response: httpx.Response) -> str:
    try:
        return response.text[:300]
    except Exception:  # noqa: BLE001
        return ""


class RateLimiter:
    """全局 QPS 限速器。"""

    def __init__(self, qps: float) -> None:
        self._interval = 1.0 / qps if qps > 0 else 0.0
        self._lock = threading.Lock()
        self._next_allowed = 0.0

    def wait(self) -> None:
        if self._interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next_allowed)
            self._next_allowed = start + self._interval
            delay = start - now
        if delay > 0:
            time.sleep(delay)
