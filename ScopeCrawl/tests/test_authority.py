from __future__ import annotations

import pytest

from scopecrawl.authority import DomainFilter, assess_page, combine_authority
from scopecrawl.models import AuthorityLists, DomainAssessment, PageAssessment
from tests.conftest import make_doc, make_settings


def make_filter(whitelist=None, blacklist=None) -> DomainFilter:
    lists = AuthorityLists.model_validate(
        {"whitelist": whitelist or [], "blacklist": blacklist or []}
    )
    return DomainFilter(lists, make_settings().authority)


def test_domain_filter_blacklist_rejects():
    assessment = make_filter(blacklist=[{"domain": "spam.example"}]).assess("spam.example")
    assert assessment.decision == "reject"
    assert assessment.domain_score == 0.0


def test_domain_filter_whitelist_exact_and_subdomain():
    domain_filter = make_filter(whitelist=[{"domain": "ema.gov.eg"}])
    assert domain_filter.assess("ema.gov.eg").domain_score == 1.0
    assert domain_filter.assess("weather.ema.gov.eg").domain_score == 1.0


def test_domain_filter_wildcard():
    domain_filter = make_filter(whitelist=[{"domain": "*.gov.eg"}])
    assert domain_filter.assess("capmas.gov.eg").domain_score == 1.0
    # 通配符不匹配裸域名，按未知域名保守放行
    bare = domain_filter.assess("gov.eg")
    assert bare.decision == "allow"
    assert bare.domain_score == 0.4


def test_domain_filter_tld_scores():
    assessment = make_filter().assess("nasa.gov")
    assert assessment.decision == "allow"
    assert assessment.domain_score == 0.9
    assert "tld:gov" in assessment.signals


def test_domain_filter_unknown_allows_with_flag():
    assessment = make_filter().assess("unknown-site.example")
    assert assessment.decision == "allow"
    assert assessment.domain_score == 0.4
    assert "unknown_domain" in assessment.signals


def test_domain_filter_shortener_is_review():
    assessment = make_filter().assess("bit.ly")
    assert assessment.decision == "review"
    assert assessment.domain_score == 0.3


def test_assess_page_full_signals_scores_one():
    settings = make_settings()
    page = assess_page(make_doc(), settings.authority, settings.extract)
    assert page.page_score == 1.0
    assert page.quality_flags == ["metadata_limited"]


def test_assess_page_flags_missing_signals():
    settings = make_settings()
    page = assess_page(
        make_doc(
            published_at=None,
            outbound_links=0,
            markdown="short " * 60,
        ),
        settings.authority,
        settings.extract,
    )
    assert {"metadata_limited", "no_date", "short_content"} <= set(page.quality_flags)
    assert page.page_score < 0.5


def test_assess_page_ad_penalty():
    settings = make_settings()
    page = assess_page(make_doc(kept_link_text_ratio=0.6), settings.authority, settings.extract)
    assert page.page_score == pytest.approx(0.8)
    assert any("广告/导航比例过高" in reason for reason in page.reasons)


def test_combine_authority_levels():
    settings = make_settings()
    domain = DomainAssessment(domain="example.com", decision="allow", domain_score=1.0)
    page = PageAssessment(page_score=1.0)
    result = combine_authority(domain, page, settings.authority)
    assert result.authority_score == 1.0
    assert result.authority_level == "high"

    low_page = PageAssessment(page_score=0.0)
    low_domain = DomainAssessment(domain="d", decision="allow", domain_score=0.3)
    low = combine_authority(low_domain, low_page, settings.authority)
    assert low.authority_level == "low"
    assert "low_authority" in low.quality_flags


def test_combine_authority_attaches_domain_and_page():
    settings = make_settings()
    domain = DomainAssessment(domain="example.com", decision="allow", domain_score=1.0)
    page = PageAssessment(page_score=1.0)
    result = combine_authority(domain, page, settings.authority)
    assert result.domain is domain
    assert result.page is page


def test_combine_authority_propagates_unknown_domain():
    settings = make_settings()
    domain = DomainAssessment(
        domain="d",
        decision="allow",
        domain_score=0.4,
        signals=["unknown_domain"],
    )
    page = PageAssessment(page_score=0.5)
    result = combine_authority(domain, page, settings.authority)
    assert "unknown_domain" in result.quality_flags
