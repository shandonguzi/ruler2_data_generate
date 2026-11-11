import pytest

from rubric.mtqc import JUDGES
from rubric.mtqc.cli import run
from conftest import FakeAPI, business_result, repair_response


def test_initial_perfect_scores_do_not_repair(settings, capsys):
    config = settings()
    api = FakeAPI(config, lambda name, payload, _: business_result(name, payload))
    result = run(config, api)[0]
    assert sum(api.counts.values()) == 4
    assert result["stop_reason"] == "PASS_INITIAL" and not result["was_revised"]
    assert result["eligible_for_ingestion"]
    assert "触发修复" not in capsys.readouterr().out


@pytest.mark.parametrize("judge,score,initial_decision", [
    (JUDGES[0], 1, "REPAIR"), (JUDGES[1], 1, "REJECT"), (JUDGES[1], 2, "REVIEW"),
    (JUDGES[2], 1, "REPAIR"), (JUDGES[2], 2, "REPAIR"),
    (JUDGES[3], 1, "REPAIR"), (JUDGES[3], 2, "REPAIR"),
])
def test_one_repair_rechecks_every_dimension(settings, case, capsys, judge, score, initial_decision):
    case["dialogue"]["current"]["assistant"] = "bad"
    config = settings([case])
    def callback(name, payload, number):
        if name == "dialogue_repair":
            assert set(payload["latest_evaluation"]["judge_results"]) == set(JUDGES)
            assert payload["original_dialogue"]["current"]["assistant"] == "bad"
            return repair_response(payload)
        return business_result(name, payload, score if name == judge and payload["dialogue"]["current"]["assistant"] == "bad" else 3)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["stop_reason"] == "PASS_AFTER_REPAIR"
    assert result["repair_attempt_count"] == result["repair_count"] == 1
    assert result["was_revised"] and result["final_differs_original"]
    assert result["final_revision_id"] == "r1"
    assert [rev["evaluation"]["quality_decision"] for rev in result["revisions"]] == [initial_decision, "PASS"]
    assert api.counts == {**dict.fromkeys(JUDGES, 2), "dialogue_repair": 1}
    assert result["revisions"][1]["dialogue_diff"][0]["path"] == "/dialogue/current/assistant"
    output = capsys.readouterr().out
    assert output.count("触发修复 1/3") == 1
    label = dict(zip(JUDGES, ("因子", "问答", "连贯", "画像")))[judge]
    assert f"{label} {score}（低于 3 分）" in output
    for revision in result["revisions"]:
        for name in JUDGES[1:]:
            judge_result = revision["evaluation"]["judge_results"][name]
            assert judge_result["evidence_turn_ids"] == []
            assert judge_result["details"] == {}
    for call in api.calls:
        if call[0] in JUDGES:
            assert "latest_evaluation" not in call[1] and "revision_id" not in call[1]
    # A completed run is entirely replayed without another request.
    assert run(config, api)[0] == result
    assert sum(api.counts.values()) == 9
    assert "触发修复" not in capsys.readouterr().out


def test_multiple_low_dimensions_trigger_one_repair_with_all_feedback(settings, capsys):
    config = settings()
    def callback(name, payload, number):
        if name == "dialogue_repair":
            assert payload["latest_evaluation"]["blocking_dimensions"] == list(JUDGES[1:])
            return repair_response(payload, "fixed")
        changed = payload["dialogue"]["current"]["assistant"] == "fixed"
        return business_result(name, payload, 2 if name in JUDGES[1:] and not changed else 3)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["eligible_for_ingestion"] and result["repair_attempt_count"] == 1
    assert api.counts == {**dict.fromkeys(JUDGES, 2), "dialogue_repair": 1}
    assert "触发修复 1/3：问答 2、连贯 2、画像 2（低于 3 分）" in capsys.readouterr().out


def test_full_budget_and_degradation_preserved(settings):
    config = settings()
    def callback(name, payload, number):
        if name == "dialogue_repair":
            assert payload["repair_round"] == number
            assert len(payload["repair_history"]) == number - 1
            return repair_response(payload, answer=f"version-{number}")
        return business_result(name, payload, 1 if name == JUDGES[0] else 3)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["stop_reason"] == "MAX_REPAIR_ROUNDS"
    assert len(result["revisions"]) == 4
    assert result["repair_attempt_count"] == result["repair_count"] == 3
    assert result["final_revision_id"] == "r3" and not result["eligible_for_ingestion"]
    assert sum(api.counts.values()) == 19


@pytest.mark.parametrize("action,expected", [("blocked", "REPAIR_BLOCKED"), ("unchanged", "NO_CHANGE"), ("revised", "NO_CHANGE")])
def test_non_modifying_repairs(settings, action, expected):
    config = settings()
    def callback(name, payload, number):
        return repair_response(payload, action=action) if name == "dialogue_repair" else business_result(name, payload, 2 if name == JUDGES[1] else 3)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["stop_reason"] == expected
    assert result["repair_count"] == 0 and result["repair_attempt_count"] == 1
    assert not result["was_revised"] and len(api.calls) == 5


def test_duplicate_revision_stops_cycle(settings):
    config = settings()
    def callback(name, payload, number):
        if name == "dialogue_repair":
            return repair_response(payload, "changed" if number == 1 else "Z站")
        return business_result(name, payload, 1 if name == JUDGES[0] else 3)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["stop_reason"] == "DUPLICATE_REVISION"
    assert result["repair_count"] == 1 and result["repair_attempt_count"] == 2
    assert result["final_revision_id"] == "r1" and len(api.calls) == 10


def test_immutable_fields_rejected_and_repair_technical_budget_separate(settings):
    config = settings()
    def callback(name, payload, number):
        if name == "dialogue_repair":
            value = repair_response(payload, "fixed")
            if number < 3:
                value["seed_qa"] = {"answer": "cheat"}
            return value
        return business_result(name, payload, 1 if name == JUDGES[0] and payload["dialogue"]["current"]["assistant"] != "fixed" else 3)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["repair_attempt_count"] == 1 and result["repair_count"] == 1
    assert api.counts["dialogue_repair"] == 3 and len(result["repair_attempts"][0]["attempt_ids"]) == 3
    assert result["original_case"]["seed_qa"]["answer"] == "Z站"


def test_repair_regression_cannot_reuse_old_scores(settings):
    config = settings(repair={"max_rounds": 1})
    def callback(name, payload, number):
        if name == "dialogue_repair":
            return repair_response(payload, "changed")
        changed = payload["dialogue"]["current"]["assistant"] == "changed"
        return business_result(name, payload, 1 if ((name == JUDGES[0] and not changed) or (name == JUDGES[1] and changed)) else 3)
    api = FakeAPI(config, callback)
    result = run(config, api)[0]
    assert result["revisions"][0]["evaluation"]["quality_decision"] == "REPAIR"
    assert result["overall_decision"] == "REJECT" and result["stop_reason"] == "MAX_REPAIR_ROUNDS"


@pytest.mark.parametrize("pointer", ["/scores", "/judge_results/unknown/reason", "/judge_results/factor_compliance/details/failed_factors/9", "/judge_results/final_qa_validation/reason/extra"])
def test_invalid_feedback_pointer(settings, pointer):
    config = settings()
    def callback(name, payload, number):
        if name == "dialogue_repair":
            value = repair_response(payload, "changed")
            value["change_summary"][0]["feedback_refs"] = [pointer]
            return value
        return business_result(name, payload, 2 if name == JUDGES[1] else 3)
    result = run(config, FakeAPI(config, callback))[0]
    assert result["stop_reason"] == "REPAIR_ERROR" and result["repair_count"] == 0
