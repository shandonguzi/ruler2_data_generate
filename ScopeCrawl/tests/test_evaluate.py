from __future__ import annotations

import json

import pytest

from scopecrawl.evaluate import (
    DomainFilter,
    apply_diversity,
    assess_page,
    combine_authority,
    parse_json_object,
    review_content,
    should_drop,
)
from scopecrawl.models import (
    AuthorityLists,
    AuthorityResult,
    ContentScore,
    DiversitySettings,
    Document,
    DocumentEvaluation,
    DomainAssessment,
    ExtractedDoc,
    PageAssessment,
    RelevanceResult,
)
from tests.conftest import FakeLLM, make_settings

MARKDOWN = "word " * 300


def make_doc(**overrides) -> ExtractedDoc:
    data = {
        "doc_id": "0123456789abcdef",
        "url": "https://example.com/a",
        "final_url": "https://example.com/a",
        "title": "title",
        "site": "example.com",
        "author": "Author",
        "published_at": "2026-11-01T00:00:00+00:00",
        "fetched_at": "2026-11-10T00:00:00+00:00",
        "lang": "en",
        "markdown": MARKDOWN,
        "matched_queries": [],
        "has_jsonld": True,
        "outbound_links": 2,
        "outbound_text_ratio": 0.0,
        "extractor": "tavily-extract",
    }
    data.update(overrides)
    return ExtractedDoc(**data)


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
            author=None,
            published_at=None,
            has_jsonld=False,
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
    page = assess_page(make_doc(outbound_text_ratio=0.6), settings.authority, settings.extract)
    assert page.signals["ad_penalty"] == settings.authority.page.max_ad_penalty
    assert page.page_score == pytest.approx(0.8)


def test_combine_authority_levels():
    settings = make_settings()
    domain = DomainAssessment(domain="example.com", decision="allow", domain_score=1.0)
    page = PageAssessment(doc_id="x", url="https://example.com", page_score=1.0)
    result = combine_authority(domain, page, settings.authority)
    assert result.authority_score == 1.0
    assert result.authority_level == "high"

    low_page = PageAssessment(doc_id="x", url="u", page_score=0.0)
    low_domain = DomainAssessment(domain="d", decision="allow", domain_score=0.3)
    low = combine_authority(low_domain, low_page, settings.authority)
    assert low.authority_level == "low"
    assert "low_authority" in low.quality_flags


def test_combine_authority_attaches_domain_and_page():
    settings = make_settings()
    domain = DomainAssessment(domain="example.com", decision="allow", domain_score=1.0)
    page = PageAssessment(doc_id="x", url="u", page_score=1.0)
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
    page = PageAssessment(doc_id="x", url="u", page_score=0.5)
    result = combine_authority(domain, page, settings.authority)
    assert "unknown_domain" in result.quality_flags


def _review(settings, client):
    return review_content(make_doc(), settings.run, client, settings.review)


def test_review_content_success_recomputes_level():
    settings = make_settings()
    payload = json.dumps(
        {
            "relevance_score": 0.9,
            "relevance_level": "low",
            "content_richness_score": 0.8,
            "content_richness_level": "low",
            "content_quality_score": 0.75,
            "content_quality_level": "low",
            "content_reason": "信息密集且结构清晰",
            "matched_scope": "s4",
            "location_match": True,
            "language_match": True,
            "temporal_match": True,
            "flags": ["off_topic", "unknown_flag"],
            "reason": "直接相关",
        }
    )
    client = FakeLLM([payload])
    result = _review(settings, client)
    assert result.relevance.relevance_score == 0.9
    assert result.relevance.relevance_level == "high"
    assert result.relevance.matched_scope == "s4"
    assert result.relevance.flags == ["off_topic"]
    assert result.relevance.model == "fake-model"
    assert result.content_richness.score == 0.8
    assert result.content_richness.level == "high"
    assert result.content_quality.score == 0.75
    assert result.content_quality.level == "high"
    assert result.content_richness.reason == "信息密集且结构清晰"
    assert client.calls[0][1] is True


def test_review_content_clamps_and_defaults_missing_fields():
    settings = make_settings()
    client = FakeLLM(['{"relevance_score": 1.7, "matched_scope": "bogus"}'])
    result = _review(settings, client)
    assert result.relevance.relevance_score == 1.0
    assert result.relevance.relevance_level == "high"
    assert result.relevance.matched_scope == "none"
    assert result.relevance.location_match is None
    assert result.content_richness.score is None
    assert result.content_richness.level is None
    assert result.content_quality.score is None


