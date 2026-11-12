from __future__ import annotations

import copy

import pytest
import yaml

from scopecrawl.config import ConfigError, load_authority, load_settings, resolve_path
from tests.conftest import yaml_config


def _write(tmp_path, data) -> object:
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def test_load_settings_defaults_and_overrides(tmp_path):
    path = _write(tmp_path, yaml_config())
    settings = load_settings(
        path,
        overrides={"s3": "新场景", "language": "en", "location": ""},
        env={},
        require_secrets=False,
    )
    assert settings.run.s3 == "新场景"
    assert settings.run.s4 == "气象灾害查询"
    assert settings.run.language == "en"
    assert settings.run.location is None


def test_keywords_scenarios_path_default_and_override(tmp_path):
    settings = load_settings(_write(tmp_path, yaml_config()), env={}, require_secrets=False)
    assert settings.keywords.scenarios_path == "config/scenarios.json"

    data = yaml_config()
    data["keywords"]["scenarios_path"] = "custom/scenes.json"
    settings = load_settings(_write(tmp_path, data), env={}, require_secrets=False)
    assert settings.keywords.scenarios_path == "custom/scenes.json"


def test_load_settings_requires_secrets(tmp_path):
    path = _write(tmp_path, yaml_config())
    with pytest.raises(ConfigError, match="LLM 模型名"):
        load_settings(path, env={}, require_secrets=True)


def test_load_settings_resolves_env_and_secrets(tmp_path):
    path = _write(tmp_path, yaml_config())
    env = {
        "SCOPECRAWL_TAVILY_API_KEY": "tvly-test",
        "SCOPECRAWL_LLM_MODEL": "test-model",
        "SCOPECRAWL_LLM_API_KEY": "llm-test",
        "SCOPECRAWL_LLM_BASE_URL": "https://llm.example/v1",
    }
    settings = load_settings(path, env=env, require_secrets=True)
    assert settings.llm_model == "test-model"
    assert settings.tavily_api_key.get_secret_value() == "tvly-test"
    assert settings.llm_api_key.get_secret_value() == "llm-test"
    assert settings.llm_base_url == "https://llm.example/v1"


