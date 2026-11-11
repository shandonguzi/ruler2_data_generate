import json
from pathlib import Path
from copy import deepcopy

import pytest

from rubric.mtqc import JUDGES
from rubric.mtqc.cli import main, preview, run
from rubric.mtqc.llm_client import LLMClient
from rubric.mtqc.records import Records, config_identity, read_log
from rubric.mtqc.schemas import judge_schema
from rubric.mtqc.validators import normalize_case
from conftest import FakeAPI


@pytest.mark.parametrize("override", [
    {"repair": {"max_rounds": True}}, {"repair": {"max_rounds": -1}},
    {"repair": {"enabled": True}, "evaluation": {"fail_fast": True}},
    {"llm": {"max_attempts": 0}}, {"llm": {"concurrency": True}},
    {"llm": {"temperature": float("inf")}},
    {"llm": {"api_key": " "}}, {"llm": {"api_key": True}},
    {"llm": {"api_url": "https://key@example.com/v1"}},
    {"llm": {"request_parameters": {"messages": []}}},
    {"decision": {"require_factor_score": 2}}, {"decision": {"min_persona_score": True}},
    {"decision": {"min_coherence_score": 2}}, {"decision": {"min_persona_score": 2}},
    {"output": ""}, {"output": True}, {"output": "cases.jsonl"},
    {"output": {"results_jsonl": "results.jsonl"}},
])
def test_config_rejects_unsafe_or_invalid_values(settings, override):
    with pytest.raises(ValueError):
        settings(**override)


def test_preview_never_creates_records(settings):
    config = settings()
    result = preview(config)
    assert result["max_judge_tasks"] == 16 and result["max_api_requests"] == 57
    assert not Path(config["output"]["records_dir"]).exists()


@pytest.mark.parametrize("endpoint,expected", [
    ("https://example.com/v1", "https://example.com/v1/chat/completions"),
    ("https://example.com/v1/", "https://example.com/v1/chat/completions"),
    ("https://example.com/compatible-mode/v1", "https://example.com/compatible-mode/v1/chat/completions"),
    ("https://example.com/v1/chat/completions", "https://example.com/v1/chat/completions"),
    ("https://example.com/v1/chat/completions/", "https://example.com/v1/chat/completions"),
    ("https://example.com/custom/completion", "https://example.com/custom/completion"),
])
def test_base_urls_resolve_to_chat_completions_without_duplicating_path(settings, endpoint, expected):
    config = settings(llm={"api_url": endpoint})
    assert config["llm"]["api_url"] == expected


def test_removed_credential_parameter_is_rejected_without_exposing_it(settings):
    secret = "sk-secret-value-must-not-appear"
    with pytest.raises(ValueError, match="expected mapping with keys") as error:
        settings(llm={"api_key_env": secret})
    assert secret not in str(error.value)


@pytest.mark.parametrize("environment_key", ["", "environment-key"])
@pytest.mark.parametrize("configured_key", [None, "configured-key"])
def test_config_api_key_ignores_environment_and_is_not_persisted(settings, monkeypatch, environment_key, configured_key):
    config = settings(llm={"api_key": configured_key} if configured_key else {})
    monkeypatch.setenv("OPENAI_API_KEY", environment_key)
    client = LLMClient(config, None)
    assert client.api_key == (configured_key or "")
    identity = config_identity(config)
    assert "api_key" not in identity["llm"]
    config["llm"]["api_key"] = "rotated-key"
    assert config_identity(config) == identity


def test_manifest_rejects_config_input_and_prompt_changes(settings, case, tmp_path):
    config = settings()
    run(config, FakeAPI(config))
    for mutate in (
        lambda c: c["llm"].update(api_url="https://different.example/v1"),
        lambda c: c["llm"].update(temperature=1),
        lambda c: c["judges"][JUDGES[0]].update(prompt_sha256="changed"),
        lambda c: c["repair"].update(max_rounds=1),
    ):
        changed = deepcopy(config)
        mutate(changed)
        with pytest.raises(ValueError, match="changed"):
            run(changed, FakeAPI(changed))
    path = Path(config["input"]["path"])
    path.write_text(path.read_text() + "\n")
    with pytest.raises(ValueError, match="changed"):
        run(config, FakeAPI(config))


def test_real_payload_controls_cache_identity(settings, case):
    config = settings()
    original = normalize_case(case)
    def identities(case):
        from rubric.mtqc.orchestrator import build_judge_payload
        with Records(config, "input-hash") as records:
            client = LLMClient(config, records)
            return {name: client.identity(case["case_id"], name, build_judge_payload(case, name), config["judges"][name], judge_schema(name, case)) for name in JUDGES}
    baseline = identities(original)
    for field, changed_names in (("persona", {JUDGES[3]}), ("factors", {JUDGES[0]}), ("seed_qa", {JUDGES[1]}), ("dialogue", set(JUDGES))):
        changed = deepcopy(original)
        if field == "persona":
            changed[field]["description"] = "another persona"
        elif field == "factors":
            changed[field]["general"]["history_turn_count"]["value"] = 8
        elif field == "seed_qa":
            changed[field]["answer"] = "different gold"
        else:
            changed[field]["current"]["assistant"] = "different dialogue"
        new = identities(changed)
        assert {name for name in JUDGES if baseline[name] != new[name]} == changed_names


def test_torn_tail_is_recovered_but_middle_corruption_is_fatal(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"valid":1}\n{"incomplete":')
    with pytest.warns(RuntimeWarning, match="torn"):
        assert read_log(path) == [{"valid": 1}]
    assert path.read_bytes() == b'{"valid":1}\n'
    assert len(list(tmp_path.glob("*.bak"))) == 1
    path.write_text('{"a":1}\nnope\n{"b":2}\n')
    with pytest.raises(ValueError, match="Corrupt"):
        read_log(path)
    path.write_text('{"a":1}')
    assert read_log(path) == [{"a": 1}]
    assert path.read_bytes().endswith(b"\n")


def test_directory_lock(settings):
    config = settings()
    with Records(config, "input-hash"):
        with pytest.raises(ValueError, match="Another process"):
            Records(config, "input-hash")


def test_export_does_not_call_llm(settings, tmp_path):
    config = settings()
    results = run(config, FakeAPI(config))
    assert main(["export", "--config", str(tmp_path / "config.yaml")]) == 0
    assert json.loads(Path(config["output"]["results_jsonl"]).read_text()) == results[0]
    assert Path(config["output"]["report_path"]).exists()


def test_wire_schema_retains_property_names_matching_constraint_keywords():
    from rubric.mtqc.llm_client import _wire_schema
    schema = {"type": "object", "properties": {"pattern": {"type": "string", "minLength": 1}}, "required": ["pattern"], "additionalProperties": False}
    assert _wire_schema(schema)["properties"] == {"pattern": {"type": "string"}}
