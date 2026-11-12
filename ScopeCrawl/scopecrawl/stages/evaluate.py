"""阶段 5：评估并筛选文档。"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import TYPE_CHECKING

from ..evaluate import (
    apply_diversity,
    assess_page,
    combine_authority,
    review_content,
    should_drop,
)
from ..models import (
    AuthorityResult,
    Document,
    DocumentEvaluation,
    DomainAssessment,
    ExtractedDoc,
    Failure,
    RelevanceResult,
    Settings,
)
from ..summary import (
    DIVERSITY_LABELS,
    DROP_REASON_LABELS,
    STAGE_LABELS,
    RunSummary,
    format_counts,
)
from .log import log_stage

if TYPE_CHECKING:
    from ..llm import LLMClient

logger = logging.getLogger(__name__)


def _score_text(level: str | None, score: float | None) -> str:
    if score is None:
        return level or "未评估"
    return f"{level or '未评估'} {score:.2f}"


class EvaluateStage:
    """内容复核、权威度评估、丢弃判定与多样性限流。"""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        summary: RunSummary,
        *,
        prompt_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.summary = summary
        self.prompt_path = prompt_path

    def run(
        self,
        documents: list[tuple[ExtractedDoc, DomainAssessment | None]],
    ) -> list[Document]:
        log_stage("评估筛选")
        evaluated: list[tuple[int, Document]] = []
        total = len(documents)
        done = 0
        logger.info("[评估筛选] 开始内容复核与权威度评估（%d 篇）…", total)

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
                        "[评估筛选] 完成 %d/%d（%s失败）：%s",
                        done,
                        total,
                        stage,
                        label,
                    )
                    self.summary.failures.append(failure)
                else:
                    logger.info(
                        "[评估筛选] 完成 %d/%d：相关 %s | 丰富 %s | 质量 %s | 权威 %s：%s",
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
                    logger.warning(
                        "[评估筛选] 内容维度缺失：%s", document.extracted.url
                    )
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
            self.summary.dropped_documents += 1
            for reason in reasons:
                self.summary.drop_reasons[reason] += 1
            logger.warning(
                "[评估筛选] 丢弃：%s（%s）",
                document.extracted.title or document.extracted.url,
                "、".join(DROP_REASON_LABELS.get(reason, reason) for reason in reasons),
            )
        logger.info(
            "[评估筛选] 结束：保留 %d 篇，丢弃 %d 篇",
            len(kept),
            self.summary.dropped_documents,
        )

        if not self.settings.diversity.enabled:
            logger.info("[评估筛选] 多样性限流已禁用，跳过")
            return kept

        logger.info("[评估筛选] 多样性开始：按单 query/单域名配额筛选 %d 篇…", len(kept))
        kept, diversity_reasons = apply_diversity(kept, self.settings.diversity)
        self.summary.diversity_reasons.update(diversity_reasons)
        self.summary.diversity_dropped = sum(diversity_reasons.values())
        detail = format_counts(diversity_reasons, DIVERSITY_LABELS)
        logger.info(
            "[评估筛选] 多样性结束：保留 %d 篇，丢弃 %d 篇%s",
            len(kept),
            self.summary.diversity_dropped,
            f"（{detail}）" if detail else "",
        )
        return kept

    def _evaluate_one(self, doc: ExtractedDoc, domain: DomainAssessment | None):
        failure: Failure | None = None
        try:
            evaluation = review_content(
                doc,
                self.settings.run,
                self.llm,
                self.settings.review,
                prompt_path=self.prompt_path,
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
