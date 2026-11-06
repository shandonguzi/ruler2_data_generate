import copy
import json
from threading import Barrier, Lock

import pytest

from intent_engine.atomic.hierarchy_review import NodeDecision, LeafNamingDecision, review_hierarchy
from intent_engine.atomic.topic_hierarchy import build_topic_tree, attach_topic_memberships, index_topic_tree
from intent_engine.exporting.sentence_results import build_sentence_results
from intent_engine.infrastructure.progress import ProgressReporter


def source():
    merges = [dict(Parent_ID=4,Child_Left_ID=0,Child_Right_ID=1,Distance=.2),
              dict(Parent_ID=5,Child_Left_ID=2,Child_Right_ID=3,Distance=.3),
              dict(Parent_ID=6,Child_Left_ID=4,Child_Right_ID=5,Distance=.9)]
    atoms = [dict(intent_id=f'r{i}:i0',row_id=i,raw_text=f'text {i}',text=f'text {i}',span=f'text {i}',
                  slots={},status='valid',cluster_id=i,topic_pending=False) for i in range(4)]
    data = dict(run_id='test',parameters={},errors=[],
        rows=[dict(row_id=i,raw_text=f'text {i}') for i in range(4)],intents=atoms,
        clusters=[dict(cluster_id=i,review=dict(verdict='consistent',object=f'object {i}',action='query',reason='evidence'),
                       review_samples=[copy.deepcopy(atoms[i])]) for i in range(4)],
        topic_hierarchy=build_topic_tree(list(range(4)),{i:[str(i)] for i in range(4)},merges))
    attach_topic_memberships(data)
    return data


class LLM:
    def __init__(self, reject=('merge:6',), fail=()):
        self.calls=[]; self.reject=reject; self.fail=fail
    def chat_json(self, prompt, user, schema, **kwargs):
        p=json.loads(user);self.calls.append(p)
        if p['node_id'] in self.fail:
            raise RuntimeError('offline')
        return schema(reason='different subject' if p['node_id'] in self.reject else 'shared subject',
                      description='scope '+p['node_id'],label='named '+p['node_id'],
                      **({'semantic_clear':p['node_id'] not in self.reject,'semantic_issue':'conflicting_semantics' if p['node_id'] in self.reject else 'none'} if schema is LeafNamingDecision else {'decision':'separate' if p['node_id'] in self.reject else 'group'}))


def run(data,llm,workers=2):
    reporter=ProgressReporter();reporter.plan([{'id':'domains','label':'审核'}]);reporter.start('domains')
    return review_hierarchy(data,llm,workers,reporter,lambda:None)


def test_rejected_root_keeps_two_branches_and_exports_the_same_named_paths():
    data=source();raw=copy.deepcopy(data['topic_hierarchy']);llm=LLM()
    run(data,llm)
    assert data['bertopic_hierarchy']==raw
    tree=data['topic_hierarchy'];nodes,paths=index_topic_tree(tree,{0,1,2,3})
    assert tree['root_ids']==['merge:4','merge:5'] and tree['status']=='forest'
    assert 'merge:6' not in nodes and tree['max_depth']==2
    assert len(data['candidate_taxonomy']['tasks'])==4
    final=build_sentence_results(data)
    assert final['records'][0]['topics_by_level']==[
        {'level':1,'topics':['named merge:4']},{'level':2,'topics':['named topic:0']}]
    assert all(set(s)=={'text'} for c in llm.calls if c['kind']=='leaf' for s in c['members'])
    assert all('members' not in c and len(c['children'])==2 for c in llm.calls if c['kind']=='parent')


def test_each_native_depth_runs_in_parallel_and_parent_waits_for_children():
    data=source();barrier=Barrier(2,timeout=5);lock=Lock();finished=set();maximum=active=0
    class Concurrent(LLM):
        def chat_json(self,prompt,user,schema,**kwargs):
            nonlocal active,maximum
            p=json.loads(user)
            with lock:
                active+=1;maximum=max(maximum,active)
                if p['kind']=='parent':assert all(c['node_id'] in finished for c in p['children'])
            if p['node_id']!='merge:6':barrier.wait()
            out=super().chat_json(prompt,user,schema,**kwargs)
            with lock:active-=1;finished.add(p['node_id'])
            return out
    state=run(data,Concurrent(),2)
    assert maximum==2 and [x['native_level'] for x in state['levels']]==[3,2,1]
    assert [x['requests'] for x in state['levels']]==[4,2,1]


