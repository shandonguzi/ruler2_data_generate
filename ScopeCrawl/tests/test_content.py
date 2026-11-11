from __future__ import annotations

import json

import httpx
import pytest
import respx

from scopecrawl.content import (
    TAVILY_EXTRACT_ENDPOINT,
    CleanResult,
    ExtractError,
    TavilyExtractor,
    build_extracted_doc,
    check_quality,
    chunk_urls,
    clean_content,
    plain_length,
)
from scopecrawl.models import ExtractedContent, UrlTask
from scopecrawl.utils import visible_length
from tests.conftest import make_settings


def _extractor(settings=None, **kwargs) -> TavilyExtractor:
    return TavilyExtractor(
        (settings or make_settings()).extract,
        api_key="tvly-test",
        **kwargs,
    )


def _response(results: list[dict] | None = None, failed: list[dict] | None = None) -> dict:
    return {"results": results or [], "failed_results": failed or []}


def test_chunk_urls_splits_into_batches():
    urls = [f"https://example.com/{index}" for index in range(45)]
    batches = chunk_urls(urls, 20)
    assert [len(batch) for batch in batches] == [20, 20, 5]


@respx.mock
def test_extract_batch_maps_out_of_order_and_failures():
    route = respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_response(
                results=[
                    {"url": "https://example.com/b", "raw_content": "B"},
                    {"url": "https://example.com/a", "raw_content": "A"},
                ],
                failed=[{"url": "https://example.com/c", "error": "blocked"}],
            ),
        )
    )
    results = _extractor().extract_batch(
        [
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        ]
    )
    assert [item.raw_content for item in results] == ["A", "B", ""]
    assert results[2].error_type == "failed"
    assert results[2].error == "blocked"
    body = json.loads(route.calls[0].request.content)
    assert body["urls"] == [
        "https://example.com/a",
        "https://example.com/b",
        "https://example.com/c",
    ]
    assert body["extract_depth"] == "basic"
    assert body["format"] == "markdown"
    assert body["include_images"] is False
    assert body["include_favicon"] is False


@respx.mock
def test_extract_batch_dedupes_duplicate_urls():
    route = respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_response(results=[{"url": "https://example.com/a", "raw_content": "A"}]),
        )
    )
    results = _extractor().extract_batch(
        ["https://example.com/a", "http://example.com/a/#frag"]
    )
    assert [item.raw_content for item in results] == ["A", "A"]
    body = json.loads(route.calls[0].request.content)
    assert body["urls"] == ["https://example.com/a"]


@respx.mock
def test_extract_batch_empty_content_and_final_url():
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            200,
            json=_response(
                results=[
                    {"url": "https://example.com/a", "raw_content": "  "},
                    {"url": "https://example.com/b", "raw_content": "B"},
                ]
            ),
        )
    )
    results = _extractor().extract_batch(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert results[0].error_type == "empty"
    assert results[0].final_url == "https://example.com/a"
    assert results[1].raw_content == "B"
    assert results[1].final_url == "https://example.com/b"


@respx.mock
def test_extract_batch_400_uses_detail_failed_results():
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(
            400,
            json={
                "detail": {
                    "failed_results": [
                        {"url": "https://example.com/a", "error": "invalid url"}
                    ]
                }
            },
        )
    )
    results = _extractor().extract_batch(
        ["https://example.com/a", "https://example.com/b"]
    )
    assert results[0].error_type == "invalid_batch"
    assert results[0].error == "invalid url"
    assert results[1].error_type == "invalid_batch"


@pytest.mark.parametrize("status", [401, 432])
@respx.mock
def test_extract_batch_auth_and_quota_are_fatal(status):
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(return_value=httpx.Response(status))
    with pytest.raises(ExtractError) as exc:
        _extractor().extract_batch(["https://example.com/a"])
    assert exc.value.fatal


@respx.mock
def test_extract_batch_retries_429():
    route = respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        side_effect=[
            httpx.Response(429, headers={"retry-after": "1"}),
            httpx.Response(
                200,
                json=_response(results=[{"url": "https://example.com/a", "raw_content": "A"}]),
            ),
        ]
    )
    delays: list[float] = []
    results = _extractor(sleep=delays.append).extract_batch(["https://example.com/a"])
    assert results[0].raw_content == "A"
    assert delays == [1.0]
    assert route.call_count == 2


@respx.mock
def test_extract_batch_service_error_returns_failures():
    settings = make_settings(extract={"max_retries": 0})
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(return_value=httpx.Response(503))
    results = _extractor(settings).extract_batch(["https://example.com/a"])
    assert results[0].error_type == "service_error"
    assert not results[0].fatal


