import json
from types import SimpleNamespace

import pytest

from doc2qa.prompts import JUDGE_RULES, prompt_sha256
from doc2qa.quality import JudgeResult, deduplicate, deterministic_checks, validate_qa
from doc2qa.schema import evidence_for
from doc2qa.synthesis import map_golden, rules_for_context


@pytest.fixture
def metro_qa(bundle, config):
    docs, chunks, contexts = bundle
    context = next(
        c
        for c in contexts
        if len(c.chunk_ids) == 1 and "35 stations: 30 surface" in chunks[c.chunk_ids[0]].text
    )
    golden = SimpleNamespace(
        input="How many stations does Cairo Metro Line 1 have according to the source?",
        expected_output="The source lists 35 stations: 30 surface and 5 underground.",
        additional_metadata={"qa_type": "factual"},
    )
    qa = map_golden(golden, context, chunks, docs, config, "run_test", 0)
    return qa, context


class FakeRemote:
    def __init__(self, **scores):
        self.scores = {dimension: 3 for dimension in JUDGE_RULES} | scores
        self.calls = []

    def structured(self, system, payload, schema):
        dimension = next(key for key, rules in JUDGE_RULES.items() if rules == system)
        self.calls.append((dimension, json.loads(payload), schema))
        return schema.model_validate({"score": self.scores[dimension]})


def test_offline_never_auto_passes(metro_qa, bundle, config):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    result = validate_qa(qa, context, chunks, docs, config)
    assert result.review_status == "pending_review"
    assert result.quality_scores == {}


def test_numeric_hallucination_and_forged_quote(metro_qa, bundle, config):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    qa.answer = "It has 999999 stations."
    assert "unsupported_numeric_literal" in deterministic_checks(qa, chunks, docs, context)
    qa.evidence[0].quote = "Invented evidence"
    assert "evidence_quote_not_in_source" in deterministic_checks(qa, chunks, docs, context)


def test_railway_currency_not_inferred(bundle, config):
    docs, chunks, contexts = bundle
    context = next(
        c
        for c in contexts
        if len(c.chunk_ids) == 1 and "Alexandria | 190 | 150" in chunks[c.chunk_ids[0]].text
    )
    qa = map_golden(
        SimpleNamespace(
            input="What is the first-class fare to Alexandria in the railway table?",
            expected_output="190 EGP",
            additional_metadata={"qa_type": "factual"},
        ),
        context,
        chunks,
        docs,
        config,
        "test",
        0,
    )
    assert "unsupported_unit_or_currency" in deterministic_checks(qa, chunks, docs, context)


def test_grounded_candidate_passes_only_with_valid_judge(metro_qa, bundle, config):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    judge = FakeRemote()
    result = validate_qa(qa, context, chunks, docs, config, judge)
    assert result.review_status == "auto_pass"
    assert result.evidence == qa.evidence
    assert result.qa_type == qa.qa_type
    assert result.difficulty == qa.difficulty
    assert result.judge_result == {dimension: 3 for dimension in JUDGE_RULES}
    assert result.quality_scores == {dimension: 1.0 for dimension in JUDGE_RULES} | {"overall": 1.0}
    assert [dimension for dimension, _, _ in judge.calls] == list(JUDGE_RULES)
    for _, payload, schema in judge.calls:
        assert schema is JudgeResult
        assert payload["question"] == qa.question
        assert payload["answer"] == qa.answer
        assert payload["primary_chunk_ids"] == context.chunk_ids
        assert payload["primary_document_ids"] == context.document_ids
        assert payload["language"] == config.language
        assert payload["task_rules"] == rules_for_context(context, docs, config.synthesis)
        assert payload["citations"] == [
            {"chunk_id": ev.chunk_id, "quote": ev.quote} for ev in qa.evidence
        ]
        for chunk_id in context.chunk_ids + context.support_chunk_ids:
            assert any(chunks[chunk_id].text in text for text in payload["evidence"])


