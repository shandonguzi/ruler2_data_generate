"""阶段 5：评估并筛选文档。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..authority import assess_page, combine_authority
from ..evaluate import (
    load_review_prompt,
    review_content,
    should_drop,
)
from ..models import (
    STAGE_LABELS,
    AuthorityResult,
    Document,
    DocumentEvaluation,
    DomainAssessment,
    ExtractedDoc,
    Failure,
    RelevanceResult,
    Settings,
)
from ..summary import DROP_REASON_LABELS, EvaluateStats
from ..utils import iter_concurrent, log_stage

if TYPE_CHECKING:
    from ..llm import LLMClient

logger = logging.getLogger(__name__)


def _score_text(level: str | None, score: float | None) -> str:
    if score is None:
        return level or "未评估"
    return f"{level or '未评估'} {score:.2f}"


class EvaluateStage:
    """内容复核、权威度评估与丢弃判定。"""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        *,
        prompt_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.prompt_path = prompt_path

    def run(
        self,
        documents: list[tuple[ExtractedDoc, DomainAssessment | None]],
    ) -> tuple[list[Document], EvaluateStats]:
        log_stage("评估筛选")
        stats = EvaluateStats()
        evaluated: list[tuple[int, Document]] = []
        total = len(documents)
        done = 0
        logger.info("[评估筛选] 开始内容复核与权威度评估（%d 篇）…", total)
        template = None
        if self.settings.review.enabled:
            try:
                template = load_review_prompt(self.prompt_path)
            except Exception:  # noqa: BLE001 - 读取失败留给逐篇复核记录
                pass

        for index, future in iter_concurrent(
            documents,
            lambda item: self._evaluate_one(item[0], item[1], template),
            self.settings.llm.concurrency,
        ):
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
                stats.failures.append(failure)
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
            for reason in reasons:
                stats.drop_reasons[reason] += 1
            logger.warning(
                "[评估筛选] 丢弃：%s（%s）",
                document.extracted.title or document.extracted.url,
                "、".join(DROP_REASON_LABELS.get(reason, reason) for reason in reasons),
            )
        logger.info(
            "[评估筛选] 结束：保留 %d 篇，丢弃 %d 篇",
            len(kept),
            sum(stats.drop_reasons.values()),
        )
        return kept, stats

    def _evaluate_one(
        self, doc: ExtractedDoc, domain: DomainAssessment | None, template: str | None
    ):
        failure: Failure | None = None
        try:
            evaluation = review_content(
                doc,
                self.settings.run,
                self.llm,
                self.settings.review,
                prompt_path=self.prompt_path,
                template=template,
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

        if domain is None:
            evaluation.authority = combine_authority(
                None, None, self.settings.authority
            )
        else:
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
                failure = failure or Failure(
                    stage="authority",
                    url=doc.url,
                    error_type="evaluation_error",
                    message=str(exc),
                )

        document = Document(extracted=doc, evaluation=evaluation)
        return document, failure
