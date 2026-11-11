from pathlib import Path

import pytest

from rubric.mtqc import JUDGES
from rubric.mtqc.cli import run
from rubric.mtqc.records import Records
from rubric.mtqc.report import batch_statistics
from conftest import FakeAPI, business_result, repair_response


def test_report_uses_case_denominators_and_does_not_convert_null_to_one(settings):
    config = settings()
    def callback(name, payload, number):
        if name == JUDGES[0]:
            return TimeoutError("network")
        return business_result(name, payload)
    results = run(config, FakeAPI(config, callback))
    with Records(config, create=False) as records:
        stats = batch_statistics(results, records)
    assert stats["case_count"] == 1
    assert stats["initial"]["complete_cases"] == 0
    assert stats["initial"]["score_distributions"][JUDGES[0]]["partial_or_error_cases"] == {"null": 1}
    assert stats["initial"]["factor_pass_rate"]["denominator"] == 0
    assert stats["initial"]["cohort_rates"]["complete"]["case_count"] == 0
    assert stats["initial"]["cohort_rates"]["partial_or_error"]["case_count"] == 1
    assert stats["initial"]["final_qa_pass_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert stats["initial"]["cohort_rates"]["partial_or_error"]["final_qa_pass_rate"]["rate"] == 1.0
    assert stats["api"]["judge"]["requests"] == 6
    assert stats["api"]["judge"]["tokens"]["total_tokens"] == 45


def test_repair_statistics_count_samples_once_and_report_escapes_html(settings, case):
    case["case_id"] = '<script>alert("x")</script>'
    case["factors"]["test"] = {
        "retained": {"value": True, "definition": "must hold"},
        "optional": {"value": "x", "definition": "optional", "required": False},
    }
    config = settings([case])
    def callback(name, payload, number):
        if name == "dialogue_repair":
            assert payload["latest_evaluation"]["failed_factors"] == [
                {"key": "general.history_turn_count", "expected": 0, "reason": "needs repair"},
            ]
            response = repair_response(payload, "fixed")
            response["change_summary"][0]["feedback_refs"] = [
                "/judge_results/factor_compliance/details/failed_factors/0/reason",
            ]
            return response
        return business_result(name, payload, 1 if name == JUDGES[0] and payload["dialogue"]["current"]["assistant"] != "fixed" else 3)
    results = run(config, FakeAPI(config, callback))
    with Records(config, create=False) as records:
        stats = batch_statistics(results, records)
    assert stats["case_count"] == 1
    assert stats["repair"]["success_rate"] == {"numerator": 1, "denominator": 1, "rate": 1.0}
    assert stats["initial"]["factor_pass_rate"]["rate"] == 0
    assert stats["final"]["factor_pass_rate"]["rate"] == 1
    assert stats["initial"]["factor_failure_rates"] == {
        "general.history_turn_count": {"numerator": 1, "denominator": 1, "rate": 1.0},
        "test.retained": {"numerator": 0, "denominator": 1, "rate": 0.0},
    }
    assert stats["final"]["factor_failure_rates"] == {
        key: {"numerator": 0, "denominator": 1, "rate": 0.0}
        for key in ("general.history_turn_count", "test.retained")
    }
    assert stats["api"]["judge"]["requests"] == 8 and stats["api"]["repair"]["requests"] == 1
    assert '<script>' not in Path(config["output"]["report_path"]).read_text()


@pytest.mark.parametrize("score", [1, 2, 3, None])
def test_final_qa_pass_rate_uses_scores_and_excludes_errors(settings, score):
    config = settings(repair={"enabled": False})
    def callback(name, payload, number):
        if name == JUDGES[1]:
            return TimeoutError("network") if score is None else {"score": score, "reason": "reason"}
        return business_result(name, payload)
    results = run(config, FakeAPI(config, callback))
    with Records(config, create=False) as records:
        stats = batch_statistics(results, records)
    expected = {"numerator": int(score == 3), "denominator": int(score is not None),
                "rate": None if score is None else float(score == 3)}
    for stage in ("initial", "final"):
        assert "issue_types" not in stats[stage]
        assert stats[stage]["final_qa_pass_rate"] == expected
        assert "seed_answer_preservation" not in stats[stage]
        cohort = "partial_or_error" if score is None else "complete"
        assert stats[stage]["cohort_rates"][cohort]["final_qa_pass_rate"] == expected
