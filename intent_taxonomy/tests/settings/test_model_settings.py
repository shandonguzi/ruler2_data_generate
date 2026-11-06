from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from fastapi import HTTPException
from pydantic import ValidationError

from intent_engine.settings.config import load_config, model_settings_snapshot, save_model_settings
import web_server as web


@pytest.fixture
def model_config(tmp_path, monkeypatch):
    path = tmp_path / 'config' / 'config.yaml'
    path.parent.mkdir()
    path.write_text('''# workspace config
llm:
  base_url: "http://localhost:8000/v1" # local server
  model: "old-model"
  api_key: "saved-secret"
  max_tokens: 1600
  timeout: 120
# Embedding comment
embedding:
  base_url: ""
  model: ""
  api_key_env: "INTENT_EMBEDDING_API_KEY"
  cache_version: "1"
# Preserve pipeline
pipeline:
  text_column: "用户提问"
paths:
  reports_dir: data/reports
  checkpoint_dir: data/checkpoints
''', encoding='utf-8')
    monkeypatch.setattr(web, 'CONFIG_PATH', path)
    monkeypatch.setattr(web, 'jobs', web.JobManager())
    return path


def request_for(path, **overrides):
    data = model_settings_snapshot(path)
    for name in ('llm', 'embedding'):
        data[name].pop('api_key_configured')
    data.update(overrides)
    return web.ModelSettingsRequest.model_validate(data)


def test_save_updates_file_and_runtime_without_exposing_keys(model_config):
    req = request_for(model_config)
    req.llm.model = 'new-chat-model'
    req.embedding.base_url = 'http://localhost:8001/v1'
    req.embedding.model = 'new-embedding-model'
    req.embedding.api_key = web.SecretStr('new-secret')
    before = yaml.safe_load(model_config.read_text())
    response = web.api_save_model_settings(req)
    after = yaml.safe_load(model_config.read_text())
    assert after['llm']['model'] == 'new-chat-model'
    assert after['llm']['api_key'] == 'saved-secret'
    assert after['embedding']['api_key'] == 'new-secret'
    assert after['pipeline'] == before['pipeline']
    assert after['paths'] == before['paths']
    assert after['llm']['timeout'] == 120
    assert after['embedding']['cache_version'] == '1'
    assert '# workspace config' in model_config.read_text()
    assert '# local server' in model_config.read_text()
    assert '# Preserve pipeline' in model_config.read_text()
    assert 'saved-secret' not in json.dumps(response)
    assert 'new-secret' not in json.dumps(web.api_model_settings())
    assert response['embedding']['api_key_configured']
    assert load_config(str(model_config))['llm']['model'] == 'new-chat-model'
    assert web._cfg()['embedding']['model'] == 'new-embedding-model'


def test_stale_revision_or_active_job_cannot_overwrite(model_config):
    req = request_for(model_config)
    original = model_config.read_text()
    model_config.write_text(original + '\n# changed outside browser\n')
    with pytest.raises(HTTPException) as error:
        web.api_save_model_settings(req)
    assert error.value.status_code == 409
    assert model_config.read_text().endswith('# changed outside browser\n')
    req = request_for(model_config)
    web.jobs.jobs['active'] = web.Job('active', 'discover', [], status='running')
    with pytest.raises(HTTPException) as error:
        web.api_save_model_settings(req)
    assert error.value.status_code == 409
    assert model_config.read_text().endswith('# changed outside browser\n')


def test_key_clear_is_explicit_and_omitted_key_is_preserved(model_config):
    req = request_for(model_config)
    req.llm.clear_api_key = True
    response = web.api_save_model_settings(req)
    assert not response['llm']['api_key_configured']
    assert yaml.safe_load(model_config.read_text())['llm']['api_key'] == ''


def test_partial_embedding_settings_rejected(model_config):
    req = request_for(model_config)
    req.embedding.model = 'only-model'
    original = model_config.read_bytes()
    with pytest.raises(HTTPException) as error:
        web.api_save_model_settings(req)
    assert error.value.status_code == 400
    assert model_config.read_bytes() == original


