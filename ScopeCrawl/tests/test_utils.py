from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

import pytest

from scopecrawl.models import SearchResult, UrlTask
from scopecrawl.utils import (
    dedupe_results,
    index_url_reason,
    interleave_by_query,
    iter_concurrent,
    make_doc_id,
    normalize_url,
    to_iso,
)


def test_normalize_lowercase_default_port_fragment_and_slash():
    assert normalize_url("HTTP://Example.COM:80/a/#frag") == "example.com/a"
    assert normalize_url("https://Example.COM:443/a") == "example.com/a"
    assert normalize_url("https://example.com/") == "example.com"


def test_normalize_strips_tracking_and_sorts_query():
    url = "https://Example.com/p?b=2&utm_source=news&a=1&fbclid=xyz"
    assert normalize_url(url) == "example.com/p?a=1&b=2"
    # 不剥离时只排序
    assert (
        normalize_url(url, strip_tracking=False)
        == "example.com/p?a=1&b=2&fbclid=xyz&utm_source=news"
    )


def test_normalize_scheme_www_and_custom_port():
    assert normalize_url("http://a.com/x") == normalize_url("https://a.com/x/")
    assert normalize_url("https://www.a.com/x") == "www.a.com/x"
    assert normalize_url("https://www.a.com/x", merge_www=True) == "a.com/x"
    assert normalize_url("https://a.com:8443/x") == "a.com:8443/x"


def test_normalize_url_reuses_cached_result():
    normalize_url.cache_clear()
    url = "https://example.com/a?utm_source=news"
    assert normalize_url(url) == normalize_url(url)
    assert normalize_url.cache_info().hits >= 1


def test_dedupe_results_merges_queries_and_keeps_order():
    items = [
        (0, "q1", SearchResult(url="https://a.com/1", rank=2)),
        (0, "q1", SearchResult(url="https://b.com/1", rank=1)),
        (
            1,
            "q2",
            SearchResult(
                url="http://a.com/1/#x",
                rank=1,
                title="补标题",
                published_at="2026-11-01T00:00:00+00:00",
            ),
        ),
        (1, "q2", SearchResult(url="https://c.com/1", rank=3)),
    ]
    tasks = dedupe_results(items)
    assert [task.normalized_url for task in tasks] == ["a.com/1", "b.com/1", "c.com/1"]
    assert tasks[0].matched_queries == ["q1", "q2"]
    assert tasks[0].first_query_index == 0
    assert tasks[0].title == "补标题"
    assert tasks[0].published_at == "2026-11-01T00:00:00+00:00"
    assert tasks[1].matched_queries == ["q1"]


def test_iter_concurrent_returns_index_and_future():
    pairs = list(iter_concurrent([3, 1, 2], lambda value: value * 2, 2))
    assert sorted((index, future.result()) for index, future in pairs) == [
        (0, 6),
        (1, 2),
        (2, 4),
    ]


def test_iter_concurrent_close_does_not_wait_pending_tasks():
    started = threading.Event()
    release = threading.Event()

    def work(value):
        if value:
            started.set()
            release.wait(10)
        return value

    stream = iter_concurrent(range(4), work, 2)
    next(stream)
    assert started.wait(1)
    then = time.monotonic()
    stream.close()
    try:
        assert time.monotonic() - then < 1
    finally:
        release.set()


def test_interleave_by_query_rotates_across_queries():
    tasks = [
        UrlTask(url="https://a.com/0", normalized_url="a.com/0", first_query_index=0),
        UrlTask(url="https://a.com/1", normalized_url="a.com/1", first_query_index=0),
        UrlTask(url="https://b.com/0", normalized_url="b.com/0", first_query_index=1),
    ]
    assert [task.normalized_url for task in interleave_by_query(tasks, 2)] == [
        "a.com/0",
        "b.com/0",
    ]
    # 未触达上限时保持原顺序
    assert interleave_by_query(tasks, 10) == tasks


def test_doc_id_is_stable_and_salted():
    first = make_doc_id("example.com/a")
    assert first == make_doc_id("example.com/a")
    assert len(first) == 16
    assert make_doc_id("example.com/a", 1) != first


def test_to_iso_handles_naive_iso_and_rfc2822():
    assert to_iso("2026-11-10") == "2026-11-10T00:00:00+00:00"
    assert to_iso(datetime(2026, 11, 10, 12, 0)) == "2026-11-10T12:00:00+00:00"
    assert to_iso("Mon, 10 Nov 2026 12:00:00 GMT") == "2026-11-10T12:00:00+00:00"
    assert to_iso("not a date") is None
    assert to_iso(datetime(2026, 11, 10, 12, 0, tzinfo=UTC)).endswith("+00:00")


@pytest.mark.parametrize(
    "url",
    [
        "https://m.cn.nytimes.com",
        "https://www.weather.com.cn",
        "https://example.com",
        "https://example.com/",
        "https://example.com/index.html",
        "https://example.com/tag/foo",
        "https://example.com/category/news",
        "https://example.com/search?q=x",
        "https://example.com/news/",
        "https://example.com/login",
        "https://example.com/list?page=2",
        "https://example.com/wap/news_list.aspx",
        "https://example.com/article_list",
    ],
)
def test_index_url_reason_drops_portal_pages(url):
    assert index_url_reason(url) is not None


@pytest.mark.parametrize(
    "url",
    [
        "https://zh.wikipedia.org/wiki/Main_Page",
        "https://baike.baidu.com/item/abc",
        "https://zhuanlan.zhihu.com/p/687440120",
        "https://example.com/2026/11/article",
        "https://example.com/about",
        "https://example.com/zh/country/china/history",
        "https://example.com/playlist",
        "https://example.com/detail/84",
        "not a url",
        "http://[bad",
    ],
)
def test_index_url_reason_keeps_articles(url):
    assert index_url_reason(url) is None


def test_index_url_reason_keeps_wordpress_p_parameter():
    assert index_url_reason("https://example.com/2026/11/post?p=123") is None
