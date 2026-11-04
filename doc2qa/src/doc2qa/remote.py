from __future__ import annotations

import json
import logging
import os

from .config import SynthesisConfig

log = logging.getLogger(__name__)
EMBEDDING_BATCH_SIZE = 20


def unsupported_response_format(exc) -> bool:
    """Only negotiate formats for an explicit format-support error."""
    if getattr(exc, "status_code", None) not in {400, 422}:
        return False
    body = getattr(exc, "body", None)
    message = json.dumps(body, ensure_ascii=False) if body else str(exc)
    message = message.casefold()
    return any(
        term in message for term in ("response_format", "json_schema", "json_object")
    ) and any(
        term in message
        for term in ("unavailable", "unsupported", "not supported", "does not support")
    )


class Remote:
    """Bounded OpenAI-compatible structured calls, with explicit usage accounting."""

    def __init__(self, config: SynthesisConfig):
        if not os.getenv("OPENAI_API_KEY"):
            raise ValueError(
                "OPENAI_API_KEY is not configured. Use prepare or generate --smoke for offline verification."
            )
        from openai import OpenAI

        self.client = OpenAI(timeout=config.timeout, max_retries=config.max_retries)
        self.config = config
        self._response_formats = {}
        self.usage = {
            "judge_calls": 0,
            "embedding_calls": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "embedding_tokens": 0,
        }

    def structured(self, system: str, payload: str, schema):
        if not self.config.judge_model.strip():
            raise ValueError("Configure DEEPEVAL_JUDGE_MODEL in .env")
        model = self.config.judge_model
        modes = ("json_schema", "json_object", "text")
        first = modes.index(self._response_formats.get(model, "json_schema"))
        for mode in modes[first:]:
            messages = [{"role": "system", "content": system}, {"role": "user", "content": payload}]
            if mode != "json_schema":
                # The provider enforces no schema here; keep the same contract
                # in the prompt and validate every response locally below.
                messages[0]["content"] += (
                    "\nReturn only a JSON object matching this JSON schema:\n"
                    + json.dumps(schema.model_json_schema(), ensure_ascii=False)
                )
            self.usage["judge_calls"] += 1
            try:
                if mode == "json_schema":
                    response = self.client.beta.chat.completions.parse(
                        model=model, messages=messages, response_format=schema
                    )
                else:
                    options = (
                        {"response_format": {"type": "json_object"}}
                        if mode == "json_object"
                        else {}
                    )
                    response = self.client.chat.completions.create(
                        model=model, messages=messages, **options
                    )
            except Exception as exc:
                if mode != "text" and unsupported_response_format(exc):
                    next_mode = modes[modes.index(mode) + 1]
                    self._response_formats[model] = next_mode
                    log.warning(
                        "Judge response format unsupported: model=%s format=%s status=%s; "
                        "falling back to %s; error=%s",
                        model,
                        mode,
                        getattr(exc, "status_code", None),
                        next_mode,
                        exc,
                    )
                    continue
                log.exception(
                    "Judge request failed: model=%s format=%s status=%s",
                    model,
                    mode,
                    getattr(exc, "status_code", None),
                )
                raise
            self._response_formats[model] = mode
            if response.usage:
                self.usage["prompt_tokens"] += response.usage.prompt_tokens
                self.usage["completion_tokens"] += response.usage.completion_tokens
            choice = response.choices[0]
            message = choice.message
            if message.refusal or choice.finish_reason != "stop":
                raise ValueError("Judge refused or returned an incomplete response")
            if mode == "json_schema":
                if message.parsed is None:
                    raise ValueError("Judge returned no parsed result")
                return message.parsed
            if not message.content:
                raise ValueError("Judge returned no JSON content")
            return schema.model_validate_json(message.content)

    def embed(self, texts: list[str], model: str) -> list[list[float]]:
        if not model.strip():
            raise ValueError("Configure DEEPEVAL_EMBEDDING_MODEL in .env")
        vectors = []
        for start in range(0, len(texts), EMBEDDING_BATCH_SIZE):
            self.usage["embedding_calls"] += 1
            result = self.client.embeddings.create(
                model=model, input=texts[start : start + EMBEDDING_BATCH_SIZE]
            )
            vectors.extend(
                item.embedding for item in sorted(result.data, key=lambda item: item.index)
            )
            self.usage["embedding_tokens"] += result.usage.total_tokens
        return vectors