def test_rejected_lower_merges_can_be_grouped_only_by_reviewing_all_promoted_children():
    data=source();llm=LLM(reject=('merge:4','merge:5'))
    run(data,llm)
    tree=data['topic_hierarchy'];nodes,_=index_topic_tree(tree)
    assert nodes['merge:6']['children']==['topic:0','topic:1','topic:2','topic:3']
    assert len(llm.calls[-1]['children'])==4
    native=copy.deepcopy(tree);native['source']='BERTopic.hierarchical_topics'
    with pytest.raises(ValueError):index_topic_tree(native)


def test_leaf_failure_is_visible_and_not_exported_or_wrapped_in_an_approved_parent():
    data=source();run(data,LLM(fail=('topic:0',)))
    assert data['hierarchy_review']['status']=='completed_with_errors'
    assert 'topic:0' not in data['task_topic_nodes'].values()
    assert data['intents'][0]['topic_pending']
    assert not any(x['topics'] for x in build_sentence_results(data)['records'][0]['topics_by_level'])
    assert data['errors'][0]['node_id']=='topic:0'


def test_frontier_limit_prevents_unbounded_parent_requests():
    data=source();data['parameters']['hierarchy_child_limit']=2
    llm=LLM(reject=('merge:4','merge:5'));run(data,llm)
    assert not any(p['node_id']=='merge:6' for p in llm.calls)
    assert data['hierarchy_review']['reviews'][-1]['skipped']=='child_summary_limit'
    assert len(data['topic_hierarchy']['root_ids'])==4


def test_tree_view_filters_and_counts_use_the_same_final_paths_as_export():
    from intent_engine.models import Taxonomy
    from intent_engine.workflows.candidate_iteration import fingerprint
    from intent_engine.workflows.workflow_visuals import visualization_page
    data=source();run(data,LLM())
    noise=dict(intent_id='r4:i0',row_id=4,raw_text='noise',text='noise',span='noise',slots={},
               status='valid',cluster_id=-1,topic_pending=True)
    data['intents'].append(noise);data['rows'].append(dict(row_id=4,raw_text='noise'))
    attach_topic_memberships(data)
    taxonomy=Taxonomy.model_validate(data['candidate_taxonomy'])
    records=[]
    for atom in data['intents']:
        records.append({**atom,'status':'known','task_id':data['cluster_task_ids'][str(max(0,atom['cluster_id']))],
                        'taxonomy_version':taxonomy.version,'taxonomy_fingerprint':fingerprint(taxonomy),'error':None})
    data['iteration']={'rounds':[{}],'assignments':records,'validated_taxonomy_version':taxonomy.version}
    before=copy.deepcopy(data)
    view=visualization_page(data,view='hierarchy',topic='topic:0')
    assert {r['row_id'] for r in view['records']}=={0,4}
    leaf=next(n for n in view['hierarchy']['nodes'] if n['node_id']=='topic:0')
    assert leaf['intent_count']==2
    assert view['records'][1]['intents'][0]['cluster_id']==-1
    assert view['records'][1]['intents'][0]['topic_path']==['merge:4','topic:0']
    assert data==before


def test_node_requests_strip_evidence_metadata_without_mutating_artifacts():
    data=source()
    review=data['clusters'][0]['review']
    review.update(evidence_ids=['do-not-send'],boundary_ids=['do-not-send'],
                  split_groups=[{'label':'old group','evidence_ids':['do-not-send']}],
                  unused_metadata={'evidence_ids':['do-not-send']})
    original=copy.deepcopy(data['clusters'])
    llm=LLM();run(data,llm)
    for request in llm.calls:
        assert 'do-not-send' not in json.dumps(request)
        if request['kind']=='leaf':
            assert set(request['review']) <= {'reason','object','action','semantic_clear','verdict'}
            assert all(set(sample)=={'text'} for sample in request['members'])
        else:
            assert all('node_id' in child for child in request['children'])
            assert 'members' not in request
    for cluster, saved in zip(data['clusters'],original):
        assert cluster['review']==saved['review']
        assert cluster['review_samples']==saved['review_samples']
    assert data['intents'][0]['intent_id']==original[0]['review_samples'][0]['intent_id']


