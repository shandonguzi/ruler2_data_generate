"""跨 run 的 URL 历史表。

只记录成功写盘的文档 URL，所有场景共享同一张表；
匹配方式是规范化 URL 精确相等，不做域名或前缀排除。
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Iterable
from pathlib import Path

from .config import PROJECT_ROOT

logger = logging.getLogger(__name__)

HISTORY_FILENAME = ".history.json"
DEFAULT_HISTORY_PATH = PROJECT_ROOT / HISTORY_FILENAME


class UrlHistory:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.urls: set[str] = set(self._read())

    def contains(self, normalized_url: str) -> bool:
        return normalized_url in self.urls

    def record(self, normalized_urls: Iterable[str]) -> None:
        """记录已收 URL；入参必须是 ``normalize_url`` 之后的键。"""
        self.urls.update(url for url in normalized_urls if url)

    def save(self) -> None:
        """合并写回，保留并发运行期间新增的条目。"""
        urls = set(self._read())
        urls.update(self.urls)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=self.path.parent, prefix=".history-", suffix=".json"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(sorted(urls), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def _read(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:
            logger.warning("URL 历史表读取失败，按空表处理：%s（%s）", self.path, exc)
            return []
        return data if isinstance(data, list) else []


def reset_history(path: str | Path) -> bool:
    """清空历史表；返回是否实际删除。"""
    target = Path(path)
    try:
        target.unlink()
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("无法清空 URL 历史表：%s（%s）", target, exc)
        return False
    return True
