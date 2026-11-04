import json
from types import SimpleNamespace

import pytest

from doc2qa.storage import evaluation_dataset, export
from doc2qa.synthesis import (
    align_docs_golden,
    build_synthesizer,
    generate_context,
    generate_docs,
    smoke_candidates,
)


def test_locked_deepeval_constructor_and_context_contract(bundle, config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://example.invalid/v1")
    from doc2qa.synthesis_generation import EvolutionStylingMixin

    engine = build_synthesizer(config)
    assert engine.model.base_url == "https://example.invalid/v1"
    assert engine.filtration_config.critic_model is engine.model
    assert engine.styling_config.scenario is None
    assert engine.styling_config.input_format is None
    docs, chunks, contexts = bundle
    context = contexts[0]

    def fake(self, **kwargs):
        assert kwargs["include_expected_output"] is True
        assert kwargs["source_files"] == [context.context_id]
        assert kwargs["contexts"][0][0].startswith("[chunk_id=")
        assert len(kwargs["contexts"][0]) == len(context.chunk_ids + context.support_chunk_ids)
        return [
            SimpleNamespace(
                input="Q", expected_output="A", additional_metadata={"qa_type": "factual"}
            )
        ]

    monkeypatch.setattr(EvolutionStylingMixin, "generate_goldens_from_contexts", fake)
    goldens, _ = generate_context(context, chunks, config)
    assert goldens[0].expected_output == "A"


@pytest.mark.parametrize(
    ("model", "judge", "priced"),
    [
        ("deepseek-v4.1-flash", "deepseek-v4.1-flash", False),
        ("gpt-4.1-mini", "deepseek-v4.1-flash", True),
        ("deepseek-v4.1-flash", "gpt-4.1-mini", False),
        ("gpt-4.1-mini", "gpt-4.1-mini", True),
    ],
)
def test_real_synthesis_handles_unknown_prices(bundle, config, monkeypatch, model, judge, priced):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    from deepeval.models import OpenAIModel

    config.synthesis.model = model
    config.synthesis.judge_model = judge
    config.synthesis.max_goldens_per_context = 1
    calls = []

    def response(self, prompt, schema=None):
        calls.append((self.name, schema.__name__ if schema else "text"))
        data = {
            "SyntheticDataList": {"data": [{"input": "What is recorded?", "qa_type": "factual"}]},
            "SyntheticData": {"input": "What is recorded?", "qa_type": "factual"},
        }
        result = schema.model_validate(data[schema.__name__]) if schema else "Recorded fact"
        return result, self.calculate_cost(100, 20)

    async def async_response(self, prompt, schema=None):
        return response(self, prompt, schema)

    # Keep the real DeepEval generation, optional evolution and cost resets.
    # Only replace model requests; unknown models still return their real None cost.
    monkeypatch.setattr(OpenAIModel, "a_generate", async_response)
    monkeypatch.setattr(OpenAIModel, "generate", response)
    docs, chunks, contexts = bundle
    engine = build_synthesizer(config)
    from doc2qa.context import context_text

    for _ in range(2):
        goldens = engine.generate_goldens_from_contexts(
            contexts=[context_text(contexts[0], chunks)],
            max_goldens_per_context=1,
            include_expected_output=True,
        )
        assert len(goldens) == 1
        assert goldens[0].expected_output == "Recorded fact"
        assert (engine.synthesis_cost is not None) is priced
        if priced:
            assert engine.synthesis_cost > 0
    # The same guard also covers the synchronous helpers.
    assert engine._generate("text prompt") == "Recorded fact"
    from deepeval.synthesizer.schema import SyntheticData

    assert engine._generate_schema("schema prompt", SyntheticData, engine.model).input
    assert all(name == model for name, _ in calls)
    assert not any(schema in {"InputFeedback", "RewrittenInput"} for _, schema in calls)
    assert any(schema == "text" for _, schema in calls)


def test_locked_deepeval_docs_contract(bundle, config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    from deepeval.synthesizer import Synthesizer

    docs, _, _ = bundle
    # This contract test covers the uncustomized DeepEval docs entry point.
    config.synthesis.document_rules = {}
    config.synthesis.context_rules = {}

    def fake(self, **kwargs):
        assert len(kwargs["document_paths"]) == 5
        settings = kwargs["context_construction_config"]
        assert settings.allow_cross_file_contexts is True
        assert settings.critic_model.name == config.synthesis.judge_model
        return []

    monkeypatch.setattr(Synthesizer, "generate_goldens_from_docs", fake)
    assert generate_docs(list(docs.values()), config)[0] == []


def test_docs_alignment_preserves_original_offsets(bundle):
    docs, chunks, _ = bundle
    target = next(d for d in docs.values() if "railway" in d.path)
    piece = "Alexandria | 190 | 150"
    context = align_docs_golden(SimpleNamespace(context=[piece]), docs, chunks)
    chunk = chunks[context.chunk_ids[0]]
    assert target.text[chunk.char_start : chunk.char_end] == piece
    with pytest.raises(ValueError, match="ambiguous"):
        align_docs_golden(
            SimpleNamespace(context=["CAIRO GOVERNORATE TRANSPORTATION SERVICES"]), docs, chunks
        )


def test_deepeval_export_contains_goldens_without_actual_output(bundle, config, tmp_path):
    docs, chunks, contexts = bundle
    qa = smoke_candidates(contexts[0], chunks, docs, config, "serializer_test")[0]
    qa.review_status = "auto_pass"
    export([qa], tmp_path)
    data = json.loads((tmp_path / "deepeval_goldens.json").read_text())
    assert "actual_output" not in data[0]
    dataset = evaluation_dataset(tmp_path / "deepeval_goldens.json")
    assert len(dataset.goldens) == 1
    assert dataset.goldens[0].expected_output == qa.answer


def test_whole_document_passes_all_evidence_and_multiple_qa_limit_to_deepeval(
    bundle, config, monkeypatch
):
    from doc2qa.context import build_contexts, context_text
    from doc2qa.quality import deterministic_checks

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    from doc2qa.synthesis_generation import EvolutionStylingMixin

    docs, chunks, _ = bundle
    config.context.strategy = "whole_document"
    config.synthesis.max_goldens_per_context = 10
    contexts, _ = build_contexts(list(chunks.values()), config.context)
    context = next(c for c in contexts if "metro_en.txt" in docs[c.document_ids[0]].path)
    assert len(context.chunk_ids) > config.context.max_chunks

    def fake(self, **kwargs):
        assert kwargs["contexts"] == [context_text(context, chunks)]
        assert kwargs["max_goldens_per_context"] == 10
        return [
            SimpleNamespace(
                input=f"What does source section {i + 1} report?",
                expected_output=chunks[c].text,
                additional_metadata={"qa_type": "factual"},
            )
            for i, c in enumerate(context.chunk_ids[:2])
        ]

    monkeypatch.setattr(EvolutionStylingMixin, "generate_goldens_from_contexts", fake)
    goldens, _ = generate_context(context, chunks, config)
    assert len(goldens) == 2
    from doc2qa.synthesis import map_golden

    records = [
        map_golden(g, context, chunks, docs, config, "whole_document_test", i)
        for i, g in enumerate(goldens)
    ]
    assert len({q.qa_id for q in records}) == 2
    assert all(not deterministic_checks(q, chunks, docs, context) for q in records)
