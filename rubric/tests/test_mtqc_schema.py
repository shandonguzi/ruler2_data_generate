from copy import deepcopy

import pytest

from rubric.mtqc import JUDGES
from rubric.mtqc.cli import preview, run
from rubric.mtqc.orchestrator import build_judge_payload
from rubric.mtqc.validators import normalize_case, parse_json, validate_judge_result
from conftest import FakeAPI, business_result


def test_normalization_and_minimal_payloads(case):
    normalized = normalize_case(case)
    assert normalized["factors"]["general"]["history_turn_count"]["required"] is True
    assert "required" not in case["factors"]["general"]["history_turn_count"]
    keys = [{"factors", "dialogue"}, {"seed_qa", "dialogue"}, {"dialogue"}, {"persona", "dialogue"}]
    for name, expected in zip(JUDGES, keys):
        assert set(build_judge_payload(normalized, name)) == expected


@pytest.mark.parametrize("mutate", [
    lambda c: c["factors"]["general"]["history_turn_count"].update(required=False),
    lambda c: c["factors"]["general"]["history_turn_count"].update(required=1),
    lambda c: c["dialogue"]["current"].update(turn_id=True),
    lambda c: c["dialogue"]["current"].update(turn_id=2),
    lambda c: c["dialogue"]["current"].update(user=" "),
    lambda c: c.update(unknown="oops"),
    lambda c: c["dialogue"]["current"].update(role="system"),
])
def test_invalid_case(case, mutate):
    mutate(case)
    with pytest.raises(ValueError):
        normalize_case(case)


def test_count_mismatch_is_a_quality_problem(case):
    case["factors"]["general"]["history_turn_count"]["value"] = 8
    assert normalize_case(case)["dialogue"]["history"] == []


@pytest.mark.parametrize("section", ["general", "test"])
def test_optional_factor_excluded(case, section):
    case["factors"][section]["optional"] = {"value": "x", "definition": "optional", "required": False}
    case = normalize_case(case)
    original = deepcopy(case)
    payload = build_judge_payload(case, JUDGES[0])
    assert payload["factors"] == {
        "general": {"history_turn_count": case["factors"]["general"]["history_turn_count"]},
        "test": {},
    }
    assert case == original
    result = business_result(JUDGES[0], payload)
    validate_judge_result(JUDGES[0], result, case)
    assert result["failed_factors"] == []


@pytest.mark.parametrize("score", [1, 3])
def test_factor_failure_details(case, score):
    case = normalize_case(case)
    result = business_result(JUDGES[0], build_judge_payload(case, JUDGES[0]), score)
    validate_judge_result(JUDGES[0], result, case)
    assert set(result) == {"score", "reason", "evidence_turn_ids", "failed_factors"}
    assert result["failed_factors"] == ([] if score == 3 else [
        {"key": "general.history_turn_count", "expected": 0, "reason": "needs repair"},
    ])


@pytest.mark.parametrize("mutate", [
    lambda r: r.update(score=2), lambda r: r.update(score=True), lambda r: r.update(score=3.0),
    lambda r: r.update(score=3),
    lambda r: r.update(failed_factors=[]),
    lambda r: r["failed_factors"].append(deepcopy(r["failed_factors"][0])),
    lambda r: r["failed_factors"].append({**r["failed_factors"][0], "reason": "another reason"}),
    lambda r: r["failed_factors"][0].update(expected=False),
    lambda r: r["failed_factors"][0].update(expected=0.0),
    lambda r: r["failed_factors"][0].update(expected=1),
    lambda r: r.update(failed_factors=[{"key": "general.history_turn_count"}]),
    lambda r: r.update(failed_factors=["general.history_turn_count"]),
    lambda r: r["failed_factors"][0].update(key="test.unknown"),
    lambda r: r["failed_factors"][0].update(reason=" "),
    lambda r: r["failed_factors"][0].update(passed=False),
    lambda r: r.update(evidence_turn_ids=[100]),
    lambda r: r.update(factor_checks=[]),
    lambda r: r.update(unexpected="field"),
    lambda r: r.update(reason=" "),
])
def test_factor_contract(case, mutate):
    case = normalize_case(case)
    result = business_result(JUDGES[0], build_judge_payload(case, JUDGES[0]), 1)
    mutate(result)
    with pytest.raises(ValueError):
        validate_judge_result(JUDGES[0], result, case)


def test_composite_factor_values(case):
    case["factors"]["test"]["complex"] = {"value": {"domains": ["rail", "bus"], "count": 2, "flag": True}, "definition": "all"}
    case = normalize_case(case)
    result = business_result(JUDGES[0], build_judge_payload(case, JUDGES[0]), 1)
    result["failed_factors"].append({"key": "test.complex", "expected": deepcopy(case["factors"]["test"]["complex"]["value"]),
                                     "reason": "requirements unmet"})
    validate_judge_result(JUDGES[0], result, case)
    result["failed_factors"][-1]["expected"]["flag"] = 1
    with pytest.raises(ValueError):
        validate_judge_result(JUDGES[0], result, case)


@pytest.mark.parametrize("score", [1, 2, 3])
@pytest.mark.parametrize("name", JUDGES[1:])
def test_judge_only_requires_score_and_reason(case, name, score):
    case = normalize_case(case)
    result = business_result(name, build_judge_payload(case, name), score)
    assert result == {"score": score, "reason": "reason"}
    validate_judge_result(name, result, case)


@pytest.mark.parametrize("result", [
    {"score": 0, "reason": "reason"},
    {"score": True, "reason": "reason"},
    {"score": 3.0, "reason": "reason"},
    {"score": 3, "reason": " "},
    {"score": 3},
    {"reason": "reason"},
    *[{"score": 3, "reason": "reason", key: value} for key, value in {
        "evidence_turn_ids": [], "resolved_final_question": "question",
        "seed_intent_preserved": True, "seed_answer_preserved": True,
        "current_intents": ["intent"], "issues": [],
        "persona_conflicts": [], "naturalness_issues": [],
    }.items()],
])
@pytest.mark.parametrize("name", JUDGES[1:])
def test_score_reason_judge_rejects_invalid_or_extra_fields(case, name, result):
    with pytest.raises(ValueError):
        validate_judge_result(name, result, normalize_case(case))


def test_duplicate_ids_and_missing_factors_never_call_models(settings, case):
    bad = deepcopy(case)
    bad["case_id"] = "empty-factor"
    bad["factors"] = {"general": {}, "test": {}}
    config = settings([case, case, bad, {"dialogue": {}}])
    api = FakeAPI(config)
    assert preview(config)["valid_cases"] == 0
    results = run(config, api)
    assert not api.calls
    for result in results:
        assert result["overall_decision"] == "INPUT_ERROR"
        assert result["original_case"] is None and result["revisions"] == []
        assert result["initial_scores"] == dict.fromkeys(JUDGES)
        assert not result["eligible_for_ingestion"]
    assert results[-1]["case_id"] is None


def test_strict_json_parser():
    for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
        with pytest.raises(ValueError):
            parse_json(text)


def test_overflow_number_retains_raw_line(settings):
    from pathlib import Path
    config = settings()
    Path(config["input"]["path"]).write_text('{"number":1e10000}\n')
    result = run(config, FakeAPI(config))[0]
    assert result["overall_decision"] == "INPUT_ERROR" and result["raw_input"] == '{"number":1e10000}'
