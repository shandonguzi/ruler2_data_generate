"""运行汇总：统计结构、丢弃原因标签与控制台输出。"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from .models import Failure

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
    unfetchable_skipped: int = 0
    prefilter_reasons: Counter = field(default_factory=Counter)
    history_skipped: int = 0
    diversity_reasons: Counter = field(default_factory=Counter)
    quality_reasons: Counter = field(default_factory=Counter)
    drop_reasons: Counter = field(default_factory=Counter)
    failures: list[Failure] = field(default_factory=list)

    # 丢弃篇数统一由原因 Counter 求和，避免两个来源各自累加后不一致。
    @property
    def prefiltered_urls(self) -> int:
        return sum(self.prefilter_reasons.values())

    @property
    def quality_dropped(self) -> int:
        return sum(self.quality_reasons.values())

    @property
    def dropped_documents(self) -> int:
        return sum(self.drop_reasons.values())

    @property
    def diversity_dropped(self) -> int:
        return sum(self.diversity_reasons.values())


class StageAborted(Exception):
    """阶段遇到致命错误；stats 携带该阶段尚未合并的统计。"""

    def __init__(self, stage: str, stats: Any) -> None:
        super().__init__(f"阶段中止：{stage}")
        self.stage = stage
        self.stats = stats


@dataclass
class KeywordStats:
    keywords: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)

    def merge_into(self, summary: RunSummary) -> None:
        summary.keywords = self.keywords
        summary.failures.extend(self.failures)


@dataclass
class SearchStats:
    failures: list[Failure] = field(default_factory=list)

    def merge_into(self, summary: RunSummary) -> None:
        summary.failures.extend(self.failures)


@dataclass
class UrlFilterStats:
    history_skipped: int = 0
    prefilter_reasons: Counter = field(default_factory=Counter)
    rejected_domains: int = 0
    unfetchable_skipped: int = 0

    def merge_into(self, summary: RunSummary) -> None:
        summary.history_skipped = self.history_skipped
        summary.prefilter_reasons.update(self.prefilter_reasons)
        summary.rejected_domains = self.rejected_domains
        summary.unfetchable_skipped = self.unfetchable_skipped


@dataclass
class ExtractStats:
    failures: list[Failure] = field(default_factory=list)
    quality_reasons: Counter = field(default_factory=Counter)

    def merge_into(self, summary: RunSummary) -> None:
        summary.failures.extend(self.failures)
        summary.quality_reasons.update(self.quality_reasons)


@dataclass
class EvaluateStats:
    failures: list[Failure] = field(default_factory=list)
    drop_reasons: Counter = field(default_factory=Counter)

    def merge_into(self, summary: RunSummary) -> None:
        summary.failures.extend(self.failures)
        summary.drop_reasons.update(self.drop_reasons)


@dataclass
class DiversityStats:
    reasons: Counter = field(default_factory=Counter)

    def merge_into(self, summary: RunSummary) -> None:
        summary.diversity_reasons.update(self.reasons)


@dataclass
class DocumentStats:
    documents: int = 0
    failures: list[Failure] = field(default_factory=list)

    def merge_into(self, summary: RunSummary) -> None:
        summary.documents = self.documents
        summary.failures.extend(self.failures)


def format_counts(counts: Counter, labels: dict[str, str]) -> str:
    """把 Counter 格式化为「标签 数量」的中文明细。"""
    return "、".join(
        f"{labels.get(reason, reason)} {count}" for reason, count in counts.most_common()
    )


def print_summary(summary: RunSummary) -> None:
    print(f"运行完成：处理 {summary.processed_urls} 个 URL，生成 {summary.documents} 篇文档。")
    if summary.history_skipped:
        print(f"历史跳过 {summary.history_skipped} 个已收 URL。")
    if (
        summary.rejected_domains
        or summary.unfetchable_skipped
        or summary.prefiltered_urls
        or summary.dropped_documents
        or summary.diversity_dropped
        or summary.quality_dropped
    ):
        print(
            f"域名拒绝 {summary.rejected_domains} 个，"
            f"无法提取跳过 {summary.unfetchable_skipped} 个，"
            f"粗筛丢弃 {summary.prefiltered_urls} 个，"
            f"质量丢弃 {summary.quality_dropped} 篇，"
            f"评估丢弃 {summary.dropped_documents} 篇，"
            f"多样性丢弃 {summary.diversity_dropped} 篇。"
        )
    if summary.prefilter_reasons:
        print(f"粗筛丢弃明细：{format_counts(summary.prefilter_reasons, PREFILTER_LABELS)}。")
    if summary.quality_reasons:
        print(f"质量丢弃明细：{format_counts(summary.quality_reasons, QUALITY_LABELS)}。")
    if summary.drop_reasons:
        print(f"评估丢弃明细：{format_counts(summary.drop_reasons, DROP_REASON_LABELS)}。")
    if summary.diversity_reasons:
        print(f"多样性丢弃明细：{format_counts(summary.diversity_reasons, DIVERSITY_LABELS)}。")
