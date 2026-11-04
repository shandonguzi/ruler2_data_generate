import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doc2qa.cli import main
from doc2qa.pipeline import generate
from doc2qa.storage import read_jsonl


def test_smoke_end_to_end_resume_and_input_version_guard(tmp_path, config):
    config.output = str(tmp_path / "runs")
    directory, manifest = generate(config, smoke=True, limit=3)
    assert manifest["status"] == "completed"
    assert manifest["summary"]["accepted"] == 0
    assert manifest["summary"]["pending_review"] == 3
    progress = json.loads((directory / "progress.json").read_text())
    assert progress["status"] == "completed"
    assert not progress["active_operations"]
    assert sum(c["final_pending_review"] for c in progress["contexts"].values()) == 3
    assert sum(c["final_accepted"] for c in progress["contexts"].values()) == 0
    history_before = read_jsonl(directory / "events.jsonl")
    assert read_jsonl(directory / "dataset.jsonl") == []
    review = read_jsonl(directory / "review_queue.jsonl")
    assert all(q["generation_metadata"]["engine"] == "offline_smoke" for q in review)
    _, resumed = generate(config, smoke=True, limit=3, resume=directory)
    assert resumed["summary"] == manifest["summary"]
    history_after = read_jsonl(directory / "events.jsonl")
    assert history_after[: len(history_before)] == history_before
    assert sum(e["stage"] == "context" and e["status"] == "restored" for e in history_after) == 3
    config.synthesis.model = "changed-model"
    with pytest.raises(ValueError, match="Resume refused"):
        generate(config, smoke=True, limit=3, resume=directory)


def test_real_mode_without_key_fails_early(tmp_path, config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config.output = str(tmp_path / "runs")
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        generate(config)
    assert not Path(config.output).exists()


def test_validate_cli_keeps_smoke_out_of_goldens(tmp_path, config):
    config.output = str(tmp_path / "runs")
    directory, _ = generate(config, smoke=True, limit=2)
    output = tmp_path / "validated.jsonl"
    assert (
        main(
            [
                "validate",
                "--input",
                str(directory / "candidates.jsonl"),
                "--output",
                str(output),
                "--offline",
            ]
        )
        == 0
    )
    assert output.read_text(encoding="utf-8") == ""
    summary = json.loads((tmp_path / "validated_artifacts/summary.json").read_text())
    assert summary["pending_review"] == 2


def test_whole_document_pipeline_and_resume_skip_retrieval(tmp_path, config):
    config.context.strategy = "whole_document"
    config.context.embedding = "openai"
    config.context.entities = []
    config.context.relations = []
    config.output = str(tmp_path / "runs")
    directory, manifest = generate(config, smoke=True)
    assert manifest["preparation"]["contexts"] == 5
    assert manifest["preparation"]["cross_document_contexts"] == 0
    assert manifest["summary"]["candidates"] == 5
    assert manifest["summary"]["pending_review"] == 5
    assert manifest["summary"]["accepted"] == 0
    saved = json.loads((directory / "config.json").read_text())
    assert saved["context"]["strategy"] == "whole_document"
    assert saved["context"]["embedding_model"] == "text-embedding-3-small"
    assert main(["generate", "--resume", str(directory)]) == 0


def test_docs_mode_does_not_silently_replace_whole_document_strategy(tmp_path, config):
    config.mode = "docs"
    config.context.strategy = "whole_document"
    config.output = str(tmp_path / "runs")
    with pytest.raises(ValueError, match="Whole-document contexts require mode=contexts"):
        generate(config)
    assert not Path(config.output).exists()


def test_synthesis_failure_details_are_saved_before_interrupt(
    tmp_path, config, monkeypatch, caplog
):
    from doc2qa import pipeline

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setattr(pipeline, "Remote", lambda _: SimpleNamespace(usage={}))
    config.context.strategy = "whole_document"
    config.output = str(tmp_path / "runs")
    calls = 0

    def failing(*args):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TypeError("unpriced model cost is None")
        manifest_path = next(Path(config.output).glob("*/manifest.json"))
        saved = json.loads(manifest_path.read_text())
        assert saved["errors"][0]["error_message"] == "unpriced model cost is None"
        raise KeyboardInterrupt

    monkeypatch.setattr(pipeline, "generate_context", failing)
    with pytest.raises(KeyboardInterrupt):
        generate(config)
    manifest_path = next(Path(config.output).glob("*/manifest.json"))
    saved = json.loads(manifest_path.read_text())
    assert saved["status"] == "interrupted"
    assert saved["errors"][0]["error_type"] == "TypeError"
    assert any(record.exc_info for record in caplog.records)
    assert not list((manifest_path.parent / "checkpoints").glob("*.jsonl"))


def test_unknown_synthesis_cost_is_exported_as_null(tmp_path, config, monkeypatch):
    from doc2qa import pipeline

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setattr(pipeline, "Remote", lambda _: SimpleNamespace(usage={}))
    costs = iter([None, 0.1])
    monkeypatch.setattr(
        pipeline,
        "generate_context",
        lambda *args: (
            [
                SimpleNamespace(
                    input="Question",
                    expected_output="Answer",
                    additional_metadata={"qa_type": "factual"},
                )
            ],
            next(costs),
        ),
    )
    monkeypatch.setattr(pipeline, "validate_qa", lambda qa, *args: qa)
    monkeypatch.setattr(pipeline, "deduplicate", lambda records, *args: records)
    config.context.strategy = "whole_document"
    config.output = str(tmp_path / "runs")
    directory, manifest = generate(config, limit=2)
    assert manifest["status"] == "completed"
    assert manifest["deepeval_cost_usd"] is None
    assert json.loads((directory / "manifest.json").read_text())["deepeval_cost_usd"] is None
    _, resumed = generate(config, limit=2, resume=directory)
    assert resumed["deepeval_cost_usd"] is None


def test_generate_cli_interrupt_returns_130(monkeypatch):
    from doc2qa import cli

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "generate", interrupted)
    assert main(["generate", "--smoke"]) == 130


