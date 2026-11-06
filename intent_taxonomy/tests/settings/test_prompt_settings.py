import json
from types import SimpleNamespace

import pytest
import yaml
from fastapi import HTTPException

from intent_engine.settings.config import load_config
from intent_engine.settings.prompt_settings import prompt_catalog, prompt_settings_snapshot, save_prompt_settings, resolve_prompt
from intent_engine.atomic.atomic_discovery import EXTRACT_PROMPT, REVIEW_PROMPT
from intent_engine.classification.classifier import ATOMIC_CLASSIFY_SYSTEM
import web_server as web


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / 'config' / 'config.yaml'
    path.parent.mkdir()
    path.write_text('''# preserve comment
llm:
  base_url: http://localhost:8000/v1
  model: test
  api_key: secret-never-return
embedding: {}
paths:
  reports_dir: data/reports
  checkpoint_dir: data/checkpoints
pipeline: {}
''')
    monkeypatch.setattr(web, 'CONFIG_PATH', path)
    monkeypatch.setattr(web, 'jobs', web.JobManager())
    return path


def test_catalog_covers_runtime_prompts_with_unique_keys():
    catalog = prompt_catalog()
    assert len(catalog) == len({e['key'] for e in catalog})
    keys = {e['key'] for e in catalog}
    assert {'atomic_discovery.EXTRACT_PROMPT', 'cluster_refinement.SPLIT_PROMPT',
            'classifier.ATOMIC_CLASSIFY_SYSTEM', 'classifier.PAIRWISE_SYSTEM',
            'hierarchy_review.NODE_PROMPT', 'hierarchy_review.LEAF_PROMPT', 'classifier.FINAL_MATCH_SYSTEM'} <= keys
    assert 'cluster_refinement.REVIEW_PROMPT' not in keys


def test_save_multiline_preserves_other_config_reset_and_revision(config_path):
    original_config = config_path.read_bytes()
    before = yaml.safe_load(config_path.read_text())
    state = prompt_settings_snapshot(config_path)
    assert 'secret-never-return' not in json.dumps(state)
    custom = '自定义审核\n返回 JSON：{"reason":"依据"}\n保留原句 $() `text`'
    saved = save_prompt_settings(config_path, state['revision'], {'atomic_discovery.REVIEW_PROMPT': custom})
    entry = next(p for p in saved['prompts'] if p['key']=='atomic_discovery.REVIEW_PROMPT')
    from pathlib import Path
    assert Path(entry['override_path']).read_text() == custom
    assert entry['source_path'] == entry['override_path']
    assert config_path.read_bytes() == original_config
    cfg = load_config(config_path)
    assert resolve_prompt(REVIEW_PROMPT, cfg['llm']['prompt_overrides']) == custom
    assert resolve_prompt(EXTRACT_PROMPT, cfg['llm']['prompt_overrides']) == EXTRACT_PROMPT
    assert yaml.safe_load(config_path.read_text())['llm'] == before['llm']
    assert '# preserve comment' in config_path.read_text()
    with pytest.raises(ValueError, match='已变更'):
        save_prompt_settings(config_path, state['revision'], {'atomic_discovery.REVIEW_PROMPT': 'stale'})
    restored = save_prompt_settings(config_path, saved['revision'], {'atomic_discovery.REVIEW_PROMPT': None})
    assert not Path(entry['override_path']).exists()
    assert not any(p['customized'] for p in restored['prompts'])
    assert resolve_prompt(REVIEW_PROMPT, load_config(config_path)['llm']['prompt_overrides']) == REVIEW_PROMPT


@pytest.mark.parametrize('updates', [{}, {'unknown': 'x'}, {'atomic_discovery.REVIEW_PROMPT': ''},
                                    {'atomic_discovery.REVIEW_PROMPT': ' '}, {'atomic_discovery.REVIEW_PROMPT': 'x'*32001}])
def test_invalid_edits_leave_file_unchanged(config_path, updates):
    before = config_path.read_bytes()
    with pytest.raises(ValueError):
        save_prompt_settings(config_path, prompt_settings_snapshot(config_path)['revision'], updates)
    assert config_path.read_bytes() == before


def test_api_conflict_and_running_job_guard(config_path):
    state = web.api_prompt_settings()
    req = web.PromptSettingsRequest(revision=state['revision'], prompts={'atomic_discovery.REVIEW_PROMPT':'custom'})
    saved = web.api_save_prompt_settings(req)
    assert saved['revision'] != state['revision']
    with pytest.raises(HTTPException) as exc:
        web.api_save_prompt_settings(req)
    assert exc.value.status_code == 409
    web.jobs.jobs['active'] = SimpleNamespace(status='running',job_id='active',job_type='classification')
    with pytest.raises(HTTPException) as exc:
        web.api_save_prompt_settings(web.PromptSettingsRequest(revision=saved['revision'],prompts=req.prompts))
    assert exc.value.status_code == 409


