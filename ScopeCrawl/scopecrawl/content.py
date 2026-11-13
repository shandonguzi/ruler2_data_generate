"""正文清洗、质量闸门与通用提取辅助。"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from urllib.parse import urlsplit

from .models import (
    ExtractedContent,
    ExtractedDoc,
    ExtractSettings,
    UrlTask,
)
from .utils import (
    make_doc_id,
    normalize_url,
    to_iso,
    url_host,
    utc_now,
    visible_length,
)

_SENTENCE_RE = re.compile(r"[。！？；.!?;؟]")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+")
_LIST_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:|-]+\|?\s*$")
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
# 清洗输出专用：允许 href 中含一层配对括号（维基 `Foo_(bar)`、JS 模板 `{{alink('x')}}`）。
_STRIP_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?:[^()\s]|\([^()]*\))*(?:\s+\"[^\"]*\")?\)")
_STRIP_LINK_RE = re.compile(r"\[([^\]]*)\]\((?:[^()\s]|\([^()]*\))*(?:\s+\"[^\"]*\")?\)")
# 源文本缺失左括号时残留的孤立链接尾部，如“文字。](https://...)”
_STRIP_ORPHAN_LINK_RE = re.compile(r"\]\((?:[^()\s]|\([^()]*\))*(?:\s+\"[^\"]*\")?\)")
_BARE_URL_RE = re.compile(r"https?://\S+")

_NAV_WORDS = (
    "首页",
    "登录",
    "注册",
    "导航",
    "热门",
    "相关推荐",
    "更多",
    "菜单",
    "栏目",
    "关于我们",
    "联系我们",
    "隐私",
    "条款",
    "版权",
    "目录",
    "进入词条",
    "全站搜索",
    "国际版",
    "帮助",
    "切换模式",
    "播报",
    "上传视频",
    "讨论",
)

_ZERO_WIDTH_RE = re.compile(r"[\u200b-\u200f\ufeff\u00a0]")

_FOOTER_STRONG = (
    "版权所有",
    "copyright",
    "icp备",
    "icp证",
    "公安备案",
    "备案号",
    "gmt+8",
    "gmt+08",
    "processed in",
    "powered by",
    "免责声明",
    "免责申明",
    "隐私政策",
    "服务条款",
)
_FOOTER_WORDS = _FOOTER_STRONG + (
    "版权",
    "备案",
    "联系我们",
    "联系电话",
    "电话",
    "邮箱",
    "微信",
    "地址",
    "技术支持",
    "营业执照",
    "侵权",
    "侵犯",
    "著作权",
    "合法权益",
    "原作者",
    "用户",
    "作者",
    "平台",
    "免责",
)
_UI_WORDS = (
    "举报",
    "收藏",
    "点赞",
    "评论",
    "登录后",
    "点击此处",
    "返回顶部",
    "分享到",
)
_CONTACT_RE = re.compile(
    r"(?:电话|邮箱|微信|地址)\s*[:：]|\b\d{7,}\b|[\w.+-]+@[\w-]+\.[\w.]+"
)
_CATEGORY_RE = re.compile(r"(?:category|分类|special:categories)", re.IGNORECASE)
_NAV_HEADING_WORDS = (
    "目录",
    "导航",
    "菜单",
    "相关推荐",
    "网站地图",
    "快速链接",
    "热门目的地",
    "热门旅游目的地",
    "页脚",
    "友情链接",
)


@dataclass
class CleanResult:
    markdown: str
    plain_chars: int
    outbound_links: int
    # 清洗后保留块内的链接文字占比，用于页面评分中的广告/导航扣分。
    kept_link_text_ratio: float
    # 清洗前全文的链接文字占比，用于质量闸门判断链接堆砌。
    raw_link_text_ratio: float
    nav_ratio: float
    substantive_blocks: int
    first_heading: str | None


class ExtractError(Exception):
    """正文提取失败；fatal 表示应终止整个 run。"""

    def __init__(
        self,
        error_type: str,
        message: str,
        *,
        fatal: bool = False,
        failed: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.error_type = error_type
        self.message = message
        self.fatal = fatal
        self.failed = failed or []


def dedupe_inputs(urls: list[str]) -> tuple[list[str], list[str], dict[str, int]]:
    """返回 (原始输入的去重键, 唯一 URL, 键到唯一索引的映射)。"""
    keys: list[str] = []
    unique_urls: list[str] = []
    key_to_unique: dict[str, int] = {}
    for raw_url in urls:
        key = normalize_url(raw_url) or raw_url
        keys.append(key)
        if key not in key_to_unique:
            key_to_unique[key] = len(unique_urls)
            unique_urls.append(raw_url)
    return keys, unique_urls, key_to_unique


def expand_outcomes(
    urls: list[str],
    keys: list[str],
    key_to_unique: dict[str, int],
    unique_urls: list[str],
    outcomes: list[ExtractedContent],
) -> list[ExtractedContent]:
    """把唯一 URL 的结果展开为与原始输入等长、顺序一致的结果。"""
    by_unique = {url: outcome for url, outcome in zip(unique_urls, outcomes, strict=False)}
    expanded: list[ExtractedContent] = []
    for index, raw_url in enumerate(urls):
        outcome = by_unique.get(unique_urls[key_to_unique[keys[index]]])
        if outcome is None:
            expanded.append(
                ExtractedContent(
                    url=raw_url,
                    error_type="missing_result",
                    error="提取结果缺失",
                )
            )
        else:
            expanded.append(replace(outcome, url=raw_url))
    return expanded


def chunk_urls(urls: list[str], size: int) -> list[list[str]]:
    if size < 1:
        raise ValueError("size 必须大于 0")
    return [urls[index : index + size] for index in range(0, len(urls), size)]


def clean_content(
    raw: str,
    *,
    url: str,
    strip_links: bool = False,
    strip_images: bool = False,
) -> CleanResult:
    """清洗 Extract 返回的 Markdown：去导航/图片/重复块，保留正文结构。

    ``strip_links`` / ``strip_images`` 只影响最终 markdown 与 first_heading；
    导航、去重和质量指标均基于清洗前文本计算。
    """
    text = (raw or "").lstrip("﻿")
    text = html.unescape(text).replace("\r\n", "\n").replace("\r", "\n")
    text = _ZERO_WIDTH_RE.sub("", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    pre_visible = max(visible_length(text), 1)

    blocks = [block.strip("\n") for block in re.split(r"\n\s*\n", text) if block.strip()]
    total_link_text = 0
    metadata: list[tuple[str, bool, float, bool]] = []
    keys = [re.sub(r"\s+", "", block.strip()).casefold() for block in blocks]
    first_seen: dict[str, int] = {}
    for index, key in enumerate(keys):
        first_seen.setdefault(key, index)
    counts: dict[str, int] = {}
    for key in keys:
        counts[key] = counts.get(key, 0) + 1

    for index, block in enumerate(blocks):
        stripped = block.strip()
        link_text, link_ratio = _link_stats(stripped)
        total_link_text += link_text
        plain = visible_length(_LINK_RE.sub(r"\1", _IMAGE_RE.sub("", stripped)))
        key = keys[index]

        if _is_image_only(stripped):
            metadata.append((block, True, link_ratio, True))
            continue
        plain_without_links = visible_length(
            _LINK_RE.sub("", _IMAGE_RE.sub("", stripped))
        )
        if plain_without_links == 0 and _LINK_RE.search(stripped):
            metadata.append((block, True, link_ratio, True))
            continue
        if (
            counts[key] >= 3
            and first_seen[key] != index
            and not _is_heading(stripped)
            and plain < 30
        ):
            metadata.append((block, True, link_ratio, True))
            continue
        metadata.append((block, _is_nav_block(stripped, link_ratio), link_ratio, False))

    keep = [not drop for _, _, _, drop in metadata]
    _trim_leading_nav(metadata, keep)
    _trim_edges(metadata, keep)
    _drop_middle_nav(metadata, keep)
    _trim_trailing_nav_sections(metadata, keep)

    kept_blocks = [
        block
        for (block, _, _, _), keep_flag in zip(metadata, keep, strict=False)
        if keep_flag
    ]
    joined = "\n\n".join(kept_blocks).strip()
    removed_visible = max(pre_visible - visible_length(joined), 0)
    markdown = _strip_inline_markdown(
        joined, strip_links=strip_links, strip_images=strip_images
    )

    retained_link_text = 0
    outbound: set[str] = set()
    host = url_host(url)
    substantive = 0
    for block in kept_blocks:
        link_text, link_ratio = _link_stats(block)
        retained_link_text += link_text
        for _, href in _LINK_RE.findall(block):
            if is_http_url(href) and url_host(href) != host:
                outbound.add(href)
        if _is_substantive(block, link_ratio):
            substantive += 1

    retained_visible = max(visible_length(joined), 1)
    first_heading = _first_heading(kept_blocks)
    if first_heading is not None and (strip_links or strip_images):
        first_heading = (
            _strip_inline_markdown(
                first_heading, strip_links=strip_links, strip_images=strip_images
            )
            or None
        )
    return CleanResult(
        markdown=markdown,
        plain_chars=plain_length(markdown),
        outbound_links=len(outbound),
        kept_link_text_ratio=min(max(retained_link_text / retained_visible, 0.0), 1.0),
        raw_link_text_ratio=min(max(total_link_text / pre_visible, 0.0), 1.0),
        nav_ratio=min(max(removed_visible / pre_visible, 0.0), 1.0),
        substantive_blocks=substantive,
        first_heading=first_heading,
    )


def plain_length(text: str) -> int:
    """去掉 Markdown 语法、链接 URL、图片后的纯文本长度。"""
    value = _IMAGE_RE.sub("", text or "")
    value = _LINK_RE.sub(r"\1", value)
    lines: list[str] = []
    for line in value.splitlines():
        if _TABLE_SEP_RE.match(line):
            continue
        line = _HEADING_RE.sub("", line)
        line = _LIST_RE.sub("", line)
        line = line.lstrip("> ").strip()
        lines.append(line)
    value = "\n".join(lines)
    value = value.replace("```", "")
    value = _BARE_URL_RE.sub("", value)
    value = re.sub(r"[*_`~|]", "", value)
    return visible_length(value)


def _strip_inline_markdown(
    text: str, *, strip_links: bool, strip_images: bool
) -> str:
    """按开关清洗行内链接/图片；围栏代码块原样保留，图片先于链接处理。"""
    if not (strip_links or strip_images):
        return text
    parts = re.split(r"(```.*?```)", text, flags=re.S)
    for index in range(0, len(parts), 2):  # 奇数下标是代码块，跳过
        if strip_images:
            parts[index] = _STRIP_IMAGE_RE.sub("", parts[index])
        if strip_links:
            parts[index] = _STRIP_LINK_RE.sub(r"\1", parts[index])
            parts[index] = _STRIP_ORPHAN_LINK_RE.sub("", parts[index])
        parts[index] = re.sub(r"[ \t]+\n", "\n", parts[index])
        parts[index] = re.sub(r"\n{3,}", "\n\n", parts[index])
    return "".join(parts).strip()


# 视为正常正文的字符区间：中文、日韩、阿拉伯/希伯来文、通用标点与 emoji。
_READABLE_RANGES = (
    (0x00A0, 0x00A0),
    (0x00A9, 0x00A9),
    (0x00AE, 0x00AE),
    (0x00B0, 0x00B0),
    (0x00B7, 0x00B7),
    (0x00C0, 0x024F),  # 拉丁扩展：正常西文重音字母
    (0x0590, 0x06FF),  # 希伯来、阿拉伯
    (0x0750, 0x077F),
    (0x2000, 0x206F),  # 通用标点
    (0x20A0, 0x20CF),  # 货币符号
    (0x2100, 0x214F),  # 字母式符号
    (0x2190, 0x21FF),  # 箭头
    (0x3000, 0x303F),  # CJK 标点
    (0x3040, 0x30FF),  # 假名
    (0x3400, 0x4DBF),  # CJK 扩展 A
    (0x4E00, 0x9FFF),  # CJK
    (0xAC00, 0xD7AF),  # 谚文
    (0xFB50, 0xFDFF),
    (0xFE70, 0xFEFF),
    (0xFF00, 0xFFEF),  # 全角
    (0x1F000, 0x1FAFF),  # emoji
)
_GARBLED_MIN_NON_ASCII = 200
_READABLE_CLASS = "".join(
    f"\\U{low:08x}-\\U{high:08x}" for low, high in _READABLE_RANGES
)
_NON_ASCII_RE = re.compile(r"[^\x00-\x7f\s]")
_GARBLED_RE = re.compile(rf"[^\x00-\x7f\s{_READABLE_CLASS}]")


def _garbled_ratio(text: str) -> float:
    """非 ASCII 字符中落在预期文种之外的比例；样本太小时返回 0。"""
    non_ascii = len(_NON_ASCII_RE.findall(text))
    if non_ascii < _GARBLED_MIN_NON_ASCII:
        return 0.0
    return len(_GARBLED_RE.findall(text)) / non_ascii


def check_quality(clean: CleanResult, settings: ExtractSettings) -> tuple[str, str] | None:
    """返回 (error_type, message)；None 表示通过。

    ``nav_ratio`` 只作为诊断指标，不作为独立丢弃条件：原始页面导航多，
    不代表清洗后的正文不可用。
    """
    garbled = _garbled_ratio(clean.markdown)
    if garbled > settings.max_garbled_ratio:
        return "garbled_content", f"疑似编码乱码（异常字符占比 {garbled:.0%}）"
    if clean.plain_chars < settings.min_chars:
        return "short_content", f"正文过短（{clean.plain_chars} < {settings.min_chars}）"
    if clean.substantive_blocks < settings.min_paragraphs:
        return (
            "few_paragraphs",
            f"实质段落不足（{clean.substantive_blocks} < {settings.min_paragraphs}）",
        )
    if clean.raw_link_text_ratio > settings.max_link_text_ratio:
        return (
            "link_heavy",
            "链接文本占比过高"
            f"（{clean.raw_link_text_ratio:.2f} > {settings.max_link_text_ratio:.2f}）",
        )
    return None


def build_extracted_doc(
    task: UrlTask,
    content: ExtractedContent,
    clean: CleanResult,
    *,
    extractor: str,
) -> ExtractedDoc:
    title = (task.title or "").strip() or clean.first_heading or url_host(task.url)
    return ExtractedDoc(
        doc_id=make_doc_id(task.normalized_url),
        url=task.url,
        final_url=content.final_url or task.url,
        title=title,
        site=url_host(task.url),
        published_at=to_iso(task.published_at),
        fetched_at=to_iso(utc_now()) or "",
        lang=None,
        markdown=clean.markdown,
        plain_chars=clean.plain_chars,
        matched_queries=list(task.matched_queries),
        outbound_links=clean.outbound_links,
        kept_link_text_ratio=clean.kept_link_text_ratio,
        extractor=extractor,
    )


def failure_outcomes(
    urls: list[str],
    error_type: str,
    message: str,
    *,
    failed: list[tuple[str, str]] | None = None,
) -> list[ExtractedContent]:
    """把一组 URL 统一包装成失败结果，可按 URL 覆盖错误信息。"""
    per_url = dict(failed or ())
    return [
        ExtractedContent(
            url=url,
            final_url=url,
            error_type=error_type,
            error=per_url.get(url, message),
        )
        for url in urls
    ]


def is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in ("http", "https") and bool(parsed.netloc)


def _link_stats(block: str) -> tuple[int, float]:
    links = _LINK_RE.findall(_IMAGE_RE.sub("", block))
    text_chars = sum(visible_length(text) for text, _ in links)
    visible = max(visible_length(block), 1)
    return text_chars, min(text_chars / visible, 1.0)


def _is_heading(block: str) -> bool:
    first = block.splitlines()[0] if block.splitlines() else ""
    return bool(_HEADING_RE.match(first))


def _is_image_only(block: str) -> bool:
    without_images = _IMAGE_RE.sub("", block).strip()
    return bool(block.strip()) and not without_images


def _is_table(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    if len(lines) < 2:
        return len(lines) == 1 and lines[0].count("|") >= 2
    return any(_TABLE_SEP_RE.match(line) for line in lines) or all(
        line.count("|") >= 2 for line in lines
    )


def _is_list(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    return bool(lines) and all(_LIST_RE.match(line) for line in lines)


def _is_quote(block: str) -> bool:
    return block.lstrip().startswith(">")


def _is_code(block: str) -> bool:
    return block.lstrip().startswith("```")


def _is_nav_block(block: str, link_ratio: float) -> bool:
    if _is_heading(block):
        return _is_nav_heading(block)
    if _is_table(block):
        return _is_navbox_table(block)
    if _is_quote(block) or _is_code(block):
        return False
    if _is_list(block):
        items = [line for line in block.splitlines() if line.strip()]
        link_items = sum(1 for line in items if _LINK_RE.search(line))
        if link_items >= max(2, int(len(items) * 0.8)):
            return True
        anchor_items = sum(1 for line in items if "](" in line and "(#" in line)
        if anchor_items >= max(2, int(len(items) * 0.8)):
            return True
        average = sum(visible_length(_LIST_RE.sub("", line)) for line in items) / max(len(items), 1)
        return len(items) >= 3 and average < 15 and link_ratio >= 0.5
    lines = [line for line in block.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    text = lines[0].strip()
    visible = visible_length(text)
    if visible >= 60:
        return False
    if any(word in text for word in _NAV_WORDS):
        return visible < 20 or link_ratio >= 0.3
    if visible >= 15 or _SENTENCE_RE.search(text):
        return False
    return link_ratio >= 0.5


def _is_footer_block(block: str) -> bool:
    text = _LINK_RE.sub(r"\1", _IMAGE_RE.sub("", block))
    lowered = text.casefold()
    if any(word in lowered for word in _FOOTER_STRONG):
        return True
    signals = sum(1 for word in _FOOTER_WORDS if word in lowered)
    return signals >= 3 or (signals >= 2 and bool(_CONTACT_RE.search(text)))


def _is_category_block(block: str) -> bool:
    text = re.sub(r"[*_`]", "", _LINK_RE.sub("", _IMAGE_RE.sub("", block))).strip()
    if text in {
        "分类：",
        "分类:",
        "隐藏分类：",
        "隐藏分类:",
        "Categories:",
        "Hidden categories:",
    }:
        return True
    links = _LINK_RE.findall(_IMAGE_RE.sub("", block))
    if not links:
        return False
    category_links = sum(
        1
        for label, href in links
        if _CATEGORY_RE.search(href) or _CATEGORY_RE.search(label)
    )
    return category_links >= max(1, int(len(links) * 0.6))


def _is_ui_noise_block(block: str) -> bool:
    lines = [line for line in block.splitlines() if line.strip()]
    if len(lines) != 1:
        return False
    text = _LINK_RE.sub(r"\1", _IMAGE_RE.sub("", block)).strip()
    return visible_length(text) <= 30 and any(word in text for word in _UI_WORDS)


def _is_short_menu(block: str) -> bool:
    if not _is_list(block):
        return False
    items = [line for line in block.splitlines() if line.strip()]
    if len(items) < 3:
        return False
    if any(_SENTENCE_RE.search(_LIST_RE.sub("", line)) for line in items):
        return False
    average = sum(
        visible_length(_LIST_RE.sub("", line)) for line in items
    ) / len(items)
    return average < 12


def _is_nav_heading(block: str) -> bool:
    if not _is_heading(block):
        return False
    heading = _HEADING_RE.sub("", block.splitlines()[0]).strip()
    return any(word in heading for word in _NAV_HEADING_WORDS)


def _is_infobox_table(block: str) -> bool:
    if not _is_table(block):
        return False
    lines = [line for line in block.splitlines() if line.strip()]
    pipes = max((line.count("|") for line in lines), default=0)
    links = _LINK_RE.findall(_IMAGE_RE.sub("", block))
    return 2 <= pipes <= 3 and len(lines) >= 2 and len(links) >= 3


def _is_navbox_table(block: str) -> bool:
    if not _is_table(block):
        return False
    links = _LINK_RE.findall(_IMAGE_RE.sub("", block))
    if len(links) < 8:
        return False
    link_chars = sum(visible_length(label) for label, _ in links)
    return link_chars / max(plain_length(block), 1) >= 0.4


def _is_substantive(block: str, link_ratio: float) -> bool:
    if _is_heading(block) or _is_image_only(block):
        return False
    if link_ratio > 0.5:
        return False
    if _is_list(block):
        items = [line for line in block.splitlines() if line.strip()]
        link_items = sum(1 for line in items if _LINK_RE.search(line))
        if link_items >= max(2, int(len(items) * 0.8)):
            return False
        return len(items) >= 2
    if _is_table(block):
        return len([line for line in block.splitlines() if line.strip()]) >= 2
    return plain_length(block) >= 30


def _trim_leading_nav(
    metadata: list[tuple[str, bool, float, bool]], keep: list[bool]
) -> None:
    for index, (block, navish, link_ratio, dropped) in enumerate(metadata):
        if dropped or navish or _is_infobox_table(block):
            keep[index] = False
            continue
        if _is_heading(block):
            following = _next_content(metadata, index + 1)
            if following is not None and _is_substantive(
                metadata[following][0], metadata[following][2]
            ):
                break
            keep[index] = False
            continue
        if not _is_substantive(block, link_ratio):
            keep[index] = False
            continue
        break


def _next_content(
    metadata: list[tuple[str, bool, float, bool]], index: int
) -> int | None:
    for current in range(index, len(metadata)):
        if not metadata[current][3] and not metadata[current][1]:
            return current
    return None


def _trim_edges(metadata: list[tuple[str, bool, float, bool]], keep: list[bool]) -> None:
    index = 0
    while index < len(metadata):
        block, navish, _, dropped = metadata[index]
        if dropped or (navish and _is_heading(block)) or not _is_edge_content(block):
            keep[index] = False
            index += 1
            continue
        break
    index = len(metadata) - 1
    while index >= 0:
        block, navish, _, dropped = metadata[index]
        if _is_heading(block):
            if navish or not _has_kept_content_after(metadata, keep, index):
                keep[index] = False
                index -= 1
                continue
            break
        if (
            dropped
            or _is_footer_block(block)
            or _is_ui_noise_block(block)
            or _is_short_menu(block)
            or _is_category_block(block)
            or _is_navbox_table(block)
            or not _is_edge_content(block)
        ):
            keep[index] = False
            index -= 1
            continue
        break


def _has_kept_content_after(
    metadata: list[tuple[str, bool, float, bool]], keep: list[bool], index: int
) -> bool:
    return any(
        keep[current] and not _is_heading(metadata[current][0])
        for current in range(index + 1, len(metadata))
    )


def _trim_trailing_nav_sections(
    metadata: list[tuple[str, bool, float, bool]], keep: list[bool]
) -> None:
    """删除尾部导航区（网站地图、快速链接等）及其后的链接块，再清空尾部标题。"""
    for index in range(len(metadata) - 1, -1, -1):
        if not keep[index] or not _is_nav_heading(metadata[index][0]):
            continue
        keep[index] = False
        for current in range(index + 1, len(metadata)):
            if keep[current] and _is_heading(metadata[current][0]):
                break
            keep[current] = False
    for index in range(len(metadata) - 1, -1, -1):
        if (
            keep[index]
            and _is_heading(metadata[index][0])
            and not _has_kept_content_after(metadata, keep, index)
        ):
            keep[index] = False


def _is_edge_content(block: str) -> bool:
    if _is_heading(block) or _is_table(block) or _is_list(block) or _is_code(block):
        return True
    if _is_quote(block):
        return True
    text = _LINK_RE.sub(r"\1", _IMAGE_RE.sub("", block))
    return plain_length(text) >= 30 and bool(_SENTENCE_RE.search(text))


def _drop_middle_nav(metadata: list[tuple[str, bool, float, bool]], keep: list[bool]) -> None:
    for index in range(len(metadata)):
        if not keep[index] or not metadata[index][1]:
            continue
        left = _nearest_kept(metadata, keep, index, step=-1)
        right = _nearest_kept(metadata, keep, index, step=1)
        left_heading = left is not None and _is_heading(metadata[left][0])
        right_heading = right is not None and _is_heading(metadata[right][0])
        left_nav = left is not None and metadata[left][1]
        right_nav = right is not None and metadata[right][1]
        if left_heading or right_heading or (left_nav and right_nav):
            keep[index] = False


def _nearest_kept(
    metadata: list[tuple[str, bool, float, bool]],
    keep: list[bool],
    index: int,
    *,
    step: int,
) -> int | None:
    current = index + step
    while 0 <= current < len(metadata):
        if keep[current]:
            return current
        current += step
    return None


def _first_heading(blocks: list[str]) -> str | None:
    for block in blocks:
        if not _is_heading(block):
            continue
        text = _HEADING_RE.sub("", block.splitlines()[0]).strip()
        if text and not any(word in text for word in _NAV_WORDS):
            return text
    return None
