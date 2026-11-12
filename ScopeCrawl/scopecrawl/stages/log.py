"""阶段日志分隔。"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


def log_stage(name: str) -> None:
    """在控制台打印显著分隔行，标记一个新阶段的开始。"""
    logger.info("================ %s ================", name)
