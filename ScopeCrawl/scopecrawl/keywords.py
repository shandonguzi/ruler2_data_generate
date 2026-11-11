"""关键词生成：渲染 prompt、调用 LLM、解析与清洗关键词列表。"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from pathlib import Path

from .config import PROJECT_ROOT
from .models import KeywordsSettings, RunInput
from .utils import render_template

logger = logging.getLogger(__name__)

DEFAULT_PROMPT_PATH = PROJECT_ROOT / "prompts" / "keywords.md"

LANGUAGE_NAMES = {
    "zh": "Simplified Chinese",
    "en": "English",
    "ar": "Modern Standard Arabic",
    "ar-EG": "Egyptian Arabic",
}

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE_RE = re.compile(r"\s+")
_META_HINTS = ("查询", "信息", "服务", "指南", "方法", "怎么用")

_REGENERATE_MESSAGE = (
    "Those queries copy the S3/S4 catalog labels, use meta words, or fail to "
    "diversify. Regenerate them as short, natural searches: one concrete aspect or "
    "topic of the scenario per query. Do not include the S3/S4 text or words like "
    "query/information/service/guide/how to. Output only a JSON object with a "
    "'queries' array, or a JSON array of strings."
)


class KeywordError(Exception):
    """关键词生成或解析失败，属于致命错误。"""


def generate_keywords(
    run: RunInput,
    client,
    settings: KeywordsSettings,
    *,
    prompt_path: str | Path | None = None,
) -> list[str]:
    if not run.s3.strip() or not run.s4.strip():
        raise KeywordError("s3/s4 不能为空")

    template = _read_prompt(Path(prompt_path) if prompt_path else DEFAULT_PROMPT_PATH)
    try:
        rendered = render_template(
            template,
            {
                "s3": run.s3,
                "s4": run.s4,
                "location": run.location or "none",
                "language_name": LANGUAGE_NAMES[run.language],
                "max_queries": settings.max_queries,
            },
        )
    except KeyError as exc:
        raise KeywordError(f"关键词 prompt 缺少变量：{exc}") from exc
    messages: list[dict[str, str]] = [{"role": "user", "content": rendered}]

    keywords: list[str] | None = None
    scene: dict | None = None
    last_output = ""
    for attempt in range(2):
        if attempt == 1:
            messages = messages + [
                {"role": "assistant", "content": last_output},
                {
                    "role": "user",
                    "content": (
                        "Your previous output was not a valid JSON array of strings or a "
                        "JSON object with a 'queries' array. Output only that JSON, with "
                        "no explanations and no Markdown code fences."
                    ),
                },
            ]
        try:
            last_output = client.complete(messages, json_mode=False)
        except Exception as exc:  # noqa: BLE001 - 统一转成 KeywordError
            if attempt == 1:
                raise KeywordError(f"LLM 调用失败：{exc}") from exc
            logger.warning("关键词生成调用失败，重试一次：%s", exc)
            continue
        keywords, parsed_scene = _parse_payload(last_output)
        if parsed_scene is not None:
            scene = parsed_scene
        if keywords is not None:
            break

    if keywords is None:
        raise KeywordError("LLM 输出无法解析为关键词数组")

    cleaned = _clean_keywords(keywords)
    if not cleaned:
        raise KeywordError("LLM 未生成任何有效关键词")

    if scene:
        _log_scene(scene)

    if _copies_catalog_labels(cleaned, run):
        logger.warning("关键词照抄了 S3/S4 标签，要求模型重新生成")
        try:
            retry_output = client.complete(
                messages
                + [
                    {"role": "assistant", "content": last_output},
                    {"role": "user", "content": _REGENERATE_MESSAGE},
                ],
                json_mode=False,
            )
            retry_keywords, retry_scene = _parse_payload(retry_output)
        except Exception as exc:  # noqa: BLE001 - 保留首轮结果
            logger.warning("重新生成关键词失败，保留首轮结果：%s", exc)
        else:
            if retry_scene is not None and scene is None:
                _log_scene(retry_scene)
            retry_cleaned = _clean_keywords(retry_keywords or [])
            if retry_cleaned and not _copies_catalog_labels(retry_cleaned, run):
                cleaned = retry_cleaned
            else:
                logger.warning("重新生成的关键词仍包含 S3/S4 标签，保留首轮结果")

    result = cleaned[: settings.max_queries]
    logger.info("关键词生成完成：prompt_version=%s count=%d", settings.prompt_version, len(result))
    return result


def parse_keywords(text: str) -> list[str] | None:
    """从模型输出中容错解析字符串数组；失败返回 None。"""
    queries, _ = _parse_payload(text)
    return queries


def _parse_payload(text: str) -> tuple[list[str] | None, dict | None]:
    """解析 queries 与可选 scene；缺失的部分返回 None。"""
    payload = _strip_noise(text)
    if not payload:
        return None, None

    data = _try_json(payload)
    if isinstance(data, dict):
        return _from_object(data)
    if isinstance(data, list):
        return _as_string_list(data), None

    start = payload.find("[")
    end = payload.rfind("]")
    if start != -1 and end > start:
        data = _try_json(payload[start : end + 1])
        if isinstance(data, list):
            return _as_string_list(data), None

    start = payload.find("{")
    end = payload.rfind("}")
    if start != -1 and end > start:
        data = _try_json(payload[start : end + 1])
        if isinstance(data, dict):
            return _from_object(data)
    return None, None


def _from_object(data: dict) -> tuple[list[str] | None, dict | None]:
    scene = data.get("scene") if isinstance(data.get("scene"), dict) else None
    for key in ("queries", "keywords"):
        candidate = data.get(key)
        if isinstance(candidate, list):
            return _as_string_list(candidate), scene
    return None, scene


def _strip_noise(text: str) -> str:
    value = (text or "").lstrip("﻿").strip()
    if "```" in value:
        blocks = re.findall(r"```(?:json)?\s*(.*?)```", value, re.DOTALL | re.IGNORECASE)
        if blocks:
            value = blocks[0].strip()
    return value


def _try_json(text: str):
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return None


def _as_string_list(value: list) -> list[str] | None:
    items = [item for item in value if isinstance(item, str) and item.strip()]
    if not items:
        return None
    return items


def _clean_keywords(items: list[str]) -> list[str]:
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in items:
        text = _WHITESPACE_RE.sub(" ", unicodedata.normalize("NFKC", item)).strip()
        if not text:
            continue
        key = _dedupe_key(text)
        if not key or key in seen:
            continue
        seen.add(key)
        cleaned.append(text)
    return cleaned


def _log_scene(scene: dict) -> None:
    logger.info(
        "[关键词] 场景画像：location_relevant=%s time_sensitive=%s question_types=%s doc_forms=%s",
        scene.get("location_relevant"),
        scene.get("time_sensitive"),
        scene.get("question_types"),
        scene.get("needed_doc_forms"),
    )


def _dedupe_key(text: str) -> str:
    value = unicodedata.normalize("NFKC", text).casefold()
    value = _PUNCT_RE.sub("", value)
    return _WHITESPACE_RE.sub("", value)


def _copies_catalog_labels(queries: list[str], run: RunInput) -> bool:
    """检测查询是否把 S3/S4 场景标签原样拼进了搜索词。"""
    s3 = _dedupe_key(run.s3)
    s4 = _dedupe_key(run.s4)
    s4_looks_meta = bool(s4) and any(hint in s4 for hint in _META_HINTS)
    for query in queries:
        key = _dedupe_key(query)
        if s3 and s4 and s3 in key and s4 in key:
            return True
        if s4_looks_meta and s4 in key:
            return True
    return False


def _read_prompt(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KeywordError(f"无法读取关键词 prompt：{path}：{exc}") from exc