def test_review_content_failure_marks_unevaluated():
    settings = make_settings()
    client = FakeLLM([RuntimeError("timeout"), RuntimeError("timeout")])
    result = _review(settings, client)
    assert result.relevance.relevance_score is None
    assert "relevance_unevaluated" in result.relevance.flags
    assert len(client.calls) == 2


def test_review_content_invalid_json_retries():
    settings = make_settings()
    client = FakeLLM(["not json", '{"relevance_score": 0.5}'])
    result = _review(settings, client)
    assert result.relevance.relevance_score == 0.5
    assert len(client.calls) == 2


def test_review_content_disabled_and_empty_content():
    settings = make_settings(review={"enabled": False})
    disabled = review_content(make_doc(), settings.run, FakeLLM([]), settings.review)
    assert disabled.relevance.relevance_score is None
    assert disabled.relevance.reason

    enabled = make_settings().review
    empty = review_content(make_doc(markdown=""), settings.run, FakeLLM([]), enabled)
    assert empty.relevance.flags == ["no_content"]


def test_parse_json_object_with_fence():
    assert parse_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert parse_json_object('noise {"a": 2} tail') == {"a": 2}
    assert parse_json_object("nope") is None


def test_should_drop_matrix():
    review_settings = make_settings().review
    authority_settings = make_settings().authority

    none_result = RelevanceResult(relevance_level="none")
    low_result = RelevanceResult(relevance_level="low")
    high_result = RelevanceResult(relevance_level="high")
    listing_result = RelevanceResult(relevance_level="low", content_type="listing")
    low_authority = AuthorityResult(authority_score=0.1)

    dropped, reasons = should_drop(
        DocumentEvaluation(relevance=none_result, authority=low_authority),
        review_settings,
        authority_settings,
    )
    assert dropped and reasons == ["relevance_none"]

    dropped, reasons = should_drop(
        DocumentEvaluation(relevance=high_result, authority=low_authority),
        review_settings,
        authority_settings,
    )
    assert not dropped and reasons == []

    dropped, reasons = should_drop(
        DocumentEvaluation(relevance=listing_result, authority=low_authority),
        review_settings,
        authority_settings,
    )
    assert dropped and "structural" in reasons

    lenient = review_settings.model_copy(update={"drop_low": False})
    dropped, reasons = should_drop(
        DocumentEvaluation(relevance=low_result, authority=low_authority),
        lenient,
        authority_settings,
    )
    assert not dropped and reasons == []

    dropped, reasons = should_drop(
        DocumentEvaluation(relevance=low_result, authority=low_authority),
        review_settings,
        authority_settings,
    )
    assert dropped and reasons == ["relevance_low"]


def test_should_drop_content_both_low():
    settings = make_settings(review={"content": {"drop_when_both_low": True}})
    evaluation = DocumentEvaluation(
        relevance=RelevanceResult(relevance_level="high"),
        content_richness=ContentScore(score=0.2, level="low"),
        content_quality=ContentScore(score=0.3, level="low"),
        authority=AuthorityResult(authority_score=0.8),
    )
    dropped, reasons = should_drop(evaluation, settings.review, settings.authority)
    assert dropped and reasons == ["content_both_low"]

    evaluation.content_quality = ContentScore(score=0.8, level="high")
    dropped, reasons = should_drop(evaluation, settings.review, settings.authority)
    assert not dropped and reasons == []


def test_should_drop_unevaluated_when_configured():
    settings = make_settings(review={"drop_unevaluated": True})
    evaluation = DocumentEvaluation(
        relevance=RelevanceResult(flags=["relevance_unevaluated"]),
        authority=AuthorityResult(authority_score=0.8),
    )
    dropped, reasons = should_drop(evaluation, settings.review, settings.authority)
    assert dropped and reasons == ["relevance_unevaluated"]


def test_should_drop_keeps_unevaluated_when_disabled():
    settings = make_settings(review={"drop_unevaluated": False})
    evaluation = DocumentEvaluation(
        relevance=RelevanceResult(flags=["relevance_unevaluated"]),
        authority=AuthorityResult(authority_score=0.8),
    )
    dropped, reasons = should_drop(evaluation, settings.review, settings.authority)
    assert not dropped and reasons == []


def test_should_drop_no_content():
    settings = make_settings()
    evaluation = DocumentEvaluation(
        relevance=RelevanceResult(flags=["no_content"]),
        authority=AuthorityResult(authority_score=0.8),
    )
    dropped, reasons = should_drop(evaluation, settings.review, settings.authority)
    assert dropped and reasons == ["no_content"]


