from __future__ import annotations

import json
import logging

from scopecrawl.history import HISTORY_FILENAME, UrlHistory
from scopecrawl.models import ExtractedContent
from scopecrawl.pipeline import Pipeline
from scopecrawl.search import SearchError
from scopecrawl.utils import normalize_url
from tests.conftest import (
    MARKDOWN_NAV,
    MARKDOWN_OK,
    FakeExtractor,
    FakeLLM,
    FakeProvider,
    RouterLLM,
    make_search_response,
    make_settings,
)


def _provider():
    return FakeProvider(
        {
            "关键词一": make_search_response(
                "关键词一", ["https://example.com/a", "https://example.com/b"]
            ),
            "关键词二": make_search_response(
                "关键词二", ["https://example.com/a", "https://example.com/c"]
            ),
        }
    )


def _extractor():
    return FakeExtractor(
        {
            "https://example.com/a": MARKDOWN_OK,
            "https://example.com/b": MARKDOWN_OK,
            "https://example.com/c": None,
        }
    )


def _run(tmp_path, settings=None, **kwargs):
    settings = settings or make_settings()
    pipeline = Pipeline(
        settings,
        output_root=tmp_path,
        history_path=tmp_path / HISTORY_FILENAME,
        llm_client=kwargs.pop("llm_client", RouterLLM()),
        provider=kwargs.pop("provider", _provider()),
        extractor=kwargs.pop("extractor", _extractor()),
        **kwargs,
    )
    return pipeline.run()


def _seed_history(tmp_path, urls):
    history = UrlHistory(tmp_path / HISTORY_FILENAME)
    history.record(normalize_url(url) for url in urls)
    history.save()


def test_pipeline_end_to_end_mock(tmp_path, capsys):
    summary, code = _run(tmp_path)

    assert code == 0
    assert summary.keywords == ["关键词一", "关键词二"]
    assert summary.processed_urls == 3  # a 在两个关键词中只保留一次
    assert summary.documents == 2
    assert len(summary.failures) == 1
    assert summary.failures[0].stage == "extract"
    assert summary.failures[0].url == "https://example.com/c"

    run_dir = tmp_path / summary.run_id
    files = sorted(path.name for path in run_dir.iterdir())
    assert files == ["metadata.json", "title 0.md", "title 1.md"]

    content = (run_dir / "title 0.md").read_text(encoding="utf-8")
    assert not content.startswith("---\n")
    assert "relevance_score" not in content

    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    assert metadata["s3"] == "天气查询"
    assert metadata["s4"] == "气象灾害查询"
    assert len(metadata["documents"]) == 2
    record = metadata["documents"][0]
    assert record["file"] == "title 0.md"
    assert record["relevance_score"] == 0.9
    assert record["content_richness_score"] == 0.8
    assert record["content_quality_score"] == 0.75
    assert record["evaluation_decision"] == "keep"
    assert record["authority_score"] is not None
    assert record["domain_decision"] == "allow"
    assert "关键词一" in record["matched_queries"]

    output = capsys.readouterr().out
    assert "运行完成：处理 3 个 URL，生成 2 篇文档" in output
    assert "失败" not in output


def test_pipeline_location_sets_country_hint(tmp_path):
    provider = _provider()
    _run(tmp_path, provider=provider)
    assert provider.requests
    assert provider.requests[0].country == "EG"


def test_pipeline_logs_stage_progress(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="scopecrawl.pipeline"):
        summary, code = _run(tmp_path)
    assert code == 0
    messages = [record.getMessage() for record in caplog.records]
    for stage in (
        "[关键词]",
        "[搜索]",
        "[去重]",
        "[粗筛]",
        "[域名预筛]",
        "[提取]",
        "[评估]",
        "[多样性]",
        "[写入]",
    ):
        assert any(stage in message for message in messages), stage
    assert any("[提取] 完成 1/3" in message for message in messages)
    assert any("[评估] 完成 2/2" in message for message in messages)


def test_pipeline_keyword_failure_returns_1(tmp_path):
    llm = FakeLLM([RuntimeError("down"), RuntimeError("down")])
    summary, code = _run(tmp_path, llm_client=llm)
    assert code == 1
    assert summary.documents == 0
    assert summary.failures[0].stage == "keyword"


def test_pipeline_search_400_aborts(tmp_path):
    error = SearchError("tavily", 400, False, "bad language")
    provider = FakeProvider({"关键词一": error, "关键词二": error})
    summary, code = _run(tmp_path, provider=provider)
    assert code == 1
    assert all(failure.stage == "search" for failure in summary.failures)
    assert summary.documents == 0