def test_judge_receives_only_rules_for_current_document_and_context(bundle, config):
    from pathlib import Path

    docs, chunks, contexts = bundle
    first = next(c for c in contexts if len(c.document_ids) == 1)
    second = next(
        c for c in contexts if len(c.document_ids) == 1 and c.document_ids != first.document_ids
    )
    config.synthesis.document_rules = {
        Path(docs[first.document_ids[0]].path).name: ["FIRST_AUDIENCE_RULE"],
        Path(docs[second.document_ids[0]].path).name: ["SECOND_AUDIENCE_RULE"],
        "unrelated.txt": ["UNRELATED_RULE"],
    }
    config.synthesis.context_rules = {first.context_id: ["EXACT_CONTEXT_RULE"]}
    for context, own, other in (
        (first, "FIRST_AUDIENCE_RULE", "SECOND_AUDIENCE_RULE"),
        (second, "SECOND_AUDIENCE_RULE", "FIRST_AUDIENCE_RULE"),
    ):
        qa = map_golden(
            SimpleNamespace(
                input="What transport information is available for Cairo passengers?",
                expected_output=chunks[context.chunk_ids[0]].text,
                additional_metadata={"qa_type": "factual"},
            ),
            context,
            chunks,
            docs,
            config,
            "rules_test",
            0,
        )
        judge = FakeRemote()
        result = validate_qa(qa, context, chunks, docs, config, judge)
        assert result.review_status == "auto_pass"
        assert len(judge.calls) == 5
        for _, payload, _ in judge.calls:
            assert own in payload["task_rules"]
            assert other not in payload["task_rules"]
            assert "UNRELATED_RULE" not in payload["task_rules"]
            assert ("EXACT_CONTEXT_RULE" in payload["task_rules"]) == (context == first)


def test_context_dependent_queries_receive_standalone_reference_score(metro_qa, bundle, config):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    qa.question = "List the station counts in the above text."
    assert not deterministic_checks(qa, chunks, docs, context)
    judge = FakeRemote(self_containment=1)
    result = validate_qa(qa, context, chunks, docs, config, judge)
    assert result.review_status == "auto_pass"
    assert result.quality_scores["self_containment"] == 0.0
    assert result.quality_scores["overall"] == 0.8
    assert "without Context, prior conversation or the answer" in JUDGE_RULES["self_containment"]


@pytest.mark.parametrize(
    ("scores", "overall", "status"),
    [
        ({"question_clarity": 1}, 0.8, "auto_pass"),
        ({"self_containment": 2, "answer_correctness": 2}, 0.8, "auto_pass"),
        ({"self_containment": 1, "answer_correctness": 2}, 0.7, "rejected"),
        ({"personification": 1}, 0.8, "auto_pass"),
        ({"personification": 1, "answer_correctness": 2}, 0.7, "rejected"),
        ({dimension: 1 for dimension in JUDGE_RULES}, 0.0, "rejected"),
        ({dimension: 2 for dimension in JUDGE_RULES}, 0.5, "rejected"),
        ({dimension: 3 for dimension in JUDGE_RULES}, 1.0, "auto_pass"),
    ],
)
def test_equal_weight_mapping_and_threshold(metro_qa, bundle, config, scores, overall, status):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    result = validate_qa(qa, context, chunks, docs, config, FakeRemote(**scores))
    assert result.quality_scores["overall"] == overall
    assert result.review_status == status
    assert ("below_threshold_overall" in result.reasons) == (status == "rejected")


def test_overall_threshold_is_configurable(metro_qa, bundle, config):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    config.quality.overall_threshold = 0.9
    result = validate_qa(qa, context, chunks, docs, config, FakeRemote(answer_correctness=1))
    assert result.quality_scores["overall"] == 0.8
    assert result.review_status == "rejected"


@pytest.mark.parametrize("failed_call", [1, 3, 5])
def test_incomplete_judging_is_pending_and_retryable(metro_qa, bundle, config, failed_call):
    qa, context = metro_qa
    docs, chunks, _ = bundle

    class FailingRemote(FakeRemote):
        def structured(self, *args):
            if len(self.calls) + 1 == failed_call:
                raise TimeoutError("Judge timed out")
            return super().structured(*args)

    # Revalidation must not retain scores or decisions from an earlier run.
    qa.quality_scores = {"overall": 1.0}
    qa.judge_result = {dimension: 3 for dimension in JUDGE_RULES}
    qa.review_status = "auto_pass"
    result = validate_qa(qa, context, chunks, docs, config, FailingRemote())
    assert result.review_status == "pending_review"
    assert result.judge_result is None
    assert result.quality_scores == {}
    assert "judge_error:TimeoutError" in result.reasons


@pytest.mark.parametrize("score", [0, 4, 1.5, 0.5, 3.0, True, "3", None])
def test_invalid_judge_scores_are_not_accepted(metro_qa, bundle, config, score):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    with pytest.raises(ValueError):
        JudgeResult.model_validate({"score": score})
    result = validate_qa(qa, context, chunks, docs, config, FakeRemote(answer_correctness=score))
    assert result.review_status == "pending_review"
    assert result.quality_scores == {}
    assert "judge_error:ValidationError" in result.reasons


