"""内容复核、丢弃判定与多样性选择。"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from .config import PROJECT_ROOT
from .models import (
    LANGUAGE_NAMES,
    AuthoritySettings,
    ContentScore,
    DiversitySettings,
    Document,
    DocumentEvaluation,
    ExtractedDoc,
    RelevanceResult,
    ReviewSettings,
    RunConfig,
    ScoreThresholds,
)
from .utils import (
    read_prompt,
    render_template,
    strip_json_noise,
    to_iso,
    try_json,
    utc_now,
)

logger = logging.getLogger(__name__)

DEFAULT_REVIEW_PROMPT = PROJECT_ROOT / "prompts" / "review.md"

ALLOWED_FLAGS = {
    "off_topic",
    "outdated",
    "aggregator_page",
    "partial_match",
    "no_content",
    "location_mismatch",
    "language_mismatch",
    "relevance_unevaluated",
}

def load_review_prompt(prompt_path: str | Path | None = None) -> str:
    """读取复核 prompt；阶段可在并发前调用一次后复用。"""
    return read_prompt(
        Path(prompt_path) if prompt_path else DEFAULT_REVIEW_PROMPT,
        label="复核 prompt",
        error_cls=RuntimeError,
    )


def review_content(
    doc: ExtractedDoc,
    run: RunConfig,
    client,
    settings: ReviewSettings,
    *,
    prompt_path: str | Path | None = None,
    template: str | None = None,
) -> DocumentEvaluation:
    model = getattr(client, "model_name", None)
    reviewed_at = to_iso(utc_now())

    if not settings.enabled:
        return DocumentEvaluation(
            relevance=RelevanceResult(
                reason="内容复核已禁用", model=model, reviewed_at=reviewed_at
            )
        )
    if not doc.markdown.strip():
        return DocumentEvaluation(
            relevance=RelevanceResult(
                flags=["no_content"],
                reason="正文为空，未调用复核模型",
                model=model,
                reviewed_at=reviewed_at,
            )
        )

    if template is None:
        template = load_review_prompt(prompt_path)
    rendered = render_template(
        template,
        {
            "s3": run.s3,
            "s4": run.s4,
            "matched_queries": " | ".join(doc.matched_queries) or "none",
            "location": run.location or "none",
            "language_name": LANGUAGE_NAMES[run.language],
            "time_range": run.time_range or "none",
            "title": doc.title or "none",
            "url": doc.url,
            "published_at": doc.published_at or "none",
            "page_language": doc.lang or "none",
            # 按已确认决策，正文不截断，完整交给模型。
            "content": doc.markdown,
        },
    )

    messages: list[dict[str, str]] = [{"role": "user", "content": rendered}]
    last_output = ""
    parsed: dict | None = None
    last_error = ""

    for attempt in range(2):
        if attempt == 1:
            messages = messages + [
                {"role": "assistant", "content": last_output},
                {
                    "role": "user",
                    "content": (
                        "Return only one valid JSON object matching the schema. "
                        "Do not include explanations or code fences."
                    ),
                },
            ]
        try:
            last_output = client.complete(messages, json_mode=True)
        except Exception as exc:  # noqa: BLE001 - 失败降级，不中断主流程
            last_error = str(exc)
            continue
        parsed = parse_json_object(last_output)
        if parsed is not None:
            break

    if parsed is None:
        logger.warning(
            "内容复核失败：%s（正文 %d 字符，响应前 200 字符：%r）",
            doc.url,
            len(doc.markdown),
            last_output[:200],
        )
        return DocumentEvaluation(
            relevance=RelevanceResult(
                flags=["relevance_unevaluated"],
                reason=f"内容复核失败：{last_error or '模型未返回合法 JSON'}",
                model=model,
                reviewed_at=reviewed_at,
            )
        )

    score = _to_score(parsed.get("relevance_score"))
    content_type = parsed.get("content_type")
    if content_type not in ("article", "listing", "widget", "other"):
        content_type = "other"
    if content_type in ("listing", "widget") and score is not None:
        score = min(score, 0.3)
    level = _level_for(score, settings) if score is not None else None
    scope = parsed.get("matched_scope")
    if scope not in ("s3", "s4", "both", "none"):
        scope = "none"

    raw_flags = parsed.get("flags")
    flags = [
        item
        for item in (raw_flags if isinstance(raw_flags, list) else [])
        if isinstance(item, str) and item in ALLOWED_FLAGS
    ]
    if content_type in ("listing", "widget") and "aggregator_page" not in flags:
        flags.append("aggregator_page")

    content_reason = str(parsed.get("content_reason") or "")
    return DocumentEvaluation(
        relevance=RelevanceResult(
            relevance_score=score,
            relevance_level=level,
            content_type=content_type,
            matched_scope=scope,
            location_match=_to_bool(parsed.get("location_match")),
            language_match=_to_bool(parsed.get("language_match")),
            temporal_match=_to_bool(parsed.get("temporal_match")),
            flags=flags,
            reason=str(parsed.get("reason") or ""),
            model=model,
            reviewed_at=reviewed_at,
        ),
        content_richness=_parse_content_score(
            parsed, "content_richness", settings.thresholds, content_reason
        ),
        content_quality=_parse_content_score(
            parsed, "content_quality", settings.thresholds, content_reason
        ),
    )


def should_drop(
    evaluation: DocumentEvaluation,
    review_settings: ReviewSettings,
    authority_settings: AuthoritySettings,
) -> tuple[bool, list[str]]:
    """返回 (是否丢弃, 命中的原因码)；原因码用于汇总与排查。"""
    relevance = evaluation.relevance
    reasons: list[str] = []

    if review_settings.drop_structural and relevance.content_type in ("listing", "widget"):
        reasons.append("structural")
    if "no_content" in relevance.flags:
        reasons.append("no_content")
    if relevance.relevance_level == "none" and review_settings.drop_none:
        reasons.append("relevance_none")
    if relevance.relevance_level == "low" and review_settings.drop_low:
        reasons.append("relevance_low")
    if (
        review_settings.enabled
        and review_settings.drop_unevaluated
        and "relevance_unevaluated" in relevance.flags
    ):
        reasons.append("relevance_unevaluated")

    content = review_settings.content
    richness = evaluation.content_richness.score
    quality = evaluation.content_quality.score
    if (
        content.drop_when_both_low
        and richness is not None
        and quality is not None
        and richness < content.richness_low
        and quality < content.quality_low
    ):
        reasons.append("content_both_low")

    authority = evaluation.authority
    if (
        authority.authority_score is not None
        and authority.authority_score < authority_settings.scoring.low_threshold
        and authority_settings.scoring.drop_low
    ):
        reasons.append("authority_low")
    return bool(reasons), reasons


def apply_diversity(
    documents: list[Document],
    settings: DiversitySettings,
) -> tuple[list[Document], Counter[str]]:
    """按单 query / 单域名配额做全局同质化限流；返回结果保持原队列顺序。"""
    if not settings.enabled:
        logger.info("[评估筛选] 多样性限流已禁用，跳过")
        return documents, Counter()
    if settings.max_per_query is None and settings.max_per_domain is None:
        return documents, Counter()

    ranked = sorted(
        enumerate(documents),
        key=lambda item: (
            -_score_value(item[1].evaluation.relevance.relevance_score),
            -_score_value(item[1].evaluation.content_quality.score),
            -_score_value(item[1].evaluation.content_richness.score),
            -_score_value(item[1].evaluation.authority.authority_score),
            item[0],
        ),
    )
    reasons: Counter[str] = Counter()
    query_counts: Counter[str] = Counter()
    domain_counts: Counter[str] = Counter()
    selected: set[int] = set()

    for index, document in ranked:
        queries = document.extracted.matched_queries
        query_key = queries[0] if queries else ""
        domain = document.extracted.site
        if (
            settings.max_per_query is not None
            and query_counts[query_key] >= settings.max_per_query
        ):
            reasons["query_quota"] += 1
            continue
        if (
            settings.max_per_domain is not None
            and domain_counts[domain] >= settings.max_per_domain
        ):
            reasons["domain_quota"] += 1
            continue
        selected.add(index)
        query_counts[query_key] += 1
        domain_counts[domain] += 1

    kept = [document for index, document in enumerate(documents) if index in selected]
    return kept, reasons


def _score_value(value: float | None) -> float:
    return -1.0 if value is None else float(value)


def parse_json_object(text: str) -> dict | None:
    value = strip_json_noise(text)

    data = try_json(value)
    if isinstance(data, dict):
        return data

    start = value.find("{")
    end = value.rfind("}")
    if start != -1 and end > start:
        data = try_json(value[start : end + 1])
        if isinstance(data, dict):
            return data
    return None


def _to_score(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return min(max(float(value), 0.0), 1.0)


def _to_bool(value) -> bool | None:
    return value if isinstance(value, bool) else None


def _level_for(score: float, settings: ReviewSettings) -> str:
    thresholds = settings.thresholds
    if score >= thresholds.high:
        return "high"
    if score >= thresholds.medium:
        return "medium"
    if score >= thresholds.low:
        return "low"
    return "none"


def _parse_content_score(
    parsed: dict,
    prefix: str,
    thresholds: ScoreThresholds,
    reason: str,
) -> ContentScore:
    """解析内容丰富程度/内容质量；模型自报的 level 不采信，按 score 重算。"""
    score = _to_score(parsed.get(f"{prefix}_score"))
    if score is None:
        return ContentScore(reason="内容维度未返回")
    if score >= thresholds.high:
        level = "high"
    elif score >= thresholds.medium:
        level = "medium"
    else:
        level = "low"
    return ContentScore(score=score, level=level, reason=reason)
