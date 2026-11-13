"""域名预筛、页面规则评分与权威度综合。"""

from __future__ import annotations

from .models import (
    AuthorityLists,
    AuthorityResult,
    AuthoritySettings,
    DomainAssessment,
    ExtractedDoc,
    ExtractSettings,
    PageAssessment,
)
from .utils import domain_matches

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
            )

        if _is_suspicious(host):
            return DomainAssessment(
                domain=host,
                decision="review",
                domain_score=0.3,
                signals=["suspicious_domain"],
                reasons=["域名命中可疑规则（短链或批量生成特征）"],
            )

        score = self.settings.domain.unknown_domain_score
        return DomainAssessment(
            domain=host,
            decision="allow",
            domain_score=score,
            signals=["unknown_domain"],
            reasons=["未知域名，保守放行"],
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

    ratio = doc.kept_link_text_ratio
    if ratio > 0.5:
        ad_penalty = settings.page.max_ad_penalty
    elif ratio > 0.3:
        ad_penalty = settings.page.max_ad_penalty / 2
    else:
        ad_penalty = 0.0

    # 搜索/提取接口只返回正文，没有 author / JSON-LD，按可用信号与固定分母重归一化。
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
    reasons: list[str] = ["无 HTML 元数据（搜索/提取接口仅返回正文）"]
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
        page_score=round(score, 4),
        quality_flags=flags,
        reasons=reasons,
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
