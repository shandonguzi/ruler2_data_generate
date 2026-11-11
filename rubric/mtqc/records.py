"""Durable append-only events, atomic exports, and exclusive run ownership."""
from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

from . import SCHEMA_VERSION
from .validators import canonical, parse_json


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def atomic_write(path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_log(path: Path) -> list:
    if not path.exists():
        return []
    data = path.read_bytes()
    lines = data.splitlines(keepends=True)
    records, offset = [], 0
    for index, line in enumerate(lines):
        try:
            record = parse_json(line.decode("utf-8"))
            if not isinstance(record, dict):
                raise ValueError("event must be an object")
        except (ValueError, UnicodeDecodeError) as exc:
            if index != len(lines) - 1 or line.endswith(b"\n"):
                raise ValueError(f"Corrupt event log {path}:{index + 1}") from exc
            # Preserve forensic bytes; only an incomplete final record may be repaired.
            backup = path.with_name(f"{path.name}.torn-{uuid.uuid4().hex}.bak")
            backup.write_bytes(line)
            with path.open("r+b") as handle:
                handle.truncate(offset)
                handle.flush()
                os.fsync(handle.fileno())
            warnings.warn(f"Recovered torn final record in {path}; bytes preserved in {backup}", RuntimeWarning)
            break
        records.append(record)
        offset += len(line)
    if records and data and not data.endswith(b"\n") and offset == len(data):
        # Complete JSON without trailing newline must be separated from the next append.
        with path.open("ab") as handle:
            handle.write(b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    return records


def config_identity(config: dict) -> dict:
    # Keep credentials out of persisted manifests and cache identities.
    llm = {key: value for key, value in config["llm"].items() if key != "api_key"}
    return {**config, "llm": llm, "schema_version": SCHEMA_VERSION,
            "schema_source_sha256": hashlib.sha256(Path(__file__).with_name("schemas.py").read_bytes()).hexdigest()}


class Records:
    NAMES = ("original_cases", "revisions", "judge_results", "repair_attempts", "attempts")

    def __init__(self, config: dict, input_hash=None, create=True, schema_hashes=None):
        self.path = Path(config["output"]["records_dir"])
        if not create and not self.path.exists():
            raise ValueError("No records_dir exists to export")
        self.path.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._handle = (self.path / ".lock").open("a+b")
        try:
            if os.name == "nt":
                import msvcrt
                if self._handle.seek(0, 2) == 0:
                    self._handle.write(b"0")
                    self._handle.flush()
                self._handle.seek(0)
                msvcrt.locking(self._handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            raise ValueError("Another process owns this records_dir") from exc
        try:
            manifest_path = self.path / "manifest.json"
            identity = config_identity(config)
            if manifest_path.exists():
                self.manifest = parse_json(manifest_path.read_text(encoding="utf-8"))
                if self.manifest["identity"] != identity or (input_hash is not None and self.manifest["input_sha256"] != input_hash):
                    raise ValueError("Input/config/prompt/schema changed; use a new records_dir and run_id")
                if schema_hashes is not None and self.manifest.get("response_schema_hashes") != schema_hashes:
                    raise ValueError("Response schema changed; use a new records_dir and run_id")
            else:
                if not create or any((self.path / f"{name}.jsonl").exists() for name in self.NAMES):
                    raise ValueError("Missing manifest for existing run records")
                self.manifest = {"run_id": uuid.uuid4().hex, "created_at": now(),
                                 "input_sha256": input_hash, "identity": identity,
                                 "response_schema_hashes": schema_hashes}
                atomic_write(manifest_path, json.dumps(self.manifest, ensure_ascii=False, indent=2))
            self.events = {name: read_log(self.path / f"{name}.jsonl") for name in self.NAMES}
            self._indexes = {}
            for name, events in self.events.items():
                for event in events:
                    self._index(name, event)
        except BaseException:
            self.close()
            raise

    def _index(self, name, event):
        for field in ("input_id", "task_id", "repair_attempt_id"):
            if event.get(field) is not None:
                self._indexes.setdefault((name, field, event[field]), []).append(event)

    def append(self, name: str, event: dict) -> dict:
        if name not in self.NAMES:
            raise ValueError(f"Unknown event stream: {name}")
        event = {**event, "event_time": now()}
        line = canonical(event) + "\n"
        with self._lock:
            with (self.path / f"{name}.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
            self.events[name].append(event)
            self._index(name, event)
        return event

    def select(self, name: str, **matches) -> list:
        with self._lock:
            buckets = [self._indexes.get((name, field, value), []) for field, value in matches.items()
                       if field in ("input_id", "task_id", "repair_attempt_id") and value is not None]
            candidates = min(buckets, key=len) if buckets else self.events[name]
            return [event.copy() for event in candidates if all(event.get(k) == v for k, v in matches.items())]

    def close(self):
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
