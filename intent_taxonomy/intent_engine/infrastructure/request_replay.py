"""Replay validated successful calls from one explicitly selected task."""
import hashlib
import json
import threading
from pathlib import Path


def identity(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class RequestReplay:
    def __init__(self, path):
        self.lock = threading.Lock()
        self.records = {}
        if path:
            for line in Path(path).read_text(encoding='utf-8').splitlines():
                try:
                    record = json.loads(line)
                    if record.get('status') == 'success' and record.get('kind', 'llm') != 'embedding' and 'parsed' in record:
                        self.records[identity(record['input'])] = record['parsed']
                except (ValueError, KeyError, TypeError):
                    continue

    def get(self, payload, schema):
        with self.lock:
            value = self.records.get(identity(payload))
        if value is None:
            return None
        try:
            return schema.model_validate(value) if schema else json.loads(json.dumps(value))
        except ValueError:
            return None
