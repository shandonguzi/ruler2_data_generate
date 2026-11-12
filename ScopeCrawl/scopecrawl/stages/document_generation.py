"""阶段 6：生成文档。"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import Document, Failure
from ..storage import RunWriter
from ..summary import DocumentStats
from ..utils import log_stage

logger = logging.getLogger(__name__)


class DocumentGenerationStage:
    """把保留的文档写成 Markdown，并追加维护 metadata.jsonl。"""

    def __init__(self, writer: RunWriter) -> None:
        self.writer = writer

    def run(self, documents: list[Document]) -> DocumentStats:
        log_stage("生成文档")
        stats = DocumentStats()
        # 同一 doc_id 重复写会复用同一路径，documents 表示唯一输出文件数。
        written_paths: set[Path] = set()
        total = len(documents)
        logger.info("[生成文档] 开始写入 %d 篇文档到 %s", total, self.writer.run_dir)
        for index, document in enumerate(documents, 1):
            try:
                path = self.writer.write(document)
            except OSError as exc:
                logger.warning(
                    "[生成文档] 失败 %d/%d：%s（%s）",
                    index,
                    total,
                    document.extracted.url,
                    exc,
                )
                stats.failures.append(
                    Failure(
                        stage="write",
                        url=document.extracted.url,
                        error_type="write_error",
                        message=str(exc),
                    )
                )
            else:
                written_paths.add(path)
                logger.info("[生成文档] %d/%d：%s", index, total, path.name)
        stats.documents = len(written_paths)
        logger.info("[生成文档] 结束：写入 %d 篇文档", stats.documents)
        return stats