def test_load_settings_rejects_invalid_language(tmp_path):
    data = yaml_config()
    data["run"]["language"] = "fr"
    with pytest.raises(ConfigError, match="language"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_load_settings_rejects_unknown_config_key(tmp_path):
    data = yaml_config()
    data["review"]["llm"] = {"model": "x"}
    with pytest.raises(ConfigError, match="review"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_load_settings_rejects_bad_weights(tmp_path):
    data = yaml_config()
    data["authority"].setdefault("scoring", {})["weights"] = {"domain": 0.9, "page": 0.4}
    with pytest.raises(ConfigError, match="weights"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_load_settings_rejects_empty_scene(tmp_path):
    data = yaml_config()
    data["run"]["s3"] = "  "
    with pytest.raises(ConfigError, match="s3"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_load_settings_rejects_missing_model(tmp_path):
    path = _write(tmp_path, yaml_config())
    env = {
        "SCOPECRAWL_TAVILY_API_KEY": "tvly-test",
        "SCOPECRAWL_LLM_API_KEY": "llm-test",
    }
    with pytest.raises(ConfigError, match="LLM 模型名"):
        load_settings(path, env=env, require_secrets=True)


def test_load_settings_rejects_model_in_yaml(tmp_path):
    data = yaml_config()
    data["llm"]["model"] = "should-not-be-here"
    with pytest.raises(ConfigError, match="llm"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_load_settings_reads_dotenv_file(tmp_path):
    path = _write(tmp_path, yaml_config())
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / ".env").write_text(
        "\n".join(
            [
                "# comment",
                "export SCOPECRAWL_TAVILY_API_KEY='tvly-test'",
                'SCOPECRAWL_LLM_MODEL="test-model"',
                "SCOPECRAWL_LLM_API_KEY=llm-test",
            ]
        ),
        encoding="utf-8",
    )
    settings = load_settings(
        path,
        env=None,
        project_root=tmp_path,
        require_secrets=True,
    )
    assert settings.llm_model == "test-model"
    assert settings.tavily_api_key.get_secret_value() == "tvly-test"
    assert settings.llm_api_key.get_secret_value() == "llm-test"


def test_load_settings_requires_dotenv_file(tmp_path):
    path = _write(tmp_path, yaml_config())
    with pytest.raises(ConfigError, match=".env"):
        load_settings(
            path,
            env=None,
            project_root=tmp_path,
            require_secrets=True,
        )


def test_load_settings_ignores_process_environment(tmp_path, monkeypatch):
    path = _write(tmp_path, yaml_config())
    monkeypatch.setenv("SCOPECRAWL_LLM_MODEL", "from-os-env")
    monkeypatch.setenv("SCOPECRAWL_TAVILY_API_KEY", "from-os-env")
    monkeypatch.setenv("SCOPECRAWL_LLM_API_KEY", "from-os-env")
    with pytest.raises(ConfigError, match="LLM 模型名"):
        load_settings(path, env={}, require_secrets=True)


def test_load_authority_missing_file_returns_empty(tmp_path):
    lists = load_authority(project_root=tmp_path)
    assert lists.whitelist == []
    assert lists.blacklist == []
    assert lists.unfetchable == []


def test_load_authority_parses_entries(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "authority.yaml").write_text(
        yaml.safe_dump(
            {
                "whitelist": [{"domain": "*.gov.eg", "reason": "政府"}],
                "blacklist": [{"domain": "spam.example", "reason": "垃圾站"}],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    lists = load_authority(project_root=tmp_path)
    assert lists.whitelist[0].domain == "*.gov.eg"
    assert lists.blacklist[0].reason == "垃圾站"
    assert lists.unfetchable == []


def test_load_authority_parses_unfetchable(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "authority.yaml").write_text(
        yaml.safe_dump(
            {
                "unfetchable": [
                    {"domain": "youtube.com", "reason": "视频页无可提取正文"}
                ],
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    lists = load_authority(project_root=tmp_path)
    assert lists.unfetchable[0].domain == "youtube.com"
    assert lists.unfetchable[0].reason == "视频页无可提取正文"


def test_load_authority_rejects_overlap(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "authority.yaml").write_text(
        yaml.safe_dump(
            {
                "whitelist": [{"domain": "reuters.com"}],
                "blacklist": [{"domain": "*.reuters.com"}],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="冲突"):
        load_authority(project_root=tmp_path)


def test_config_data_not_mutated_between_calls():
    data = copy.deepcopy(yaml_config())
    assert data["run"]["s3"] == "天气查询"


def test_load_settings_rejects_bad_extract_settings(tmp_path):
    data = yaml_config()
    data["extract"]["batch_size"] = 21
    with pytest.raises(ConfigError, match="batch_size"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)

    data = yaml_config()
    data["extract"]["timeout_seconds"] = 61
    with pytest.raises(ConfigError, match="timeout_seconds"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)

    data = yaml_config()
    data["extract"]["max_link_text_ratio"] = 1.5
    with pytest.raises(ConfigError):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)

    data = yaml_config()
    data["extract"]["provider"] = "other"
    with pytest.raises(ConfigError):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)

    data = yaml_config()
    data["extract"]["timeout_seconds"] = 0.5
    with pytest.raises(ConfigError, match="timeout_seconds"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)

    data = yaml_config()
    data["search"]["provider"] = "other"
    with pytest.raises(ConfigError, match="provider"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)

    data = yaml_config()
    data["search"]["concurrency"] = 0
    with pytest.raises(ConfigError, match="concurrency"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_resolve_path_anchors_relative_to_root(tmp_path):
    assert resolve_path(tmp_path / "a") == tmp_path / "a"
    assert resolve_path("a", root=tmp_path) == tmp_path / "a"


def test_load_settings_accepts_content_and_unevaluated_keys(tmp_path):
    data = yaml_config()
    data["review"] = {
        "enabled": True,
        "drop_low": True,
        "drop_unevaluated": True,
        "content": {
            "richness_low": 0.3,
            "quality_low": 0.5,
            "drop_when_both_low": True,
        },
    }
    settings = load_settings(_write(tmp_path, data), env={}, require_secrets=False)
    assert settings.review.content.richness_low == 0.3
    assert settings.review.content.quality_low == 0.5
    assert settings.review.content.drop_when_both_low is True
    assert settings.review.drop_unevaluated is True


def test_load_settings_rejects_bad_content_thresholds(tmp_path):
    data = yaml_config()
    data["review"] = {"content": {"richness_low": 1.5}}
    with pytest.raises(ConfigError, match="richness_low"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_load_settings_accepts_prefilter_and_diversity(tmp_path):
    data = yaml_config()
    data["search"]["prefilter"] = {"enabled": False}
    data["diversity"] = {"enabled": False, "max_per_query": 3, "max_per_domain": None}
    settings = load_settings(_write(tmp_path, data), env={}, require_secrets=False)
    assert settings.search.prefilter.enabled is False
    assert settings.diversity.enabled is False
    assert settings.diversity.max_per_query == 3
    assert settings.diversity.max_per_domain is None


def test_load_settings_rejects_bad_diversity(tmp_path):
    data = yaml_config()
    data["diversity"] = {"max_per_query": 0}
    with pytest.raises(ConfigError, match="max_per_query"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)


def test_load_settings_rejects_removed_fetch_section(tmp_path):
    data = yaml_config()
    data["fetch"] = {"obey_robots": False}
    with pytest.raises(ConfigError, match="fetch"):
        load_settings(_write(tmp_path, data), env={}, require_secrets=False)
