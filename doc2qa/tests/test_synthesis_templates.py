from pathlib import Path

import pytest

from doc2qa import synthesis_templates as local
from doc2qa.synthesis import build_synthesizer


@pytest.mark.parametrize("async_mode", [True, False])
@pytest.mark.parametrize(
    ("evolution_name", "evolution_method"),
    [
        ("MULTICONTEXT", "multi_context_evolution"),
        ("CONCRETIZING", "concretizing_evolution"),
        ("CONSTRAINED", "constrained_evolution"),
        ("COMPARATIVE", "comparative_question_evolution"),
        ("REASONING", "reasoning_evolution"),
        ("HYPOTHETICAL", "hypothetical_scenario_evolution"),
        ("IN_BREADTH", "in_breadth_evolution"),
    ],
)
def test_generation_uses_editable_local_prompts_at_every_stage(
    config, monkeypatch, async_mode, evolution_name, evolution_method
):
    """Exercise the real orchestration and every evolution route without filtering."""
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    from deepeval.models import OpenAIModel
    from deepeval.synthesizer import Evolution
    from deepeval.synthesizer import synthesizer as backend, templates as upstream

    methods = [
        ("generation", "SynthesizerTemplate", "generate_synthetic_inputs"),
        ("styling", "SynthesizerTemplate", "rewrite_evolved_input"),
        ("answer", "SynthesizerTemplate", "generate_synthetic_expected_output"),
        ("evolution", "EvolutionTemplate", evolution_method),
    ]

    def forbidden(*args, **kwargs):
        pytest.fail("The package prompt was used instead of a local prompt")

    def edited_template(stage, method):
        def render(*args, **kwargs):
            return f"LOCAL::{stage}\n" + method(*args, **kwargs)

        return staticmethod(render)

    # Simulate editing local prompt text. Disable the corresponding package
    # templates, including its already-captured evolution function references.
    for stage, class_name, method_name in methods:
        cls = getattr(local, class_name)
        monkeypatch.setattr(cls, method_name, edited_template(stage, getattr(cls, method_name)))
        monkeypatch.setattr(getattr(upstream, class_name), method_name, staticmethod(forbidden))
    monkeypatch.setattr(backend, "evolution_map", {key: forbidden for key in backend.evolution_map})
    # Restore module registrations at teardown so this test has no global effects.
    monkeypatch.setattr(backend, "SynthesizerTemplate", backend.SynthesizerTemplate)
    monkeypatch.setattr(backend.Synthesizer, "_rewrite_inputs", forbidden)
    monkeypatch.setattr(backend.Synthesizer, "_a_rewrite_inputs", forbidden)
    monkeypatch.setattr(upstream.FilterTemplate, "evaluate_synthetic_inputs", forbidden)
    monkeypatch.setattr(upstream.SynthesizerTemplate, "rewrite_synthetic_inputs", forbidden)

    calls = []

    def response(self, prompt, schema=None):
        marker = prompt.splitlines()[0]
        assert marker.startswith("LOCAL::")
        stage = marker.removeprefix("LOCAL::")
        calls.append((stage, prompt))
        data = {
            "generation": {"data": [{"input": "What is recorded?", "qa_type": "factual"}]},
            "styling": {"input": "What fact does the source report?", "qa_type": "factual"},
            "evolution": "Which recorded fact is reported?",
            "answer": "Recorded fact",
        }[stage]
        return (schema.model_validate(data) if schema else data), 0.0

    async def async_response(self, prompt, schema=None):
        return response(self, prompt, schema)

    monkeypatch.setattr(OpenAIModel, "generate", response)
    monkeypatch.setattr(OpenAIModel, "a_generate", async_response)
    config.synthesis.max_retries = 2
    engine = build_synthesizer(config, context_rules="TASK_RULE_MARKER")
    engine.async_mode = async_mode
    engine.evolution_config.num_evolutions = 1
    engine.evolution_config.evolutions = {getattr(Evolution, evolution_name): 1.0}
    goldens = engine.generate_goldens_from_contexts(
        contexts=[["LOCAL EVIDENCE"]], max_goldens_per_context=1, include_expected_output=True
    )

    assert [stage for stage, _ in calls] == [
        "generation",
        "evolution",
        "styling",
        "answer",
    ]
    assert goldens[0].input == "What fact does the source report?"
    assert goldens[0].expected_output == "Recorded fact"
    assert "LOCAL EVIDENCE" in calls[0][1]
    for stage, prompt in calls:
        if stage in {"styling", "answer"}:
            assert "LOCAL EVIDENCE" in prompt
        if stage == "styling":
            assert "TASK_RULE_MARKER" in prompt
            assert local.GENERAL_RULES.strip() in prompt
    assert "LOCAL EVIDENCE" in calls[1][1]
    assert "Which recorded fact is reported?" in calls[2][1]
    assert "LOCAL EVIDENCE" in calls[3][1]
    assert "What fact does the source report?" in calls[3][1]
    assert "synthetic_input_quality" not in goldens[0].additional_metadata