@respx.mock
def test_extract_batch_non_json_protocol_error():
    settings = make_settings(extract={"max_retries": 0})
    respx.post(TAVILY_EXTRACT_ENDPOINT).mock(
        return_value=httpx.Response(200, text="not json")
    )
    results = _extractor(settings).extract_batch(["https://example.com/a"])
    assert results[0].error_type == "protocol_error"


def test_extract_batch_empty_list_makes_no_request():
    assert _extractor().extract_batch([]) == []


def test_clean_content_removes_edge_nav_and_keeps_middle_short_paragraph():
    raw = """[首页](https://example.com/)

[登录](https://example.com/login)

# 标题

第一段正文，包含足够的内容用于测试。这里再补充一些文字，让它成为真正的段落。

开罗

第二段正文，包含足够的内容用于测试。这里再补充一些文字，让它成为真正的段落。

[更多](https://example.com/more)
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "首页" not in clean.markdown
    assert "登录" not in clean.markdown
    assert "更多" not in clean.markdown
    assert "开罗" in clean.markdown
    assert clean.first_heading == "标题"
    assert clean.nav_ratio > 0


def test_clean_content_keeps_table_list_code_and_inline_link():
    raw = """# 标题

| 城市 | 天气 |
|---|---|
| 开罗 | 晴 |

- 第一项
- 第二项

```text
code
```

