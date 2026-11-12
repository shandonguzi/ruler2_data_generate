from __future__ import annotations

import json

from scopecrawl.models import (
    AuthorityResult,
    ContentScore,
    Document,
    DocumentEvaluation,
    DomainAssessment,
    ExtractedDoc,
    PageAssessment,
    RelevanceResult,
)
from scopecrawl.storage import METADATA_FILENAME, RunWriter, build_metadata
from tests.conftest import make_settings


def make_document(
    url="https://example.com/a",
    doc_id="0123456789abcdef",
    title="标题：埃及沙尘暴",
) -> Document:
    return Document(
        extracted=ExtractedDoc(
            doc_id=doc_id,
            url=url,
            final_url=url,
            title=title,
            site="example.com",
            author="Author",
            published_at="2026-11-01T00:00:00+00:00",
            fetched_at="2026-11-10T00:00:00+00:00",
            lang="zh",
            markdown="# 正文\n\n这是一段中文内容。",
            matched_queries=["埃及 沙尘暴"],
            has_jsonld=True,
            outbound_links=2,
            extractor="tavily-extract",
        ),
        evaluation=DocumentEvaluation(
            relevance=RelevanceResult(
                relevance_score=0.9,
                relevance_level="high",
                content_type="article",
                matched_scope="s4",
                location_match=True,
                language_match=True,
                temporal_match=True,
                flags=[],
                reason="直接相关",
                model="fake-model",
                reviewed_at="2026-11-10T01:00:00+00:00",
            ),
            content_richness=ContentScore(
                score=0.8, level="high", reason="信息密集"
            ),
            content_quality=ContentScore(
                score=0.75, level="high", reason="结构清晰"
            ),
            authority=AuthorityResult(
                authority_score=0.82,
                authority_level="high",
                quality_flags=[],
                authority_reasons=["命中白名单"],
                domain=DomainAssessment(
                    domain="example.com",
                    decision="allow",
                    domain_score=1.0,
                ),
                page=PageAssessment(
                    doc_id=doc_id,
                    url=url,
                    page_score=0.55,
                ),
            ),
        ),
    )


def read_metadata(run_dir) -> list[dict]:
    lines = (run_dir / METADATA_FILENAME).read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def read_documents(run_dir) -> list[dict]:
    return [record for record in read_metadata(run_dir) if record["type"] == "document"]


def test_writer_creates_flat_run_dir(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    assert writer.run_dir == tmp_path / writer.run_id
    assert writer.run_dir.exists()
    assert not (writer.run_dir / "markdown").exists()
    assert writer.run_id.startswith("天气查询-气象灾害查询-埃及-zh-20")


def test_write_body_and_metadata(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    document = make_document()
    path = writer.write(document)

    assert path.parent == writer.run_dir
    assert path.name == "标题：埃及沙尘暴.md"
    assert path.suffix == ".md"
    assert path.read_text(encoding="utf-8") == "# 正文\n\n这是一段中文内容。\n"

    metadata = read_metadata(writer.run_dir)
    run = metadata[0]
    assert run["type"] == "run"
    assert run["s3"] == "天气查询"
    assert run["s4"] == "气象灾害查询"
    assert run["location"] == "埃及"
    assert run["language"] == "zh"

    documents = read_documents(writer.run_dir)
    assert len(documents) == 1
    record = documents[0]
    assert record["file"] == "标题：埃及沙尘暴.md"
    assert record["doc_id"] == "0123456789abcdef"
    assert record["title"] == "标题：埃及沙尘暴"
    assert record["matched_queries"] == ["埃及 沙尘暴"]
    assert record["domain_decision"] == "allow"
    assert record["domain_score"] == 1.0
    assert record["page_score"] == 0.55
    assert record["relevance_score"] == 0.9
    assert record["content_type"] == "article"
    assert record["relevance_flags"] == []
    assert record["content_richness_score"] == 0.8
    assert record["content_richness_level"] == "high"
    assert record["content_quality_score"] == 0.75
    assert record["content_quality_level"] == "high"
    assert record["content_reason"] == "信息密集"
    assert record["evaluation_decision"] == "keep"
    assert record["evaluation_drop_reasons"] == []
    assert record["quality_flags"] == []
    assert record["extractor"] == "tavily-extract"
    assert record["published_at"] == "2026-11-01T00:00:00+00:00"


def test_run_dir_contains_md_and_metadata(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    writer.write(make_document())
    files = {path.name for path in writer.run_dir.iterdir()}
    assert files == {"标题：埃及沙尘暴.md", METADATA_FILENAME}


def test_doc_id_collision_gets_salt(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    first = make_document(url="https://example.com/a", doc_id="sameid0000000000")
    second = make_document(url="https://example.com/b", doc_id="sameid0000000000")
    writer.write(first)
    writer.write(second)
    assert first.extracted.doc_id == "sameid0000000000"
    assert second.extracted.doc_id != "sameid0000000000"
    assert len(list(writer.run_dir.glob("*.md"))) == 2
    assert len(read_documents(writer.run_dir)) == 2


def test_duplicate_titles_get_numbered_suffix(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    paths = [
        writer.write(make_document(url=f"https://example.com/{index}")) for index in range(3)
    ]
    assert [path.name for path in paths] == [
        "标题：埃及沙尘暴.md",
        "标题：埃及沙尘暴(1).md",
        "标题：埃及沙尘暴(2).md",
    ]


def test_same_url_rewrites_same_file(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    first = writer.write(make_document())
    second = writer.write(make_document())
    assert first == second
    assert len(list(writer.run_dir.glob("*.md"))) == 1
    assert len(read_documents(writer.run_dir)) == 1


def test_empty_title_falls_back_to_doc_id(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    path = writer.write(make_document(title="   "))
    assert path.name == "0123456789abcdef.md"


def test_unsafe_title_chars_are_sanitized(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    path = writer.write(make_document(title='a/b\\c:d*e?f"g<h>i|j'))
    assert path.name == "a_b_c_d_e_f_g_h_i_j.md"


def test_long_title_is_truncated_by_bytes(tmp_path):
    settings = make_settings()
    writer = RunWriter(tmp_path, settings.run)
    path = writer.write(make_document(title="沙" * 200))
    assert len(path.stem.encode("utf-8")) <= 180
    assert path.stem


def test_metadata_allows_missing_evaluations():
    document = make_document()
    document.evaluation.authority.domain = None
    document.evaluation.authority.page = None
    document.evaluation.relevance.relevance_score = None
    document.evaluation.relevance.relevance_level = None
    document.evaluation.content_richness = ContentScore()
    document.evaluation.content_quality = ContentScore()
    document.evaluation.authority.authority_score = None
    document.evaluation.authority.authority_level = None

    record = build_metadata(document)
    assert record["domain_decision"] is None
    assert record["page_score"] is None
    assert record["relevance_score"] is None
    assert record["content_richness_score"] is None
    assert record["content_quality_score"] is None
    assert record["authority_score"] is None
