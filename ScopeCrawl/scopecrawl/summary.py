"""运行汇总：统计结构、丢弃原因标签与控制台输出。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .models import Failure

STAGE_LABELS = {
    "keyword": "关键词生成",
    "search": "检索",
    "extract": "提取清洗",
    "quality": "质量",
    "review": "内容复核",
    "authority": "权威度评估",
    "write": "生成文档",
}

QUALITY_LABELS = {
    "short_content": "短正文",
    "few_paragraphs": "段落不足",
    "link_heavy": "链接过多",
    "garbled_content": "编码乱码",
}

DROP_REASON_LABELS = {
    "structural": "结构页",
    "no_content": "无正文",
    "relevance_none": "相关性不足",
    "relevance_low": "相关性偏低",
    "relevance_unevaluated": "相关性未评估",
    "content_both_low": "内容空洞",
    "authority_low": "权威度过低",
}

PREFILTER_LABELS = {
    "homepage": "首页",
    "index_page": "索引页",
    "category_page": "栏目页",
    "channel_page": "频道页",
    "section_page": "分区页",
    "search_page": "搜索页",
}

DIVERSITY_LABELS = {
    "query_quota": "单查询超限",
    "domain_quota": "单域名超限",
}


@dataclass
class RunSummary:
    run_id: str
    keywords: list[str] = field(default_factory=list)
    processed_urls: int = 0
    documents: int = 0
    rejected_domains: int = 0
    prefiltered_urls: int = 0
    prefilter_reasons: Counter = field(default_factory=Counter)
    history_skipped: int = 0
    dropped_documents: int = 0
    diversity_dropped: int = 0
    diversity_reasons: Counter = field(default_factory=Counter)
    quality_dropped: int = 0
    quality_reasons: Counter = field(default_factory=Counter)
    drop_reasons: Counter = field(default_factory=Counter)
    failures: list[Failure] = field(default_factory=list)


def print_summary(summary: RunSummary) -> None:
    print(f"运行完成：处理 {summary.processed_urls} 个 URL，生成 {summary.documents} 篇文档。")
    if summary.history_skipped:
        print(f"历史跳过 {summary.history_skipped} 个已收 URL。")
    if (
        summary.rejected_domains
        or summary.prefiltered_urls
        or summary.dropped_documents
        or summary.diversity_dropped
        or summary.quality_dropped
    ):
        print(
            f"域名拒绝 {summary.rejected_domains} 个，"
            f"粗筛丢弃 {summary.prefiltered_urls} 个，"
            f"质量丢弃 {summary.quality_dropped} 篇，"
            f"评估丢弃 {summary.dropped_documents} 篇，"
            f"多样性丢弃 {summary.diversity_dropped} 篇。"
        )
    if summary.prefilter_reasons:
        detail = "、".join(
            f"{PREFILTER_LABELS.get(reason, reason)} {count}"
            for reason, count in summary.prefilter_reasons.most_common()
        )
        print(f"粗筛丢弃明细：{detail}。")
    if summary.quality_reasons:
        detail = "、".join(
            f"{QUALITY_LABELS.get(reason, reason)} {count}"
            for reason, count in summary.quality_reasons.most_common()
        )
        print(f"质量丢弃明细：{detail}。")
    if summary.drop_reasons:
        detail = "、".join(
            f"{DROP_REASON_LABELS.get(reason, reason)} {count}"
            for reason, count in summary.drop_reasons.most_common()
        )
        print(f"评估丢弃明细：{detail}。")
    if summary.diversity_reasons:
        detail = "、".join(
            f"{DIVERSITY_LABELS.get(reason, reason)} {count}"
            for reason, count in summary.diversity_reasons.most_common()
        )
        print(f"多样性丢弃明细：{detail}。")
