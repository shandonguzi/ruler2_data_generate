import json

import pytest

from doc2qa import pipeline
from doc2qa.cli import main
from doc2qa.quality import DuplicateResult
from doc2qa.storage import read_jsonl, write_json, write_jsonl
from doc2qa.synthesis import smoke_candidates


@pytest.fixture
def scored_run(tmp_path, bundle, config):
    docs, chunks, contexts = bundle
    source = tmp_path / "source"
    source.mkdir()
    qa = smoke_candidates(contexts[0], chunks, docs, config, "test")[0]
    qa.question = "How many stations are on Cairo Metro Line 1?"
    qa.review_status = "auto_pass"
    qa.reasons = []
    qa.judge_result = {
        "question_clarity": 3,
        "self_containment": 3,
        "groundedness": 3,
        "evidence_coverage": 3,
        "completeness": 3,
    }
    qa.quality_scores = {key: 1.0 for key in qa.judge_result} | {"overall": 1.0}
    other = qa.model_copy(deep=True)
    other.qa_id = "other"
    other.question = "What is the station count of Cairo Metro Line 1?"
    write_json(source / "config.json", config.model_dump())
    write_jsonl(source / "scored_candidates.jsonl", [qa, other])
    return source, qa, other


def forbidden(*args, **kwargs):
    pytest.fail("Deduplication must not generate QA or repeat quality judging")


def test_dedup_command_only_calls_embeddings_and_duplicate_judge(scored_run, tmp_path, monkeypatch):
    source, first, _ = scored_run
    calls = []

    class FakeRemote:
        usage = {"judge_calls": 1, "embedding_calls": 1}

        def embed(self, texts, model):
            calls.append(("embed", texts))
            return [[1.0, 0.0]] * len(texts)

        def structured(self, system, payload, schema):
            assert schema is DuplicateResult
            calls.append(("dedup", json.loads(payload)))
            return schema(duplicate=True, reason="Same requested fact")

    monkeypatch.setattr(pipeline, "Remote", lambda _: FakeRemote())
    monkeypatch.setattr(pipeline, "validate_qa", forbidden)
    monkeypatch.setattr(pipeline, "generate_context", forbidden)
    output = tmp_path / "result"
    assert main(["dedup", "--run-dir", str(source), "--output", str(output)]) == 0
    assert [stage for stage, _ in calls] == ["embed", "dedup"]
    assert len(read_jsonl(output / "dataset.jsonl")) == 1
    assert read_jsonl(output / "duplicates.jsonl")[0]["duplicate_of"] == first.qa_id
    assert all(
        q["quality_scores"]["overall"] == 1.0 for q in read_jsonl(output / "candidates.jsonl")
    )
    assert all(
        q["review_status"] == "auto_pass" for q in read_jsonl(source / "scored_candidates.jsonl")
    )


def test_checkpoint_input_keeps_original_order_and_exact_only_never_calls_remote(
    scored_run, tmp_path, config, monkeypatch
):
    source, first, other = scored_run
    # Original candidates may be pending after an embedding failure; only use
    # their IDs/order, and recover scores/status from the validated checkpoints.
    first_pending = first.model_copy(update={"review_status": "pending_review"})
    write_jsonl(source / "candidates.jsonl", [first_pending, other])
    checkpoints = source / "checkpoints"
    write_jsonl(checkpoints / "a.validated.jsonl", [other])
    write_jsonl(checkpoints / "z.validated.jsonl", [first])
    write_jsonl(checkpoints / "ctx_generated.jsonl", [first])
    # Supply a run without the consolidated scoring file.
    bundle = tmp_path / "checkpoint_source"
    bundle.mkdir()
    (bundle / "checkpoints").symlink_to(checkpoints, target_is_directory=True)
    write_jsonl(bundle / "candidates.jsonl", [first_pending, other])
    write_json(bundle / "config.json", config.model_dump())
    monkeypatch.setattr(pipeline, "Remote", forbidden)
    monkeypatch.setattr(pipeline, "validate_qa", forbidden)
    output = tmp_path / "result"
    assert main(["dedup", "--run-dir", str(bundle), "--output", str(output), "--exact-only"]) == 0
    records = read_jsonl(output / "dataset.jsonl")
    assert [q["qa_id"] for q in records] == [first.qa_id, other.qa_id]
    assert json.loads((output / "config.json").read_text())["quality"]["semantic_dedup"] is False


def test_explicit_scored_file_and_source_output_guard(scored_run, tmp_path, config, monkeypatch):
    source, _, _ = scored_run
    monkeypatch.setattr(pipeline, "Remote", forbidden)
    output = tmp_path / "result"
    assert (
        main(
            [
                "dedup",
                "--run-dir",
                str(source),
                "--input",
                str(source / "scored_candidates.jsonl"),
                "--output",
                str(output),
                "--exact-only",
            ]
        )
        == 0
    )
    assert main(["dedup", "--run-dir", str(source), "--output", str(source), "--exact-only"]) == 2


def test_rejects_post_dedup_input_instead_of_silently_skipping_candidates(
    scored_run, tmp_path, config
):
    source, first, _ = scored_run
    first.review_status = "pending_review"
    first.reasons = ["semantic_dedup_error:PermissionDeniedError"]
    path = tmp_path / "post_dedup.jsonl"
    write_jsonl(path, [first])
    with pytest.raises(ValueError, match="pre-deduplication"):
        pipeline.rededuplicate(path, tmp_path / "result", source, config, exact_only=True)


@pytest.mark.parametrize(
    "legacy_types",
    [
        [
            "factual",
            "numeric_fact",
            "list",
            "conditional",
            "comparison",
            "multi_hop",
            "table_lookup",
            "source_limitation",
        ],
        ["table_lookup"],
    ],
)
def test_dedup_can_read_old_generation_config_after_type_removal(
    scored_run, tmp_path, monkeypatch, legacy_types
):
    source, _, _ = scored_run
    path = source / "config.json"
    saved = json.loads(path.read_text())
    saved["synthesis"]["question_types"] = legacy_types
    saved["synthesis"]["type_weights"] = {legacy_types[-1]: 2}
    write_json(path, saved)
    before = path.read_bytes()
    monkeypatch.setattr(pipeline, "Remote", forbidden)
    output = tmp_path / "result"
    assert main(["dedup", "--run-dir", str(source), "--output", str(output), "--exact-only"]) == 0
    assert len(read_jsonl(output / "dataset.jsonl")) == 2
    assert path.read_bytes() == before
    assert json.loads((output / "config.json").read_text())["synthesis"]["question_types"] == [
        "factual",
        "numeric_fact",
        "list",
        "conditional",
        "comparison",
    ]
