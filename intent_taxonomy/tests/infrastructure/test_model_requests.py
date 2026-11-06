import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
import yaml

import intent_engine.infrastructure.model_requests as requests


def test_extra_body_and_thinking_preserve_siblings_and_types():
    config = {'thinking_mode': 'off', 'extra_body': {'chat_template_kwargs': {'custom': 3, 'enable_thinking': True}, 'top_k': 20}}
    body = requests.request_extra_body(config)
    assert body == {'chat_template_kwargs': {'custom': 3, 'enable_thinking': False}, 'top_k': 20}
    assert config['extra_body']['chat_template_kwargs']['enable_thinking'] is True
    assert requests.request_extra_body({'thinking_mode': 'on', 'thinking_parameter': 'enable_thinking'}) == {'enable_thinking': True}
    assert requests.request_extra_body({'extra_body': {'thinking': {'type': 'enabled'}}}) == {'thinking': {'type': 'enabled'}}


@pytest.mark.parametrize('config', [{'extra_body': {'model': 'other'}}, {'extra_body': {'stream': True}},
                                   {'extra_body': {'x': float('nan')}},
                                   {'thinking_mode': 'off', 'extra_body': {'chat_template_kwargs': 'bad'}},
                                   {'thinking_mode': 'on', 'thinking_parameter': 'x..y'}])
def test_invalid_parameters_rejected(config):
    with pytest.raises(ValueError):
        requests.request_extra_body(config)


def test_usage_details_are_subsets_not_added_twice():
    response = {'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'total_tokens': 150,
                          'prompt_tokens_details': {'cached_tokens': 30},
                          'completion_tokens_details': {'reasoning_tokens': 20}}}
    usage = requests.response_usage(response)
    ledger = requests.UsageLedger()
    ledger.apply({'kind': 'llm', 'model': 'qwen', **usage})
    ledger.apply({'kind': 'embedding', 'model': 'embedding', **requests.response_usage({'usage': {'prompt_tokens': 10, 'total_tokens': 10}}, 'embedding')})
    ledger.apply({'kind': 'llm', 'model': 'qwen', **requests.response_usage({})})
    ledger.apply({'kind': 'llm', 'model': 'qwen', 'failed': True})
    stats = ledger.snapshot()
    assert stats['total_tokens'] == 160
    assert stats['prompt_tokens'] == 110 and stats['completion_tokens'] == 50
    assert stats['reasoning_tokens'] == 20 and stats['cached_tokens'] == 30
    assert stats['requests'] == 4 and stats['reported_requests'] == 2
    assert stats['unreported_requests'] == 2 and stats['failed_requests'] == 1
    assert not stats['complete']


def test_parallel_requests_and_delta_are_isolated(monkeypatch):
    monkeypatch.setattr(requests, 'USAGE', requests.UsageLedger())
    before = requests.USAGE.snapshot()
    def call(_):
        requests.tracked_request('llm', {'model': 'test'}, lambda: SimpleNamespace(usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3, total_tokens=5)))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(call, range(100)))
    assert requests.usage_since(before)['total_tokens'] == 500
    after = requests.USAGE.snapshot()
    call(None)
    assert requests.usage_since(after)['requests'] == 1
    assert requests.usage_since(after)['total_tokens'] == 5


def test_usage_event_contains_no_prompts_or_keys(monkeypatch, capsys):
    monkeypatch.setenv('INTENT_PROGRESS', '1')
    requests.tracked_request('llm', {'model': 'test', 'api_key': 'secret'}, lambda **kw: {'usage': {'prompt_tokens': 3, 'completion_tokens': 1}}, messages='private user text')
    output = capsys.readouterr().out
    assert requests.USAGE_PREFIX in output
    assert 'secret' not in output and 'private user text' not in output
    event = json.loads(output.split(requests.USAGE_PREFIX)[1])
    assert event['total_tokens'] == 4