def test_single_text_leaf_names_are_reused_while_parents_are_reviewed():
    data=source();first=LLM();run(data,first)
    labels={n['node_id']:(n['label'],n['description']) for n in data['topic_hierarchy']['nodes'] if n['is_leaf']}
    second=LLM();state=run(data,second)
    assert state['reused_nodes']==4
    assert len(second.calls)==3 and all(p['kind']=='parent' for p in second.calls)
    assert state['completed_nodes']==7
    assert sum(level['requests'] for level in state['levels'])==3
    assert len([r for r in state['reviews'] if r['skipped']=='reused_singleton'])==4
    for node in data['topic_hierarchy']['nodes']:
        if node['is_leaf']:
            assert (node['label'],node['description'])==labels[node['node_id']]
            assert node['naming_source']=='reused_singleton'


@pytest.mark.parametrize('change',['text','action','model','prompt','pending','missing_fingerprint','semantic_flag','multiple_texts'])
def test_singleton_reuse_invalidates_changes_or_missing_review(change):
    data=source();first=LLM();first.cfg={'model':'original'};run(data,first)
    current=LLM();current.cfg={'model':'original'}
    node=next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:0')
    if change=='text':
        data['intents'][0]['text']='changed text'
        data['clusters'][0]['review_samples'][0]['text']='changed text'
    elif change=='action':
        data['clusters'][0]['review']['action']='changed action'
    elif change=='model':
        current.cfg['model']='new model'
    elif change=='prompt':
        current.cfg['prompt_overrides']={'hierarchy_review.LEAF_PROMPT':'changed prompt'}
    elif change=='pending':
        node['semantic_status']='pending'
    elif change=='missing_fingerprint':
        node.pop('naming_fingerprint')
    elif change=='semantic_flag':
        node['semantic_clear']=False
    else:
        atom={**data['intents'][0],'intent_id':'extra','text':'another text'}
        data['intents'].append(atom)
        data['clusters'][0]['review_samples'].append(copy.deepcopy(atom))
    run(data,current)
    assert any(p['node_id']=='topic:0' for p in current.calls)
    if change not in {'model','prompt'}:
        assert not any(p['node_id']=='topic:1' for p in current.calls)


def multileaf_source():
    data=source()
    atom=dict(data['intents'][0],intent_id='r4:i0',row_id=4,raw_text='other wording',text='other wording',span='other wording')
    data['intents'].append(atom)
    data['rows'].append({'row_id':4,'raw_text':'other wording'})
    data['clusters'][0]['review_samples'].append(copy.deepcopy(atom))
    return data


