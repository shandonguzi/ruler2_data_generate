import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from doc2qa import pipeline
from doc2qa.config import load_config
from doc2qa.context import build_contexts
from doc2qa.prompts import GENERAL_RULES, prompt_sha256
from doc2qa.synthesis import generate_context, generate_docs, rules_for_context


def whole_document_bundle(bundle, config):
    docs, chunks, _ = bundle
    config.context.strategy = "whole_document"
    contexts, _ = build_contexts(list(chunks.values()), config.context)
    brt = next(
        c
        for c in contexts
        if Path(docs[c.document_ids[0]].path).name == "cairo_bus_rapid_transit_en.txt"
    )
    other = next(c for c in contexts if c.context_id != brt.context_id)
    return docs, chunks, brt, other


def test_rules_are_scoped_to_source_documents_and_exact_context_id(bundle, config):
    docs, _, brt, other = whole_document_bundle(bundle, config)
    brt_id, other_id = brt.document_ids[0], other.document_ids[0]
    config.synthesis.document_rules = {
        Path(docs[brt_id].path).name: ["BRT only"],
        Path(docs[other_id].path).name: ["Other document only"],
        "unrelated.txt": ["Must not leak"],
    }
    config.synthesis.context_rules = {brt.context_id: ["Exact context only"]}
    selected = rules_for_context(brt, docs, config.synthesis)
    assert "BRT only" in selected
    assert "Exact context only" in selected
    assert "Other document only" not in selected
    assert "Must not leak" not in selected
    assert brt_id in selected
    assert "Exact context only" not in rules_for_context(other, docs, config.synthesis)

    # Cross-document selection preserves labels rather than treating each rule as global.
    combined = brt.model_copy(update={"document_ids": [brt_id, other_id], "context_id": "cross"})
    selected = rules_for_context(combined, docs, config.synthesis)
    assert f"[document_id={brt_id}]\n- BRT only" in selected
    assert f"[document_id={other_id}]\n- Other document only" in selected
    assert "Exact context only" not in selected
    config.synthesis.document_rules = {}
    assert rules_for_context(other, docs, config.synthesis) == ""


def test_configured_rules_reach_generation_and_styling_without_context_leaks(
    bundle, config, monkeypatch
):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    from deepeval.models import OpenAIModel

    docs, chunks, brt, other = whole_document_bundle(bundle, config)
    config.synthesis.document_rules = {"cairo_bus_rapid_transit_en.txt": ["BRT_RULE_MARKER"]}
    config.synthesis.context_rules = {brt.context_id: ["CONTEXT_RULE_MARKER"]}
    config.synthesis.max_goldens_per_context = 1
    config.synthesis.num_evolutions = 1
    prompts = []

    async def response(self, prompt, schema=None):
        prompts.append((schema.__name__ if schema else "text", prompt))
        data = {
            "SyntheticDataList": {"data": [{"input": "What is recorded?", "qa_type": "factual"}]},
            "SyntheticData": {"input": "What is recorded?", "qa_type": "factual"},
        }
        return (schema.model_validate(data[schema.__name__]) if schema else "Recorded fact"), 0.0

    monkeypatch.setattr(OpenAIModel, "a_generate", response)
    for context, matched in [(brt, True), (other, False)]:
        prompts.clear()
        selected = rules_for_context(context, docs, config.synthesis)
        goldens, _ = generate_context(context, chunks, config, selected)
        assert goldens[0].expected_output == "Recorded fact"
        initial = next(prompt for schema, prompt in prompts if schema == "SyntheticDataList")
        styling = next(prompt for schema, prompt in prompts if schema == "SyntheticData")
        answer = next(prompt for schema, prompt in prompts if "## Answer_requirements\n" in prompt)
        assert "## General_rules\n" in initial
        assert GENERAL_RULES.strip() in initial
        assert "## Task_specific\n" in initial
        assert GENERAL_RULES.strip() in styling
        assert "Context:\n" in answer
        assert "What is recorded?" in answer
        quality = [p for s, p in prompts if s in {"InputFeedback", "RewrittenInput"}]
        assert quality == []
        for prompt in (initial, styling):
            assert f"language '{config.language}'" in prompt
            assert "Consider every allowed question type" in prompt
            assert "scenario" not in prompt.lower()
            assert "input format" not in prompt.lower()
            assert "generation settings" not in prompt.lower()
            assert ("BRT_RULE_MARKER" in prompt) is matched
            assert ("CONTEXT_RULE_MARKER" in prompt) is matched
        # Evolution retains its separate template; other stages share scoped task rules.
        for schema, prompt in prompts:
            if (
                schema
                not in {
                    "SyntheticDataList",
                    "SyntheticData",
                    "InputFeedback",
                    "RewrittenInput",
                }
                and "## Answer_requirements\n" not in prompt
            ):
                assert "BRT_RULE_MARKER" not in prompt
                assert "CONTEXT_RULE_MARKER" not in prompt


def test_pipeline_passes_and_snapshots_configured_rules(bundle, config, tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setattr(pipeline, "Remote", lambda _: SimpleNamespace(usage={}))
    config.context.strategy = "whole_document"
    config.output = str(tmp_path / "runs")
    config.synthesis.document_rules = {"cairo_bus_rapid_transit_en.txt": ["BRT_PIPELINE_RULE"]}
    seen = []

    def fake(context, chunks, config, context_rules):
        seen.append((context.context_id, context_rules))
        return [
            SimpleNamespace(
                input="Question",
                expected_output="Answer",
                additional_metadata={"qa_type": "factual"},
            )
        ], 0.0

    monkeypatch.setattr(pipeline, "generate_context", fake)
    monkeypatch.setattr(pipeline, "validate_qa", lambda qa, *args: qa)
    monkeypatch.setattr(pipeline, "deduplicate", lambda records, *args: records)
    directory, manifest = pipeline.generate(config)
    assert manifest["status"] == "completed"
    assert len(seen) == 5
    assert sum("BRT_PIPELINE_RULE" in rules for _, rules in seen) == 1
    saved = json.loads((directory / "config.json").read_text())
    assert saved["synthesis"]["document_rules"] == config.synthesis.document_rules
    assert manifest["prompt_sha256"] == prompt_sha256(
        document_rules=config.synthesis.document_rules, context_rules=config.synthesis.context_rules
    )
    assert manifest["prompt_sha256"] != prompt_sha256()
    _, resumed = pipeline.generate(config, resume=directory)
    assert resumed["status"] == "completed"
    assert len(seen) == 5
    config.synthesis.document_rules["cairo_bus_rapid_transit_en.txt"] = ["Changed rule"]
    with pytest.raises(ValueError, match="Resume refused"):
        pipeline.generate(config, resume=directory)


def test_docs_mode_rejects_rules_before_any_model_calls(config, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config.mode = "docs"
    config.context.strategy = "chunks"
    config.synthesis.document_rules = {"brt.txt": ["A rule"]}
    with pytest.raises(ValueError, match="mode=contexts"):
        pipeline.generate(config)
    with pytest.raises(ValueError, match="mode=contexts"):
        generate_docs([], config)


def test_shipped_brt_rules_are_visible_in_task_yaml():
    root = Path(__file__).resolve().parents[1]
    generic = load_config(root / "configs/default.yaml")
    transport = load_config(root / "configs/transport_qa.yaml")
    assert generic.synthesis.document_rules == {}
    assert transport.synthesis.document_rules["cairo_bus_rapid_transit_en.txt"]
