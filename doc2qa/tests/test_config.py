import os
from pathlib import Path

import pytest

from doc2qa.config import Config, load_config, require_models


MODEL_VARIABLES = ("DEEPEVAL_MODEL", "DEEPEVAL_JUDGE_MODEL", "DEEPEVAL_EMBEDDING_MODEL")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    # dotenv writes directly to os.environ; isolate those writes between tests too.
    monkeypatch.setattr(os, "environ", os.environ.copy())


def test_dotenv_models_are_loaded_and_environment_takes_precedence(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in MODEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "DEEPEVAL_MODEL=local-generator\n"
        "DEEPEVAL_JUDGE_MODEL=local-judge\n"
        "DEEPEVAL_EMBEDDING_MODEL=local-embedding\n"
    )
    monkeypatch.setenv("DEEPEVAL_MODEL", "environment-generator")
    config = load_config()
    assert config.synthesis.model == "environment-generator"
    assert config.synthesis.judge_model == "local-judge"
    assert config.context.embedding_model == "local-embedding"
    # Snapshots retain resolved names so a resume does not pick up unrelated model changes.
    assert Config.model_validate(config.model_dump()) == config


@pytest.mark.parametrize(
    ("section", "field", "environment"),
    [
        ("synthesis", "model", "DEEPEVAL_MODEL"),
        ("synthesis", "judge_model", "DEEPEVAL_JUDGE_MODEL"),
        ("context", "embedding_model", "DEEPEVAL_EMBEDDING_MODEL"),
    ],
)
def test_yaml_models_have_clear_migration_errors(tmp_path, section, field, environment):
    path = tmp_path / "config.yaml"
    path.write_text(f"{section}:\n  {field}: old-model\n")
    with pytest.raises(ValueError, match=environment):
        load_config(path)


def test_missing_models_do_not_prevent_offline_config_loading(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for name in MODEL_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    config = load_config()
    assert config.context.strategy == "whole_document"
    require_models(config)
    with pytest.raises(ValueError, match="DEEPEVAL_MODEL.*DEEPEVAL_JUDGE_MODEL"):
        require_models(config, generation=True, judge=True)
    with pytest.raises(ValueError, match="DEEPEVAL_EMBEDDING_MODEL"):
        require_models(config, embedding=True)


def test_shipped_yaml_has_no_models_or_domain_keywords():
    root = Path(__file__).resolve().parents[1]
    for name in ("default.yaml", "transport_qa.yaml"):
        config = load_config(root / "configs" / name)
        assert config.context.strategy == "whole_document"
        assert config.quality.overall_threshold == 0.8
        assert config.context.entities == []
        assert config.context.relations == []


def test_empty_optional_base_url_uses_sdk_default(tmp_path, monkeypatch):
    from doc2qa.remote import Remote

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_BASE_URL", "")
    monkeypatch.setenv("OPENAI_API_KEY", "offline-test-key")
    config = load_config()
    remote = Remote(config.synthesis)
    assert str(remote.client.base_url) == "https://api.openai.com/v1/"
    remote.client.close()


def test_document_and_context_rules_load_merge_and_round_trip(tmp_path):
    (tmp_path / "base.yaml").write_text(
        "synthesis:\n"
        "  document_rules:\n"
        "    brt.txt:\n"
        "      - 保留规划状态\n"
        "    metro.txt:\n"
        "      - 保留线路名称\n",
        encoding="utf-8",
    )
    path = tmp_path / "task.yaml"
    path.write_text(
        "extends: base.yaml\n"
        "synthesis:\n"
        "  document_rules:\n"
        "    metro.txt: []\n"
        "  context_rules:\n"
        "    ctx_example:\n"
        "      - 只提问当前证据支持的票价\n",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.synthesis.document_rules == {"brt.txt": ["保留规划状态"], "metro.txt": []}
    assert config.synthesis.context_rules == {"ctx_example": ["只提问当前证据支持的票价"]}
    assert Config.model_validate_json(config.model_dump_json()) == config


@pytest.mark.parametrize("field", ["document_rules", "context_rules"])
@pytest.mark.parametrize("rules", [{"file.txt": "not-a-list"}, {"file.txt": ["  "]}, {" ": []}])
def test_invalid_rule_configuration_fails_before_generation(field, rules):
    with pytest.raises(ValueError):
        Config.model_validate({"synthesis": {field: rules}})


def test_quality_uses_one_overall_threshold():
    config = Config()
    assert config.quality.overall_threshold == 0.8
    for threshold in (-0.1, 1.1):
        with pytest.raises(ValueError):
            Config.model_validate({"quality": {"overall_threshold": threshold}})
    # Old per-dimension thresholds must fail visibly instead of being silently ignored.
    with pytest.raises(ValueError):
        Config.model_validate({"quality": {"groundedness": 0.85}})


def test_removed_internal_quality_threshold_is_rejected():
    with pytest.raises(ValueError):
        Config.model_validate({"synthesis": {"input_quality_threshold": 0.7}})
