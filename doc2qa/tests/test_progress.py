import asyncio
import json
import logging

import pytest

from doc2qa.pipeline import generate
from doc2qa.progress import RunProgress, context_scope, current_progress
from doc2qa.storage import read_jsonl
from doc2qa.synthesis import generate_context


@pytest.mark.parametrize("num_evolutions", [0, 1])
def test_real_synthesis_logs_outputs_and_concurrent_item_identity_without_filtering(
    tmp_path, bundle, config, monkeypatch, caplog, capsys, num_evolutions
):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    from deepeval.models import OpenAIModel

    config.synthesis.max_goldens_per_context = 3
    config.synthesis.num_evolutions = num_evolutions

    async def response(self, prompt, schema=None):
        await asyncio.sleep(0)
        if schema:
            data = {
                # Fewer questions than requested, with duplicates to exercise item identity.
                "SyntheticDataList": {
                    "data": [
                        {"input": "Question?", "qa_type": "factual"},
                        {"input": "Question?", "qa_type": "factual"},
                    ]
                },
                "SyntheticData": {"input": "Styled question?", "qa_type": "factual"},
            }
            output = schema.model_validate(data[schema.__name__])
        else:
            output = "Model text output"
        return output, self.calculate_cost(100, 20)

    monkeypatch.setattr(OpenAIModel, "a_generate", response)
    _, chunks, contexts = bundle
    with RunProgress(tmp_path) as progress, context_scope(contexts[0].context_id):
        goldens, _ = generate_context(contexts[0], chunks, config)
        snapshot = json.loads((tmp_path / "progress.json").read_text())
        assert snapshot["contexts"][contexts[0].context_id]["inputs_used"] == 2
        assert "inputs_checked" not in snapshot["contexts"][contexts[0].context_id]
        assert "inputs_meeting_threshold" not in snapshot["contexts"][contexts[0].context_id]
        assert snapshot["contexts"][contexts[0].context_id]["answers_generated"] == 2
        assert "final_accepted" not in snapshot["contexts"][contexts[0].context_id]
        assert not progress.snapshot["active_operations"]
    assert len(goldens) == 2
    events = read_jsonl(tmp_path / "events.jsonl")
    completed = [e for e in events if e["status"] == "completed"]
    assert all(e["context_id"] == contexts[0].context_id for e in completed if e["stage"] != "run")
    for stage in ("question_evolution", "question_styling", "answer_generation"):
        items = [e for e in completed if e["stage"] == stage]
        expected = (
            []
            if stage in {"question_evolution", "question_styling"} and num_evolutions == 0
            else [1, 2]
        )
        assert sorted(e["item_index"] for e in items) == expected
        assert all(e["output"] for e in items)
    assert not any(e["stage"] in {"input_quality", "input_rewrite"} for e in events)
    answers = [e for e in completed if e["stage"] == "answer_generation"]
    expected_question = "Question?" if num_evolutions == 0 else "Styled question?"
    assert all(e["question"] == expected_question for e in answers)
    assert not any("0%" in record.message for record in caplog.records)
    assert "Generating up to" not in capsys.readouterr().out
    assert "内部质量" not in (tmp_path / "run.log").read_text()
    intermediate = tmp_path / "intermediate" / contexts[0].context_id
    assert len(read_jsonl(intermediate / "initial_questions.jsonl")) == 2
    assert not (intermediate / "internal_quality.jsonl").exists()
    saved = read_jsonl(intermediate / "generated_qa.jsonl")
    assert sorted(row["item_index"] for row in saved) == [1, 2]
    assert all(row["review_status"] == "not_validated" for row in saved)
    assert all(row["question"] == expected_question for row in saved)


def test_initial_questions_survive_later_synthesis_failure(tmp_path, config, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    from deepeval.models import OpenAIModel

    async def response(self, prompt, schema=None):
        if schema and schema.__name__ == "SyntheticDataList":
            return schema.model_validate(
                {"data": [{"input": "Saved initial question?", "qa_type": "factual"}]}
            ), 0.0
        raise RuntimeError("Answer service unavailable")

    monkeypatch.setattr(OpenAIModel, "a_generate", response)
    config.context.strategy = "whole_document"
    config.output = str(tmp_path)
    directory, manifest = generate(config, limit=1)
    assert manifest["status"] == "partial"
    events = read_jsonl(directory / "events.jsonl")
    initial = next(
        e for e in events if e["stage"] == "input_generation" and e["status"] == "result"
    )
    assert initial["output"][0]["input"] == "Saved initial question?"
    assert any(e["stage"] == "answer_generation" and e["status"] == "failed" for e in events)
    snapshot = json.loads((directory / "progress.json").read_text())
    assert snapshot["status"] == "partial"
    assert not snapshot["active_operations"]
    assert "Answer service unavailable" in (directory / "run.log").read_text()
    assert not list((directory / "checkpoints").glob("*.jsonl"))


def test_interrupt_and_append_on_resume(tmp_path):
    previous_handlers = list(logging.getLogger("doc2qa").handlers)
    progress = RunProgress(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with progress, context_scope("ctx_test"):
            with progress.operation("input_generation", "批量生成初始问题"):
                snapshot = json.loads((tmp_path / "progress.json").read_text())
                assert len(snapshot["active_operations"]) == 1
                raise KeyboardInterrupt
    snapshot = json.loads((tmp_path / "progress.json").read_text())
    assert snapshot["status"] == "interrupted"
    assert not snapshot["active_operations"]
    assert current_progress.get() is None
    assert logging.getLogger("doc2qa").handlers == previous_handlers
    events_before = read_jsonl(tmp_path / "events.jsonl")
    assert any(
        e["status"] == "interrupted" and e["context_id"] == "ctx_test" for e in events_before
    )
    with RunProgress(tmp_path) as resumed:
        resumed.record("context", "restored", "复用检查点")
    events = read_jsonl(tmp_path / "events.jsonl")
    assert events[: len(events_before)] == events_before
    assert len({e["attempt_id"] for e in events}) == 2
