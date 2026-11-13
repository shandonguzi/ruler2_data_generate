from __future__ import annotations

import json
import logging

import pytest

from scopecrawl.evaluate import DomainFilter
from scopecrawl.history import HISTORY_FILENAME, UrlHistory
from scopecrawl.models import ExtractedContent
from scopecrawl.pipeline import Pipeline
from scopecrawl.search import SearchError
from scopecrawl.storage import METADATA_FILENAME
from scopecrawl.utils import normalize_url
from tests.conftest import (
    MARKDOWN_NAV,
    MARKDOWN_OK,
    CombinedProvider,
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
            "关键词一": make_search_response(["https://example.com/a", "https://example.com/b"]),
            "关键词二": make_search_response(["https://example.com/a", "https://example.com/c"]),
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
    provider = kwargs.pop("provider", _provider())
    extractor = kwargs.pop("extractor", None)
    if not isinstance(provider, CombinedProvider):
        provider = CombinedProvider(provider, extractor or _extractor())
        provider.extract_batch_size = getattr(
            settings.providers, settings.provider
        ).extract.batch_size
    pipeline = Pipeline(
        settings,
        output_root=tmp_path,
        history_path=tmp_path / HISTORY_FILENAME,
        llm_client=kwargs.pop("llm_client", RouterLLM()),
        provider=provider,
        **kwargs,
    )
    return pipeline.run()


def test_pipeline_resolves_relative_paths_against_project_root(tmp_path):
    pipeline = Pipeline(
        make_settings(),
        project_root=tmp_path,
        output_root="out",
        history_path="data/history.json",
        scenarios_path="config/scenarios.json",
        prompt_paths={"review": "prompts/review.md"},
        llm_client=FakeLLM([]),
        provider=CombinedProvider(_provider(), _extractor()),
    )
    assert pipeline.output_root == tmp_path / "out"
    assert pipeline.history_path == tmp_path / "data/history.json"
    assert pipeline.prompt_paths["review"] == tmp_path / "prompts/review.md"


def _seed_history(tmp_path, urls):
    history = UrlHistory(tmp_path / HISTORY_FILENAME)
    history.record(normalize_url(url) for url in urls)
    history.save()


def _read_metadata(run_dir):
    lines = (run_dir / METADATA_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def test_pipeline_domain_prefilter_assesses_each_host_once(tmp_path, monkeypatch):
    calls: list[str] = []
    original = DomainFilter.assess

    def counting(self, domain):
        calls.append(domain)
        return original(self, domain)

    monkeypatch.setattr(DomainFilter, "assess", counting)
    summary, code = _run(tmp_path)
    assert code == 0
    # 三个 URL 都属于 example.com，只评估一次。
    assert calls == ["example.com"]


def test_pipeline_extract_preserves_order_across_batches(tmp_path):
    urls = [f"https://example.com/{name}" for name in "abcd"]
    provider = FakeProvider(
        {
            "关键词一": make_search_response(urls),
            "关键词二": make_search_response([]),
        }
    )
    extractor = FakeExtractor({url: MARKDOWN_OK for url in urls})
    settings = make_settings(
        extract={"concurrency": 4},
        providers={"tavily": {"extract": {"batch_size": 1}}},
        diversity={"max_per_query": None, "max_per_domain": None},
    )

    summary, code = _run(
        tmp_path, settings=settings, provider=provider, extractor=extractor
    )

    assert code == 0
    assert summary.documents == 4
    records = _read_metadata(tmp_path / summary.run_id)
    documents = [record for record in records if record["type"] == "document"]
    # 批次按完成顺序返回，但文档仍按 URL 入队顺序写盘。
    assert [record["url"] for record in documents] == urls
    assert [record["file"] for record in documents] == [
        "title 0.md",
        "title 1.md",
        "title 2.md",
        "title 3.md",
    ]


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
    assert files == [METADATA_FILENAME, "title 0.md", "title 1.md"]

    content = (run_dir / "title 0.md").read_text(encoding="utf-8")
    assert not content.startswith("---\n")
    assert "relevance_score" not in content

    metadata = _read_metadata(run_dir)
    run = metadata[0]
    assert run["type"] == "run"
    assert run["s3"] == "天气查询"
    assert run["s4"] == "气象灾害查询"
    documents = [record for record in metadata if record["type"] == "document"]
    assert len(documents) == 2
    record = documents[0]
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
    with caplog.at_level(logging.INFO, logger="scopecrawl"):
        summary, code = _run(tmp_path)
    assert code == 0
    messages = [record.getMessage() for record in caplog.records]
    for stage in (
        "[关键词生成]",
        "[检索]",
        "[URL 筛选]",
        "[URL 筛选] 粗筛",
        "[URL 筛选] 域名预筛",
        "[提取清洗]",
        "[评估筛选]",
        "[评估筛选] 多样性",
        "[生成文档]",
    ):
        assert any(stage in message for message in messages), stage
    assert any("[提取清洗] 完成 1/3" in message for message in messages)
    assert any("[评估筛选] 完成 2/2" in message for message in messages)
    for stage in ("关键词生成", "检索", "URL 筛选", "提取清洗", "评估筛选", "生成文档"):
        assert any(
            f"================ {stage} ================" in message for message in messages
        ), stage


def test_pipeline_keyword_failure_returns_1(tmp_path):
    llm = FakeLLM([RuntimeError("down"), RuntimeError("down")])
    summary, code = _run(tmp_path, llm_client=llm)
    assert code == 1
    assert summary.documents == 0
    assert len(summary.failures) == 1
    assert summary.failures[0].stage == "keyword"


def test_pipeline_search_400_aborts(tmp_path):
    error = SearchError("tavily", 400, "bad language")
    provider = FakeProvider({"关键词一": error, "关键词二": error})
    summary, code = _run(tmp_path, provider=provider)
    assert code == 1
    assert len(summary.failures) == 2
    assert all(failure.stage == "search" for failure in summary.failures)
    assert summary.documents == 0


def test_pipeline_all_search_failures_aborts(tmp_path):
    error = SearchError("tavily", 500, "server error")
    provider = FakeProvider({"关键词一": error, "关键词二": error})
    summary, code = _run(tmp_path, provider=provider)
    assert code == 1


def test_pipeline_partial_search_failure_continues(tmp_path):
    provider = FakeProvider(
        {
            "关键词一": make_search_response(["https://example.com/a"]),
            "关键词二": SearchError("tavily", 500, "server error"),
        }
    )
    summary, code = _run(tmp_path, provider=provider)
    assert code == 0
    assert summary.documents == 1
    assert any(failure.stage == "search" for failure in summary.failures)


def test_pipeline_relevance_none_can_be_dropped(tmp_path, capsys):
    llm = RouterLLM()
    llm.complete = lambda messages, json_mode=False: (
        '["关键词一", "关键词二"]'
        if "search query generator" in messages[0]["content"]
        else '{"relevance_score": 0.1, "matched_scope": "none"}'
    )
    summary, code = _run(tmp_path, llm_client=llm)
    assert code == 0
    assert summary.documents == 0
    assert summary.dropped_documents == 2
    assert "评估丢弃明细：相关性不足 2" in capsys.readouterr().out


def test_pipeline_authority_disabled_writes_null_fields(tmp_path):
    settings = make_settings(authority={"enabled": False})
    summary, code = _run(tmp_path, settings=settings)
    assert code == 0
    assert summary.documents == 2
    run_dir = tmp_path / summary.run_id
    records = _read_metadata(run_dir)
    record = next(item for item in records if item["type"] == "document")
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
            "关键词一": make_search_response(urls[:3]),
            "关键词二": make_search_response(urls[3:]),
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
            "关键词一": make_search_response(urls[:3]),
            "关键词二": make_search_response(urls[3:]),
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
    assert history.contains("example.com/cut1")
    assert not history.contains("example.com/cut2")


def test_pipeline_domain_prefilter_runs_before_url_prefilter(tmp_path):
    authority = tmp_path / "authority.yaml"
    authority.write_text(
        "blacklist:\n  - domain: spam.example\n    reason: 测试\n", encoding="utf-8"
    )
    provider = FakeProvider(
        {
            "关键词一": make_search_response(["https://spam.example", "https://example.com/a"]),
            "关键词二": make_search_response([]),
        }
    )
    extractor = FakeExtractor({"https://example.com/a": MARKDOWN_OK})
    settings = make_settings(
        authority={"enabled": True, "config_path": str(authority)}
    )

    summary, code = _run(
        tmp_path, settings=settings, provider=provider, extractor=extractor
    )

    assert code == 0
    # 黑名单域名整体拒绝，不再计入 URL 粗筛的 homepage 原因。
    assert summary.rejected_domains == 1
    assert summary.prefiltered_urls == 0
    assert summary.processed_urls == 1
    history = UrlHistory(tmp_path / HISTORY_FILENAME)
    assert history.contains("spam.example")


@pytest.mark.parametrize("authority_enabled", [True, False])
def test_pipeline_skips_unfetchable_domains_before_truncation(
    tmp_path, authority_enabled
):
    authority = tmp_path / "authority.yaml"
    authority.write_text(
        "unfetchable:\n  - domain: youtube.com\n    reason: 视频页\n", encoding="utf-8"
    )
    provider = FakeProvider(
        {
            "关键词一": make_search_response(
                ["https://www.youtube.com/watch?v=1", "https://example.com/a"]
            ),
            "关键词二": make_search_response([]),
        }
    )
    extractor = FakeExtractor({"https://example.com/a": MARKDOWN_OK})
    settings = make_settings(
        authority={"enabled": authority_enabled, "config_path": str(authority)},
        search={"dedupe": {"max_urls": 1}},
        diversity={"max_per_query": None, "max_per_domain": None},
    )

    summary, code = _run(
        tmp_path, settings=settings, provider=provider, extractor=extractor
    )

    assert code == 0
    # 视频站先被跳过，配额留给可提取的 URL；该过滤不受 authority.enabled 影响。
    assert summary.unfetchable_skipped == 1
    assert summary.processed_urls == 1
    assert summary.documents == 1
    assert extractor.calls == [["https://example.com/a"]]
    history = UrlHistory(tmp_path / HISTORY_FILENAME)
    assert history.contains("www.youtube.com/watch?v=1")


def test_pipeline_interleaves_queries_under_max_urls(tmp_path):
    groups = {
        "关键词一": [f"https://example.com/a{i}" for i in range(3)],
        "关键词二": [f"https://example.com/b{i}" for i in range(3)],
        "关键词三": [f"https://example.com/c{i}" for i in range(3)],
    }
    provider = FakeProvider(
        {query: make_search_response(urls) for query, urls in groups.items()}
    )
    extractor = FakeExtractor(
        {url: MARKDOWN_OK for urls in groups.values() for url in urls}
    )
    settings = make_settings(
        search={"dedupe": {"max_urls": 3}},
        diversity={"max_per_query": None, "max_per_domain": None},
    )

    summary, code = _run(
        tmp_path,
        settings=settings,
        llm_client=RouterLLM(keywords=list(groups)),
        provider=provider,
        extractor=extractor,
    )

    assert code == 0
    assert summary.processed_urls == 3
    # 每个关键词各保留 1 条，靠后的关键词不会被前面吃满配额
    first = [groups["关键词一"][0], groups["关键词二"][0], groups["关键词三"][0]]
    assert extractor.calls == [first]


def test_pipeline_prefilter_drops_portal_homepages(tmp_path):
    provider = FakeProvider(
        {
            "关键词一": make_search_response(
                ["https://m.cn.nytimes.com", "https://example.com/a"]
            ),
            "关键词二": make_search_response(["https://example.com/a"]),
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
            "关键词一": make_search_response(urls[:4]),
            "关键词二": make_search_response(urls[4:]),
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


def test_pipeline_injects_coverage_points_into_keyword_prompt(tmp_path):
    scenarios = tmp_path / "scenarios.json"
    scenarios.write_text(
        json.dumps(
            {
                "智能对话": {
                    "联网搜索": {
                        "天气查询": [
                            {
                                "s4": "气象灾害查询",
                                "coverage_points": ["埃及常见气象灾害", "沙尘暴预警"],
                            }
                        ]
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    llm = RouterLLM()
    _run(tmp_path, llm_client=llm, scenarios_path=scenarios)
    keyword_prompt = llm.calls[0][0][0]["content"]
    assert "- 埃及常见气象灾害" in keyword_prompt
    assert "- 沙尘暴预警" in keyword_prompt


def test_pipeline_missing_scenarios_file_still_generates_keywords(tmp_path):
    summary, code = _run(tmp_path, scenarios_path=tmp_path / "missing.json")
    assert code == 0
    assert summary.keywords == ["关键词一", "关键词二"]


def test_pipeline_uses_same_provider_for_search_and_extract(tmp_path):
    provider = CombinedProvider(_provider(), _extractor())
    summary, code = _run(tmp_path, provider=provider)
    assert code == 0
    assert provider.requests
    assert provider.calls


def test_pipeline_search_quota_aborts(tmp_path):
    error = SearchError("deepseek", 402, "quota")
    provider = FakeProvider({"关键词一": error, "关键词二": error})
    summary, code = _run(tmp_path, provider=provider)
    assert code == 1
    assert summary.documents == 0