@pytest.mark.parametrize('url', ['file:///tmp/model', 'http://user:password@localhost/v1', 'http://localhost:bad/v1'])
def test_bad_url_is_rejected(url):
    with pytest.raises(ValidationError):
        web.ModelEndpointRequest(base_url=url, model='model')


@pytest.mark.parametrize('initial', ['llm: {model: old, timeout: 12}\n', 'llm: {}\n', 'llm: null\n'])
def test_missing_embedding_and_inline_sections_can_be_updated(tmp_path, initial):
    path = tmp_path / 'config.yaml'
    path.write_text(initial+'pipeline: {text_column: text}\n')
    state = model_settings_snapshot(path)
    save_model_settings(path, state['revision'], {'llm': {'model': 'new', 'base_url': 'http://localhost/v1'},
                                                'embedding': {'model': '', 'base_url': ''}})
    cfg = yaml.safe_load(path.read_text())
    assert cfg['llm']['model'] == 'new'
    assert cfg['embedding']['model'] == ''
    assert cfg['pipeline']['text_column'] == 'text'


def test_failed_atomic_replace_preserves_original(model_config, monkeypatch):
    import intent_engine.settings.config as module
    original = model_config.read_bytes()
    def fail(*args):
        raise OSError('disk error')
    monkeypatch.setattr(module.os, 'replace', fail)
    with pytest.raises(OSError):
        save_model_settings(model_config, model_settings_snapshot(model_config)['revision'], {'llm': {'model': 'new'}})
    assert model_config.read_bytes() == original
    assert list(model_config.parent.glob('.model-config-*')) == []


def test_llm_uses_environment_key(monkeypatch):
    import intent_engine.infrastructure.llm_client as module
    seen = {}
    monkeypatch.setenv('CUSTOM_LLM_KEY', 'from-env')
    monkeypatch.setattr(module, 'OpenAI', lambda **kwargs: seen.update(kwargs))
    module.LocalLLM({'base_url': 'http://localhost/v1', 'model': 'model', 'api_key': 'from-file', 'api_key_env': 'CUSTOM_LLM_KEY'})
    assert seen['api_key'] == 'from-env'


@pytest.fixture
def model_probe(model_config, monkeypatch):
    from types import SimpleNamespace
    import openai
    calls = {'chat': [], 'embedding': [], 'clients': []}
    class FakeClient:
        def __init__(self, **kwargs):
            calls['clients'].append(kwargs)
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.chat_create))
            self.embeddings = SimpleNamespace(create=self.embedding_create)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def chat_create(self, **kwargs):
            calls['chat'].append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='OK'))])
        def embedding_create(self, **kwargs):
            calls['embedding'].append(kwargs)
            return SimpleNamespace(data=[SimpleNamespace(index=0, embedding=[.1, .2, .3])])
    monkeypatch.setattr(openai, 'OpenAI', FakeClient)
    for env in ('INTENT_LLM_API_KEY', 'INTENT_EMBEDDING_API_KEY'):
        monkeypatch.delenv(env, raising=False)
    return model_config, calls, FakeClient


def test_probe_uses_unsaved_form_and_does_not_write(model_probe):
    path, calls, _ = model_probe
    original = path.read_bytes()
    req = web.ModelEndpointRequest(base_url='http://other.test/v1', model='unsaved-chat')
    result = web.api_check_model('llm', req)
    assert result['ok'] and result['model'] == 'unsaved-chat'
    assert calls['chat'][0]['model'] == 'unsaved-chat'
    assert calls['clients'][0]['base_url'] == 'http://other.test/v1'
    import os
    assert 'other.test' in os.environ['NO_PROXY'].split(',')
    assert os.environ['NO_PROXY'] == os.environ['no_proxy']
    assert calls['clients'][0]['api_key'] == 'saved-secret'
    assert calls['clients'][0]['max_retries'] == 0
    assert calls['clients'][0]['timeout'] == 15
    assert path.read_bytes() == original
    assert 'saved-secret' not in json.dumps(result)
    result = web.api_check_model('embedding', web.ModelEndpointRequest(base_url='http://embedding.test/v1', model='new-embedding'))
    assert result['dimensions'] == 3
    assert calls['embedding'][0]['model'] == 'new-embedding'


