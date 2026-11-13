from __future__ import annotations

from scopecrawl.content import (
    CleanResult,
    build_extracted_doc,
    check_quality,
    chunk_urls,
    clean_content,
    plain_length,
)
from scopecrawl.models import ExtractedContent, UrlTask
from scopecrawl.utils import visible_length
from tests.conftest import make_settings


def test_chunk_urls_splits_into_batches():
    urls = [f"https://example.com/{index}" for index in range(45)]
    batches = chunk_urls(urls, 20)
    assert [len(batch) for batch in batches] == [20, 20, 5]


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


def test_clean_content_strip_links_keeps_text():
    raw = (
        "正文段落包含足够多的内容，用于通过边缘导航规则并保留下来，"
        '这里带一个[参考链接](https://example.org/report "报告")。再补充一些文字。'
    )
    clean = clean_content(raw, url="https://example.com/article", strip_links=True)
    assert "参考链接" in clean.markdown
    assert "example.org" not in clean.markdown
    assert "报告" not in clean.markdown
    assert "](" not in clean.markdown


def test_clean_content_strip_links_handles_parentheses_in_href():
    raw = (
        "正文段落包含足够多的内容，用于通过边缘导航规则并保留下来，"
        "包含[维基条目](/wiki/Foo_(bar))与[模板链接]({{alink('read_more')}})。"
        "再补充一些文字。"
    )
    clean = clean_content(raw, url="https://example.com/article", strip_links=True)
    assert "维基条目" in clean.markdown
    assert "模板链接" in clean.markdown
    assert "/wiki/" not in clean.markdown
    assert "alink" not in clean.markdown
    assert "}})" not in clean.markdown


def test_clean_content_strip_links_removes_orphan_destination():
    raw = (
        "正文段落包含足够多的内容，用于通过边缘导航规则并保留下来。\n\n"
        "评估、规划、实施和衡量软件做法与功能，用于对业务应用组合进行现代化改造和简化。"
        "](https://example.org/orphan)\n\n"
        "后续正文继续补充足够多的文字，确保整体长度满足要求并保留下来。"
    )
    clean = clean_content(raw, url="https://example.com/article", strip_links=True)
    assert "评估、规划" in clean.markdown
    assert "example.org" not in clean.markdown
    assert "](" not in clean.markdown


def test_clean_content_strip_images_removes_whole_image():
    raw = (
        "正文段落包含足够多的内容，用于通过边缘导航规则并保留下来，"
        "这里带一张![示意图](https://example.com/a.png)，"
        "后面继续中文正文以确保段落完整。"
    )
    clean = clean_content(raw, url="https://example.com/article", strip_images=True)
    assert "示意图" not in clean.markdown
    assert "a.png" not in clean.markdown
    assert "![" not in clean.markdown
    assert "这里带一张" in clean.markdown
    assert "后面继续中文正文" in clean.markdown


def test_clean_content_strip_switches_skip_fenced_code():
    raw = """# 标题

正文段落包含足够多的内容，用于通过边缘导航规则并保留下来，带[正文链接](https://example.org/body)，再补充一些文字。

```text
[代码链接](https://example.org/code)
![代码图片](https://example.org/code.png)
```
"""
    clean = clean_content(
        raw,
        url="https://example.com/article",
        strip_links=True,
        strip_images=True,
    )
    assert "正文链接" in clean.markdown
    assert "[正文链接](https://example.org/body)" not in clean.markdown
    assert "[代码链接](https://example.org/code)" in clean.markdown
    assert "![代码图片](https://example.org/code.png)" in clean.markdown


def test_clean_content_strip_switches_keep_metrics():
    raw = (
        "正文段落包含足够多的内容，用于通过边缘导航规则并保留下来，"
        "这里带一张![示意图](https://example.com/a.png)和一个"
        "[参考链接](https://example.org/report)，再补充一些文字。"
    )
    before = clean_content(raw, url="https://example.com/article")
    after = clean_content(
        raw,
        url="https://example.com/article",
        strip_links=True,
        strip_images=True,
    )
    for field in (
        "plain_chars",
        "outbound_links",
        "kept_link_text_ratio",
        "raw_link_text_ratio",
        "nav_ratio",
        "substantive_blocks",
    ):
        assert getattr(after, field) == getattr(before, field), field


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
        "kept_link_text_ratio": 0.0,
        "raw_link_text_ratio": 0.0,
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
    assert check_quality(_clean(raw_link_text_ratio=0.9), settings)[0] == "link_heavy"
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
    doc = build_extracted_doc(task, content, clean, extractor="tavily-extract")
    assert doc.title == "正文标题"
    assert doc.site == "example.com"
    assert doc.final_url == task.url
    assert doc.published_at == "2026-11-01T00:00:00+00:00"
    assert doc.matched_queries == ["中国 美食"]
    assert doc.plain_chars == clean.plain_chars
    assert doc.extractor == "tavily-extract"


def test_build_extracted_doc_falls_back_to_host():
    task = UrlTask(url="https://example.com/a", normalized_url="example.com/a")
    content = ExtractedContent(url=task.url, raw_content="raw")
    clean = _clean(markdown="正文", first_heading=None)
    doc = build_extracted_doc(task, content, clean, extractor="tavily-extract")
    assert doc.title == "example.com"
