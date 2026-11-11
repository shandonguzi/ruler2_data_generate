"""场景表：读取 S1→S2→S3→[S4 条目] 嵌套结构，按 S3/S4 查找必须覆盖要点。"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .config import PROJECT_ROOT

logger = logging.getLogger(__name__)

DEFAULT_SCENARIOS_PATH = PROJECT_ROOT / "config" / "scenarios.json"


def load_scenarios(path: str | Path | None = None) -> dict[str, Any]:
    """读取场景表；文件缺失或格式非法时告警并返回空表。"""
    file = Path(path) if path is not None else DEFAULT_SCENARIOS_PATH
    try:
        data = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("无法读取场景表，按无必须覆盖要点继续：%s：%s", file, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("场景表格式错误，顶层必须是对象：%s", file)
        return {}
    return data


def find_coverage_points(tree: Mapping[str, Any], s3: str, s4: str) -> list[str] | None:
    """按 S3/S4 精确查找；场景未收录返回 None，已收录但无要点返回空列表。"""
    s3_key, s4_key = s3.strip(), s4.strip()
    for s2_tree in tree.values():
        if not isinstance(s2_tree, Mapping):
            continue
        for s3_tree in s2_tree.values():
            if not isinstance(s3_tree, Mapping):
                continue
            for entry in s3_tree.get(s3_key, ()):
                if not isinstance(entry, Mapping) or entry.get("s4") != s4_key:
                    continue
                points = entry.get("coverage_points") or []
                return [p.strip() for p in points if isinstance(p, str) and p.strip()]
    return None