def test_dimension_response_has_only_one_field():
    assert set(JudgeResult.model_json_schema()["properties"]) == {"score"}
    for score in (1, 2, 3):
        assert JudgeResult.model_validate({"score": score}).model_dump() == {"score": score}
    with pytest.raises(ValueError):
        JudgeResult.model_validate({"score": 3, "reasons": []})


def test_dimension_prompts_and_fingerprint(monkeypatch):
    assert set(JUDGE_RULES) == {
        "question_clarity",
        "self_containment",
        "completeness",
        "answer_correctness",
        "personification",
    }
    initial_hash = prompt_sha256()
    for dimension, rules in list(JUDGE_RULES.items()):
        assert f"Evaluate ONLY {dimension}" in rules
        for other in JUDGE_RULES.keys() - {dimension}:
            assert other not in rules
        monkeypatch.setitem(JUDGE_RULES, dimension, rules + "\nChanged rule.")
        assert prompt_sha256() != initial_hash
        monkeypatch.setitem(JUDGE_RULES, dimension, rules)


def test_local_failures_skip_all_judge_requests(metro_qa, bundle, config):
    qa, context = metro_qa
    docs, chunks, _ = bundle
    qa.evidence[0].quote = "Not in the document"
    judge = FakeRemote()
    result = validate_qa(qa, context, chunks, docs, config, judge)
    assert result.review_status == "rejected"
    assert "evidence_quote_not_in_source" in result.reasons
    assert judge.calls == []


def test_cross_document_requires_both_primary_sources(bundle, config):
    docs, chunks, contexts = bundle
    context = next(c for c in contexts if len(c.document_ids) > 1)
    chunk = chunks[context.chunk_ids[0]]
    qa = map_golden(
        SimpleNamespace(
            input="According to the source, what does this transport document report?",
            expected_output=chunk.text,
            additional_metadata={"qa_type": "factual"},
        ),
        context,
        chunks,
        docs,
        config,
        "test",
        0,
    )
    qa.evidence = [evidence_for(chunk, docs[chunk.document_id])]
    qa.source_document_ids = [chunk.document_id]
    result = validate_qa(qa, context, chunks, docs, config, FakeRemote())
    assert result.review_status == "rejected"
    assert "cross_document_evidence_incomplete" in result.reasons


def test_exact_dedup_and_different_line_numbers(metro_qa, config):
    qa, _ = metro_qa
    qa.review_status = "auto_pass"
    duplicate = qa.model_copy(deep=True)
    duplicate.qa_id = "other"
    duplicate.question = qa.question.upper() + "!"
    other = qa.model_copy(deep=True)
    other.qa_id, other.question = "line2", qa.question.replace("Line 1", "Line 2")
    records = deduplicate([qa, duplicate, other], config)
    assert records[1].review_status == "duplicate"
    assert records[1].duplicate_of == qa.qa_id
    assert records[2].review_status == "auto_pass"


def test_identical_questions_with_conflicting_answers_are_reviewed(metro_qa, config):
    qa, _ = metro_qa
    qa.review_status = "auto_pass"
    conflict = qa.model_copy(deep=True)
    conflict.qa_id, conflict.answer = "conflict", "40 stations"
    result = deduplicate([qa, conflict], config)
    assert all(q.review_status == "pending_review" for q in result)


def test_semantic_dedup_needs_llm_confirmation(metro_qa, config):
    from doc2qa.quality import DuplicateResult

    class SemanticRemote:
        def embed(self, texts, model):
            return [[1.0, 0.0] for _ in texts]

        def structured(self, *args):
            return DuplicateResult(duplicate=False, reason="different facts")

    qa, _ = metro_qa
    qa.review_status = "auto_pass"
    other = qa.model_copy(deep=True)
    other.qa_id = "different_question"
    other.question = "What are the endpoints of Cairo Metro Line 1?"
    result = deduplicate([qa, other], config, SemanticRemote())
    assert all(q.review_status == "auto_pass" for q in result)


def test_evidence_for_rejects_paraphrase(bundle):
    docs, chunks, _ = bundle
    chunk = next(iter(chunks.values()))
    with pytest.raises(ValueError):
        evidence_for(chunk, docs[chunk.document_id], "This is paraphrased.")