@pytest.mark.parametrize("async_mode", [True, False])
@pytest.mark.parametrize("num_evolutions", [0, 1, 2])
def test_styling_runs_only_after_enabled_evolution_and_preserves_context(
    config, monkeypatch, async_mode, num_evolutions
):
    import asyncio
    from deepeval.models import OpenAIModel

    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    markers = ["EVIDENCE_A", "EVIDENCE_B"]
    stages = []

    def response(self, prompt, schema=None):
        marker = next(m for m in markers if m in prompt)
        assert sum(m in prompt for m in markers) == 1
        if schema and schema.__name__ == "SyntheticDataList":
            data = {
                "data": [
                    {
                        "input": "What is listed in this document?",
                        "used_source_files": [marker],
                        "qa_type": "factual",
                    },
                    # Verify sync and async paths enforce the configured limit.
                    {
                        "input": "Excess question?",
                        "used_source_files": [marker],
                        "qa_type": "factual",
                    },
                ]
            }
        elif schema:
            stages.append(("styling", marker))
            data = {"input": "Which facts does this document list?", "qa_type": "factual"}
        else:
            stage = "answer" if "## Answer_requirements\n" in prompt else "evolution"
            stages.append((stage, marker))
            if stage == "answer":
                query = (
                    "What is listed in this document?"
                    if num_evolutions == 0
                    else "Which facts does this document list?"
                )
                assert f"\nQuery:\n{query}\n" in prompt
            data = "Document facts"
        return (schema.model_validate(data) if schema else data), 0.0

    async def async_response(self, prompt, schema=None):
        await asyncio.sleep(0)
        return response(self, prompt, schema)

    monkeypatch.setattr(OpenAIModel, "generate", response)
    monkeypatch.setattr(OpenAIModel, "a_generate", async_response)
    engine = build_synthesizer(config)
    engine.async_mode = async_mode
    from deepeval.synthesizer import Synthesizer

    def forbidden_filter(*args, **kwargs):
        pytest.fail("Internal input scoring and low-score rewriting must never be invoked")

    monkeypatch.setattr(Synthesizer, "_rewrite_inputs", forbidden_filter)
    monkeypatch.setattr(Synthesizer, "_a_rewrite_inputs", forbidden_filter)
    engine.evolution_config.num_evolutions = num_evolutions
    if num_evolutions == 0:

        def forbidden(*args, **kwargs):
            pytest.fail("Evolution and styling must not be invoked when num_evolutions=0")

        monkeypatch.setattr(engine, "_evolve_input", forbidden)
        monkeypatch.setattr(engine, "_a_evolve_input", forbidden)
        monkeypatch.setattr(local.SynthesizerTemplate, "rewrite_evolved_input", forbidden)
    goldens = engine.generate_goldens_from_contexts(
        contexts=[[marker] for marker in markers],
        max_goldens_per_context=1,
    )
    assert len(goldens) == 2
    assert sorted(g.additional_metadata["used_source_files"][0] for g in goldens) == sorted(markers)
    expected_styling = sorted(markers) if num_evolutions > 0 else []
    assert sorted(marker for stage, marker in stages if stage == "styling") == expected_styling
    assert sorted(marker for stage, marker in stages if stage == "answer") == sorted(markers)
    assert sum(stage == "evolution" for stage, _ in stages) == 2 * num_evolutions
    assert all(len(g.additional_metadata["evolutions"]) == num_evolutions for g in goldens)
    # Scope restoration matters when subsequent generation reuses this worker.
    from doc2qa.synthesis_context import _inputs

    assert _inputs.get() is None


def test_local_prompt_edit_changes_manifest_hash_and_resume_fingerprint(
    config, tmp_path, monkeypatch
):
    from doc2qa import prompts
    from doc2qa.pipeline import fingerprint

    before_prompt = prompts.prompt_sha256()
    before_run = fingerprint(config, [], smoke=False, limit=None)
    project_copy = tmp_path / "doc2qa"
    project_copy.mkdir()
    original = Path(prompts.__file__)
    for path in original.parent.glob("*.py"):
        (project_copy / path.name).write_bytes(path.read_bytes())
    copied_templates = project_copy / "synthesis_templates.py"
    copied_templates.write_text(copied_templates.read_text() + "\n# Local prompt edit\n")
    monkeypatch.setattr(prompts, "__file__", str(project_copy / "prompts.py"))
    from doc2qa import pipeline

    monkeypatch.setattr(pipeline, "__file__", str(project_copy / "pipeline.py"))
    assert prompts.prompt_sha256() != before_prompt
    assert fingerprint(config, [], smoke=False, limit=None) != before_run


def test_generation_styling_and_evolution_require_standalone_queries():
    import json

    context = "In Cairo, Sadat Metro station is near the Egyptian Museum."
    initial = local.SynthesizerTemplate.generate_synthetic_inputs(context, 3)
    assert "understandable on their own without seeing Context" in initial
    assert "never invent an entity name" in initial.lower()
    examples = json.JSONDecoder().raw_decode(initial.split("Example JSON:\n", 1)[1])[0]
    assert len(examples["data"]) == 2
    assert all("Einstein" in item["input"] for item in examples["data"])
    assert "photoelectric" not in examples["data"][0]["input"]
    assert "1921" not in examples["data"][1]["input"]
    styling = local.SynthesizerTemplate.rewrite_evolved_input(
        "When did this bridge open?", context=context
    )
    assert "Make the query understandable without Context or prior conversation" in styling
    assert "stand alone without Context" in local.EvolutionTemplate.base_instruction
    assert not hasattr(local, "FilterTemplate")
    assert not hasattr(local.SynthesizerTemplate, "rewrite_synthetic_inputs")
