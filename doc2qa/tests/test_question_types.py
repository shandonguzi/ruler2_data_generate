import asyncio
from collections import Counter
from types import SimpleNamespace

import pytest

from doc2qa import pipeline, prompts
from doc2qa.progress import RunProgress
from doc2qa.question_types import QUESTION_TYPES
from doc2qa.storage import read_jsonl
from doc2qa.synthesis import build_synthesizer, map_golden
from doc2qa.synthesis_schema import SyntheticDataList


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("num_evolutions", [0, 1])
def test_each_context_accepts_unequal_type_mix_and_keeps_final_labels(
    config, monkeypatch, tmp_path, async_mode, num_evolutions
):
    from deepeval.models import OpenAIModel

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    initial_types = ["numeric_fact", "list", "numeric_fact", "conditional"]
    seen_contexts = []

    def response(self, prompt, schema=None):
        if schema and schema.__name__ == "SyntheticDataList":
            for name in config.synthesis.question_types:
                assert f"### {name}\n- " in prompt
            assert "Counts may be unequal" in prompt
            seen_contexts.append("EVIDENCE_A" if "EVIDENCE_A" in prompt else "EVIDENCE_B")
            data = {
                "data": [
                    {"input": f"Question about named subject {index}?", "qa_type": kind}
                    for index, kind in enumerate(initial_types)
                ]
            }
        elif schema:
            assert "Select qa_type for the final edited query" in prompt
            data = {"input": "How do the named bridges compare?", "qa_type": "comparison"}
        else:
            data = (
                "The named bridges differ."
                if "Answer_requirements" in prompt
                else "Compare the named bridges."
            )
        return (schema.model_validate(data) if schema else data), 0.0

    async def a_response(self, prompt, schema=None):
        await asyncio.sleep(0)
        return response(self, prompt, schema)

    monkeypatch.setattr(OpenAIModel, "generate", response)
    monkeypatch.setattr(OpenAIModel, "a_generate", a_response)
    config.synthesis.num_evolutions = num_evolutions
    engine = build_synthesizer(config)
    engine.async_mode = async_mode
    with RunProgress(tmp_path):
        goldens = engine.generate_goldens_from_contexts(
            contexts=[["EVIDENCE_A"], ["EVIDENCE_B"]],
            source_files=["ctx_A", "ctx_B"],
            max_goldens_per_context=4,
        )
    assert sorted(seen_contexts) == ["EVIDENCE_A", "EVIDENCE_B"]
    for source in ("ctx_A", "ctx_B"):
        items = [g for g in goldens if g.source_file == source]
        assert len(items) == 4
        assert Counter(g.additional_metadata["initial_qa_type"] for g in items) == Counter(
            initial_types
        )
        expected = Counter({"comparison": 4}) if num_evolutions else Counter(initial_types)
        assert Counter(g.additional_metadata["qa_type"] for g in items) == expected
        if async_mode:
            saved = read_jsonl(tmp_path / "intermediate" / source / "generated_qa.jsonl")
            assert Counter(q["qa_type"] for q in saved) == expected
            initial = read_jsonl(tmp_path / "intermediate" / source / "initial_questions.jsonl")
            assert Counter(q["qa_type"] for q in initial) == Counter(initial_types)


@pytest.mark.parametrize("async_mode", [False, True])
@pytest.mark.parametrize("invalid", ["missing", "unknown", "disallowed"])
def test_missing_unknown_and_unconfigured_labels_fail_visibly(
    config, monkeypatch, async_mode, invalid
):
    from deepeval.models import OpenAIModel

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    config.synthesis.question_types = ["factual"]
    item = {"input": "When did the Golden Gate Bridge open?"}
    if invalid != "missing":
        item["qa_type"] = "unknown" if invalid == "unknown" else "comparison"

    def response(self, prompt, schema=None):
        return schema.model_validate({"data": [item]}), 0.0

    async def a_response(self, prompt, schema=None):
        return response(self, prompt, schema)

    monkeypatch.setattr(OpenAIModel, "generate", response)
    monkeypatch.setattr(OpenAIModel, "a_generate", a_response)
    engine = build_synthesizer(config)
    engine.async_mode = async_mode
    with pytest.raises(ValueError):
        engine.generate_goldens_from_contexts(contexts=[["Bridge evidence"]])


def test_generated_labels_survive_judge_checkpoints_and_type_summary(
    tmp_path, bundle, config, monkeypatch
):
    from deepeval.models import OpenAIModel

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    _, chunks, _ = bundle
    types = ["numeric_fact", "numeric_fact", "list", "comparison"]
    answer = next(iter(chunks.values())).text
    config.context.strategy = "whole_document"
    config.synthesis.num_evolutions = 0
    config.synthesis.max_goldens_per_context = 4
    config.quality.semantic_dedup = False
    config.output = str(tmp_path / "runs")

    async def response(self, prompt, schema=None):
        if schema:
            assert schema is SyntheticDataList
            return schema.model_validate(
                {
                    "data": [
                        {"input": f"Named Cairo topic {index}?", "qa_type": kind}
                        for index, kind in enumerate(types)
                    ]
                }
            ), 0.0
        return answer, 0.0

    class Judge:
        usage = {}

        def structured(self, system, payload, schema):
            return schema(score=3)

    monkeypatch.setattr(OpenAIModel, "a_generate", response)
    monkeypatch.setattr(pipeline, "Remote", lambda _: Judge())
    directory, manifest = pipeline.generate(config, limit=1)
    assert manifest["status"] == "completed"
    assert manifest["summary"]["accepted_per_type"] == dict(Counter(types))
    rows = read_jsonl(directory / "dataset.jsonl")
    assert [q["qa_type"] for q in rows] == types
    assert all(q["generation_metadata"]["qa_type_source"] == "generation" for q in rows)
    assert all("requested_qa_type" not in q["generation_metadata"] for q in rows)
    assert all(len(q["judge_result"]) == 5 for q in rows)


def test_map_golden_requires_its_own_label(bundle, config):
    docs, chunks, contexts = bundle
    with pytest.raises(ValueError, match="per-query qa_type"):
        map_golden(
            SimpleNamespace(input="Query", expected_output="Answer"),
            contexts[0],
            chunks,
            docs,
            config,
            "run",
            0,
        )


def test_type_preferences_are_soft_and_definitions_are_fingerprinted(config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    config.synthesis.type_weights = {"comparison": 3}
    task = build_synthesizer(config).styling_config.task
    assert "comparison=3" in task
    assert "not a fixed quota" in task
    before = prompts.prompt_sha256()
    monkeypatch.setitem(QUESTION_TYPES, "list", QUESTION_TYPES["list"] + " Changed definition.")
    assert prompts.prompt_sha256() != before


@pytest.mark.parametrize(
    "types", [[], ["unknown"], ["multi_hop"], ["table_lookup"], ["source_limitation"]]
)
def test_config_rejects_undefined_question_types(types):
    from doc2qa.config import Config

    with pytest.raises(ValueError):
        Config.model_validate({"synthesis": {"question_types": types}})
