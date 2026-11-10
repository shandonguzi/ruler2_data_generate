from scripts.manual_semantic_model_test import read_test_env
import pytest


def test_env_supports_quotes_comments_and_no_execution(tmp_path):
    path=tmp_path/'.env'
    path.write_text('# comment\nexport INTENT_MANUAL_TEST_MODEL="test-model" # note\nINTENT_MANUAL_TEST_API_KEY=\'test$literal#key\'\nOTHER=ignored\n')
    assert read_test_env(path)=={'INTENT_MANUAL_TEST_MODEL':'test-model','INTENT_MANUAL_TEST_API_KEY':'test$literal#key'}
    assert read_test_env(tmp_path/'missing')=={}


def test_invalid_env_does_not_expose_value(tmp_path):
    path=tmp_path/'.env';path.write_text('INTENT_MANUAL_TEST_API_KEY="secret')
    with pytest.raises(ValueError) as error:read_test_env(path)
    assert 'secret' not in str(error.value)


def test_manual_thinking_disabled_by_default(monkeypatch):
    from scripts.manual_semantic_model_test import thinking_body
    monkeypatch.delenv('INTENT_MANUAL_TEST_ENABLE_THINKING',raising=False)
    assert thinking_body({})=={'enable_thinking':False}
    assert thinking_body({'INTENT_MANUAL_TEST_ENABLE_THINKING':'false'})=={'enable_thinking':False}


def test_every_registered_prompt_has_two_manual_cases_and_small_batches():
    from scripts.prompt_test_cases import cases,response_schema
    from scripts.manual_prompt_suite import select_cases
    from intent_engine.settings.prompt_settings import prompt_catalog
    fixtures=cases();keys={c['prompt'] for c in fixtures}
    assert keys=={p['key'] for p in prompt_catalog()}
    assert len(fixtures)==len(keys)*2
    assert len({c['id'] for c in fixtures})==len(fixtures)
    for c in fixtures:
        assert c['input'] and c['expected'] and c['focus']
        assert response_schema(c['prompt']) is not None or c['prompt'].startswith('skill_router.')
    assert len(select_cases(fixtures,batch=1))==3
    with pytest.raises(ValueError):select_cases(fixtures,prompts=sorted(keys)[:2],examples=2)


def test_semantic_expectations_and_invalid_ids_are_checked():
    from scripts.prompt_test_cases import cases,check_response
    fixtures=cases()
    c=next(c for c in fixtures if c['id']=='classifier.PAIRWISE_SYSTEM:match')
    assert check_response(c,{'reason':'相符','task_id':'t','match':False,'confidence':.9})[1]==['match']
    c=next(c for c in fixtures if c['id']=='classifier.FINAL_MATCH_SYSTEM:known')
    with pytest.raises(ValueError):check_response(c,{'reason':'相符','selected_task_id':'invented','status':'known'})


def test_suite_preview_never_constructs_model_client(monkeypatch,capsys):
    from scripts import manual_prompt_suite as suite
    import openai
    monkeypatch.setattr('sys.argv',['manual_prompt_suite.py','--batch','1'])
    monkeypatch.setattr(suite,'read_test_env',lambda p:{'INTENT_MANUAL_TEST_ENABLE_THINKING':'false'})
    def forbidden(*a,**kw):raise AssertionError('preview must never create model client')
    monkeypatch.setattr(openai,'OpenAI',forbidden)
    assert suite.main()==0
    assert '实际请求0次' in capsys.readouterr().out


def test_eval_aliases_and_deepseek_thinking(tmp_path):
    from scripts.manual_semantic_model_test import thinking_body
    path=tmp_path/'.env'
    path.write_text('EVAL_VLLM_BASE_URL=https://api.deepseek.com\nEVAL_VLLM_MODEL=deepseek-flash\nEVAL_VLLM_API_KEY=test-key\nINTENT_MANUAL_TEST_ENABLE_THINKING=false\n')
    values=read_test_env(path)
    assert values['INTENT_MANUAL_TEST_MODEL']=='deepseek-flash'
    assert values['INTENT_MANUAL_TEST_API_KEY']=='test-key'
    assert thinking_body(values)=={'thinking':{'type':'disabled'}}
