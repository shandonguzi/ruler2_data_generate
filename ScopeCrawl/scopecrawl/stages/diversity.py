"""多样性限流：按单 query / 单域名配额筛选文档。"""

from __future__ import annotations

import logging

from ..evaluate import apply_diversity
from ..models import Document, Settings
from ..summary import DIVERSITY_LABELS, DiversityStats, format_counts

logger = logging.getLogger(__name__)


class DiversityStage:
    """在评估之后、写盘之前限制同一查询与同一域名的文档数量。"""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def run(self, documents: list[Document]) -> tuple[list[Document], DiversityStats]:
        kept, reasons = apply_diversity(documents, self.settings.diversity)
        detail = format_counts(reasons, DIVERSITY_LABELS)
        logger.info(
            "[评估筛选] 多样性结束：保留 %d 篇，丢弃 %d 篇%s",
            len(kept),
            sum(reasons.values()),
            f"（{detail}）" if detail else "",
        )
        return kept, DiversityStats(reasons)