def test_real_generation_to_five_dimension_judge_without_internal_filtering(
    tmp_path, bundle, config, monkeypatch
):
    from deepeval.models import OpenAIModel
    from doc2qa import pipeline
    from doc2qa.prompts import JUDGE_RULES

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    docs, chunks, _ = bundle
    answer = next(iter(chunks.values())).text
    config.context.strategy = "whole_document"
    config.synthesis.max_goldens_per_context = 1
    config.synthesis.num_evolutions = 0
    config.quality.semantic_dedup = False
    config.output = str(tmp_path / "runs")
    stages = []

    async def response(self, prompt, schema=None):
        if schema:
            assert schema.__name__ == "SyntheticDataList"
            stages.append("initial")
            return schema.model_validate(
                {
                    "data": [
                        {
                            "input": "What does the Cairo BRT transport report describe?",
                            "qa_type": "factual",
                        }
                    ]
                }
            ), 0.0
        stages.append("answer")
        return answer, 0.0

    class Judge:
        usage = {}

        def structured(self, system, payload, schema):
            dimension = next(key for key, rules in JUDGE_RULES.items() if rules == system)
            stages.append(dimension)
            data = json.loads(payload)
            for document_id in data["primary_document_ids"]:
                assert Path(docs[document_id].path).name in data["task_rules"]
            return schema(score=2 if dimension == "personification" else 3)

    monkeypatch.setattr(OpenAIModel, "a_generate", response)
    monkeypatch.setattr(pipeline, "Remote", lambda _: Judge())
    directory, manifest = generate(config, limit=1)
    assert manifest["status"] == "completed"
    assert stages == ["initial", "answer", *JUDGE_RULES]
    accepted = read_jsonl(directory / "dataset.jsonl")
    assert len(accepted) == 1
    assert accepted[0]["quality_scores"]["overall"] == 0.9
    assert accepted[0]["judge_result"]["personification"] == 2
    assert set(accepted[0]["judge_result"]) == {
        "question_clarity",
        "self_containment",
        "completeness",
        "answer_correctness",
        "personification",
    }
    assert accepted[0]["quality_scores"]["answer_correctness"] == 1.0
    events = read_jsonl(directory / "events.jsonl")
    assert not any(e["stage"] in {"input_quality", "input_rewrite"} for e in events)
    assert not list((directory / "intermediate").glob("*/internal_quality.jsonl"))


def test_revalidate_preserves_completed_scores_when_embedding_fails(
    tmp_path, bundle, config, monkeypatch
):
    from doc2qa import pipeline
    from doc2qa.storage import write_jsonl
    from doc2qa.synthesis import map_golden

    docs, chunks, contexts = bundle
    context = contexts[0]
    source = tmp_path / "source"
    source.mkdir()
    output = tmp_path / "validated"
    records = [
        map_golden(
            SimpleNamespace(
                input=question,
                expected_output=chunks[context.chunk_ids[0]].text,
                additional_metadata={"qa_type": "factual"},
            ),
            context,
            chunks,
            docs,
            config,
            "test",
            index,
        )
        for index, question in enumerate(
            ("What stations serve Cairo Metro?", "Which stations are on Cairo Metro?")
        )
    ]
    for name, items in (
        ("documents", docs.values()),
        ("chunks", chunks.values()),
        ("contexts", contexts),
        ("candidates", records),
    ):
        write_jsonl(source / f"{name}.jsonl", items)

    class FakeRemote:
        usage = {}

        def structured(self, system, payload, schema):
            return schema(score=3)

        def embed(self, *args):
            raise ValueError("Embedding access denied")

    monkeypatch.setattr(pipeline, "Remote", lambda _: FakeRemote())
    summary = pipeline.revalidate(source / "candidates.jsonl", output, source, config, False)
    assert summary["pending_review"] == 2
    assert summary["reasons"] == {"semantic_dedup_error:ValueError": 2}
    scored = read_jsonl(output / "scored_candidates.jsonl")
    assert all(q["review_status"] == "auto_pass" for q in scored)
    assert all(q["quality_scores"]["overall"] == 1.0 for q in scored)
    assert "Embedding access denied" in (output / "run.log").read_text()
    assert (
        len(
            [
                e
                for e in read_jsonl(output / "events.jsonl")
                if e["stage"] == "final_validation" and e["status"] == "result"
            ]
        )
        == 2
    )
