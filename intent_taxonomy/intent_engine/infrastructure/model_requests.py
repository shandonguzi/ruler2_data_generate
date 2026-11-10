"""Shared extra-body handling and server-reported model usage accounting."""
from __future__ import annotations

import copy
import json
import os
import threading
import time
import uuid
from typing import Any, Callable

USAGE_PREFIX = "@@INTENT_USAGE@@"
COUNTERS = ("requests", "failed_requests", "reported_requests", "unreported_requests",
            "prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens",
            "cached_reported_requests", "reasoning_reported_requests")
TOKEN_FIELDS = ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens")


def request_extra_body(cfg: dict, *, thinking=True) -> dict:
    body = copy.deepcopy(cfg.get("extra_body") or {})
    if not isinstance(body, dict):
        raise ValueError("extra_body 必须是 JSON 对象")
    if any(not isinstance(key, str) or not key.strip() for key in body):
        raise ValueError("额外参数的 key 不能为空")
    if set(body) & {"model", "messages", "input", "stream"}:
        raise ValueError("额外参数不能覆盖 model、messages、input 或 stream")
    json.dumps(body, allow_nan=False)
    mode = cfg.get("thinking_mode") or "default"
    if thinking and mode != "default":
        if mode not in {"on", "off"}:
            raise ValueError("思考模式必须为 default、on 或 off")
        path = (cfg.get("thinking_parameter") or "chat_template_kwargs.enable_thinking").split(".")
        if any(not part.strip() for part in path) or len(path) > 8 or path[0] in {"model", "messages", "input", "stream"}:
            raise ValueError("思考参数路径无效")
        node = body
        for part in path[:-1]:
            if part not in node:
                node[part] = {}
            if not isinstance(node[part], dict):
                raise ValueError("思考参数路径与额外参数的值冲突")
            node = node[part]
        node[path[-1]] = mode == "on"
    return body


def _field(value: Any, name: str):
    return value.get(name) if isinstance(value, dict) else getattr(value, name, None)


def response_usage(response, kind="llm") -> dict:
    """Missing usage is unknown, never a fabricated zero-token response."""
    usage = _field(response, "usage")
    def count(obj, key):
        value = _field(obj, key)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    prompt = count(usage, "prompt_tokens")
    completion = count(usage, "completion_tokens")
    total = count(usage, "total_tokens")
    if kind == "embedding" and prompt is not None:
        completion = 0
    reported = prompt is not None and completion is not None
    if reported and total is None:
        total = prompt + completion
    cached = count(_field(usage, "prompt_tokens_details"), "cached_tokens")
    reasoning = count(_field(usage, "completion_tokens_details"), "reasoning_tokens")
    return {"reported": reported, "prompt_tokens": prompt, "completion_tokens": completion,
            "total_tokens": total, "cached_tokens": cached, "reasoning_tokens": reasoning}


class UsageLedger:
    def __init__(self):
        self._lock = threading.Lock()
        self._groups = {}

    @classmethod
    def from_snapshot(cls, snapshot: dict):
        ledger = cls()
        for group in snapshot.get("models", []):
            key = (group["kind"], group["model"], group.get("base_url", ""))
            ledger._groups[key] = {"kind": key[0], "model": key[1], "base_url": key[2],
                                   **{name: group.get(name, 0) for name in COUNTERS}}
        return ledger

    def apply(self, event: dict):
        # Events contain accounting fields only, never prompts, responses or API keys.
        with self._lock:
            key = (event["kind"], event["model"], event.get("base_url", ""))
            group = self._groups.setdefault(key, {"kind": key[0], "model": key[1], "base_url": key[2],
                                                    **{name: 0 for name in COUNTERS}})
            group["requests"] += 1
            if event.get("failed"):
                group["failed_requests"] += 1
            if event.get("reported"):
                group["reported_requests"] += 1
            else:
                group["unreported_requests"] += 1
            if event.get("cached_tokens") is not None:
                group["cached_reported_requests"] += 1
            if event.get("reasoning_tokens") is not None:
                group["reasoning_reported_requests"] += 1
            for name in TOKEN_FIELDS:
                value = event.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    group[name] += value

    def snapshot(self):
        with self._lock:
            groups = copy.deepcopy(list(self._groups.values()))
        return {**{key: sum(group[key] for group in groups) for key in COUNTERS}, "models": groups,
                "source": "server_usage", "complete": all(group["unreported_requests"] == 0 for group in groups)}


USAGE = UsageLedger()
_EMIT_LOCK = threading.Lock()


def usage_since(before: dict) -> dict:
    after = USAGE.snapshot()
    old_groups = {(g["kind"], g["model"], g["base_url"]): g for g in before["models"]}
    models = []
    for group in after["models"]:
        old = old_groups.get((group["kind"], group["model"], group["base_url"]), {})
        delta = {key: group[key] - old.get(key, 0) for key in COUNTERS}
        if delta["requests"]:
            models.append({**{key: group[key] for key in ("kind", "model", "base_url")}, **delta})
    return {**{key: after[key] - before[key] for key in COUNTERS}, "models": models,
            "source": "server_usage", "complete": all(g["unreported_requests"] == 0 for g in models)}


def tracked_request(kind: str, cfg: dict, request: Callable, **kwargs):
    from intent_engine.infrastructure.job_pause import wait_if_paused
    wait_if_paused()
    event = {"kind": kind, "model": cfg["model"], "base_url": cfg.get("base_url", "")}
    trace = None
    if kind == "embedding" and os.environ.get("INTENT_PROGRESS") == "1":
        trace = {"request_id": uuid.uuid4().hex, "kind": kind,
                 "run_id": os.environ.get("INTENT_TRACE_RUN_ID"),
                 "attempt": 1, "started_at": time.time(), "input": kwargs}
    try:
        response = request(**kwargs)
    except Exception as error:
        body = getattr(error, "body", None)
        event.update(response_usage({"usage": body.get("usage")} if isinstance(body, dict) else {}, kind))
        event["failed"] = True
        if trace is not None:
            trace.update(status="failed", error=type(error).__name__ + ": " + str(error))
        raise
    else:
        event.update(response_usage(response, kind))
        if trace is not None:
            data = _field(response, "data") or []
            trace.update(status="success", output={"vector_count": len(data),
                         "dimensions": [len(_field(item, "embedding") or []) for item in data],
                         "note": "仅记录返回数量与维度，完整向量保存在计算结果或缓存中。"})
        return response
    finally:
        USAGE.apply(event)
        if os.environ.get("INTENT_PROGRESS") == "1":
            with _EMIT_LOCK:
                print("\n" + USAGE_PREFIX + json.dumps(event, ensure_ascii=False, separators=(",", ":")), flush=True)
                if trace is not None:
                    trace["duration_seconds"] = time.time() - trace["started_at"]
                    trace["usage"] = response_usage(response, kind) if not event.get("failed") else response_usage({})
                    print("\n@@INTENT_TRACE@@" + json.dumps(trace, ensure_ascii=False, default=str), flush=True)
