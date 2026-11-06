"""Structured, opt-in progress events shared by CLI workers and the web server.

Overall percentages measure planned phases, not elapsed time or LLM latency.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field

PROGRESS_PREFIX = "@@INTENT_PROGRESS@@"


@dataclass
class ProgressState:
    steps: list[dict] = field(default_factory=list)
    current_id: str | None = None
    updated_at: float | None = None

    def apply(self, event: dict) -> bool:
        kind = event.get("event")
        if kind == "plan":
            steps = event.get("steps")
            if not isinstance(steps, list) or not steps:
                return False
            if any(not isinstance(s, dict) or not isinstance(s.get("id"), str)
                   or not isinstance(s.get("label"), str) for s in steps):
                return False
            if len({s["id"] for s in steps}) != len(steps):
                return False
            self.steps = [{"id": s["id"], "label": s["label"], "round": max(0, int(s.get("round", 0))),
                           "status": "pending", "completed": 0, "total": None,
                           "cached": 0, "failed": 0, "unit": "条"} for s in steps]
            self.current_id = None
        elif kind in {"start", "update", "complete", "skip"}:
            step = next((s for s in self.steps if s["id"] == event.get("id")), None)
            if step is None:
                return False
            if kind == "start":
                if step["status"] != "pending":
                    return False
                step["status"] = "running"
                self.current_id = step["id"]
            elif kind == "update":
                if step["status"] != "running":
                    return False
                total = event.get("total")
                completed = max(0, int(event.get("completed", 0)))
                step.update(total=None if total is None else max(0, int(total)),
                            completed=completed, cached=max(0, int(event.get("cached", 0))),
                            failed=max(0, int(event.get("failed", 0))), unit=str(event.get("unit", "条")))
                if "reused" in event:
                    step["reused"] = max(0, int(event["reused"]))
                if "selection_scope" in event:
                    step["selection_scope"] = str(event["selection_scope"])
                if step["total"] is not None:
                    step["completed"] = min(completed, step["total"])
            elif kind == "complete":
                if step["status"] != "running":
                    return False
                status = event.get("status", "completed")
                if status not in {"completed", "completed_with_errors", "failed", "blocked", "skipped"}:
                    return False
                step["status"] = status
            elif step["status"] == "pending":
                step["status"] = "skipped"
            else:
                return False
        else:
            return False
        self.updated_at = time.time()
        return True

    def snapshot(self, job_status: str = "running") -> dict:
        steps = [dict(s) for s in self.steps]
        current = next((s for s in steps if s["id"] == self.current_id), None)
        if job_status in {"failed", "cancelled"} and current and current["status"] == "running":
            current["status"] = job_status
        completed = sum(s["status"] in {"completed", "completed_with_errors"} for s in steps)
        processed = sum(s["status"] in {"completed", "completed_with_errors", "failed", "blocked"} for s in steps)
        skipped = sum(s["status"] == "skipped" for s in steps)
        total = len(steps) - skipped
        fraction = 0.0
        if current and current["status"] == "running" and current["total"]:
            fraction = min(.99, current["completed"] / current["total"])
        percent = round((processed + fraction) / total * 100, 1) if total else None
        if job_status == "succeeded":
            percent = 100.0
        elif percent is not None:
            percent = min(percent, 99.0)
        return {"percent": percent, "estimated": True, "current": current, "steps": steps,
                "completed_stages": completed, "total_stages": total, "skipped_stages": skipped,
                "processed_stages": processed,
                "round": current["round"] if current else 0,
                "rounds": max((s["round"] for s in steps), default=0), "updated_at": self.updated_at}


class ProgressReporter:
    def __init__(self):
        self.state = ProgressState()
        self._last_emit = 0.0

    def _emit(self, event: dict, *, publish: bool = True):
        if self.state.apply(event) and publish and os.environ.get("INTENT_PROGRESS") == "1":
            # tqdm writes carriage-return lines without a trailing newline to
            # stderr, which shares the worker's pipe with stdout. Frame each
            # event on its own line so it cannot be swallowed by a progress bar.
            print("\n" + PROGRESS_PREFIX + json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)

    def plan(self, steps: list[dict]):
        self._emit({"event": "plan", "steps": steps})

    def start(self, step_id: str):
        self.complete()
        self._emit({"event": "start", "id": step_id})

    def update(self, completed: int, total: int, *, cached: int = 0, failed: int = 0, unit: str = "条", reused: int | None = None, selection_scope: str | None = None):
        now = time.monotonic()
        publish = completed == 0 or completed == total or now - self._last_emit >= .25
        metadata = {}
        if reused is not None:
            metadata["reused"] = reused
        if selection_scope is not None:
            metadata["selection_scope"] = selection_scope
            publish = True
        self._emit({**metadata, "event": "update", "id": self.state.current_id, "completed": completed,
                    "total": total, "cached": cached, "failed": failed, "unit": unit}, publish=publish)
        if publish:
            self._last_emit = now

    def complete(self, status="completed"):
        if self.state.current_id:
            self._emit({"event": "complete", "id": self.state.current_id, "status": status})

    def skip(self, step_ids):
        for step_id in step_ids:
            self._emit({"event": "skip", "id": step_id})