def test_probe_auth_matches_runtime_precedence(model_probe, monkeypatch):
    _, calls, _ = model_probe
    req = web.ModelEndpointRequest(base_url='http://test/v1', model='model', api_key='new-key', api_key_env='CUSTOM_TEST_KEY')
    monkeypatch.setenv('CUSTOM_TEST_KEY', 'environment-key')
    web.api_check_model('llm', req)
    assert calls['clients'][-1]['api_key'] == 'environment-key'
    monkeypatch.delenv('CUSTOM_TEST_KEY')
    web.api_check_model('llm', req)
    assert calls['clients'][-1]['api_key'] == 'new-key'
    req.api_key = None
    req.clear_api_key = True
    web.api_check_model('llm', req)
    assert calls['clients'][-1]['api_key'] == 'EMPTY'


@pytest.mark.parametrize('vector', [[], [0, 0], [float('nan'), 1], [float('inf')]])
def test_probe_rejects_bad_vectors(model_probe, monkeypatch, vector):
    from types import SimpleNamespace
    _, _, fake = model_probe
    monkeypatch.setattr(fake, 'embedding_create', lambda self, **kwargs: SimpleNamespace(data=[SimpleNamespace(index=0, embedding=vector)]))
    with pytest.raises(HTTPException) as exc:
        web.api_check_model('embedding', web.ModelEndpointRequest(base_url='http://test/v1', model='model'))
    assert exc.value.status_code == 502


def test_probe_sanitizes_provider_errors(model_probe, monkeypatch):
    from types import SimpleNamespace
    import openai
    _, _, fake = model_probe
    def fail(self, **kwargs):
        response = SimpleNamespace(status_code=401, request=SimpleNamespace(), headers={})
        raise openai.AuthenticationError('provider-secret-body', response=response, body=None)
    monkeypatch.setattr(fake, 'chat_create', fail)
    with pytest.raises(HTTPException) as exc:
        web.api_check_model('llm', web.ModelEndpointRequest(base_url='http://test/v1', model='model'))
    assert '认证失败' in exc.value.detail
    assert 'provider-secret-body' not in exc.value.detail


def test_extra_body_roundtrip_and_thinking_check(model_probe):
    path, calls, _ = model_probe
    req = request_for(path)
    req.llm.extra_body = {'chat_template_kwargs': {'enable_thinking': True}, 'top_k': 20}
    req.llm.thinking_mode = 'off'
    req.llm.thinking_parameter = 'chat_template_kwargs.enable_thinking'
    web.api_save_model_settings(req)
    saved = web.api_model_settings()
    assert saved['llm']['extra_body']['top_k'] == 20
    assert saved['llm']['thinking_mode'] == 'off'
    original = path.read_bytes()
    web.api_check_model('llm', req.llm)
    assert calls['chat'][-1]['extra_body'] == {'chat_template_kwargs': {'enable_thinking': False}, 'top_k': 20}
    assert path.read_bytes() == original
    req = request_for(path)
    req.llm.extra_body = {'custom': [1, False, {'key': 'value'}]}
    web.api_save_model_settings(req)
    assert yaml.safe_load(path.read_text())['llm']['extra_body'] == req.llm.extra_body


def test_multiline_extra_body_yaml_is_preserved_and_updated(model_config):
    raw = model_config.read_text().replace('  max_tokens: 1600', '  extra_body:\n    top_k: 20\n    nested:\n      enabled: true\n  max_tokens: 1600')
    model_config.write_text(raw)
    req = request_for(model_config)
    req.llm.extra_body = {'top_k': 10, 'nested': {'enabled': False}}
    web.api_save_model_settings(req)
    cfg = yaml.safe_load(model_config.read_text())
    assert cfg['llm']['extra_body'] == req.llm.extra_body
    assert cfg['llm']['max_tokens'] == 1600
    assert '# Preserve pipeline' in model_config.read_text()
