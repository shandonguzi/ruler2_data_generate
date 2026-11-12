"""域名预筛、页面规则评分、内容复核与权威度综合。"""

from __future__ import annotations

import logging
from collections import Counter
from pathlib import Path

from .config import PROJECT_ROOT
from .models import (
    LANGUAGE_NAMES,
    AuthorityLists,
    AuthorityResult,
    AuthoritySettings,
    ContentScore,
    DiversitySettings,
    Document,
    DocumentEvaluation,
    DomainAssessment,
    ExtractedDoc,
    ExtractSettings,
    PageAssessment,
    RelevanceResult,
    ReviewSettings,
    RunInput,
    ScoreThresholds,
)
from .utils import (
    domain_matches,
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

SHORTENER_DOMAINS = {
    "bit.ly",
    "t.co",
    "goo.gl",
    "tinyurl.com",
    "ow.ly",
    "is.gd",
    "buff.ly",
    "cutt.ly",
    "rebrand.ly",
    "shorturl.at",
}


class DomainFilter:
    def __init__(self, lists: AuthorityLists, settings: AuthoritySettings) -> None:
        self.lists = lists
        self.settings = settings

    def assess(self, domain: str) -> DomainAssessment:
        host = (domain or "").lower().strip().rstrip(".")
        assessed_at = to_iso(utc_now()) or ""

        for entry in self.lists.blacklist:
            if domain_matches(host, entry.domain):
                reason = f"命中黑名单：{entry.domain}"
                if entry.reason:
                    reason += f"（{entry.reason}）"
                return DomainAssessment(
                    domain=host,
                    decision="reject",
                    domain_score=0.0,
                    signals=["blacklist"],
                    reasons=[reason],
                    assessed_at=assessed_at,
                )

        for entry in self.lists.whitelist:
            if domain_matches(host, entry.domain):
                reason = f"命中白名单：{entry.domain}"
                if entry.reason:
                    reason += f"（{entry.reason}）"
                return DomainAssessment(
                    domain=host,
                    decision="allow",
                    domain_score=1.0,
                    signals=["whitelist"],
                    reasons=[reason],
                    assessed_at=assessed_at,
                )

        tld = host.rsplit(".", 1)[-1] if host else ""
        if tld in self.settings.domain.tld_scores:
            score = self.settings.domain.tld_scores[tld]
            return DomainAssessment(
                domain=host,
                decision="allow",
                domain_score=score,
                signals=[f"tld:{tld}"],
                reasons=[f"政府/学术/国际组织 TLD：.{tld}"],
                assessed_at=assessed_at,
            )

        if _is_suspicious(host):
            return DomainAssessment(
                domain=host,
                decision="review",
                domain_score=0.3,
                signals=["suspicious_domain"],
                reasons=["域名命中可疑规则（短链或批量生成特征）"],
                assessed_at=assessed_at,
            )

        score = self.settings.domain.unknown_domain_score
        return DomainAssessment(
            domain=host,
            decision="allow",
            domain_score=score,
            signals=["unknown_domain"],
            reasons=["未知域名，保守放行"],
            assessed_at=assessed_at,
        )


def _is_suspicious(host: str) -> bool:
    if not host:
        return False
    if host in SHORTENER_DOMAINS:
        return True
    if "xn--" in host:
        return True
    labels = host.split(".")
    if len(host) > 60:
        return True
    for label in labels:
        if len(label) > 40:
            return True
        if len(label) >= 20:
            digits = sum(char.isdigit() for char in label)
            if digits >= len(label) * 0.4:
                return True
    return False


def assess_page(
    doc: ExtractedDoc,
    settings: AuthoritySettings,
    extract_settings: ExtractSettings,
) -> PageAssessment:
    weights = settings.page.weights
    content_length = doc.plain_chars
    min_chars = extract_settings.min_chars

    published = 1.0 if doc.published_at else 0.0

    if content_length >= 1000:
        length_score = 1.0
    elif content_length >= 500:
        length_score = 0.8
    elif content_length >= min_chars:
        length_score = 0.5
    else:
        length_score = 0.2

    if doc.outbound_links >= 2:
        citations = 1.0
    elif doc.outbound_links == 1:
        citations = 0.5
    else:
        citations = 0.0

    ratio = doc.outbound_text_ratio
    if ratio > 0.5:
        ad_penalty = settings.page.max_ad_penalty
    elif ratio > 0.3:
        ad_penalty = settings.page.max_ad_penalty / 2
    else:
        ad_penalty = 0.0

    # Extract 没有 author / JSON-LD，只用可用信号按固定分母重归一化。
    denominator = weights.published_at + weights.content_length + weights.citations
    if denominator > 0:
        score = (
            weights.published_at * published
            + weights.content_length * length_score
            + weights.citations * citations
        ) / denominator
    else:
        score = 0.0
    score = min(max(score - ad_penalty, 0.0), 1.0)

    flags: list[str] = ["metadata_limited"]
    reasons: list[str] = ["无 HTML 元数据（Tavily Extract）"]
    if not doc.published_at:
        flags.append("no_date")
        reasons.append("缺少发布时间")
    if content_length < 500:
        flags.append("short_content")
    reasons.append(f"正文长度：{content_length} 字符")
    reasons.append(f"外链引用：{doc.outbound_links}")
    if ad_penalty:
        reasons.append(f"广告/导航比例过高，扣分 {ad_penalty:.2f}")

    return PageAssessment(
        doc_id=doc.doc_id,
        url=doc.url,
        page_score=round(score, 4),
        signals={
            "published_at": published,
            "content_length": length_score,
            "citations": citations,
            "ad_penalty": ad_penalty,
        },
        quality_flags=flags,
        reasons=reasons,
        assessed_at=to_iso(utc_now()) or "",
    )


def load_review_prompt(prompt_path: str | Path | None = None) -> str:
    """读取复核 prompt；阶段可在并发前调用一次后复用。"""
    return read_prompt(
        Path(prompt_path) if prompt_path else DEFAULT_REVIEW_PROMPT,
        label="复核 prompt",
        error_cls=RuntimeError,
    )


def review_content(
    doc: ExtractedDoc,
    run: RunInput,
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


def combine_authority(
    domain: DomainAssessment | None,
    page: PageAssessment | None,
    settings: AuthoritySettings,
) -> AuthorityResult:
    if domain is None or page is None:
        return AuthorityResult(authority_reasons=["权威度评估已禁用"])

    weights = settings.scoring.weights
    score = (
        weights.get("domain", 0.6) * domain.domain_score
        + weights.get("page", 0.4) * page.page_score
    )
    score = min(max(score, 0.0), 1.0)

    if score >= settings.scoring.high_threshold:
        level = "high"
    elif score >= settings.scoring.low_threshold:
        level = "medium"
    else:
        level = "low"

    flags = list(page.quality_flags)
    if "unknown_domain" in domain.signals and "unknown_domain" not in flags:
        flags.append("unknown_domain")
    if level == "low" and "low_authority" not in flags:
        flags.append("low_authority")

    return AuthorityResult(
        authority_score=round(score, 4),
        authority_level=level,
        quality_flags=flags,
        authority_reasons=list(domain.reasons) + list(page.reasons),
        domain=domain,
        page=page,
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
