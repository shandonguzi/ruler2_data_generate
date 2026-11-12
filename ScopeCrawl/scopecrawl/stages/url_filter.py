"""阶段 3：筛选候选 URL。"""

from __future__ import annotations

import logging
from pathlib import Path

from ..config import load_authority
from ..evaluate import DomainFilter
from ..history import UrlHistory
from ..models import (
    AuthorityLists,
    DomainAssessment,
    SearchResponse,
    Settings,
    UrlTask,
)
from ..summary import PREFILTER_LABELS, UrlFilterStats, format_counts
from ..utils import (
    build_url_task,
    dedupe_results,
    domain_matches,
    index_url_reason,
    interleave_by_query,
    log_stage,
    url_host,
)

logger = logging.getLogger(__name__)


class UrlFilterStage:
    """对搜索结果做去重、历史过滤、无法提取域名过滤、域名预筛、粗筛与配额截断。"""

    def __init__(
        self,
        settings: Settings,
        history: UrlHistory,
        *,
        project_root: Path,
    ) -> None:
        self.settings = settings
        self.history = history
        self.project_root = project_root

    def run(
        self, responses: list[tuple[int, str, SearchResponse]]
    ) -> tuple[list[tuple[UrlTask, DomainAssessment | None]], UrlFilterStats]:
        log_stage("URL 筛选")
        stats = UrlFilterStats()
        candidates = self._candidates(responses, stats)
        raw_results = sum(len(response.results) for _, _, response in responses)
        logger.info(
            "[URL 筛选] 原始结果 %d 条，去重与历史过滤后候选 %d 个 URL",
            raw_results,
            len(candidates),
        )

        rejected: list[UrlTask] = []
        lists = load_authority(
            self.settings.authority.config_path, project_root=self.project_root
        )
        candidates = self._skip_unfetchable(candidates, lists, stats, rejected)
        candidates, assessments = self._domain_prefilter(
            candidates, lists, stats, rejected
        )
        if self.settings.search.prefilter.enabled:
            candidates = self._url_prefilter(candidates, stats, rejected)

        max_urls = self.settings.search.dedupe.max_urls
        if max_urls is not None and len(candidates) > max_urls:
            # 截断放在预筛之后，名额优先分配给能通过预筛的 URL。
            candidates = interleave_by_query(candidates, max_urls)
            logger.info(
                "[URL 筛选] 按关键词轮转截断到 %d 个（上限 %d）",
                len(candidates),
                max_urls,
            )
        logger.info("[URL 筛选] 进入采集队列 %d 个 URL", len(candidates))
        self._record_history(rejected, candidates)
        return [
            (task, assessments.get(url_host(task.url))) for task in candidates
        ], stats

    def _candidates(
        self,
        responses: list[tuple[int, str, SearchResponse]],
        stats: UrlFilterStats,
    ) -> list[UrlTask]:
        dedupe = self.settings.search.dedupe
        pairs = [
            (index, query, result)
            for index, query, response in responses
            for result in response.results
        ]

        if dedupe.enabled:
            # 先不截断，历史命中造成的名额空缺在最终截断时由后续结果补位。
            tasks = dedupe_results(
                pairs,
                merge_www=dedupe.merge_www,
                strip_tracking=dedupe.strip_tracking_params,
            )
        else:
            tasks = [
                build_url_task(
                    query_index,
                    query,
                    result,
                    merge_www=dedupe.merge_www,
                    strip_tracking=dedupe.strip_tracking_params,
                )
                for query_index, query, result in pairs
            ]

        kept = [task for task in tasks if not self.history.contains(task.normalized_url)]
        stats.history_skipped = len(tasks) - len(kept)
        if stats.history_skipped:
            logger.info(
                "[URL 筛选] 历史命中 %d 个已收 URL，跳过", stats.history_skipped
            )
        return kept

    def _record_history(self, rejected: list[UrlTask], queued: list[UrlTask]) -> None:
        """记录被域名/粗筛拒绝与最终入队的 URL；被 max_urls 截断的留待下次。"""
        urls = [task.normalized_url for task in rejected]
        urls.extend(task.normalized_url for task in queued)
        if not urls:
            return
        self.history.record(urls)
        try:
            self.history.save()
        except OSError as exc:
            logger.warning(
                "[URL 筛选] 保存 URL 历史表失败：%s（%s）", self.history.path, exc
            )

    def _url_prefilter(
        self,
        tasks: list[UrlTask],
        stats: UrlFilterStats,
        rejected: list[UrlTask],
    ) -> list[UrlTask]:
        logger.info("[URL 筛选] 粗筛开始：评估 %d 个 URL…", len(tasks))
        kept: list[UrlTask] = []
        for task in tasks:
            reason = index_url_reason(task.url)
            if reason is None:
                kept.append(task)
                continue
            stats.prefilter_reasons[reason] += 1
            rejected.append(task)
            logger.debug("[URL 筛选] 粗筛丢弃：%s（%s）", task.url, reason)
        detail = format_counts(stats.prefilter_reasons, PREFILTER_LABELS)
        logger.info(
            "[URL 筛选] 粗筛结束：保留 %d 个，丢弃 %d 个%s",
            len(kept),
            sum(stats.prefilter_reasons.values()),
            f"（{detail}）" if detail else "",
        )
        return kept

    def _skip_unfetchable(
        self,
        tasks: list[UrlTask],
        lists: AuthorityLists,
        stats: UrlFilterStats,
        rejected: list[UrlTask],
    ) -> list[UrlTask]:
        """跳过提取器拿不到正文的域名；在粗筛与配额截断前执行，避免占用名额。"""
        if not lists.unfetchable:
            return tasks
        kept: list[UrlTask] = []
        for task in tasks:
            host = url_host(task.url)
            entry = next(
                (
                    item
                    for item in lists.unfetchable
                    if domain_matches(host, item.domain)
                ),
                None,
            )
            if entry is None:
                kept.append(task)
                continue
            stats.unfetchable_skipped += 1
            rejected.append(task)
            logger.debug(
                "[URL 筛选] 跳过无法提取的域名：%s（%s）",
                task.url,
                entry.reason or entry.domain,
            )
        if stats.unfetchable_skipped:
            logger.info(
                "[URL 筛选] 跳过无法提取的域名 %d 个", stats.unfetchable_skipped
            )
        return kept

    def _domain_prefilter(
        self,
        tasks: list[UrlTask],
        lists: AuthorityLists,
        stats: UrlFilterStats,
        rejected: list[UrlTask],
    ) -> tuple[list[UrlTask], dict[str, DomainAssessment]]:
        if not self.settings.authority.enabled:
            logger.info("[URL 筛选] 域名预筛已禁用，跳过")
            return tasks, {}

        domain_filter = DomainFilter(lists, self.settings.authority)
        hosts = [url_host(task.url) for task in tasks]
        logger.info("[URL 筛选] 域名预筛开始：评估 %d 个域名…", len(set(hosts)))
        assessments: dict[str, DomainAssessment] = {}
        kept: list[UrlTask] = []
        for task, host in zip(tasks, hosts):
            assessment = assessments.get(host)
            if assessment is None:
                assessment = assessments[host] = domain_filter.assess(host)
            if assessment.decision == "reject":
                stats.rejected_domains += 1
                rejected.append(task)
                continue
            kept.append(task)
        logger.info(
            "[URL 筛选] 域名预筛结束：保留 %d 个 URL，拒绝 %d 个",
            len(kept),
            stats.rejected_domains,
        )
        return kept, assessments
