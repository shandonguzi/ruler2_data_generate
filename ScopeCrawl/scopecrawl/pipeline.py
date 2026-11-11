"""pipeline 编排、线程池、失败收集与控制台汇总。"""

from __future__ import annotations

import logging
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

from .config import PROJECT_ROOT, load_authority
from .content import (
    ExtractError,
    TavilyExtractor,
    build_extracted_doc,
    check_quality,
    chunk_urls,
    clean_content,
)
from .evaluate import (
    DomainFilter,
    apply_diversity,
    assess_page,
    combine_authority,
    review_content,
    should_drop,
)
from .history import DEFAULT_HISTORY_PATH, UrlHistory
from .keywords import KeywordError, generate_keywords
from .llm import LLMClient
from .models import (
    AuthorityResult,
    Document,
    DocumentEvaluation,
    DomainAssessment,
    ExtractedContent,
    ExtractedDoc,
    Failure,
    RelevanceResult,
    SearchRequest,
    SearchResponse,
    Settings,
    UrlTask,
)
from .search import SearchError, SearchProvider, country_from_location, create_search_provider
from .storage import RunWriter
from .utils import (
    dedupe_results,
    domain_matches,
    index_url_reason,
    normalize_url,
    url_host,
)

logger = logging.getLogger(__name__)

STAGE_LABELS = {
    "keyword": "关键词生成",
    "search": "搜索",
    "extract": "提取",
    "quality": "质量",
    "review": "内容复核",
    "authority": "权威度评估",
    "write": "写入",
}