正文段落包含足够多的内容用于测试，并且保留一个[参考链接](https://example.org/report)。这段文字需要超过三十个字符，避免被边缘导航规则误删。
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "| 城市 | 天气 |" in clean.markdown
    assert "- 第一项" in clean.markdown
    assert "```text" in clean.markdown
    assert "[参考链接](https://example.org/report)" in clean.markdown
    assert clean.outbound_links == 1


def test_clean_content_drops_images_and_repeated_blocks():
    raw = """![图片](https://example.com/a.png)

第一段正文包含足够多的内容，用于通过边缘导航规则并保留下来，这里再补充一些文字。

重复

重复

重复

第二段正文包含足够多的内容，用于通过边缘导航规则并保留下来，这里再补充一些文字。
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "图片" not in clean.markdown
    assert clean.markdown.count("重复") == 1


def test_clean_content_nav_ratio_counts_characters():
    raw = "\n\n".join(
        [
            "[首页](https://example.com/)",
            "[登录](https://example.com/login)",
            "[注册](https://example.com/register)",
            "[热门](https://example.com/hot)",
            "正文内容足够长，用于把导航占比压到可观测的水平。" * 2,
            "[更多](https://example.com/more)",
        ]
    )
    clean = clean_content(raw, url="https://example.com/article")
    assert clean.nav_ratio > 0.5
    assert "首页" not in clean.markdown


def test_clean_content_drops_trailing_footer_boilerplate():
    raw = """# 标题

第一段正文包含足够多的内容用于测试，并且带句号，确保不会被边缘规则误删。

第二段正文包含足够多的内容用于测试，并且带句号，确保不会被边缘规则误删。

[华人驿站](https://example.com/) ( [陕ICP备16004167号-3](https://beian.miit.gov.cn/) )

地址：某市某路 电话：12345678 邮箱：a@b.com

版权所有: 某公司

GMT+8, 2026-11-10 16:33 , Processed in 0.06 second(s)
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "ICP备" not in clean.markdown
    assert "地址：" not in clean.markdown
    assert "版权所有" not in clean.markdown
    assert "GMT+8" not in clean.markdown
    assert "第一段正文" in clean.markdown


def test_clean_content_drops_wikipedia_category_links():
    raw = """# 标题

正文段落包含足够多的内容用于测试，并且带句号，确保不会被边缘规则误删。

[分类](https://zh.wikipedia.org/wiki/Special:Categories)：

* [CS1德语来源](https://zh.wikipedia.org/wiki/Category:CS1德语来源)
* [维基百科分类](https://zh.wikipedia.org/wiki/Category:维基百科分类)
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "Category:" not in clean.markdown
    assert "Special:Categories" not in clean.markdown
    assert "正文段落" in clean.markdown


def test_clean_content_drops_leading_infobox_table():
    raw = """| 系统长度 | 50000 千米 |
|---|---|
| [轨距](https://example.com/gauge) | 1435 毫米 |
| [电气化](https://example.com/electrification) | 25 kV |
| [运营](https://example.com/operation) | 2008 年 |

# 标题

正文段落包含足够多的内容用于测试，并且带句号，确保不会被边缘规则误删。
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "系统长度" not in clean.markdown
    assert "正文段落" in clean.markdown


def test_clean_content_drops_trailing_empty_headings():
    raw = """# 标题

正文段落包含足够多的内容用于测试，并且带句号，确保不会被边缘规则误删。

### 快速链接

### 热门旅游目的地

### 网站地图
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "快速链接" not in clean.markdown
    assert "网站地图" not in clean.markdown
    assert "正文段落" in clean.markdown


def test_clean_content_drops_trailing_legal_ui_and_short_menu():
    raw = """# 标题

正文段落包含足够多的内容用于测试，并且带句号，确保不会被边缘规则误删。

免责申明

1、本站（网址：example.com）为用户提供服务，用户保证享有著作权，不会侵犯第三方合法权益。

举报收藏点赞

评论 (0条)

* 中国
* 国际
* 文化
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "免责申明" not in clean.markdown
    assert "著作权" not in clean.markdown
    assert "举报" not in clean.markdown
    assert "评论" not in clean.markdown
    assert "国际" not in clean.markdown
    assert "正文段落" in clean.markdown


def test_clean_content_drops_trailing_navbox_table():
    links = " ".join(
        f"[链接{i}](https://example.com/{i})" for i in range(10)
    )
    raw = f"""# 标题

正文段落包含足够多的内容用于测试，并且带句号，确保不会被边缘规则误删。

| {links} |
|---|
"""
    clean = clean_content(raw, url="https://example.com/article")
    assert "链接0" not in clean.markdown
    assert "正文段落" in clean.markdown


def _clean(**overrides) -> CleanResult:
    data = {
        "markdown": "正文",
        "plain_chars": 1000,
        "outbound_links": 0,
        "outbound_text_ratio": 0.0,
        "link_text_ratio": 0.0,
        "nav_ratio": 0.0,
        "substantive_blocks": 5,
        "first_heading": None,
    }
    data.update(overrides)
    return CleanResult(**data)


def test_quality_gate_ignores_nav_ratio_when_content_is_good():
    settings = make_settings().extract
    assert check_quality(_clean(plain_chars=10), settings)[0] == "short_content"
    assert check_quality(_clean(substantive_blocks=0), settings)[0] == "few_paragraphs"
    assert check_quality(_clean(link_text_ratio=0.9), settings)[0] == "link_heavy"
    assert check_quality(_clean(nav_ratio=0.9), settings) is None
    assert check_quality(_clean(), settings) is None


def test_quality_gate_drops_garbled_mojibake():
    settings = make_settings().extract
    # GBK 页面被上游按 UTF-8 误解码后，会残留大量西里尔/亚美尼亚字符
    garbled = "ѯ" * 300
    assert check_quality(_clean(markdown=garbled), settings)[0] == "garbled_content"


def test_quality_gate_keeps_normal_multilingual_content():
    settings = make_settings().extract
    for text in (
        "正常的正文内容，包含中文标点。" * 30,
        "Normal English text with “curly quotes” and café. " * 30,
        "مرحبا بالعالم، هذا نص عربي عادي. " * 30,
    ):
        assert check_quality(_clean(markdown=text), settings) is None
    # 非 ASCII 字符太少时不判定，避免误杀英文短文本
    assert check_quality(_clean(markdown="ѯ" * 50), settings) is None


def test_plain_length_ignores_markdown_and_urls():
    markdown = "# 标题\n\n[链接文字](https://example.com/very/long/url)\n\n正文"
    assert plain_length(markdown) == visible_length("标题链接文字正文")


def test_build_extracted_doc_uses_metadata_fallbacks():
    task = UrlTask(
        url="https://example.com/a",
        normalized_url="example.com/a",
        matched_queries=["中国 美食"],
        published_at="2026-11-01T00:00:00Z",
    )
    content = ExtractedContent(url=task.url, raw_content="raw")
    clean = clean_content(
        "# 正文标题\n\n这是一段足够长的正文内容，用于测试标题回退和元数据组装，"
        "再补充一些文字确保它达到实质段落的长度要求。",
        url=task.url,
    )
    doc = build_extracted_doc(task, content, clean)
    assert doc.title == "正文标题"
    assert doc.site == "example.com"
    assert doc.final_url == task.url
    assert doc.published_at == "2026-11-01T00:00:00+00:00"
    assert doc.matched_queries == ["中国 美食"]
    assert doc.extractor == "tavily-extract"


def test_build_extracted_doc_falls_back_to_host():
    task = UrlTask(url="https://example.com/a", normalized_url="example.com/a")
    content = ExtractedContent(url=task.url, raw_content="raw")
    clean = _clean(markdown="正文", first_heading=None)
    doc = build_extracted_doc(task, content, clean)
    assert doc.title == "example.com"
