"""OpenAI-compatible Chat Completions with durable, bounded technical attempts."""
from __future__ import annotations

import threading
import time
import uuid
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .records import digest
from .validators import canonical, parse_json, validate_schema


class TaskError(ValueError):
    pass


class PermanentAPIError(TaskError):
    pass


def _wire_schema(value):
    """Send the structural subset; retain stronger constraints for local validation."""
    if isinstance(value, list):
        return [_wire_schema(item) for item in value]
    if isinstance(value, dict):
        local_only = {"uniqueItems", "minLength", "pattern", "minimum", "minItems"}
        return {key: ({name: _wire_schema(spec) for name, spec in item.items()} if key == "properties"
                      else _wire_schema(item)) for key, item in value.items() if key not in local_only}
    return value


def response_format(config: dict, model: str, name: str, schema: dict) -> dict:
    mode = config["llm"]["response_format"]
    if mode == "auto":
        mode = "json_object" if model.startswith("deepseek-") else "json_schema"
    if mode == "json_object":
        return {"type": mode}
    return {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": _wire_schema(schema)}}


def effective_parameters(config: dict, model: str, name: str, schema: dict) -> dict:
    params = dict(config["llm"]["request_parameters"])
    if config["llm"]["temperature"] is not None:
        params["temperature"] = config["llm"]["temperature"]
    params["response_format"] = response_format(config, model, name, schema)
    return params


def post_completion(api_url: str, api_key: str, body: dict, timeout: float) -> str:
    request = Request(api_url, data=canonical(body).encode("utf-8"),
                      headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
    with urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8")


class LLMClient:
    def __init__(self, config: dict, records, transport=None):
        self.config, self.records = config, records
        self.transport = transport or post_completion
        self._requires_key = transport is None
        self._semaphore = threading.BoundedSemaphore(config["llm"]["concurrency"])
        self._rate_lock = threading.Lock()
        self._next_request = 0.0
        self.api_key = config["llm"].get("api_key", "")

    def _redact(self, text):
        return text.replace(self.api_key, "[REDACTED]") if self.api_key else text

    def identity(self, case_id: str, name: str, payload: dict, spec: dict, schema: dict) -> dict:
        return {"case_id": case_id, "task_name": name, "payload_hash": digest(payload),
                "prompt_sha256": spec["prompt_sha256"], "model": spec["model"],
                "api_url": self.config["llm"]["api_url"],
                "effective_request_parameters": effective_parameters(self.config, spec["model"], name, schema),
                "response_schema_hash": digest(schema), "rubric_version": self.config["rubric_version"]}

    def _parse(self, raw: str, schema: dict, validator):
        response = parse_json(raw)
        if not isinstance(response, dict) or not isinstance(response.get("choices"), list) or not response["choices"]:
            raise ValueError("Completion must contain a nonempty choices array")
        choice = response["choices"][0]
        if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
            raise ValueError("Completion choice must contain a message object")
        if choice.get("finish_reason") != "stop" or choice["message"].get("refusal"):
            raise ValueError("Truncated/refused completion")
        if not isinstance(choice["message"].get("content"), str):
            raise ValueError("Completion message content must be a JSON string")
        result = parse_json(choice["message"]["content"])
        validate_schema(result, schema)
        validator(result)
        usage = response.get("usage")
        return result, str(response.get("model", "")), usage if isinstance(usage, dict) else {}

    def call(self, identity: dict, payload: dict, spec: dict, schema: dict, validator, binding: dict) -> dict:
        task_id = digest(identity)
        common = {**binding, "task_id": task_id, "task_name": identity["task_name"]}
        events = self.records.select("attempts", task_id=task_id)
        starts = [event for event in events if event["event"] == "START"]
        attempt_ids = [event["attempt_id"] for event in starts]
        last_error = "Technical attempts exhausted or interrupted"
        # Replay durable responses before spending any remaining attempt budget.
        for event in events:
            if event["event"] != "RESPONSE":
                continue
            try:
                result, model, usage = self._parse(event["raw_response"], schema, validator)
                return {"result": result, "response_model": model, "usage": usage,
                        "attempt_ids": attempt_ids, "task_id": task_id, "identity": identity}
            except (ValueError, TypeError, KeyError, IndexError) as exc:
                last_error = str(exc)
        if any(event.get("permanent") for event in events):
            raise PermanentAPIError("Permanent API failure; inspect attempts.jsonl")
        if self._requires_key and not self.api_key:
            raise PermanentAPIError("Set llm.api_key in the config YAML")
        body = {"model": spec["model"], "messages": [
            {"role": "system", "content": spec["prompt_content"]},
            {"role": "user", "content": canonical(payload)},
        ], **identity["effective_request_parameters"]}
        for number in range(len(starts) + 1, self.config["llm"]["max_attempts"] + 1):
            attempt_id = uuid.uuid4().hex
            attempt_ids.append(attempt_id)
            attempt = {**common, "attempt_id": attempt_id, "attempt_number": number}
            self.records.append("attempts", {**attempt, "event": "START", "request_metadata": identity})
            started = time.monotonic()
            usage = {}
            try:
                with self._semaphore:
                    rate = self.config["llm"]["requests_per_second"]
                    if rate:
                        with self._rate_lock:
                            wait = max(0, self._next_request - time.monotonic())
                            if wait:
                                time.sleep(wait)
                            self._next_request = time.monotonic() + 1 / rate
                    raw = self.transport(self.config["llm"]["api_url"], self.api_key, body,
                                         self.config["llm"]["timeout_seconds"])
                self.records.append("attempts", {**attempt, "event": "RESPONSE", "raw_response": self._redact(raw)})
                envelope = parse_json(raw)
                usage = envelope.get("usage", {}) if isinstance(envelope, dict) else {}
                result, model, usage = self._parse(raw, schema, validator)
            except HTTPError as exc:
                permanent = exc.code not in (408, 429, 500, 502, 503, 504)
                last_error = self._redact(f"HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}")
                self.records.append("attempts", {**attempt, "event": "FINISH", "ok": False,
                                               "error": last_error, "permanent": permanent, "usage": usage,
                                               "elapsed_seconds": time.monotonic() - started})
                if permanent:
                    raise PermanentAPIError(last_error) from exc
            except (OSError, URLError, ValueError, TypeError, KeyError, IndexError) as exc:
                last_error = self._redact(str(exc))
                self.records.append("attempts", {**attempt, "event": "FINISH", "ok": False,
                                               "error": last_error, "usage": usage,
                                               "elapsed_seconds": time.monotonic() - started})
            else:
                self.records.append("attempts", {**attempt, "event": "FINISH", "ok": True,
                                               "usage": usage, "elapsed_seconds": time.monotonic() - started})
                return {"result": result, "response_model": model, "usage": usage,
                        "attempt_ids": attempt_ids, "task_id": task_id, "identity": identity}
            if number < self.config["llm"]["max_attempts"]:
                time.sleep(min(60, self.config["llm"]["retry_backoff_seconds"] * 2 ** (number - 1)))
        raise TaskError(last_error)
