import json
from types import SimpleNamespace
import pytest
from pydantic import ValidationError
from intent_engine.workflows import scene_mapping as mapping


def test_entire_catalog_and_one_intent_are_sent():
    scenes=mapping.load_scenes()
    assert len(scenes)==36
    calls=[]
    def chat(prompt,user,schema,**kw):
        payload=json.loads(user);calls.append(payload)
        assert payload=={'atomic_intent':'query prayer time','scenes':scenes}
        with pytest.raises(ValidationError):schema(reason='wrong',task_name='查询祈祷时间',scene_id='unknown')
        return schema(reason='fits',task_name='查询账单',scene_id=scenes[0]['scene_id'])
    result=mapping.map_choice(SimpleNamespace(chat_json=chat),{'text':'query prayer time'},scenes)
    assert result.scene_id==scenes[0]['scene_id'] and len(calls)==1


def test_run_mapping_exports_preserves_source_and_reuses_cache(tmp_path,monkeypatch):
    source={'mode':'atomic_actions','run_id':'old','completed_stages':['extract'],
        'rows':[{'row_id':1,'raw_text':'pay bill'}],
        'intents':[dict(intent_id='r1:i0',row_id=1,raw_text='pay bill',text='pay bill',span='pay bill',start=0,end=8,status='valid',cluster_id=-1)]}
    path=tmp_path/'source.json';path.write_text(json.dumps(source))
    calls=[]
    class LLM:
        cfg={'model':'test'}
        def __init__(self,cfg):pass
        def chat_json(self,prompt,user,schema,**kw):
            calls.append(user);return schema(reason='fits',task_name='查询账单',scene_id=mapping.load_scenes()[0]['scene_id'])
    monkeypatch.setattr(mapping,'LocalLLM',LLM)
    cfg={'llm':{'model':'test'},'paths':{'reports_dir':str(tmp_path/'reports')}}
    result=mapping.run_mapping(cfg,path,2)
    assert result['status']=='completed'
    assert result['intents']==source['intents']
    assert json.loads(path.read_text())==source
    assert len(calls)==1
    second=mapping.run_mapping(cfg,result['artifacts']['report'],1)
    assert len(calls)==1 and second['scene_mapping']['records'][0]['cached']
    import csv
    with __import__('pathlib').Path(second['artifacts']['workbook']).open(encoding='utf-8-sig',newline='') as stream:
        reader=csv.DictReader(stream);rows=list(reader)
        assert reader.fieldnames.index('任务')==reader.fieldnames.index('S4')+1
    assert len(rows)==36 and rows[0]['任务']=='查询账单'
    assert sum(bool(r['任务']) for r in rows)==1


def test_scene_tasks_are_grouped_deduplicated_and_keep_empty_scenes():
    scenes=[{'scene_id':'a','S4':'场景一'},{'scene_id':'b','S4':'场景二'}]
    records=[{'scene_id':'a','task_name':text,'text':'original question','status':'matched'} for text in [' first ','second','first']]
    records += [{'scene_id':'a','text':'failed','status':'failed'},{'scene_id':'other','text':'bad','status':'matched'}]
    result=mapping.scene_task_rows(scenes,records)
    assert result==[{'scene_id':'a','S4':'场景一','任务':'first\nsecond'},{'scene_id':'b','S4':'场景二','任务':''}]


def test_task_export_never_falls_back_to_original_question():
    result=mapping.scene_task_rows([{'scene_id':'a','S4':'场景'}],[
        {'scene_id':'a','text':'What is this?','raw_text':'original question','status':'matched'}])
    assert result[0]['任务']==''


def test_legacy_s4_result_only_generates_title_and_preserves_selection(tmp_path,monkeypatch):
    scenes=mapping.load_scenes()
    atom=dict(intent_id='r1:i0',row_id=1,raw_text='pay bill',text='pay bill',span='pay bill',start=0,end=8,status='valid',cluster_id=-1)
    source=dict(mode='atomic_actions',run_id='old',completed_stages=['extract'],rows=[dict(row_id=1,raw_text='pay bill')],intents=[atom],
        scene_mapping={'scenes':scenes,'records':[{k:atom[k] for k in ('intent_id','row_id','raw_text','text')}|{'scene_id':scenes[2]['scene_id'],'status':'matched','reason':'old selection'}]})
    path=tmp_path/'old.json';path.write_text(json.dumps(source))
    calls=[]
    class LLM:
        cfg={'model':'test'}
        def __init__(self,cfg):pass
        def chat_json(self,prompt,user,schema,**kwargs):
            calls.append((prompt.key,json.loads(user)))
            assert schema is mapping.TaskName
            return schema(task_name='查询账单')
    monkeypatch.setattr(mapping,'LocalLLM',LLM)
    result=mapping.run_mapping({'llm':{},'paths':{'reports_dir':str(tmp_path/'reports')}},path)
    record=result['scene_mapping']['records'][0]
    assert record['scene_id']==scenes[2]['scene_id']
    assert record['selection_source']=='previous_mapping' and record['task_name']=='查询账单'
    assert calls==[('scene_mapping.NAME_PROMPT',{'atomic_intent':'pay bill'})]
    assert json.loads(path.read_text())==source


def test_query_task_csv_has_only_two_columns_and_keeps_multiple_intents():
    import csv,io
    rows=list(csv.DictReader(io.StringIO(mapping.query_task_csv([
        {'raw_text':'问句一，问句二','text':'不应导出此问句','task_name':'查询名称','status':'matched'},
        {'raw_text':'问句一，问句二','task_name':'查询材料','status':'matched'},
        {'raw_text':'failed query','text':'fallback prohibited','status':'failed'}]))))
    assert rows==[{'query':'问句一，问句二','原子意图':'查询名称\n查询材料'}, {'query':'failed query','原子意图':''}]


def test_existing_result_can_download_query_tasks_without_llm(tmp_path,monkeypatch):
    from intent_engine.web import app as web
    from intent_engine.workflows import workflow_artifacts
    path=tmp_path/'result.json'
    path.write_text(json.dumps({'intents':[{'intent_id':'i','raw_text':'original question','task_name':'查询名称','status':'valid'}]}))
    monkeypatch.setattr(workflow_artifacts,'workflow_artifact',lambda *a:path)
    monkeypatch.setattr(mapping,'LocalLLM',lambda *a:pytest.fail('export must not call model'))
    response=web.api_query_atomic_intents('run')
    assert response.status_code==200 and response.body.startswith(b'\xef\xbb\xbf')
    assert '查询名称' in response.body.decode('utf-8-sig')
    assert 'query_atomic_intents.csv' in response.headers['content-disposition']
