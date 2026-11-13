"""阶段 4：提取并清洗正文。"""

from __future__ import annotations

import logging

from ..content import (
    ExtractError,
    build_extracted_doc,
    check_quality,
    clean_content,
)
from ..models import (
    DomainAssessment,
    ExtractedContent,
    ExtractedDoc,
    Failure,
    Settings,
    UrlTask,
)
from ..provider import Provider
from ..summary import ExtractStats, StageAborted
from ..utils import iter_concurrent, log_stage

logger = logging.getLogger(__name__)

Candidate = tuple[UrlTask, DomainAssessment | None]
DocumentSlot = tuple[ExtractedDoc, DomainAssessment | None]


def _has_content(task: UrlTask) -> bool:
    """搜索正文非空才复用；纯空白视为没有。"""
    return bool(task.content.strip())


class ExtractStage:
    """批量提取正文、清洗噪声，并通过质量闸门。"""

    def __init__(
        self,
        settings: Settings,
        provider: Provider,
    ) -> None:
        self.settings = settings
        self.provider = provider

    def run(self, candidates: list[Candidate]) -> tuple[list[DocumentSlot], ExtractStats]:
        log_stage("提取清洗")
        stats = ExtractStats()
        if not candidates:
            logger.info("[提取清洗] 结束：没有可提取的 URL")
            return [], stats

        total = len(candidates)
        batch_size = self.provider.extract_batch_size
        concurrency = self.settings.extract.concurrency
        batches = [
            candidates[index : index + batch_size]
            for index in range(0, total, batch_size)
        ]
        offsets: list[int] = []
        position = 0
        for batch in batches:
            offsets.append(position)
            position += len(batch)

        search_content = sum(1 for task, _ in candidates if _has_content(task))
        logger.info(
            "[提取清洗] 开始批量提取 %d 个 URL（搜索正文 %d，需 fetch %d；每批 %d，并发 %d）…",
            total,
            search_content,
            total - search_content,
            batch_size,
            concurrency,
        )
        outcomes: list[ExtractedContent | None] = [None] * total
        slots: list[DocumentSlot | None] = [None] * total
        fatal = False
        fatal_error: ExtractError | None = None
        failures = 0
        quality_dropped = 0
        done = 0

        for index, future in iter_concurrent(
            batches, self._batch_outcomes, concurrency
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
                outcomes[offset : offset + len(batch)] = self._fallback_outcomes(
                    batch, exc.error_type, exc.message
                )
            except Exception as exc:  # noqa: BLE001 - 单批异常不中断其他批次
                logger.warning(
                    "[提取清洗] 批次 %d/%d 异常：%s", done, len(batches), exc
                )
                outcomes[offset : offset + len(batch)] = self._fallback_outcomes(
                    batch, "extract_error", str(exc)
                )
            else:
                outcomes[offset : offset + len(results)] = results
                provided = sum(1 for task, _ in batch if _has_content(task))
                succeeded = sum(1 for item in results if not item.error_type)
                logger.info(
                    "[提取清洗] 批次 %d/%d：搜索正文 %d，需 fetch %d，成功 %d，失败 %d",
                    done,
                    len(batches),
                    provided,
                    len(batch) - provided,
                    succeeded,
                    len(results) - succeeded,
                )
            # 该批结果一返回就清洗，与仍在并发执行的其余批次重叠。
            batch_failures, batch_quality = self._process(
                candidates[offset : offset + len(batch)],
                outcomes[offset : offset + len(batch)],
                offset=offset,
                total=total,
                stats=stats,
                slots=slots,
            )
            failures += batch_failures
            quality_dropped += batch_quality

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

        documents = [slot for slot in slots if slot is not None]
        logger.info(
            "[提取清洗] 结束：成功 %d 篇，失败 %d 个 URL，质量丢弃 %d 篇",
            len(documents),
            failures,
            quality_dropped,
        )
        return documents, stats

    def _batch_outcomes(self, batch: list[Candidate]) -> list[ExtractedContent]:
        """有搜索正文的位置直接复用；只把无正文 URL 交给 provider 提取。"""
        outcomes: list[ExtractedContent | None] = [None] * len(batch)
        pending: list[int] = []
        for index, (task, _) in enumerate(batch):
            if _has_content(task):
                outcomes[index] = ExtractedContent(
                    url=task.url,
                    final_url=task.url,
                    raw_content=task.content,
                )
            else:
                pending.append(index)

        if pending:
            urls = [batch[index][0].url for index in pending]
            fetched = self.provider.extract_batch(urls)
            for index, outcome in zip(pending, fetched, strict=False):
                outcomes[index] = outcome

        return [
            outcome
            or ExtractedContent(
                url=batch[index][0].url,
                error_type="missing_result",
                error="提取结果缺失",
            )
            for index, outcome in enumerate(outcomes)
        ]

    def _fallback_outcomes(
        self,
        batch: list[Candidate],
        error_type: str,
        message: str,
    ) -> list[ExtractedContent]:
        """批次异常时保留已有搜索正文，只把需要 fetch 的 URL 记为失败。"""
        return [
            ExtractedContent(
                url=task.url,
                final_url=task.url,
                raw_content=task.content,
            )
            if _has_content(task)
            else ExtractedContent(
                url=task.url,
                final_url=task.url,
                error_type=error_type,
                error=message,
            )
            for task, _ in batch
        ]

    def _process(
        self,
        batch_tasks: list[Candidate],
        batch_outcomes: list[ExtractedContent | None],
        *,
        offset: int,
        total: int,
        stats: ExtractStats,
        slots: list[DocumentSlot | None],
    ) -> tuple[int, int]:
        """清洗一批结果并记录失败与质量丢弃，返回 (失败数, 质量丢弃数)。"""
        failures = 0
        quality_dropped = 0
        for position, ((task, domain), outcome) in enumerate(
            zip(batch_tasks, batch_outcomes, strict=False), offset
        ):
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
                    position + 1,
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

            clean = clean_content(
                outcome.raw_content,
                url=task.url,
                strip_links=self.settings.extract.strip_links,
                strip_images=self.settings.extract.strip_images,
            )
            quality_error = check_quality(clean, self.settings.extract)
            if quality_error is not None:
                error_type, message = quality_error
                quality_dropped += 1
                stats.quality_reasons[error_type] += 1
                source = "搜索正文" if _has_content(task) else "fetch"
                logger.warning(
                    "[提取清洗] 质量丢弃 %d/%d：%s（%s，来源：%s）",
                    position + 1,
                    total,
                    task.url,
                    message,
                    source,
                )
                continue

            extractor = (
                f"{self.provider.name}-search"
                if _has_content(task)
                else self.provider.extractor_name
            )
            document = build_extracted_doc(task, outcome, clean, extractor=extractor)
            slots[position] = (document, domain)
            logger.info(
                "[提取清洗] 完成 %d/%d：%s（%d 字符）",
                position + 1,
                total,
                document.title or task.url,
                clean.plain_chars,
            )
        return failures, quality_dropped