def test_chat_retries_keep_extra_params_and_count_invalid_json_responses(monkeypatch):
    import intent_engine.infrastructure.llm_client as module
    from pydantic import BaseModel
    class Result(BaseModel):
        ok: bool
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        content = '{}' if len(calls) == 1 else '{"ok":true}'
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content))], usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7))
    monkeypatch.setattr(module, 'OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    monkeypatch.setattr(module.time, 'sleep', lambda _: None)
    before = requests.USAGE.snapshot()
    result = module.LocalLLM({'base_url': 'http://test/v1', 'model': 'test', 'thinking_mode': 'off', 'extra_body': {'top_k': 20}}).chat_json('sys', 'query', schema=Result)
    assert result.ok and len(calls) == 2
    assert all(call['extra_body']['chat_template_kwargs']['enable_thinking'] is False for call in calls)
    assert requests.usage_since(before)['total_tokens'] == 14


def test_job_telemetry_accumulates_usage_separately_from_logs():
    import web_server as web
    job = web.Job('test', 'discover', [])
    manager = web.JobManager()
    manager._append(job, requests.USAGE_PREFIX + json.dumps({'kind': 'llm', 'model': 'test', 'reported': True, 'prompt_tokens': 8, 'completion_tokens': 2, 'total_tokens': 10}))
    assert job.logs == []
    assert job.to_dict()['token_usage']['total_tokens'] == 10


def test_json_mode_fallback_retains_extra_and_counts_error_usage(monkeypatch):
    import intent_engine.infrastructure.llm_client as module
    class UnsupportedFormat(Exception):
        status_code = 400
        body = {'usage': {'prompt_tokens': 1, 'completion_tokens': 0, 'total_tokens': 1}}
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        if 'response_format' in kwargs:
            raise UnsupportedFormat()
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],
                               usage={'prompt_tokens': 3, 'completion_tokens': 2, 'total_tokens': 5})
    monkeypatch.setattr(module, 'OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    before = requests.USAGE.snapshot()
    module.LocalLLM({'base_url': 'http://test/v1', 'model': 'test', 'thinking_mode': 'off'}).chat_json('sys', 'query')
    assert len(calls) == 2
    assert all(call['extra_body']['chat_template_kwargs']['enable_thinking'] is False for call in calls)
    delta = requests.usage_since(before)
    assert delta['total_tokens'] == 6
    assert delta['failed_requests'] == 1 and delta['unreported_requests'] == 0


def test_job_stage_usage_handles_parallel_requests_rounds_and_unassigned():
    import json
    from concurrent.futures import ThreadPoolExecutor
    import web_server as web
    from intent_engine.infrastructure.progress import PROGRESS_PREFIX
    manager = web.JobManager()
    job = web.Job('stage-test', 'test', [])
    def progress(event):
        manager._append(job, PROGRESS_PREFIX + json.dumps(event))
    def usage(**overrides):
        event = {'kind':'llm','model':'qwen','reported':True,'prompt_tokens':10,'completion_tokens':5,'total_tokens':15,**overrides}
        manager._append(job, requests.USAGE_PREFIX + json.dumps(event))
    usage()  # Never silently attach setup requests to a later stage.
    progress({'event':'plan','steps':[{'id':'review','label':'审核'}, {'id':'round_1:classify','label':'分类','round':1},
                                      {'id':'round_2:classify','label':'分类','round':2}, {'id':'export','label':'导出'}]})
    progress({'event':'start','id':'review'})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: usage(), range(20)))
    progress({'event':'complete','id':'review'})
    progress({'event':'start','id':'round_1:classify'})
    usage(failed=True, reported=False, prompt_tokens=None, completion_tokens=None, total_tokens=None)
    usage()
    progress({'event':'complete','id':'round_1:classify'})
    progress({'event':'start','id':'round_2:classify'})
    usage(kind='embedding',model='embedding',completion_tokens=0,total_tokens=10)
    progress({'event':'complete','id':'round_2:classify'})
    progress({'event':'start','id':'export'})
    progress({'event':'complete','id':'export'})
    result = job.to_dict()
    stages = result['stage_usage']
    assert stages['review']['requests'] == 20 and stages['review']['total_tokens'] == 300
    assert stages['round_1:classify']['requests'] == 2
    assert stages['round_1:classify']['failed_requests'] == 1
    assert stages['round_1:classify']['unreported_requests'] == 1
    assert stages['round_1:classify']['complete'] is False
    assert stages['round_2:classify']['models'][0]['kind'] == 'embedding'
    assert stages['export']['requests'] == 0
    assert stages['__unassigned__']['requests'] == 1
    assert sum(s['requests'] for s in stages.values()) == result['token_usage']['requests'] == 24
    assert sum(s['total_tokens'] for s in stages.values()) == result['token_usage']['total_tokens']
    assert job.logs == []
    # Returned accounting is isolated from the live ledger.
    stages['review']['requests'] = 999
    assert job.to_dict()['stage_usage']['review']['requests'] == 20


def test_llm_pool_random_selection_and_retry_switch(monkeypatch):
    import intent_engine.infrastructure.llm_client as module
    calls=[]
    def client(**settings):
        def create(**kwargs):
            calls.append((settings['base_url'],kwargs['model'],settings['api_key']))
            if len(calls)==1:raise ConnectionError('unavailable')
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],usage=None)
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)),close=lambda:None)
    monkeypatch.setattr(module,'OpenAI',client)
    monkeypatch.setattr(module.random,'choice',lambda choices:choices[0])
    monkeypatch.setattr(module.time,'sleep',lambda _:None)
    monkeypatch.setenv('POOL_SECOND_KEY','secondary-secret')
    llm=module.LocalLLM(dict(base_url='http://first/v1',model='first',api_key='primary-secret',max_retries=3,
        endpoints=[dict(base_url='http://second/v1',model='second',api_key_env='POOL_SECOND_KEY')]))
    assert llm.chat_json('json only','test')['ok']
    assert calls==[('http://first/v1','first','primary-secret'),('http://second/v1','second','secondary-secret')]