def test_pipeline_all_search_failures_aborts(tmp_path):
    error = SearchError("tavily", 500, True, "server error")
    provider = FakeProvider({"关键词一": error, "关键词二": error})
    summary, code = _run(tmp_path, provider=provider)
    assert code == 1


def test_pipeline_partial_search_failure_continues(tmp_path):
    provider = FakeProvider(
        {
            "关键词一": make_search_response("关键词一", ["https://example.com/a"]),
            "关键词二": SearchError("tavily", 500, True, "server error"),
        }
    )
    summary, code = _run(tmp_path, provider=provider)
    assert code == 0
    assert summary.documents == 1
    assert any(failure.stage == "search" for failure in summary.failures)


def test_pipeline_relevance_none_can_be_dropped(tmp_path):
    llm = RouterLLM()
    llm.complete = lambda messages, json_mode=False: (
        '["关键词一", "关键词二"]'
        if "search query generator" in messages[0]["content"]
        else '{"relevance_score": 0.1, "matched_scope": "none"}'
    )
    settings = make_settings(review={"drop_none": True})
    summary, code = _run(tmp_path, settings=settings, llm_client=llm)
    assert code == 0
    assert summary.documents == 0
    assert summary.dropped_documents == 2


def test_pipeline_authority_disabled_writes_null_fields(tmp_path):
    settings = make_settings(authority={"enabled": False})
    summary, code = _run(tmp_path, settings=settings)
    assert code == 0
    assert summary.documents == 2
    run_dir = tmp_path / summary.run_id
    metadata = json.loads((run_dir / "metadata.json").read_text(encoding="utf-8"))
    record = metadata["documents"][0]
    assert record["domain_decision"] is None
    assert record["page_score"] is None
    assert record["authority_score"] is None


def test_pipeline_dedupe_disabled_extracts_duplicates(tmp_path):
    settings = make_settings()
    settings.search.dedupe.enabled = False
    summary, code = _run(tmp_path, settings=settings)
    assert code == 0
    assert summary.processed_urls == 4
    # a 被两条关键词各提取一次，但 c 提取失败；同一 a 覆盖同一文件，最终 2 个文件
    assert summary.documents == 2
    assert len(list((tmp_path / summary.run_id).glob("*.md"))) == 2


def test_pipeline_skips_urls_from_scenario_history(tmp_path):
    settings = make_settings()
    _seed_history(tmp_path, ["https://example.com/a"])

    summary, code = _run(tmp_path, settings=settings)

    assert code == 0
    assert summary.history_skipped == 1
    assert summary.processed_urls == 2
    assert summary.documents == 1
    history = UrlHistory(tmp_path / HISTORY_FILENAME)
    assert history.contains("example.com/a")
    assert history.contains("example.com/b")
    # 提取失败也已在入队时记录，下次不再重复召回。
    assert history.contains("example.com/c")


def test_pipeline_backfills_after_history_skip(tmp_path):
    urls = [f"https://example.com/{name}" for name in "abcdef"]
    provider = FakeProvider(
        {
            "关键词一": make_search_response("关键词一", urls[:3]),
            "关键词二": make_search_response("关键词二", urls[3:]),
        }
    )
    extractor = FakeExtractor({url: MARKDOWN_OK for url in urls})
    settings = make_settings(
        search={"dedupe": {"max_urls": 2}},
        diversity={"max_per_query": None, "max_per_domain": None},
    )
    _seed_history(tmp_path, urls[:2])

    summary, code = _run(
        tmp_path, settings=settings, provider=provider, extractor=extractor
    )

    assert code == 0
    assert summary.history_skipped == 2
    assert summary.processed_urls == 2
    assert extractor.calls == [urls[2:4]]
    assert summary.documents == 2


