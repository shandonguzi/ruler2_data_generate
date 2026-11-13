"""流水线编排：按阶段串联文档生成流程。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .config import PROJECT_ROOT, resolve_path
from .history import DEFAULT_HISTORY_PATH, UrlHistory
from .llm import LLMClient
from .models import STAGE_LABELS, Settings
from .provider import Provider, create_provider
from .scenarios import load_scenarios
from .stages import (
    DiversityStage,
    DocumentGenerationStage,
    EvaluateStage,
    ExtractStage,
    KeywordGenerationStage,
    SearchStage,
    UrlFilterStage,
)
from .storage import RunWriter
from .summary import RunSummary, StageAborted, print_summary

logger = logging.getLogger(__name__)


class Pipeline:
    """按「关键词生成 → 检索 → URL 筛选 → 提取清洗 → 评估筛选 → 生成文档」串联各阶段。"""

    def __init__(
        self,
        settings: Settings,
        *,
        output_root: str | Path | None = None,
        llm_client=None,
        provider: Provider | None = None,
        writer_factory: Callable[..., RunWriter] = RunWriter,
        prompt_paths: dict[str, Path] | None = None,
        project_root: Path | None = None,
        scenarios_path: str | Path | None = None,
        history_path: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.project_root = project_root or PROJECT_ROOT
        self.output_root = (
            resolve_path(output_root, root=self.project_root)
            if output_root is not None
            else None
        )
        self.writer_factory = writer_factory
        self.history_path = resolve_path(
            history_path if history_path is not None else DEFAULT_HISTORY_PATH,
            root=self.project_root,
        )
        self.prompt_paths = {
            key: resolve_path(path, root=self.project_root)
            for key, path in (prompt_paths or {}).items()
        }
        self.scenarios = load_scenarios(
            resolve_path(
                scenarios_path or settings.keywords.scenarios_path,
                root=self.project_root,
            )
        )
        self.llm = llm_client or LLMClient(
            settings.llm,
            model=settings.llm_model,
            api_key=(settings.llm_api_key.get_secret_value() if settings.llm_api_key else None),
            base_url=settings.llm_base_url,
        )
        self.provider = provider or create_provider(settings)

    def run(self) -> tuple[RunSummary, int]:
        writer = self.writer_factory(self.output_root, self.settings.run)
        summary = RunSummary(run_id=writer.run_id)
        history = UrlHistory(self.history_path)
        run_config = self.settings.run
        logger.info(
            "开始运行：s3=%s，s4=%s，地点=%s，语言=%s",
            run_config.s3,
            run_config.s4,
            run_config.location or "未指定",
            run_config.language,
        )
        logger.info("输出目录：%s", writer.run_dir)
        logger.info("搜索/提取引擎：%s", self.provider.name)

        try:
            keywords, stats = KeywordGenerationStage(
                self.settings,
                self.llm,
                self.scenarios,
                prompt_path=self.prompt_paths.get("keywords"),
            ).run()
            stats.merge_into(summary)

            responses, stats = SearchStage(self.settings, self.provider).run(keywords)
            stats.merge_into(summary)

            candidates, stats = UrlFilterStage(
                self.settings, history, project_root=self.project_root
            ).run(responses)
            stats.merge_into(summary)
            summary.processed_urls = len(candidates)

            documents, stats = ExtractStage(self.settings, self.provider).run(candidates)
            stats.merge_into(summary)

            kept, stats = EvaluateStage(
                self.settings,
                self.llm,
                prompt_path=self.prompt_paths.get("review"),
            ).run(documents)
            stats.merge_into(summary)

            kept, stats = DiversityStage(self.settings).run(kept)
            stats.merge_into(summary)

            stats = DocumentGenerationStage(writer).run(kept)
            stats.merge_into(summary)
        except StageAborted as exc:
            exc.stats.merge_into(summary)
            logger.error("阶段中止：%s", STAGE_LABELS.get(exc.stage, exc.stage))
            print_summary(summary)
            return summary, 1

        print_summary(summary)
        return summary, 0


def run_pipeline(settings: Settings, **kwargs) -> int:
    try:
        summary, code = Pipeline(settings, **kwargs).run()
    except OSError as exc:
        logger.error("无法创建运行目录或写入产物：%s", exc)
        return 1
    return code
