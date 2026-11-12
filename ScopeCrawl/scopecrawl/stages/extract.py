"""阶段 4：提取并清洗正文。"""

from __future__ import annotations

import logging

from ..content import (
    ExtractError,
    TavilyExtractor,
    build_extracted_doc,
    check_quality,
    chunk_urls,
    clean_content,
    failure_outcomes,
)
from ..models import (
    DomainAssessment,
    ExtractedContent,
    ExtractedDoc,
    Failure,
    Settings,
    UrlTask,
)
from ..summary import ExtractStats, StageAborted
from ..utils import domain_matches, iter_concurrent, log_stage, url_host

logger = logging.getLogger(__name__)


class ExtractStage:
    """批量提取正文、清洗噪声，并通过质量闸门。"""

    def __init__(
        self,
        settings: Settings,
        extractor: TavilyExtractor,
    ) -> None:
        self.settings = settings
        self.extractor = extractor

    def run(
        self, candidates: list[tuple[UrlTask, DomainAssessment | None]]
    ) -> tuple[list[tuple[ExtractedDoc, DomainAssessment | None]], ExtractStats]:
        log_stage("提取清洗")
        stats = ExtractStats()
        tasks: list[tuple[UrlTask, DomainAssessment | None]] = []
        skipped = 0
        for task, domain in candidates:
            host = url_host(task.url)
            if any(domain_matches(host, rule) for rule in self.settings.extract.skip_domains):
                skipped += 1
                logger.debug("[提取清洗] 跳过不支持的域名：%s", task.url)
                continue
            tasks.append((task, domain))
        if skipped:
            logger.info("[提取清洗] 跳过不支持的域名 %d 个", skipped)
        if not tasks:
            logger.info("[提取清洗] 结束：没有可提取的 URL")
            return [], stats

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
            "[提取清洗] 开始批量提取 %d 个 URL（每批 %d，并发 %d）…",
            total,
            batch_size,
            concurrency,
        )
        outcomes: list[ExtractedContent | None] = [None] * total
        fatal = False
        fatal_error: ExtractError | None = None
        done = 0

        for index, future in iter_concurrent(
            batches, self.extractor.extract_batch, concurrency
        ):
            batch = batches[index]
            offset = offsets[index]
            done += 1
            try:
                results = future.result()
            except ExtractError as exc:
                if exc.fatal:
                    fatal = True
                    fatal_error = exc
                    logger.error(
                        "[提取清洗] 批次 %d/%d 致命错误：%s", done, len(batches), exc
                    )
                else:
                    logger.warning(
                        "[提取清洗] 批次 %d/%d 失败：%s", done, len(batches), exc
                    )
                outcomes[offset : offset + len(batch)] = failure_outcomes(
                    batch, exc.error_type, exc.message, fatal=exc.fatal
                )
            except Exception as exc:  # noqa: BLE001 - 单批异常不中断其他批次
                logger.warning(
                    "[提取清洗] 批次 %d/%d 异常：%s", done, len(batches), exc
                )
                outcomes[offset : offset + len(batch)] = failure_outcomes(
                    batch, "extract_error", str(exc)
                )
            else:
                outcomes[offset : offset + len(results)] = results
                succeeded = sum(1 for item in results if not item.error_type)
                logger.info(
                    "[提取清洗] 批次 %d/%d：成功 %d，失败 %d",
                    done,
                    len(batches),
                    succeeded,
                    len(results) - succeeded,
                )

        if fatal_error is not None:
            stats.failures.append(
                Failure(
                    stage="extract",
                    error_type=fatal_error.error_type,
                    message=fatal_error.message,
                )
            )
        if fatal:
            logger.error("[提取清洗] 因致命错误终止 run")
            raise StageAborted("extract", stats)

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
            logger.error("[提取清洗] 所有提取批次均失败，终止 run")
            raise StageAborted("extract", stats)

        documents: list[tuple[ExtractedDoc, DomainAssessment | None]] = []
        failures = 0
        quality_dropped = 0
        for index, ((task, domain), outcome) in enumerate(zip(tasks, outcomes, strict=False), 1):
            if outcome is None:
                outcome = ExtractedContent(
                    url=task.url,
                    error_type="missing_result",
                    error="提取结果缺失",
                )
            if outcome.error_type:
                failures += 1
                logger.warning(
                    "[提取清洗] 失败 %d/%d：%s（%s）",
                    index,
                    total,
                    task.url,
                    outcome.error,
                )
                stats.failures.append(
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
                stats.quality_reasons[error_type] += 1
                logger.warning(
                    "[提取清洗] 质量丢弃 %d/%d：%s（%s）",
                    index,
                    total,
                    task.url,
                    message,
                )
                continue

            if clean.nav_ratio > self.settings.extract.max_nav_line_ratio:
                logger.debug(
                    "[提取清洗] 导航占比 %.2f 超过阈值，但正文达标，保留：%s",
                    clean.nav_ratio,
                    task.url,
                )

            document = build_extracted_doc(task, outcome, clean)
            documents.append((document, domain))
            logger.info(
                "[提取清洗] 完成 %d/%d：%s（%d 字符）",
                index,
                total,
                document.title or task.url,
                clean.plain_chars,
            )

        logger.info(
            "[提取清洗] 结束：成功 %d 篇，失败 %d 个 URL，质量丢弃 %d 篇",
            len(documents),
            failures,
            quality_dropped,
        )
        return documents, stats
