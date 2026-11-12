"""阶段 1：关键词生成。"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..keywords import KeywordError, generate_keywords
from ..models import Failure, Settings
from ..scenarios import find_coverage_points
from ..summary import RunSummary
from .log import log_stage

if TYPE_CHECKING:
    from ..llm import LLMClient

logger = logging.getLogger(__name__)


class KeywordGenerationStage:
    """把 S3/S4、地点与语言展开为具体搜索词。"""

    def __init__(
        self,
        settings: Settings,
        llm: LLMClient,
        scenarios: Mapping[str, Any],
        summary: RunSummary,
        *,
        prompt_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.llm = llm
        self.scenarios = scenarios
        self.summary = summary
        self.prompt_path = prompt_path

    def run(self) -> list[str] | None:
        log_stage("关键词生成")
        run = self.settings.run
        coverage_points = find_coverage_points(self.scenarios, run.s3, run.s4)
        if coverage_points is None:
            logger.warning(
                "场景未在场景表中收录，按无必须覆盖要点继续：s3=%s，s4=%s",
                run.s3,
                run.s4,
            )
        elif coverage_points:
            logger.info(
                "[关键词生成] 必须覆盖要点 %d 条：%s",
                len(coverage_points),
                " | ".join(coverage_points),
            )

        logger.info("[关键词生成] 正在调用 LLM 生成搜索关键词…")
        try:
            keywords = generate_keywords(
                run,
                self.llm,
                self.settings.keywords,
                prompt_path=self.prompt_path,
                coverage_points=coverage_points or [],
            )
        except KeywordError as exc:
            logger.error("关键词生成失败：%s", exc)
            self.summary.failures.append(
                Failure(stage="keyword", error_type="keyword_error", message=str(exc))
            )
            return None

        logger.info("[关键词生成] 生成 %d 条：%s", len(keywords), " | ".join(keywords))
        self.summary.keywords = keywords
        return keywords
