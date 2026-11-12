"""流水线编排：按阶段串联文档生成流程。"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

from .config import PROJECT_ROOT
from .content import TavilyExtractor
from .history import DEFAULT_HISTORY_PATH, UrlHistory
from .llm import LLMClient
from .models import Settings
from .scenarios import load_scenarios
from .search import SearchProvider, create_search_provider
from .stages import (
    DocumentGenerationStage,
    EvaluateStage,
    ExtractStage,
    KeywordGenerationStage,
    SearchStage,
    UrlFilterStage,
)
from .storage import RunWriter
from .summary import RunSummary, print_summary

logger = logging.getLogger(__name__)


class Pipeline:
    """按「关键词生成 → 检索 → URL 筛选 → 提取清洗 → 评估筛选 → 生成文档」串联各阶段。"""

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
        scenarios_path: str | Path | None = None,
        history_path: str | Path | None = None,
    ) -> None:
        self.settings = settings
        self.output_root = output_root
        self.writer_factory = writer_factory
        self.project_root = project_root or PROJECT_ROOT
        self.history_path = Path(history_path) if history_path is not None else DEFAULT_HISTORY_PATH
        self.prompt_paths = prompt_paths or {}
        scenarios_file = Path(scenarios_path or settings.keywords.scenarios_path)
        self.scenarios = load_scenarios(
            scenarios_file if scenarios_file.is_absolute() else self.project_root / scenarios_file
        )
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

        keywords = KeywordGenerationStage(
            self.settings,
            self.llm,
            self.scenarios,
            summary,
            prompt_path=self.prompt_paths.get("keywords"),
        ).run()
        if keywords is None:
            print_summary(summary)
            return summary, 1

        responses = SearchStage(self.settings, self.provider, summary).run(keywords)
        if responses is None:
            print_summary(summary)
            return summary, 1

        candidates = UrlFilterStage(
            self.settings, history, summary, project_root=self.project_root
        ).run(responses)
        summary.processed_urls = len(candidates)

        documents = ExtractStage(self.settings, self.extractor, summary).run(candidates)
        if documents is None:
            print_summary(summary)
            return summary, 1

        kept = EvaluateStage(
            self.settings,
            self.llm,
            summary,
            prompt_path=self.prompt_paths.get("review"),
        ).run(documents)
        DocumentGenerationStage(writer, summary).run(kept)

        print_summary(summary)
        return summary, 0


def run_pipeline(settings: Settings, **kwargs) -> int:
    try:
        summary, code = Pipeline(settings, **kwargs).run()
    except OSError as exc:
        logger.error("无法创建运行目录或写入产物：%s", exc)
        return 1
    return code
