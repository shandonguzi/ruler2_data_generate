import copy
import pytest
from intent_engine.exporting.delivery_topics import prepare_delivery_topics
from intent_engine.exporting.sentence_results import build_sentence_results
from intent_engine.workflows.candidate_iteration import fingerprint
from intent_engine.models import Taxonomy


def source():
    taxonomy = Taxonomy.model_validate({'version':3,'domains':[{'name':'饮食'}], 'tasks':[
        {'task_id':'dish','domain':'饮食','object':'传统菜肴','action':'查询定义'}]}).model_dump()
    atoms = [{'intent_id':'r1:i0','row_id':1,'text':'What is Bazeen?','raw_text':'What is Bazeen?',
              'span':'What is Bazeen?','status':'valid','cluster_id':-1,'topic_pending':True},
             {'intent_id':'r2:i0','row_id':2,'text':'unknown','raw_text':'unknown','span':'unknown',
              'status':'ambiguous','cluster_id':None}]
    record = {**atoms[0], 'status':'known','task_id':'dish','taxonomy_version':3,
              'taxonomy_fingerprint':fingerprint(Taxonomy.model_validate(taxonomy)), 'error':None}
    from intent_engine.atomic.topic_hierarchy import build_topic_tree
    tree=build_topic_tree([8,9],{8:['传统菜肴 · 查询定义'],9:['其他叶子']},[
        {'Parent_ID':10,'Child_Left_ID':8,'Child_Right_ID':9,'Distance':.5,'Parent_Name':'饮食'}])
    tree['source']='BERTopic+LLM'
    for node in tree['nodes']:node['semantic_status']='approved'
    return {'topic_hierarchy':tree,'task_topic_nodes':{'dish':'topic:8'}, 'rows':[{'row_id':1,'raw_text':'What is Bazeen?','status':'valid'},
            {'row_id':2,'raw_text':'unknown','status':'ambiguous'}], 'intents':atoms,'candidate_taxonomy':taxonomy,
            'iteration':{'validated_taxonomy_version':3,'rounds':[{}],'assignments':[record]}}


def test_validated_noise_and_pending_atom_get_semantic_topics_without_mutating_raw_cluster():
    data=source();before=copy.deepcopy(data)
    result=build_sentence_results(data)
    assert result['records'][0]['topics_by_level']==[{'level':1,'topics':['饮食']},{'level':2,'topics':['传统菜肴 · 查询定义']}]
    assert all(not level['topics'] for level in result['records'][1]['topics_by_level'])
    assert data==before
    delivery=prepare_delivery_topics(data)
    assert delivery['coverage']['covered_rows']==1
    assert delivery['coverage']['noise_intents']==1
    assert delivery['assignments']['r1:i0']['source']=='validated_classification'


@pytest.mark.parametrize('change',['fingerprint','version','task','text','row','raw_text','span','duplicate','unvalidated','error','discovery'])
def test_stale_invalid_or_ambiguous_classifications_never_improve_coverage(change):
    data=source();a=data['iteration']['assignments'][0]
    if change=='fingerprint':a['taxonomy_fingerprint']='stale'
    if change=='version':a['taxonomy_version']=2
    if change=='task':a['task_id']='missing'
    if change=='text':a['text']='changed input'
    if change=='row':a['row_id']=9
    if change=='raw_text':a['raw_text']='changed original'
    if change=='span':a['span']='changed span'
    if change=='duplicate':data['iteration']['assignments'].append(copy.deepcopy(a))
    if change=='unvalidated':data['iteration']['validated_taxonomy_version']=2
    if change=='error':a['error']='offline'
    if change=='discovery':a['status']='discovery'
    assert prepare_delivery_topics(data)['coverage']['covered_rows']==0


def test_audited_single_example_semantics_without_iteration_and_multi_intent_dedup():
    data=source();data['iteration']={}
    data['intents'][0]['cluster_id']=8
    data['clusters']=[{'cluster_id':8,'unique_text_count':1,'review':{'verdict':'uncertain','semantic_clear':True,'object':'菜肴','action':'查询'}}]
    data['cluster_task_ids']={'8':'dish'}
    data['intents'].append({**data['intents'][0],'intent_id':'r1:i1'})
    result=build_sentence_results(data)
    assert result['records'][0]['topics_by_level'][0]['topics']==['饮食']
    assert result['records'][0]['topics_by_level'][1]['topics']==['传统菜肴 · 查询定义']
    assert len(prepare_delivery_topics(data)['assignments'])==2


def test_native_tree_drops_globally_empty_tail_levels():
    from intent_engine.atomic.topic_hierarchy import build_topic_tree
    data=source()
    data['intents'][0].update(cluster_id=0,topic_pending=False)
    data['intents'].append({'intent_id':'r3:i0','row_id':2,'status':'valid','text':'pending','cluster_id':2,'topic_pending':True})
    data['topic_hierarchy']=build_topic_tree([0,1,2],{0:['A'],1:['B'],2:['C']},[
        {'Parent_ID':3,'Parent_Name':'BC','Child_Left_ID':1,'Child_Right_ID':2,'Distance':.5},
        {'Parent_ID':4,'Parent_Name':'ALL','Child_Left_ID':0,'Child_Right_ID':3,'Distance':1}])
    assert len(build_sentence_results(data)['records'][0]['topics_by_level'])==2
    assert data['topic_hierarchy']['max_depth']==3