QUALITY_LABELS = {
    "short_content": "短正文",
    "few_paragraphs": "段落不足",
    "link_heavy": "链接过多",
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


def _score_text(level: str | None, score: float | None) -> str:
    if score is None:
        return level or "未评估"
    return f"{level or '未评估'} {score:.2f}"


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


class Pipeline:
    def __init__(
        self,
        settings: Settings,
        *,
        output_root: str | Path | None = None,
        llm_client=None,
        provider: SearchProvider | None = None,
        extractor: TavilyExtractor | None = None,
        writer_factory: Callable[..., RunWriter] = RunWriter,
        prompt_paths: dict[str, Path] | None = None,
        project_root: Path | None = None,
        history_path: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.output_root = output_root
        self.writer_factory = writer_factory
        self.project_root = project_root or PROJECT_ROOT
        self.history_path = Path(history_path) if history_path is not None else DEFAULT_HISTORY_PATH
        self.prompt_paths = prompt_paths or {}
        self.llm = llm_client or LLMClient(
            settings.llm,
            model=settings.llm_model,
            api_key=(settings.llm_api_key.get_secret_value() if settings.llm_api_key else None),
            base_url=settings.llm_base_url,
        )
        self.provider = provider or create_search_provider(
            settings.search,
            api_key=(
                settings.tavily_api_key.get_secret_value() if settings.tavily_api_key else None
            ),
        )
        self.extractor = extractor or TavilyExtractor(
            settings.extract,
            api_key=(
                settings.tavily_api_key.get_secret_value() if settings.tavily_api_key else None
            ),
        )

    def run(self) -> tuple[RunSummary, int]:
        writer = self.writer_factory(self.output_root, self.settings.run)
        summary = RunSummary(run_id=writer.run_id)
        run_config = self.settings.run
        history = UrlHistory(self.history_path)
        logger.info(
            "开始运行：s3=%s，s4=%s，地点=%s，语言=%s",
            run_config.s3,
            run_config.s4,
            run_config.location or "未指定",
            run_config.language,
        )
        logger.info("输出目录：%s", writer.run_dir)

        logger.info("[关键词] 正在调用 LLM 生成搜索关键词…")
        try:
            summary.keywords = generate_keywords(
                self.settings.run,
                self.llm,
                self.settings.keywords,
                prompt_path=self.prompt_paths.get("keywords"),
            )
        except KeywordError as exc:
            logger.error("关键词生成失败：%s", exc)
            summary.failures.append(
                Failure(stage="keyword", error_type="keyword_error", message=str(exc))
            )
            self.print_summary(summary)
            return summary, 1
        logger.info(
            "[关键词] 生成 %d 条：%s",
            len(summary.keywords),
            " | ".join(summary.keywords),
        )

        responses = self._search(summary)
        if responses is None:
            self.print_summary(summary)
            return summary, 1

        kept = self._prepare_urls(responses, summary, history)
        summary.processed_urls = len(kept)

        documents, fatal = self._extract_content(kept, summary)
        if fatal:
            self.print_summary(summary)
            return summary, 1
        evaluated = self._evaluate(documents, summary)
        self._write_all(evaluated, writer, summary)

        self.print_summary(summary)
        return summary, 0

    # ------------------------------------------------------------------ 搜索

    def _search(self, summary: RunSummary) -> list[tuple[int, str, SearchResponse]] | None:
        keywords = summary.keywords
        country = self.settings.search.defaults.country or country_from_location(
            self.settings.run.location
        )
        logger.info(
            "[搜索] country=%s，地点=%s",
            country or "none",
            self.settings.run.location or "none",
        )
        requests = [
            SearchRequest(
                query=query,
                language=self.settings.run.language,
                country=country,
                location=self.settings.run.location,
                max_results=self.settings.search.defaults.max_results,
                time_range=self.settings.run.time_range,
            )
            for query in keywords
        ]

        responses: list[SearchResponse | None] = [None] * len(requests)
        fatal_400 = False
        failed = 0
        done = 0
        workers = max(1, min(len(requests), 8))
        logger.info(
            "[搜索] 开始 %d 条查询（每条最多 %d 条结果）…",
            len(requests),
            self.settings.search.defaults.max_results,
        )

        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(self.provider.search, request): index
                for index, request in enumerate(requests)
            }
            for future in as_completed(futures):
                index = futures[future]
                done += 1
                try:
                    response = future.result()
                    responses[index] = response
                    logger.info(
                        "[搜索] 完成 %d/%d：%s → %d 条",
                        done,
                        len(requests),
                        keywords[index],
                        len(response.results),
                    )
                except SearchError as exc:
                    failed += 1
                    logger.warning(
                        "[搜索] 失败 %d/%d：%s（%s）", done, len(requests), keywords[index], exc
                    )
                    summary.failures.append(
                        Failure(
                            stage="search",
                            error_type="search_error",
                            message=f"{keywords[index]}：{exc}",
                        )
                    )
                    if exc.status_code == 400:
                        fatal_400 = True
                except Exception as exc:  # noqa: BLE001
                    failed += 1
                    logger.warning(
                        "[搜索] 失败 %d/%d：%s（%s）", done, len(requests), keywords[index], exc
                    )
                    summary.failures.append(
                        Failure(
                            stage="search",
                            error_type="search_error",
                            message=f"{keywords[index]}：{exc}",
                        )
                    )

        succeeded = sum(1 for response in responses if response is not None)
        logger.info("[搜索] 结束：成功 %d 条查询，失败 %d 条", succeeded, failed)
        if fatal_400:
            logger.error("搜索参数错误（HTTP 400），终止 run")
            return None
        if failed == len(requests):
            logger.error("所有搜索请求均失败，终止 run")
            return None

        return [
            (index, keywords[index], response)
            for index, response in enumerate(responses)
            if response is not None
        ]

    # ---------------------------------------------------------------- URL 预筛

    def _prepare_urls(
        self,
        responses: list[tuple[int, str, SearchResponse]],
        summary: RunSummary,
        history: UrlHistory,
    ) -> list[tuple[UrlTask, DomainAssessment | None]]:
        """URL 级预筛层：去重/历史 → 截断 → 粗筛 → 域名预筛。"""
        tasks = self._build_url_queue(responses, summary, history)
        raw_results = sum(len(response.results) for _, _, response in responses)
        logger.info("[去重] 原始结果 %d 条，进入预筛队列 %d 个 URL", raw_results, len(tasks))
        self._record_queue(tasks, history)

        if self.settings.search.prefilter.enabled:
            tasks = self._url_prefilter(tasks, summary)
        kept = self._domain_prefilter(tasks, summary)
        return kept

    def _build_url_queue(
        self,
        responses: list[tuple[int, str, SearchResponse]],
        summary: RunSummary,
        history: UrlHistory,
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

        kept = [task for task in tasks if not history.contains(task.normalized_url)]
        summary.history_skipped = len(tasks) - len(kept)
        if summary.history_skipped:
            logger.info("[历史] 命中 %d 个已收 URL，跳过", summary.history_skipped)

        if dedupe.max_urls is not None:
            tasks = kept[: dedupe.max_urls]
            if summary.history_skipped:
                logger.info(
                    "[历史] 由后续结果补位，进入队列 %d 个（上限 %d）",
                    len(tasks),
                    dedupe.max_urls,
                )
            return tasks
        return kept

    def _record_queue(self, tasks: list[UrlTask], history: UrlHistory) -> None:
        """进入预筛队列的 URL 立即入历史表，避免被后续筛选的页面下次重复召回。"""
        if not tasks:
            return
        history.record(task.normalized_url for task in tasks)
        try:
            history.save()
        except OSError as exc:
            logger.warning("[历史] 保存 URL 历史表失败：%s（%s）", history.path, exc)

    # ---------------------------------------------------------------- URL 粗筛

    def _url_prefilter(self, tasks: list[UrlTask], summary: RunSummary) -> list[UrlTask]:
        logger.info("[粗筛] 开始：评估 %d 个 URL…", len(tasks))
        kept: list[UrlTask] = []
        for task in tasks:
            reason = index_url_reason(task.url)
            if reason is None:
                kept.append(task)
                continue
            summary.prefiltered_urls += 1
            summary.prefilter_reasons[reason] += 1
            logger.debug("[粗筛] 丢弃：%s（%s）", task.url, reason)
        detail = "、".join(
            f"{PREFILTER_LABELS.get(reason, reason)} {count}"
            for reason, count in summary.prefilter_reasons.most_common()
        )
        logger.info(
            "[粗筛] 结束：保留 %d 个，丢弃 %d 个%s",
            len(kept),
            summary.prefiltered_urls,
            f"（{detail}）" if detail else "",
        )
        return kept

    # -------------------------------------------------------------- 域名预筛

    def _domain_prefilter(
        self, tasks: list[UrlTask], summary: RunSummary
    ) -> list[tuple[UrlTask, DomainAssessment | None]]:
        if not self.settings.authority.enabled:
            logger.info("[域名预筛] 已禁用，跳过")
            return [(task, None) for task in tasks]

        logger.info("[域名预筛] 正在评估 %d 个域名…", len(tasks))
        lists = load_authority(self.settings.authority.config_path, project_root=self.project_root)
        domain_filter = DomainFilter(lists, self.settings.authority)
        kept: list[tuple[UrlTask, DomainAssessment | None]] = []
        for task in tasks:
            assessment = domain_filter.assess(url_host(task.url))
            if assessment.decision == "reject":
                summary.rejected_domains += 1
                continue
            kept.append((task, assessment))
        logger.info(
            "[域名预筛] 保留 %d 个 URL，拒绝 %d 个",
            len(kept),
            summary.rejected_domains,
        )
        return kept

    # ---------------------------------------------------------------- 提取

    def _extract_content(
        self,
        kept: list[tuple[UrlTask, DomainAssessment | None]],
        summary: RunSummary,
    ) -> tuple[list[tuple[ExtractedDoc, DomainAssessment | None]], bool]:
        tasks: list[tuple[UrlTask, DomainAssessment | None]] = []
        skipped = 0
        for task, domain in kept:
            host = url_host(task.url)
            if any(domain_matches(host, rule) for rule in self.settings.extract.skip_domains):
                skipped += 1
                logger.debug("[提取] 跳过不支持的域名：%s", task.url)
                continue
            tasks.append((task, domain))
        if skipped:
            logger.info("[提取] 跳过不支持的域名 %d 个", skipped)
        if not tasks:
            logger.info("[提取] 结束：没有可提取的 URL")
            return [], False

        total = len(tasks)
        batch_size = self.settings.extract.batch_size
        concurrency = self.settings.extract.concurrency
        batches = chunk_urls([task.url for task, _ in tasks], batch_size)
        offsets: list[int] = []
        position = 0
        for batch in batches:
            offsets.append(position)
            position += len(batch)

        logger.info(
            "[提取] 开始批量提取 %d 个 URL（每批 %d，并发 %d）…",
            total,
            batch_size,
            concurrency,
        )
        outcomes: list[ExtractedContent | None] = [None] * total
        fatal = False
        fatal_error: ExtractError | None = None
        done = 0

        with ThreadPoolExecutor(max_workers=min(concurrency, len(batches))) as executor:
            futures = {
                executor.submit(self.extractor.extract_batch, batch): index
                for index, batch in enumerate(batches)
            }
            for future in as_completed(futures):
                index = futures[future]
                batch = batches[index]
                offset = offsets[index]
                done += 1
                try:
                    results = future.result()
                except ExtractError as exc:
                    if exc.fatal:
                        fatal = True
                        fatal_error = exc
                        logger.error("[提取] 批次 %d/%d 致命错误：%s", done, len(batches), exc)
                    else:
                        logger.warning("[提取] 批次 %d/%d 失败：%s", done, len(batches), exc)
                    outcomes[offset : offset + len(batch)] = [
                        ExtractedContent(
                            url=url,
                            error_type=exc.error_type,
                            error=exc.message,
                            fatal=exc.fatal,
                        )
                        for url in batch
                    ]
                except Exception as exc:  # noqa: BLE001 - 单批异常不中断其他批次
                    logger.warning("[提取] 批次 %d/%d 异常：%s", done, len(batches), exc)
                    outcomes[offset : offset + len(batch)] = [
                        ExtractedContent(
                            url=url,
                            error_type="extract_error",
                            error=str(exc),
                        )
                        for url in batch
                    ]
                else:
                    outcomes[offset : offset + len(results)] = results
                    succeeded = sum(1 for item in results if not item.error_type)
                    logger.info(
                        "[提取] 批次 %d/%d：成功 %d，失败 %d",
                        done,
                        len(batches),
                        succeeded,
                        len(results) - succeeded,
                    )

        if fatal_error is not None:
            summary.failures.append(
                Failure(
                    stage="extract",
                    error_type=fatal_error.error_type,
                    message=fatal_error.message,
                )
            )
        if fatal:
            logger.error("[提取] 因致命错误终止 run")
            return [], True

        all_infra_failed = bool(outcomes) and all(
            item is not None and item.error_type for item in outcomes
        ) and {item.error_type for item in outcomes if item is not None} <= {
            "network_error",
            "service_error",
            "protocol_error",
            "http_error",
            "invalid_batch",
            "invalid_url",
        }
        if all_infra_failed:
            logger.error("[提取] 所有提取批次均失败，终止 run")
            return [], True

        documents: list[tuple[ExtractedDoc, DomainAssessment | None]] = []
        failures = 0
        quality_dropped = 0
        for index, ((task, domain), outcome) in enumerate(zip(tasks, outcomes, strict=False), 1):
            if outcome is None:
                failures += 1
                summary.failures.append(
                    Failure(
                        stage="extract",
                        url=task.url,
                        error_type="missing_result",
                        message="提取结果缺失",
                    )
                )
                continue
            if outcome.error_type:
                failures += 1
                logger.warning(
                    "[提取] 失败 %d/%d：%s（%s）",
                    index,
                    total,
                    task.url,
                    outcome.error,
                )
                summary.failures.append(
                    Failure(
                        stage="extract",
                        url=task.url,
                        error_type=outcome.error_type,
                        message=outcome.error,
                    )
                )
                continue

            clean = clean_content(outcome.raw_content, url=task.url)
            quality_error = check_quality(clean, self.settings.extract)
            if quality_error is not None:
                error_type, message = quality_error
                quality_dropped += 1
                summary.quality_dropped += 1
                summary.quality_reasons[error_type] += 1
                logger.warning("[质量] 丢弃 %d/%d：%s（%s）", index, total, task.url, message)
                continue

            if clean.nav_ratio > self.settings.extract.max_nav_line_ratio:
                logger.debug(
                    "[质量] 导航占比 %.2f 超过阈值，但正文达标，保留：%s",
                    clean.nav_ratio,
                    task.url,
                )

            document = build_extracted_doc(task, outcome, clean)
            documents.append((document, domain))
            logger.info(
                "[提取] 完成 %d/%d：%s（%d 字符）",
                index,
                total,
                document.title or task.url,
                clean.plain_chars,
            )

        logger.info(
            "[提取] 结束：成功 %d 篇，失败 %d 个 URL，质量丢弃 %d 篇",
            len(documents),
            failures,
            quality_dropped,
        )
        return documents, False

    # ---------------------------------------------------------------- 评估

    def _evaluate(
        self,
        documents: list[tuple[ExtractedDoc, DomainAssessment | None]],
        summary: RunSummary,
    ) -> list[Document]:
        evaluated: list[tuple[int, Document]] = []
        total = len(documents)
        done = 0
        logger.info("[评估] 开始内容复核与权威度评估（%d 篇）…", total)

        with ThreadPoolExecutor(max_workers=self.settings.llm.concurrency) as executor:
            futures = {
                executor.submit(self._evaluate_one, doc, domain): index
                for index, (doc, domain) in enumerate(documents)
            }
            for future in as_completed(futures):
                index = futures[future]
                done += 1
                document, failure = future.result()
                label = document.extracted.title or document.extracted.url
                evaluation = document.evaluation
                if failure is not None:
                    stage = STAGE_LABELS.get(failure.stage, failure.stage)
                    logger.warning(
                        "[评估] 完成 %d/%d（%s失败）：%s",
                        done,
                        total,
                        stage,
                        label,
                    )
                    summary.failures.append(failure)
                else:
                    logger.info(
                        "[评估] 完成 %d/%d：相关 %s | 丰富 %s | 质量 %s | 权威 %s：%s",
                        done,
                        total,
                        _score_text(
                            evaluation.relevance.relevance_level,
                            evaluation.relevance.relevance_score,
                        ),
                        _score_text(
                            evaluation.content_richness.level,
                            evaluation.content_richness.score,
                        ),
                        _score_text(
                            evaluation.content_quality.level,
                            evaluation.content_quality.score,
                        ),
                        _score_text(
                            evaluation.authority.authority_level,
                            evaluation.authority.authority_score,
                        ),
                        label,
                    )
                if (
                    evaluation.content_richness.score is None
                    or evaluation.content_quality.score is None
                ):
                    logger.warning("[评估] 内容维度缺失：%s", document.extracted.url)
                evaluated.append((index, document))

        evaluated.sort(key=lambda item: item[0])
        kept: list[Document] = []
        for _, document in evaluated:
            evaluation = document.evaluation
            drop, reasons = should_drop(
                evaluation,
                self.settings.review,
                self.settings.authority,
            )
            if not drop:
                kept.append(document)
                continue
            evaluation.decision = "drop"
            evaluation.drop_reasons = reasons
            summary.dropped_documents += 1
            for reason in reasons:
                summary.drop_reasons[reason] += 1
            logger.warning(
                "[评估] 丢弃：%s（%s）",
                document.extracted.title or document.extracted.url,
                "、".join(DROP_REASON_LABELS.get(reason, reason) for reason in reasons),
            )
        logger.info(
            "[评估] 结束：保留 %d 篇，丢弃 %d 篇",
            len(kept),
            summary.dropped_documents,
        )

        logger.info("[多样性] 开始：按单 query/单域名配额筛选 %d 篇…", len(kept))
        kept, diversity_reasons = apply_diversity(kept, self.settings.diversity)
        summary.diversity_reasons.update(diversity_reasons)
        summary.diversity_dropped = sum(diversity_reasons.values())
        detail = "、".join(
            f"{DIVERSITY_LABELS.get(reason, reason)} {count}"
            for reason, count in diversity_reasons.most_common()
        )
        logger.info(
            "[多样性] 结束：保留 %d 篇，丢弃 %d 篇%s",
            len(kept),
            summary.diversity_dropped,
            f"（{detail}）" if detail else "",
        )
        return kept

    def _evaluate_one(self, doc, domain: DomainAssessment | None):
        failure: Failure | None = None
        try:
            evaluation = review_content(
                doc,
                self.settings.run,
                self.llm,
                self.settings.review,
                prompt_path=self.prompt_paths.get("review"),
            )
        except Exception as exc:  # noqa: BLE001
            evaluation = DocumentEvaluation(
                relevance=RelevanceResult(
                    flags=["relevance_unevaluated"],
                    reason=f"内容复核失败：{exc}",
                    model=getattr(self.llm, "model_name", None),
                )
            )
            failure = Failure(
                stage="review",
                url=doc.url,
                error_type="review_error",
                message=str(exc),
            )

        page = None
        if self.settings.authority.enabled and domain is not None:
            try:
                page = assess_page(doc, self.settings.authority, self.settings.extract)
                evaluation.authority = combine_authority(
                    domain, page, self.settings.authority
                )
            except Exception as exc:  # noqa: BLE001
                evaluation.authority = AuthorityResult(
                    quality_flags=["evaluation_error"],
                    authority_reasons=[str(exc)],
                )
                page = None
                failure = failure or Failure(
                    stage="authority",
                    url=doc.url,
                    error_type="evaluation_error",
                    message=str(exc),
                )
        else:
            evaluation.authority = AuthorityResult(authority_reasons=["权威度评估已禁用"])

        document = Document(extracted=doc, evaluation=evaluation)
        return document, failure

    # ---------------------------------------------------------------- 落盘

    def _write_all(
        self, documents: list[Document], writer: RunWriter, summary: RunSummary
    ) -> None:
        written: set[Path] = set()
        total = len(documents)
        logger.info("[写入] 开始写入 %d 篇文档到 %s", total, writer.run_dir)
        for index, document in enumerate(documents, 1):
            try:
                path = writer.write(document)
            except OSError as exc:
                logger.warning(
                    "[写入] 失败 %d/%d：%s（%s）",
                    index,
                    total,
                    document.extracted.url,
                    exc,
                )
                summary.failures.append(
                    Failure(
                        stage="write",
                        url=document.extracted.url,
                        error_type="write_error",
                        message=str(exc),
                    )
                )
            else:
                written.add(path)
                logger.info("[写入] %d/%d：%s", index, total, path.name)
        summary.documents = len(written)
        logger.info("[写入] 结束：写入 %d 篇文档", summary.documents)

    # ---------------------------------------------------------------- 汇总

    def print_summary(self, summary: RunSummary) -> None:
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


def run_pipeline(settings: Settings, **kwargs) -> int:
    try:
        summary, code = Pipeline(settings, **kwargs).run()
    except OSError as exc:
        logger.error("无法创建运行目录或写入产物：%s", exc)
        return 1
    return code
