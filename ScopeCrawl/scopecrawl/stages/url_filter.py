"""阶段 3：筛选候选 URL。"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import load_authority
from ..evaluate import DomainFilter
from ..history import UrlHistory
from ..models import DomainAssessment, SearchResponse, Settings, UrlTask
from ..summary import PREFILTER_LABELS, RunSummary
from ..utils import (
    dedupe_results,
    index_url_reason,
    interleave_by_query,
    normalize_url,
    url_host,
)
from .log import log_stage

logger = logging.getLogger(__name__)


class UrlFilterStage:
    """对搜索结果做去重、历史过滤、粗筛与域名预筛，得到待采集 URL。"""

    def __init__(
        self,
        settings: Settings,
        history: UrlHistory,
        summary: RunSummary,
        *,
        project_root: Path,
    ) -> None:
        self.settings = settings
        self.history = history
        self.summary = summary
        self.project_root = project_root

    def run(
        self, responses: list[tuple[int, str, SearchResponse]]
    ) -> list[tuple[UrlTask, DomainAssessment | None]]:
        log_stage("URL 筛选")
        tasks = self._build_url_queue(responses)
        raw_results = sum(len(response.results) for _, _, response in responses)
        logger.info(
            "[URL 筛选] 原始结果 %d 条，进入预筛队列 %d 个 URL",
            raw_results,
            len(tasks),
        )
        self._record_queue(tasks)

        if self.settings.search.prefilter.enabled:
            tasks = self._url_prefilter(tasks)
        return self._domain_prefilter(tasks)

    def _build_url_queue(
        self, responses: list[tuple[int, str, SearchResponse]]
    ) -> list[UrlTask]:
        dedupe = self.settings.search.dedupe
        provider = responses[0][2].provider if responses else "tavily"
        pairs = [
            (index, query, result)
            for index, query, response in responses
            for result in response.results
        ]

        if dedupe.enabled:
            # 先不截断，命中历史后由后续结果补位，最后再按上限截断。
            tasks = dedupe_results(
                pairs,
                provider=provider,
                merge_www=dedupe.merge_www,
                strip_tracking=dedupe.strip_tracking_params,
                max_urls=None,
            )
        else:
            tasks = [
                UrlTask(
                    url=result.url,
                    normalized_url=normalize_url(
                        result.url,
                        merge_www=dedupe.merge_www,
                        strip_tracking=dedupe.strip_tracking_params,
                    ),
                    matched_queries=[query],
                    best_rank=result.rank,
                    first_query_index=query_index,
                    provider=provider,
                    title=result.title,
                    snippet=result.snippet,
                    published_at=result.published_at,
                )
                for query_index, query, result in pairs
            ]

        kept = [task for task in tasks if not self.history.contains(task.normalized_url)]
        self.summary.history_skipped = len(tasks) - len(kept)
        if self.summary.history_skipped:
            logger.info(
                "[URL 筛选] 历史命中 %d 个已收 URL，跳过", self.summary.history_skipped
            )

        if dedupe.max_urls is not None:
            # 按关键词轮转取数，避免靠前的关键词占满配额导致后续关键词无 URL 进入。
            tasks = interleave_by_query(kept, dedupe.max_urls)
            if self.summary.history_skipped:
                logger.info(
                    "[URL 筛选] 由后续结果补位，按关键词轮转后进入队列 %d 个（上限 %d）",
                    len(tasks),
                    dedupe.max_urls,
                )
            return tasks
        return kept

    def _record_queue(self, tasks: list[UrlTask]) -> None:
        """进入预筛队列的 URL 立即入历史表，避免被后续筛选的页面下次重复召回。"""
        if not tasks:
            return
        self.history.record(task.normalized_url for task in tasks)
        try:
            self.history.save()
        except OSError as exc:
            logger.warning(
                "[URL 筛选] 保存 URL 历史表失败：%s（%s）", self.history.path, exc
            )

    def _url_prefilter(self, tasks: list[UrlTask]) -> list[UrlTask]:
        logger.info("[URL 筛选] 粗筛开始：评估 %d 个 URL…", len(tasks))
        kept: list[UrlTask] = []
        for task in tasks:
            reason = index_url_reason(task.url)
            if reason is None:
                kept.append(task)
                continue
            self.summary.prefiltered_urls += 1
            self.summary.prefilter_reasons[reason] += 1
            logger.debug("[URL 筛选] 粗筛丢弃：%s（%s）", task.url, reason)
        detail = "、".join(
            f"{PREFILTER_LABELS.get(reason, reason)} {count}"
            for reason, count in self.summary.prefilter_reasons.most_common()
        )
        logger.info(
            "[URL 筛选] 粗筛结束：保留 %d 个，丢弃 %d 个%s",
            len(kept),
            self.summary.prefiltered_urls,
            f"（{detail}）" if detail else "",
        )
        return kept

    def _domain_prefilter(
        self, tasks: list[UrlTask]
    ) -> list[tuple[UrlTask, DomainAssessment | None]]:
        if not self.settings.authority.enabled:
            logger.info("[URL 筛选] 域名预筛已禁用，跳过")
            return [(task, None) for task in tasks]

        logger.info("[URL 筛选] 域名预筛开始：评估 %d 个域名…", len(tasks))
        lists = load_authority(
            self.settings.authority.config_path, project_root=self.project_root
        )
        domain_filter = DomainFilter(lists, self.settings.authority)
        kept: list[tuple[UrlTask, DomainAssessment | None]] = []
        for task in tasks:
            assessment = domain_filter.assess(url_host(task.url))
            if assessment.decision == "reject":
                self.summary.rejected_domains += 1
                continue
            kept.append((task, assessment))
        logger.info(
            "[URL 筛选] 域名预筛结束：保留 %d 个 URL，拒绝 %d 个",
            len(kept),
            self.summary.rejected_domains,
        )
        return kept
