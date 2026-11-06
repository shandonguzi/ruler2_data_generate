import os
from types import SimpleNamespace

from intent_engine.infrastructure.llm_client import LocalLLM, _ensure_local_no_proxy


def test_model_hosts_merge_both_environment_variables(monkeypatch):
    monkeypatch.setenv('NO_PROXY', 'existing.internal,127.0.0.1')
    monkeypatch.setenv('no_proxy', 'lower.internal,existing.internal')
    _ensure_local_no_proxy('http://192.168.2.10:8000/v1', 'https://models.internal:9443/v1', 'http://[2001:db8::1]:8001/v1')
    entries = os.environ['NO_PROXY'].split(',')
    assert set(entries) == {'existing.internal', 'lower.internal', 'localhost', '127.0.0.1', '::1',
                            '192.168.2.10', 'models.internal', '2001:db8::1'}
    assert os.environ['NO_PROXY'] == os.environ['no_proxy']
    _ensure_local_no_proxy('https://MODELS.INTERNAL/v1')
    assert os.environ['NO_PROXY'].split(',') == entries


def test_chat_and_embedding_add_host_before_client_creation(monkeypatch, tmp_path):
    import openai
    import intent_engine.infrastructure.llm_client as module
    from intent_engine.atomic.atomic_discovery import SemanticEmbeddings
    monkeypatch.delenv('NO_PROXY', raising=False)
    monkeypatch.delenv('no_proxy', raising=False)
    seen = []
    def client(**kwargs):
        seen.append((kwargs['base_url'], os.environ['no_proxy']))
        return SimpleNamespace()
    monkeypatch.setattr(module, 'OpenAI', client)
    monkeypatch.setattr(openai, 'OpenAI', client)
    LocalLLM({'base_url': 'http://10.0.0.8:8000/v1', 'model': 'chat'})
    SemanticEmbeddings({'base_url': 'http://10.0.0.9:8001/v1', 'model': 'embedding'}, tmp_path)
    assert '10.0.0.8' in seen[0][1].split(',')
    assert {'10.0.0.8', '10.0.0.9'} <= set(seen[1][1].split(','))
