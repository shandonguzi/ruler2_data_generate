import pytest

from rubric.mtqc import JUDGES
from rubric.mtqc.cli import run
from rubric.mtqc.orchestrator import Orchestrator
from conftest import FakeAPI, business_result, repair_response


def repairing_api(config):
    def callback(name, payload, number):
        if name == "dialogue_repair":
            return repair_response(payload, "fixed")
        return business_result(name, payload, 1 if name == JUDGES[0] and payload["dialogue"]["current"]["assistant"] != "fixed" else 3)
    return FakeAPI(config, callback)


def test_resume_saved_candidate_without_regenerating(settings, monkeypatch):
    config = settings()
    api = repairing_api(config)
    original = Orchestrator.evaluate_revision
    def interrupt(self, case, revision, input_id):
        if revision["revision_id"] == "r1":
            raise KeyboardInterrupt()
        return original(self, case, revision, input_id)
    with monkeypatch.context() as patch:
        patch.setattr(Orchestrator, "evaluate_revision", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(config, api)
    assert len(api.calls) == 5
    result = run(config, api)[0]
    assert result["stop_reason"] == "PASS_AFTER_REPAIR"
    assert result["repair_attempt_count"] == result["repair_count"] == 1
    assert api.counts["dialogue_repair"] == 1 and len(api.calls) == 9


def test_resume_between_applied_event_and_revision_append(settings, monkeypatch):
    config = settings()
    api = repairing_api(config)
    original = Orchestrator._apply_saved_revisions
    interrupted = False
    def interrupt(self, input_id):
        nonlocal interrupted
        if any(event.get("status") == "APPLIED" for event in self.records.select("repair_attempts")) and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return original(self, input_id)
    with monkeypatch.context() as patch:
        patch.setattr(Orchestrator, "_apply_saved_revisions", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(config, api)
    result = run(config, api)[0]
    assert result["repair_count"] == result["repair_attempt_count"] == 1
    assert api.counts["dialogue_repair"] == 1 and len(api.calls) == 9


def test_resume_after_response_saved_before_validation(settings, monkeypatch):
    config = settings()
    api = FakeAPI(config)
    from rubric.mtqc.llm_client import LLMClient
    original = LLMClient._parse
    interrupted = False
    def interrupt(self, raw, schema, validator):
        nonlocal interrupted
        if "failed_factors" in schema.get("properties", {}) and not interrupted:
            interrupted = True
            raise KeyboardInterrupt()
        return original(self, raw, schema, validator)
    with monkeypatch.context() as patch:
        patch.setattr(LLMClient, "_parse", interrupt)
        with pytest.raises(KeyboardInterrupt):
            run(config, api)
    assert len(api.calls) == 4
    result = run(config, api)[0]
    assert result["eligible_for_ingestion"]
    assert len(api.calls) == 4


def test_interrupted_attempt_consumes_budget(settings):
    config = settings(llm={"max_attempts": 2})
    def callback(name, payload, number):
        if name == JUDGES[0]:
            return KeyboardInterrupt() if number == 1 else TimeoutError("timeout")
        return business_result(name, payload)
    api = FakeAPI(config, callback)
    with pytest.raises(KeyboardInterrupt):
        run(config, api)
    result = run(config, api)[0]
    assert api.counts[JUDGES[0]] == 2
    assert result["stop_reason"] == "JUDGE_ERROR"
    run(config, api)
    assert api.counts[JUDGES[0]] == 2


def test_pending_repair_is_one_logical_task(settings):
    config = settings()
    api = repairing_api(config)
    callback = api.callback
    interrupted = False
    def once(name, payload, number):
        nonlocal interrupted
        if name == "dialogue_repair" and not interrupted:
            interrupted = True
            return KeyboardInterrupt()
        return callback(name, payload, number)
    api.callback = once
    with pytest.raises(KeyboardInterrupt):
        run(config, api)
    result = run(config, api)[0]
    assert result["repair_attempt_count"] == result["repair_count"] == 1
    assert api.counts["dialogue_repair"] == 2
    assert len(result["repair_attempts"][0]["attempt_ids"]) == 2
