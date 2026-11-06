from __future__ import annotations

import json
import os
import time
import threading
import uuid
import random
from typing import Any, Dict, Optional, Type
from urllib.parse import urlsplit

from json_repair import repair_json
from openai import OpenAI
from pydantic import BaseModel
from intent_engine.infrastructure.model_requests import request_extra_body, tracked_request


_NO_PROXY_LOCK = threading.Lock()
_FAILURE_LOCK = threading.Lock()
_FAILURE_COUNT = 0


def model_failure_count():
    with _FAILURE_LOCK:
        return _FAILURE_COUNT


def _ensure_local_no_proxy(*base_urls: str | None) -> None:
    """Bypass proxies for model hosts, preserving entries from both env vars."""
    required = ["localhost", "127.0.0.1", "::1"]
    for url in base_urls:
        if url:
            host = urlsplit(url).hostname
            if host:
                required.append(host)
    with _NO_PROXY_LOCK:
        entries = []
        seen = set()
        for entry in [*os.environ.get("NO_PROXY", "").split(","),
                      *os.environ.get("no_proxy", "").split(","), *required]:
            entry = entry.strip()
            if entry and entry.lower() not in seen:
                entries.append(entry)
                seen.add(entry.lower())
        for key in ("NO_PROXY", "no_proxy"):
            os.environ[key] = ",".join(entries)


class LocalLLM:
    def __init__(self, cfg: Dict[str, Any]):
        _ensure_local_no_proxy(cfg["base_url"])
        self.cfg = cfg
        from intent_engine.infrastructure.request_replay import RequestReplay
        self.replay = RequestReplay(os.environ.get("INTENT_RESUME_TRACE"))
        self.client = OpenAI(
            base_url=cfg["base_url"],
            api_key=os.environ.get(cfg.get("api_key_env") or "INTENT_LLM_API_KEY") or cfg.get("api_key") or "EMPTY",
            timeout=cfg.get("timeout", 120),
            max_retries=0,
        )

    def _endpoint(self, excluded):
        pool = [self.cfg, *[{**self.cfg, **endpoint} for endpoint in self.cfg.get('endpoints', [])]]
        candidates = [i for i in range(len(pool)) if i not in excluded] or list(range(len(pool)))
        index = random.choice(candidates)
        excluded.add(index)
        cfg = pool[index]
        if index:
            cfg['api_key'] = self.cfg['endpoints'][index-1].get('api_key', 'EMPTY')
        if index == 0:
            return cfg, self.client
        _ensure_local_no_proxy(cfg['base_url'])
        client = OpenAI(base_url=cfg['base_url'],
                        api_key=os.environ.get(cfg.get('api_key_env') or 'INTENT_LLM_API_KEY') or cfg.get('api_key') or 'EMPTY',
                        timeout=cfg.get('timeout',120),max_retries=0)
        return cfg, client

    def chat_json(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: Optional[Type[BaseModel]] = None,
        max_tokens: Optional[int] = None,
    ):
        from intent_engine.settings.prompt_settings import resolve_prompt
        prompt_key = getattr(system_prompt, "key", None)
        system_prompt = resolve_prompt(system_prompt, self.cfg.get("prompt_overrides", {}))
        call_id = uuid.uuid4().hex
        last_error = None
        excluded = set()
        for attempt in range(self.cfg.get("max_retries", 3)):
            trace = {"request_id": uuid.uuid4().hex, "call_id": call_id, "run_id": os.environ.get("INTENT_TRACE_RUN_ID"), "attempt": attempt + 1, "prompt_key": prompt_key, "started_at": time.time()}
            cfg, client = self._endpoint(excluded)
            trace["base_url"] = cfg["base_url"]
            try:
                kwargs = dict(
                    model=cfg["model"],
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=cfg.get("temperature", 0.1),
                    max_tokens=max_tokens or cfg.get("max_tokens", 1600),
                )
                if cfg.get("use_json_mode", True):
                    kwargs["response_format"] = {"type": "json_object"}
                extra = request_extra_body(cfg)
                if extra:
                    kwargs["extra_body"] = extra

                cached = self.replay.get(kwargs, schema)
                if cached is not None:
                    trace.update(input=kwargs.copy(), status="success", replayed=True,
                                 parsed=cached.model_dump() if schema else cached)
                    return cached
                trace["input"] = kwargs.copy()
                try:
                    resp = tracked_request("llm", cfg, client.chat.completions.create, **kwargs)
                except Exception as error:
                    # 部分较旧 vLLM/OpenAI-compatible server 不支持 response_format。
                    # 本地模型仍受“只输出 JSON”的 prompt 约束，因此自动降级重试。
                    if "response_format" not in kwargs or getattr(error, "status_code", None) != 400:
                        raise
                    trace["fallback"] = "response_format rejected; retried without JSON mode"
                    kwargs.pop("response_format")
                    resp = tracked_request("llm", cfg, client.chat.completions.create, **kwargs)
                content = (resp.choices[0].message.content or "").strip()
                trace["output"] = content
                repaired = repair_json(content, return_objects=True)
                if isinstance(repaired, str):
                    data = json.loads(repaired)
                else:
                    data = repaired
                result = schema.model_validate(data) if schema else data
                trace.update(status="success", parsed=data)
                return result
            except Exception as e:
                trace.update(status="failed", error=type(e).__name__ + ": " + str(e))
                last_error = e
                time.sleep(1.0 + attempt)
            finally:
                if client is not self.client:
                    client.close()
                trace["duration_seconds"] = time.time() - trace["started_at"]
                if os.environ.get("INTENT_PROGRESS") == "1" and "input" in trace:
                    from intent_engine.infrastructure.model_requests import _EMIT_LOCK
                    with _EMIT_LOCK:
                        print("\n@@INTENT_TRACE@@" + json.dumps(trace, ensure_ascii=False, default=str), flush=True)

        global _FAILURE_COUNT
        with _FAILURE_LOCK:
            _FAILURE_COUNT += 1
        raise RuntimeError(f"Local LLM request failed after retries: {last_error}")
