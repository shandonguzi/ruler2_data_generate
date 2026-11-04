from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from .storage import write_json

current_progress = ContextVar("doc2qa_progress", default=None)
current_context = ContextVar("doc2qa_progress_context", default=None)
log = logging.getLogger(__name__)


@contextmanager
def context_scope(context_id: str):
    token = current_context.set(context_id)
    try:
        yield
    finally:
        current_context.reset(token)


class RunProgress:
    """Append-only event history plus an atomic snapshot, flushed during the run."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.attempt_id = uuid.uuid4().hex[:12]
        self.lock = threading.RLock()
        self.snapshot = {
            "attempt_id": self.attempt_id,
            "status": "running",
            "contexts": {},
            "active_operations": {},
        }

    def __enter__(self):
        self.token = current_progress.set(self)
        self.handler = logging.FileHandler(self.directory / "run.log", encoding="utf-8")
        self.handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        self.logger = logging.getLogger("doc2qa")
        self.previous_level = self.logger.level
        self.logger.setLevel(logging.INFO)
        self.logger.addHandler(self.handler)
        self.record("run", "started", f"批次目录：{self.directory.resolve()}")
        return self

    def __exit__(self, exc_type, exc, tb):
        try:
            status = (
                "interrupted"
                if exc_type and issubclass(exc_type, KeyboardInterrupt)
                else ("failed" if exc_type else self.snapshot.get("result_status", "completed"))
            )
            self.snapshot["status"] = status
            if self.snapshot["active_operations"]:
                self.snapshot["unfinished_operations"] = self.snapshot["active_operations"]
                self.snapshot["active_operations"] = {}
            self.record("run", status, f"批次结束：{status}")
        finally:
            self.logger.removeHandler(self.handler)
            self.handler.close()
            self.logger.setLevel(self.previous_level)
            current_progress.reset(self.token)

    def record(self, stage: str, status: str, message: str, **details):
        context_id = details.pop("context_id", current_context.get())
        event = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "attempt_id": self.attempt_id,
            "context_id": context_id,
            "stage": stage,
            "status": status,
            "message": message,
            **details,
        }
        with self.lock:
            # Open and close for each event so Ctrl+C leaves completed outputs on disk.
            with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
            self._save_intermediate(event)
            self.snapshot["updated_at"] = event["timestamp"]
            self.snapshot["last_event"] = event
            if context_id:
                context = self.snapshot["contexts"].setdefault(context_id, {})
                context.update(details.get("counts", {}))
                context["last_stage"] = stage
                context["last_status"] = status
            write_json(self.directory / "progress.json", self.snapshot)
            prefix = f"[{context_id}] " if context_id else ""
            log.info("%s%s", prefix, message, extra={"progress_event": event})

    def _save_intermediate(self, event):
        context_id = event["context_id"]
        if not context_id or "output" not in event:
            return
        rows = []
        common = {k: event[k] for k in ("timestamp", "attempt_id", "context_id")}
        if event["stage"] == "input_generation" and event["status"] == "result":
            name = "initial_questions.jsonl"
            rows = [
                {
                    **common,
                    "item_index": index,
                    "used_in_synthesis": index <= event["counts"]["inputs_used"],
                    **question,
                }
                for index, question in enumerate(event["output"], 1)
            ]
        elif event["stage"] == "answer_generation" and event["status"] == "completed":
            name = "generated_qa.jsonl"
            rows = [
                {
                    **common,
                    "item_index": event.get("item_index"),
                    "question": event.get("question"),
                    "qa_type": event.get("qa_type"),
                    "answer": event["output"],
                    "review_status": "not_validated",
                }
            ]
        if rows:
            # Keep inspection artifacts separate from the resumable checkpoints.
            directory = self.directory / "intermediate" / context_id
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / name).open("a", encoding="utf-8") as stream:
                stream.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)

    @contextmanager
    def operation(self, stage: str, title: str, **details):
        operation_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        active = {
            "context_id": current_context.get(),
            "stage": stage,
            "title": title,
            "started_at": datetime.now(timezone.utc).isoformat(),
            **details,
        }
        with self.lock:
            self.snapshot["active_operations"][operation_id] = active
        self.record(stage, "started", title + "：开始", operation_id=operation_id, **details)
        output = {}
        try:
            yield output
        except BaseException as exc:
            with self.lock:
                self.snapshot["active_operations"].pop(operation_id, None)
            status = (
                "interrupted"
                if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError))
                else "failed"
            )
            self.record(
                stage,
                status,
                f"{title}：{status} ({time.monotonic() - started:.1f}s) "
                f"{type(exc).__name__}: {exc}",
                operation_id=operation_id,
                error_type=type(exc).__name__,
                error_message=str(exc),
                elapsed_seconds=round(time.monotonic() - started, 3),
                **details,
            )
            raise
        else:
            with self.lock:
                self.snapshot["active_operations"].pop(operation_id, None)
            self.record(
                stage,
                "completed",
                f"{title}：完成 ({time.monotonic() - started:.1f}s)",
                operation_id=operation_id,
                elapsed_seconds=round(time.monotonic() - started, 3),
                **details,
                **output,
            )
