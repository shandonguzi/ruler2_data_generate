import csv
import io
import json
from pathlib import Path
from intent_engine.workflows import atomic_tasks


def test_export_only_saved_names_never_calls_llm(tmp_path,monkeypatch):
    from intent_engine.infrastructure import llm_client
    monkeypatch.setattr(llm_client,'LocalLLM',lambda *a:(_ for _ in ()).throw(AssertionError('no model')))
    source={'mode':'atomic_actions','run_id':'source','completed_stages':['extract'],
        'rows':[{'row_id':1,'raw_text':'pay bill'}],
        'intents':[dict(intent_id='r1:i0',row_id=1,raw_text='pay bill',text='pay bill',span='pay bill',start=0,end=8,status='valid',cluster_id=-1)]}
    path=tmp_path/'source.json';path.write_text(json.dumps(source))
    cfg={'paths':{'reports_dir':str(tmp_path/'reports')}}
    first=atomic_tasks.run_atomic_tasks(cfg,path)
    assert first['token_usage']['requests']==0 and first['atomic_task_names']['missing_names']==1
    rows=list(csv.DictReader(io.StringIO(Path(first['artifacts']['query_atomic_intents']).read_text(encoding='utf-8-sig'))))
    assert rows==[{'query':'pay bill','原子意图':''}]
    assert json.loads(path.read_text())==source
    source['intents'][0]['task_name']='缴纳账单';path.write_text(json.dumps(source))
    second=atomic_tasks.run_atomic_tasks(cfg,path)
    assert second['atomic_task_names']['records'][0]['task_name']=='缴纳账单'


def test_saved_name_requires_matching_intent_provenance():
    a=dict(intent_id='i',row_id=1,raw_text='question',text='atomic',status='valid')
    saved={**a,'task_name':'已有名称','status':'matched'}
    source={'intents':[a],'atomic_task_names':{'records':[saved]}}
    assert atomic_tasks.saved_task_records(source)[0]['task_name']=='已有名称'
    source['intents'][0]['text']='changed'
    assert atomic_tasks.saved_task_records(source)[0]['task_name']==''


def test_export_resolves_existing_leaf_labels_but_not_unresolved_assignments():
    atom=dict(intent_id='i',row_id=1,raw_text='question',text='atomic',status='valid',cluster_id=3)
    source={'intents':[atom],'candidate_taxonomy':{'version':2,'tasks':[{'task_id':'t'}]},
        'topic_hierarchy':{'nodes':[{'node_id':'topic:3','topic_id':3,'is_leaf':True,'semantic_status':'approved','label':'查询舞蹈伴随元素'}]},
        'task_topic_nodes':{'t':'topic:3'},'cluster_task_ids':{'3':'t'},
        'clusters':[{'cluster_id':3,'review':{'verdict':'consistent','object':'舞蹈','action':'查询伴随元素'}}]}
    assert atomic_tasks.saved_task_records(source)[0]['task_name']=='查询舞蹈伴随元素'
    source['iteration']={'assignments':[{**atom,'status':'ambiguous','taxonomy_version':2}]}
    assert atomic_tasks.saved_task_records(source)[0]['task_name']==''
    source['iteration']['assignments'][0].update(status='known',task_id='t')
    assert atomic_tasks.saved_task_records(source)[0]['name_source']=='matched_leaf'
    source['iteration']['assignments'][0]['taxonomy_version']=1
    assert atomic_tasks.saved_task_records(source)[0]['task_name']==''


def test_query_export_preserves_original_rows_and_aggregates_intents():
    rows=[{'row_id':1,'raw_text':'first'},{'row_id':2,'raw_text':'same'}, {'row_id':3,'raw_text':'same'}, {'row_id':4,'raw_text':'no intent'}]
    records=[{'row_id':1,'raw_text':'first','status':'matched','task_name':name} for name in ['查询名称','查询材料','查询名称']]
    records += [{'row_id':3,'raw_text':'same','status':'matched','task_name':'查询时间'}]
    records += [{'row_id':2,'raw_text':'same','status':'missing_name','task_name':''}]
    parsed=list(csv.DictReader(io.StringIO(atomic_tasks.query_task_csv(records,rows))))
    assert parsed==[{'query':'first','原子意图':'查询名称\n查询材料'}, {'query':'same','原子意图':''}, {'query':'same','原子意图':'查询时间'}, {'query':'no intent','原子意图':''}]
