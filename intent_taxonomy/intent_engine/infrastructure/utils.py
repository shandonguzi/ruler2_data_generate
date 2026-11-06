from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Iterable, List


def ensure_dir(path: str | Path) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def stable_task_id(domain: str, obj: str, action: str) -> str:
    raw = f"{domain.strip()}|{obj.strip()}|{action.strip()}".encode("utf-8")
    return "task_" + hashlib.sha1(raw).hexdigest()[:12]


def stable_candidate_id(domain: str, obj: str, action: str) -> str:
    raw = f"{domain.strip()}|{obj.strip()}|{action.strip()}".encode("utf-8")
    return "cand_" + hashlib.sha1(raw).hexdigest()[:12]


def compact_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def chunks(items: List[Any], size: int) -> Iterable[List[Any]]:
    for i in range(0, len(items), size):
        yield items[i:i + size]


def safe_filename(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z._-]+", "_", text).strip("_") or "output"
