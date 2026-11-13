"""阶段 4 提取清洗：搜索正文直通与 fetch 回退。"""

from __future__ import annotations

import pytest

from scopecrawl.content import ExtractError
from scopecrawl.models import ExtractedContent, UrlTask
from scopecrawl.provider import Provider
from scopecrawl.stages.extract import ExtractStage
from scopecrawl.summary import StageAborted
from tests.conftest import MARKDOWN_NAV, MARKDOWN_OK, make_settings

A = "https://example.com/a"
B = "https://example.com/b"
C = "https://example.com/c"


class RecordingProvider(Provider):
    name = "deepseek"
    extractor_name = "deepseek-fetch"
    extract_batch_size = 20

    def __init__(self, outcomes=None):
        self.outcomes = outcomes or {}
        self.calls: list[list[str]] = []

    def search(self, request):
        raise NotImplementedError

    def _extract_unique(self, urls):
        self.calls.append(list(urls))
        results = []
        for url in urls:
            value = self.outcomes.get(url)
            if isinstance(value, Exception):
                raise value
            if value is None:
                value = ExtractedContent(
                    url=url, final_url=url, raw_content=MARKDOWN_OK
                )
            results.append(value)
        return results


def _candidate(url: str, content: str = ""):
    return (UrlTask(url=url, normalized_url=url, content=content), None)


def _run(candidates, provider):
    return ExtractStage(make_settings(provider="deepseek"), provider).run(candidates)


def test_uses_search_content_without_calling_provider():
    provider = RecordingProvider()
    documents, stats = _run([_candidate(url, MARKDOWN_OK) for url in (A, B, C)], provider)

    assert provider.calls == []
    assert [document.url for document, _ in documents] == [A, B, C]
    assert all(document.extractor == "deepseek-search" for document, _ in documents)
    assert stats.failures == []


def test_fetches_urls_without_search_content():
    provider = RecordingProvider()
    documents, _ = _run([_candidate(url) for url in (A, B, C)], provider)

    assert provider.calls == [[A, B, C]]
    assert all(document.extractor == "deepseek-fetch" for document, _ in documents)


def test_mixed_batch_only_fetches_missing_content_and_keeps_order():
    provider = RecordingProvider()
    documents, _ = _run(
        [_candidate(A, MARKDOWN_OK), _candidate(B), _candidate(C, MARKDOWN_OK)],
        provider,
    )

    assert provider.calls == [[B]]
    assert [document.url for document, _ in documents] == [A, B, C]
    assert [document.extractor for document, _ in documents] == [
        "deepseek-search",
        "deepseek-fetch",
        "deepseek-search",
    ]


def test_whitespace_content_counts_as_missing():
    provider = RecordingProvider()
    _run([_candidate(A, "   \n ")], provider)
    assert provider.calls == [[A]]


def test_non_fatal_extract_error_keeps_search_content():
    provider = RecordingProvider(
        {B: ExtractError("service_error", "服务端错误", fatal=False)}
    )
    documents, stats = _run(
        [_candidate(A, MARKDOWN_OK), _candidate(B), _candidate(C, MARKDOWN_OK)],
        provider,
    )

    assert [document.url for document, _ in documents] == [A, C]
    assert len(stats.failures) == 1
    assert stats.failures[0].url == B
    assert stats.failures[0].error_type == "service_error"


def test_fatal_extract_error_aborts_run():
    provider = RecordingProvider(
        {B: ExtractError("auth_error", "鉴权失败", fatal=True)}
    )
    with pytest.raises(StageAborted):
        _run([_candidate(A, MARKDOWN_OK), _candidate(B)], provider)


def test_search_content_quality_drop_does_not_fetch():
    provider = RecordingProvider()
    documents, stats = _run([_candidate(A, MARKDOWN_NAV)], provider)

    assert provider.calls == []
    assert documents == []
    assert stats.quality_reasons["short_content"] == 1
    assert stats.failures == []


def test_all_fetch_failures_abort_but_search_content_prevents_abort():
    failed = ExtractedContent(url=A, error_type="network_error", error="网络错误")

    with pytest.raises(StageAborted):
        _run([_candidate(A)], RecordingProvider({A: failed}))

    documents, stats = _run(
        [_candidate(A, MARKDOWN_OK), _candidate(B)],
        RecordingProvider(
            {B: ExtractedContent(url=B, error_type="network_error", error="网络错误")}
        ),
    )
    assert [document.url for document, _ in documents] == [A]
    assert len(stats.failures) == 1
