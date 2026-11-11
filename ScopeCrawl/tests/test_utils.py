from __future__ import annotations

from datetime import UTC, datetime

import pytest

from scopecrawl.models import SearchResult
from scopecrawl.utils import (
    dedupe_results,
    index_url_reason,
    is_index_url,
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
                snippet="补摘要",
                published_at="2026-11-01T00:00:00+00:00",
            ),
        ),
        (1, "q2", SearchResult(url="https://c.com/1", rank=3)),
    ]
    tasks = dedupe_results(items)
    assert [task.normalized_url for task in tasks] == ["a.com/1", "b.com/1", "c.com/1"]
    assert tasks[0].matched_queries == ["q1", "q2"]
    assert tasks[0].best_rank == 1
    assert tasks[0].first_query_index == 0
    assert tasks[0].title == "补标题"
    assert tasks[0].snippet == "补摘要"
    assert tasks[0].published_at == "2026-11-01T00:00:00+00:00"
    assert tasks[1].matched_queries == ["q1"]


def test_dedupe_results_respects_max_urls():
    items = [(0, "q", SearchResult(url=f"https://a.com/{i}", rank=i + 1)) for i in range(5)]
    assert len(dedupe_results(items, max_urls=2)) == 2


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
def test_is_index_url_drops_portal_pages(url):
    assert is_index_url(url) is True


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
def test_is_index_url_keeps_articles(url):
    assert is_index_url(url) is False


def test_is_index_url_keeps_wordpress_p_parameter():
    assert is_index_url("https://example.com/2026/11/post?p=123") is False
    assert index_url_reason("https://example.com/tag/foo") == "category_page"
    assert index_url_reason("https://example.com/2026/11/article") is None