def test_review_content_caps_listing_and_adds_flag():
    settings = make_settings()
    payload = json.dumps(
        {"relevance_score": 0.9, "content_type": "listing", "matched_scope": "s4"}
    )
    result = _review(settings, FakeLLM([payload]))
    assert result.relevance.relevance_score == 0.3
    assert result.relevance.relevance_level == "low"
    assert result.relevance.content_type == "listing"
    assert "aggregator_page" in result.relevance.flags


def test_review_content_invalid_content_type_falls_back():
    settings = make_settings()
    payload = json.dumps({"relevance_score": 0.8, "content_type": "bogus"})
    result = _review(settings, FakeLLM([payload]))
    assert result.relevance.content_type == "other"
    assert result.relevance.relevance_score == 0.8


def test_review_content_partial_content_dimensions():
    settings = make_settings()
    payload = json.dumps(
        {
            "relevance_score": 0.8,
            "content_richness_score": 0.5,
            "content_reason": "信息量一般",
        }
    )
    result = _review(settings, FakeLLM([payload]))
    assert result.content_richness.score == 0.5
    assert result.content_richness.level == "medium"
    assert result.content_richness.reason == "信息量一般"
    assert result.content_quality.score is None
    assert result.content_quality.level is None


def test_review_content_prompt_contains_matched_queries():
    settings = make_settings()
    client = FakeLLM([json.dumps({"relevance_score": 0.8})])
    review_content(
        make_doc(matched_queries=["中国 美食", "中国 菜系"]),
        settings.run,
        client,
        settings.review,
    )
    prompt = client.calls[0][0][0]["content"]
    assert "中国 美食 | 中国 菜系" in prompt


def _document(
    url,
    *,
    site="example.com",
    query="q",
    relevance=0.9,
    richness=0.8,
    quality=0.8,
    authority=0.8,
) -> Document:
    return Document(
        extracted=ExtractedDoc(
            doc_id=url,
            url=url,
            final_url=url,
            title=url,
            site=site,
            fetched_at="2026-11-10T00:00:00+00:00",
            markdown="x",
            matched_queries=[query],
        ),
        evaluation=DocumentEvaluation(
            relevance=RelevanceResult(relevance_score=relevance, relevance_level="high"),
            content_richness=ContentScore(score=richness, level="high"),
            content_quality=ContentScore(score=quality, level="high"),
            authority=AuthorityResult(authority_score=authority),
        ),
    )


def test_diversity_domain_quota():
    docs = [
        _document(f"https://example.com/{index}", relevance=0.9 - index * 0.1)
        for index in range(5)
    ]
    kept, reasons = apply_diversity(
        docs, DiversitySettings(max_per_query=None, max_per_domain=2)
    )
    assert [document.extracted.url for document in kept] == [
        "https://example.com/0",
        "https://example.com/1",
    ]
    assert reasons["domain_quota"] == 3


def test_diversity_query_quota():
    docs = [
        _document(
            f"https://site{index}.com/a",
            site=f"site{index}.com",
            query="q",
            relevance=0.9 - index * 0.1,
        )
        for index in range(4)
    ]
    kept, reasons = apply_diversity(
        docs, DiversitySettings(max_per_query=2, max_per_domain=None)
    )
    assert [document.extracted.url for document in kept] == [
        "https://site0.com/a",
        "https://site1.com/a",
    ]
    assert reasons["query_quota"] == 2


def test_diversity_orders_by_scores_then_restores_queue_order():
    docs = [
        _document("https://a.com/1", site="a.com", query="q", relevance=0.5),
        _document("https://a.com/2", site="a.com", query="q2", relevance=0.9),
        _document("https://a.com/3", site="a.com", query="q3", relevance=0.7),
    ]
    kept, _ = apply_diversity(
        docs, DiversitySettings(max_per_query=None, max_per_domain=2)
    )
    assert [document.extracted.url for document in kept] == [
        "https://a.com/2",
        "https://a.com/3",
    ]


def test_diversity_disabled_with_none():
    docs = [_document(f"https://a.com/{index}") for index in range(3)]
    kept, reasons = apply_diversity(
        docs, DiversitySettings(max_per_query=None, max_per_domain=None)
    )
    assert kept == docs
    assert not reasons


def test_diversity_treats_none_scores_as_lowest():
    docs = [
        _document("https://a.com/1", relevance=None),
        _document("https://a.com/2", relevance=0.1),
    ]
    kept, _ = apply_diversity(
        docs, DiversitySettings(max_per_query=None, max_per_domain=1)
    )
    assert [document.extracted.url for document in kept] == ["https://a.com/2"]