def test_multi_text_leaf_reuses_complete_definition_and_parents_still_request():
    data=multileaf_source();run(data,LLM())
    original=copy.deepcopy(next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:0'))
    llm=LLM();state=run(data,llm)
    assert all(request['kind']=='parent' for request in llm.calls)
    assert state['reused_nodes']==4
    current=next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:0')
    assert current['naming_source']=='reused_leaf'
    for field in ('label','description','review_reason','task_id'):
        assert current[field]==original[field]


def test_renumbered_leaf_reuses_content_and_stable_task_id():
    data=multileaf_source();run(data,LLM())
    previous=copy.deepcopy(data['topic_hierarchy'])
    task_id=data['cluster_task_ids']['0']
    for atom in data['intents']:
        if atom['cluster_id']==0:
            atom['cluster_id']=10
        atom.pop('topic_path',None)
    data['clusters'][0]['cluster_id']=10
    data['bertopic_hierarchy']=build_topic_tree([10,1,2,3],{key:[str(key)] for key in [10,1,2,3]})
    reporter=ProgressReporter();reporter.plan([{'id':'domains','label':'审核'}]);reporter.start('domains')
    llm=LLM()
    review_hierarchy(data,llm,2,reporter,lambda:None,previous_hierarchy=previous)
    assert not any(request.get('node_id')=='topic:10' for request in llm.calls)
    assert data['cluster_task_ids']['10']==task_id
    assert data['task_topic_nodes'][task_id]=='topic:10'
    node=next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:10')
    assert node['label']=='named topic:0'


@pytest.mark.parametrize('change',['unsampled_member','sample','review','definition'])
def test_multi_leaf_cache_invalidates_full_membership_or_changed_review(change):
    data=multileaf_source();run(data,LLM())
    if change=='unsampled_member':
        data['intents'].append(dict(data['intents'][0],intent_id='new',row_id=0,text='unsampled new text'))
    elif change=='sample':
        data['clusters'][0]['review_samples']=data['clusters'][0]['review_samples'][:1]
    elif change=='review':data['clusters'][0]['review']['reason']='changed review boundary explanation'
    else:
        next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:0')['description']='edited outside review'
    llm=LLM();run(data,llm)
    assert any(request.get('node_id')=='topic:0' for request in llm.calls)


def test_clear_single_text_is_admitted_with_low_support_and_exported():
    data=source()
    cluster=data['clusters'][0]
    cluster.update(unique_text_count=1,review_source='singleton_skip',review={
        'verdict':'uncertain','semantic_clear':True,'object':'国家官方宗教','action':'查询',
        'reason':'单文本支持量低，但语义明确'})
    class ClearSingle(LLM):
        def chat_json(self,prompt,user,schema,**kwargs):
            payload=json.loads(user)
            if schema is LeafNamingDecision and payload['node_id']=='topic:0':
                assert 'hierarchy_review.LEAF_PROMPT'==prompt.key
                return schema(reason='只有一条样本不影响明确的所问关系',description='查询国家官方宗教',
                              label='国家官方宗教查询',semantic_clear=True)
            return super().chat_json(prompt,user,schema,**kwargs)
    run(data,ClearSingle())
    node=next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:0')
    assert node['semantic_status']=='approved' and node['semantic_clear'] is True
    assert node['support_level']==cluster['support_level']=='single_example'
    assert cluster['review']['verdict']=='uncertain'  # No fabricated multi-member consistency.
    assert cluster['hierarchy_eligible'] is True
    assert any(level['topics'] for level in build_sentence_results(data)['records'][0]['topics_by_level'])


def test_true_semantic_problem_remains_pending_even_with_clear_upstream_summary():
    data=source()
    class Unclear(LLM):
        def chat_json(self,prompt,user,schema,**kwargs):
            if schema is LeafNamingDecision and json.loads(user)['node_id']=='topic:0':
                return schema(reason='原子文本缺少必要指代背景，无法确定所问属性',description='宗教相关问题，所问属性待确认',
                              label='宗教问题待确认',semantic_clear=False,semantic_issue='missing_context')
            return super().chat_json(prompt,user,schema,**kwargs)
    run(data,Unclear())
    node=next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:0')
    assert node['semantic_status']=='pending' and node['semantic_issue']=='missing_context'
    assert 'topic:0' not in data['task_topic_nodes'].values()
    assert not any(level['topics'] for level in build_sentence_results(data)['records'][0]['topics_by_level'])


@pytest.mark.parametrize('fields',[
    {'semantic_clear':False},
    {'semantic_clear':False,'semantic_issue':'insufficient_support'},
    {'semantic_clear':True,'semantic_issue':'missing_context'},
    {'semantic_clear':True,'label':'   '},
    {'semantic_clear':True,'decision':'uncertain'},
])
def test_leaf_admission_requires_real_semantics_and_valid_names(fields):
    with pytest.raises(ValueError):
        LeafNamingDecision.model_validate({'reason':'evidence','description':'definition','label':'name',**fields})


def test_new_distinct_member_upgrades_support_without_changing_parent_review_schema():
    data=source();run(data,LLM())
    assert data['clusters'][0]['support_level']=='single_example'
    atom=dict(data['intents'][0],intent_id='new',row_id=4,raw_text='new wording',text='new wording',span='new wording')
    data['intents'].append(atom);data['rows'].append({'row_id':4,'raw_text':'new wording'})
    data['clusters'][0]['review_samples'].append(copy.deepcopy(atom))
    llm=LLM();run(data,llm)
    node=next(n for n in data['topic_hierarchy']['nodes'] if n['node_id']=='topic:0')
    assert node['support_level']==data['clusters'][0]['support_level']=='multiple_examples'
    assert node['semantic_status']=='approved'
    assert any(payload['node_id']=='topic:0' for payload in llm.calls)


def test_parent_prompt_change_does_not_invalidate_leaf_naming():
    data=source();run(data,LLM())
    llm=LLM();llm.cfg={'prompt_overrides':{'hierarchy_review.NODE_PROMPT':'changed parent grouping prompt'}}
    state=run(data,llm)
    assert state['reused_nodes']==4
    assert all(payload['kind']=='parent' for payload in llm.calls)


@pytest.mark.parametrize('review_change',[{'object':''},{'action':''},{'semantic_clear':False}])
def test_missing_or_unclear_upstream_semantics_cannot_admit_single_leaf(review_change):
    data=source();data['clusters'][0]['review'].update(review_change)
    llm=LLM();run(data,llm)
    assert not any(payload['node_id']=='topic:0' for payload in llm.calls)
    assert 'topic:0' not in data['task_topic_nodes'].values()
    assert data['clusters'][0]['hierarchy_eligible'] is False