def test_actual_llm_request_uses_override_and_keeps_user_payload(monkeypatch):
    from intent_engine.infrastructure.llm_client import LocalLLM
    sent = []
    def create(**kwargs):
        sent.append(kwargs)
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))])
    monkeypatch.setattr('intent_engine.infrastructure.llm_client.OpenAI', lambda **kwargs: SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))))
    cfg = {'base_url':'http://localhost:8000/v1','model':'test','max_retries':1,
           'prompt_overrides':{'classifier.ATOMIC_CLASSIFY_SYSTEM':'定制分类 JSON'}}
    llm = LocalLLM(cfg)
    llm.chat_json(ATOMIC_CLASSIFY_SYSTEM, '{"atomic_text":"查询电费"}')
    assert sent[0]['messages'] == [{'role':'system','content':'定制分类 JSON'},
                                    {'role':'user','content':'{"atomic_text":"查询电费"}'}]
    llm.chat_json('unregistered', '{}')
    assert sent[1]['messages'][0]['content'] == 'unregistered'


def test_external_edit_conflicts_and_new_jobs_snapshot_file_contents(config_path, monkeypatch):
    from pathlib import Path
    from intent_engine.infrastructure.llm_client import LocalLLM
    state=prompt_settings_snapshot(config_path)
    entry=next(e for e in state['prompts'] if e['key']=='classifier.ATOMIC_CLASSIFY_SYSTEM')
    path=Path(entry['override_path']);path.parent.mkdir(parents=True)
    path.write_text('file version one',encoding='utf-8')
    with pytest.raises(ValueError,match='已变更'):
        save_prompt_settings(config_path,state['revision'],{entry['key']:'stale edit'})
    frozen=load_config(config_path)['llm']
    path.write_text('file version two',encoding='utf-8')
    sent=[]
    monkeypatch.setattr('intent_engine.infrastructure.llm_client.OpenAI',lambda **kwargs:SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kwargs:(sent.append(kwargs) or
            SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{}'))]))))))
    LocalLLM({**frozen,'max_retries':1}).chat_json(ATOMIC_CLASSIFY_SYSTEM,'{}')
    LocalLLM({**load_config(config_path)['llm'],'max_retries':1}).chat_json(ATOMIC_CLASSIFY_SYSTEM,'{}')
    assert [request['messages'][0]['content'] for request in sent]==['file version one','file version two']


def test_inline_customizations_migrate_without_loss_and_reset_does_not_resurrect_them(config_path):
    from pathlib import Path
    with config_path.open('a',encoding='utf-8') as file:
        file.write('\nprompts:\n  atomic_discovery.REVIEW_PROMPT: |\n    old custom review\n    second line\n  classifier.ATOMIC_CLASSIFY_SYSTEM: old custom classify\n')
    before=yaml.safe_load(config_path.read_text())
    state=prompt_settings_snapshot(config_path)
    saved=save_prompt_settings(config_path,state['revision'],{'atomic_discovery.REVIEW_PROMPT':None})
    assert 'prompts' not in yaml.safe_load(config_path.read_text())
    assert yaml.safe_load(config_path.read_text())['llm']==before['llm']
    assert '# preserve comment' in config_path.read_text()
    classify=next(e for e in saved['prompts'] if e['key']=='classifier.ATOMIC_CLASSIFY_SYSTEM')
    assert Path(classify['source_path']).read_text()=='old custom classify'
    review=next(e for e in saved['prompts'] if e['key']=='atomic_discovery.REVIEW_PROMPT')
    assert review['value']==review['default'] and not Path(review['override_path']).exists()
    assert load_config(config_path)['llm']['prompt_overrides']['atomic_discovery.REVIEW_PROMPT']==review['default']


def test_default_file_edits_work_without_reloading_modules_and_keys_do_not_collide(config_path,tmp_path,monkeypatch):
    import shutil
    import intent_engine.settings.prompt_settings as settings
    root=tmp_path/'templates';shutil.copytree(settings.PROMPT_ROOT,root)
    monkeypatch.setattr(settings,'PROMPT_ROOT',root)
    review=root/'defaults/atomic_discovery/REVIEW_PROMPT.txt'
    extract=root/'defaults/atomic_discovery/EXTRACT_PROMPT.txt'
    review.write_text('identical default',encoding='utf-8');extract.write_text('identical default',encoding='utf-8')
    state=prompt_settings_snapshot(config_path)
    save_prompt_settings(config_path,state['revision'],{'atomic_discovery.REVIEW_PROMPT':'only review changes'})
    values=load_config(config_path)['llm']['prompt_overrides']
    assert resolve_prompt(REVIEW_PROMPT,values)=='only review changes'
    assert resolve_prompt(EXTRACT_PROMPT,values)=='identical default'
    assert str(REVIEW_PROMPT)!='identical default'  # The import-time ref is deliberately stale.


def test_file_save_failure_rolls_back_other_templates(config_path,monkeypatch):
    import intent_engine.settings.prompt_settings as settings
    state=prompt_settings_snapshot(config_path)
    original=settings._atomic_write;calls=0
    def fail_second(path,data):
        nonlocal calls
        calls+=1
        if calls==2:raise OSError('disk full')
        return original(path,data)
    monkeypatch.setattr(settings,'_atomic_write',fail_second)
    with pytest.raises(OSError,match='disk full'):
        save_prompt_settings(config_path,state['revision'],{'atomic_discovery.REVIEW_PROMPT':'one',
                                                           'atomic_discovery.EXTRACT_PROMPT':'two'})
    assert prompt_settings_snapshot(config_path)==state
    assert not list(config_path.parent.rglob('.prompt-*.tmp'))