def test_pipeline_records_screened_urls_but_not_truncated(tmp_path):
    urls = [
        "https://m.cn.nytimes.com",
        "https://example.com/short",
        "https://example.com/a",
        "https://example.com/cut1",
        "https://example.com/cut2",
    ]
    provider = FakeProvider(
        {
            "关键词一": make_search_response("关键词一", urls[:3]),
            "关键词二": make_search_response("关键词二", urls[3:]),
        }
    )
    extractor = FakeExtractor(
        {
            "https://example.com/short": MARKDOWN_NAV,
            "https://example.com/a": MARKDOWN_OK,
        }
    )
    settings = make_settings(search={"dedupe": {"max_urls": 3}})
    summary, code = _run(
        tmp_path, settings=settings, provider=provider, extractor=extractor
    )

    assert code == 0
    assert summary.prefiltered_urls == 1
    assert summary.quality_dropped == 1
    history = UrlHistory(tmp_path / HISTORY_FILENAME)
    # 被 URL 粗筛、质量闸门丢弃的 URL 仍记录；被 max_urls 截断的 URL 留给下次。
    assert history.contains("m.cn.nytimes.com")
    assert history.contains("example.com/short")
    assert history.contains("example.com/a")
    assert not history.contains("example.com/cut1")
    assert not history.contains("example.com/cut2")


def test_pipeline_prefilter_drops_portal_homepages(tmp_path):
    provider = FakeProvider(
        {
            "关键词一": make_search_response(
                "关键词一", ["https://m.cn.nytimes.com", "https://example.com/a"]
            ),
            "关键词二": make_search_response("关键词二", ["https://example.com/a"]),
        }
    )
    extractor = FakeExtractor({"https://example.com/a": MARKDOWN_OK})
    summary, code = _run(tmp_path, provider=provider, extractor=extractor)
    assert code == 0
    assert summary.prefiltered_urls == 1
    assert summary.prefilter_reasons["homepage"] == 1
    assert summary.processed_urls == 1
    assert summary.documents == 1


def test_pipeline_diversity_limits_domain(tmp_path):
    urls = [f"https://example.com/{name}" for name in "abcdef"]
    provider = FakeProvider(
        {
            "关键词一": make_search_response("关键词一", urls[:4]),
            "关键词二": make_search_response("关键词二", urls[4:]),
        }
    )
    extractor = FakeExtractor({url: MARKDOWN_OK for url in urls})
    settings = make_settings(diversity={"max_per_query": None, "max_per_domain": 2})
    summary, code = _run(
        tmp_path, settings=settings, provider=provider, extractor=extractor
    )
    assert code == 0
    assert summary.documents == 2
    assert summary.diversity_dropped == 4
    assert summary.diversity_reasons["domain_quota"] == 4


def test_pipeline_review_llm_failure_drops_when_unevaluated(tmp_path):
    class FailingRelevanceLLM(RouterLLM):
        def complete(self, messages, *, json_mode=False):
            if "search query generator" in messages[0]["content"]:
                return '["关键词一", "关键词二"]'
            raise RuntimeError("timeout")

    summary, code = _run(tmp_path, llm_client=FailingRelevanceLLM())
    assert code == 0
    assert summary.documents == 0
    assert summary.dropped_documents == 2
    assert summary.drop_reasons["relevance_unevaluated"] == 2


def test_pipeline_summary_reports_drop_reasons(tmp_path, capsys):
    llm = RouterLLM()
    llm.complete = lambda messages, json_mode=False: (
        '["关键词一", "关键词二"]'
        if "search query generator" in messages[0]["content"]
        else '{"relevance_score": 0.1, "matched_scope": "none"}'
    )
    _run(tmp_path, llm_client=llm)
    output = capsys.readouterr().out
    assert "评估丢弃明细：相关性不足 2" in output


def test_pipeline_quality_drop_is_not_a_failure(tmp_path, capsys):
    extractor = FakeExtractor(
        {
            "https://example.com/a": MARKDOWN_NAV,
            "https://example.com/b": MARKDOWN_NAV,
            "https://example.com/c": None,
        }
    )
    summary, code = _run(tmp_path, extractor=extractor)
    assert code == 0
    assert summary.documents == 0
    assert summary.quality_dropped == 2
    assert summary.quality_reasons["short_content"] == 2
    assert all(failure.stage == "extract" for failure in summary.failures)
    output = capsys.readouterr().out
    assert "质量丢弃 2 篇" in output
    assert "质量丢弃明细：短正文 2" in output


def test_pipeline_all_extract_http_failures_returns_1(tmp_path):
    failed = {
        url: ExtractedContent(url=url, error_type="service_error", error="HTTP 503")
        for url in (
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
        )
    }
    summary, code = _run(tmp_path, extractor=FakeExtractor(failed))
    assert code == 1
    assert summary.documents == 0
    assert all(failure.stage == "extract" for failure in summary.failures)
    # 入队即写入历史表，即使本次 run 因提取全部失败而终止。
    history = UrlHistory(tmp_path / HISTORY_FILENAME)
    for name in "abc":
        assert history.contains(f"example.com/{name}")
