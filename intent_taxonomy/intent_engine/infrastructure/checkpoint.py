from __future__ import annotations

import json
from pathlib import Path
from threading import Lock
from typing import Dict

from intent_engine.models import SemanticResult
from intent_engine.infrastructure.utils import ensure_dir


class JsonlCheckpoint:
    def __init__(self, path: str):
        self.path = Path(path)
        ensure_dir(self.path.parent)
        self.lock = Lock()

    def load(self) -> Dict[int, SemanticResult]:
        data: Dict[int, SemanticResult] = {}
        if not self.path.exists():
            return data
        with open(self.path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                data[int(obj["row_index"])] = SemanticResult.model_validate(obj["result"])
        return data

    def append(self, row_index: int, result: SemanticResult):
        record = {"row_index": row_index, "result": result.model_dump()}
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
