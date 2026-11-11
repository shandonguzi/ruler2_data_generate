"""兼容 /v1/chat/completions 的 LLM 客户端。"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Mapping, Sequence

import openai
from openai import OpenAI

from .models import LLMSettings

logger = logging.getLogger(__name__)

_RETRYABLE = (
    openai.RateLimitError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    openai.InternalServerError,
)


class LLMError(Exception):
    """LLM 调用失败。"""


class LLMClient:
    """线程安全的 chat completions 客户端。

    不传 temperature、max_tokens 等生成参数，模型行为由服务端默认值决定。
    """

    def __init__(
        self,
        settings: LLMSettings,
        *,
        model: str | None,
        api_key: str | None,
        base_url: str | None = None,
        client: OpenAI | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not model:
            raise LLMError("缺少 LLM 模型名")
        if client is None and not api_key:
            raise LLMError("缺少 LLM API Key")
        self.settings = settings
        self._model = model
        self._sleep = sleep
        self._semaphore = threading.BoundedSemaphore(settings.concurrency)
        self._client = client or OpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=settings.timeout_seconds,
            max_retries=0,
        )

    @property
    def model_name(self) -> str:
        return self._model

    def complete(
        self,
        messages: Sequence[Mapping[str, str]],
        *,
        json_mode: bool = False,
    ) -> str:
        use_json = json_mode
        downgraded = False
        attempt = 0

        while True:
            try:
                return self._call(list(messages), use_json=use_json)
            except openai.BadRequestError as exc:
                if use_json and not downgraded:
                    logger.warning("JSON 模式被拒绝，降级为普通文本重试")
                    use_json = False
                    downgraded = True
                    continue
                raise LLMError(f"LLM 请求无效：{exc}") from exc
            except _RETRYABLE as exc:
                if attempt >= self.settings.max_retries:
                    raise LLMError(f"LLM 调用失败：{exc}") from exc
                logger.warning(
                    "LLM 调用失败，%d 秒后重试（第 %d 次）：%s",
                    2**attempt,
                    attempt + 1,
                    exc,
                )
                self._sleep(2**attempt)
                attempt += 1
            except openai.APIStatusError as exc:
                if exc.status_code == 429 or exc.status_code >= 500:
                    if attempt >= self.settings.max_retries:
                        raise LLMError(f"LLM 调用失败：{exc}") from exc
                    logger.warning(
                        "LLM 返回 HTTP %d，%d 秒后重试（第 %d 次）",
                        exc.status_code,
                        2**attempt,
                        attempt + 1,
                    )
                    self._sleep(2**attempt)
                    attempt += 1
                    continue
                raise LLMError(f"LLM 调用失败（HTTP {exc.status_code}）") from exc

    def _call(self, messages: list[Mapping[str, str]], *, use_json: bool) -> str:
        kwargs: dict = {
            "model": self._model,
            "messages": messages,
        }
        if use_json:
            kwargs["response_format"] = {"type": "json_object"}

        started = time.monotonic()
        with self._semaphore:
            response = self._client.chat.completions.create(**kwargs)
        elapsed = time.monotonic() - started

        content = response.choices[0].message.content if response.choices else None
        if content is None or not content.strip():
            raise LLMError("LLM 返回空响应")
        logger.debug("LLM 调用完成：model=%s elapsed=%.2fs", self._model, elapsed)
        return content
