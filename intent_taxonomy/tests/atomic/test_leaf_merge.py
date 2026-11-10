import copy
import json

import numpy as np
import pytest

from intent_engine.atomic.hierarchy_review import NodeDecision, LeafNamingDecision, review_hierarchy
from intent_engine.atomic.leaf_merge import MergeProposal, MergeValidation, merge_leaves
from intent_engine.atomic.topic_hierarchy import build_topic_tree
from intent_engine.infrastructure.progress import ProgressReporter
from intent_engine.models import Taxonomy
from intent_engine.workflows.hierarchy_iteration import initial_membership_assignments, reusable_assignment


class LLM:
    cfg={'model':'test','temperature':.1}
    def __init__(self,decision='merge',valid=True):
        self.calls=[];self.decision=decision;self.valid=valid
    def chat_json(self,prompt,user,schema,**kwargs):
        value=json.loads(user);self.calls.append((schema,value))
        if schema in {NodeDecision,LeafNamingDecision}:
            return schema(reason='clear',description='original '+value['node_id'],label='original '+value['node_id'],**({'semantic_clear':True} if schema is LeafNamingDecision else {'decision':'group'}))
        if schema is MergeProposal:
            return schema(reason='only item instances differ',description='query the material used to make an item',
                          label='物品制作材料查询',object='物品制作材料',action='查询材料',decision=self.decision)
        if schema is MergeValidation:
            return schema(reason='same requested attribute' if self.valid else 'different actions',valid=self.valid)
        raise AssertionError(schema)


def seed(topics=2):
    atoms=[];clusters=[];rows=[]
    for topic in range(topics):
        members=[]
        for j in range(2):
            row_id=len(rows)+1;text=f'item {topic}, material question {j}'
            atom=dict(intent_id=f'r{row_id}:i0',row_id=row_id,text=text,span=text,raw_text=text,start=0,end=len(text),status='valid',cluster_id=topic)
            atoms.append(atom);members.append(copy.deepcopy(atom));rows.append(dict(row_id=row_id,raw_text=text))
        clusters.append(dict(cluster_id=topic,unique_text_count=2,sample_count=2,review_samples=members,keywords=['material'],
                             review=dict(reason='clear',object=f'item {topic} material',action='query material',semantic_clear=True,verdict='consistent')))
    data=dict(run_id='test',mode='atomic_actions',completed_stages=['extract','cluster','review','domains'],
              parameters={'review_examples':8},intents=atoms,rows=rows,clusters=clusters,errors=[],
              topic_hierarchy=build_topic_tree([a['cluster_id'] for a in atoms],{i:['material'] for i in range(topics)}))
    reporter=ProgressReporter();reporter.plan([{'id':'domains','label':'review'},{'id':'merge','label':'merge'}]);reporter.start('domains')
    review_hierarchy(data,LLM(),2,reporter,lambda:None)
    reporter.start('merge')
    vectors=np.array([[1.,.01*i] for i in range(len(atoms))])
    return data,vectors,reporter


@pytest.fixture
def setup(tmp_path,monkeypatch):
    from intent_engine.atomic import cluster_refinement
    def rebuild(texts,vectors,labels,options):
        keys={label:['material'] for label in set(labels) if label>=0}
        return keys,build_topic_tree(labels,keys)
    monkeypatch.setattr(cluster_refinement,'rebuild_hierarchy',rebuild)
    return {'atomic_discovery':{'leaf_merge_enabled':True,'leaf_merge_top_k':3,'leaf_merge_min_similarity':.75},
            'paths':{'checkpoint_dir':str(tmp_path)},'llm':{}}


def test_verified_merge_really_moves_members_ids_and_exports_a_single_leaf(setup):
    data,vectors,reporter=seed()
    original=copy.deepcopy(data)
    old_tasks=sorted(t['task_id'] for t in data['candidate_taxonomy']['tasks'])
    prior=initial_membership_assignments(data['intents'],data,Taxonomy.model_validate(data['candidate_taxonomy']))
    llm=LLM()
    result,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert result['applied']==1 and result['status']=='completed'
    assert len(data['clusters'])==1 and len(data['candidate_taxonomy']['tasks'])==1
    assert {a['cluster_id'] for a in data['intents']}=={0}
    assert data['task_aliases']=={old_tasks[1]:old_tasks[0]}
    task=data['candidate_taxonomy']['tasks'][0]
    assert task['task_id']==old_tasks[0] and task['object']=='物品制作材料'
    assert task['description']=='query the material used to make an item'
    assert data['topic_hierarchy']['leaf_count']==1
    assert data['bertopic_hierarchy']['leaf_count']==1
    assert data['candidate_taxonomy']['version']==original['candidate_taxonomy']['version']+1
    for atom,record in zip(data['intents'],prior):
        migrated=reusable_assignment(atom,record,data,Taxonomy.model_validate(data['candidate_taxonomy']))
        assert migrated['status']=='known' and migrated['task_id']==old_tasks[0]
        assert migrated['assignment_basis']=='audited_cluster' and migrated['confidence'] is None
    assert sum(schema is MergeProposal for schema,payload in llm.calls)==1
    assert sum(schema is MergeValidation for schema,payload in llm.calls)==1
    assert not any(schema in {NodeDecision,LeafNamingDecision} for schema,payload in llm.calls)


@pytest.mark.parametrize('decision,valid',[('keep',True),('uncertain',True),('merge',False)])
def test_rejected_or_uncertain_proposals_do_not_change_memberships(setup,decision,valid):
    data,vectors,reporter=seed();old=copy.deepcopy(data);llm=LLM(decision,valid)
    result,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert result['applied']==0
    assert data['candidate_taxonomy']==old['candidate_taxonomy']
    assert [a['cluster_id'] for a in data['intents']]==[a['cluster_id'] for a in old['intents']]
    assert 'task_aliases' not in data


def test_rebuild_failure_rolls_back_members_and_aliases(setup,monkeypatch):
    from intent_engine.atomic import cluster_refinement
    monkeypatch.setattr(cluster_refinement,'rebuild_hierarchy',lambda *a:(_ for _ in ()).throw(RuntimeError('rebuild failed')))
    data,vectors,reporter=seed();old=copy.deepcopy(data)
    result,_=merge_leaves(setup,data,LLM(),2,reporter,lambda:None,vectors=vectors)
    assert result['status']=='failed' and result['applied']==0
    assert data['candidate_taxonomy']==old['candidate_taxonomy'] and data['topic_hierarchy']==old['topic_hierarchy']
    assert [a['cluster_id'] for a in data['intents']]==[a['cluster_id'] for a in old['intents']]
    assert 'task_aliases' not in data


def test_shared_leaf_is_deferred_not_transitively_union_merged(setup):
    data,vectors,reporter=seed(3)
    result,_=merge_leaves(setup,data,LLM(),3,reporter,lambda:None,vectors=vectors)
    assert result['applied']==1
    assert len(data['candidate_taxonomy']['tasks'])==2
    assert any(pair['status']=='deferred_overlap' for pair in result['pairs'])
    assert len(data['task_aliases'])==1


def test_keep_cache_invalidates_with_definition_or_prompt_change(setup):
    data,vectors,reporter=seed();llm=LLM('keep')
    first,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert first['logical_requests']==1
    second,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert second.get('skipped') and second['logical_requests']==0
    data['candidate_taxonomy']['tasks'][0]['description']='changed boundary'
    third,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert third['logical_requests']==1
    llm.cfg={**llm.cfg,'prompt_overrides':{'leaf_merge.MERGE_PROMPT':'different instructions'}}
    fourth,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert fourth['logical_requests']==1


def test_conflict_pair_is_checked_even_below_similarity_threshold(setup):
    data,vectors,reporter=seed();vectors[:2]=[1.,0.];vectors[2:]=[0.,1.]
    conflicts=[{'status':'ambiguous','text':'overlapping intent','conflicting_task_ids':list(data['task_topic_nodes'])}]
    result,_=merge_leaves(setup,data,LLM('keep'),2,reporter,lambda:None,vectors=vectors,conflicts=conflicts)
    assert result['logical_requests']==1


def test_invalid_verification_evidence_does_not_merge(setup):
    class Invalid(LLM):
        def chat_json(self,prompt,user,schema,**kwargs):
            if schema is MergeValidation:return schema(reason='bad reference',valid=False,rejected_texts=['fabricated text'])
            return super().chat_json(prompt,user,schema,**kwargs)
    data,vectors,reporter=seed()
    result,_=merge_leaves(setup,data,Invalid(),2,reporter,lambda:None,vectors=vectors)
    assert result['applied']==0 and result['status']=='completed_with_errors'
    assert len(data['clusters'])==2


def test_merge_preparation_integrates_with_incremental_iteration_without_rematching(setup,monkeypatch):
    import intent_engine.workflows.pipeline as pipeline
    from intent_engine.workflows.hierarchy_iteration import run_hierarchy_iterations,hierarchy_iteration_steps
    data,vectors,_=seed();setup['atomic_discovery']['iteration_scope']='unresolved_only'
    class Pipeline:
        def __init__(self,*args,**kwargs):self.llm=LLM()
        def classify_dataframe(self,*args):pytest.fail('Audited merged memberships must be inherited')
    monkeypatch.setattr(pipeline,'IterativePipeline',Pipeline)
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(setup,data,1,2,reporter,lambda:None,vectors)
    assert state['status']=='completed'
    assert state['preparation']['applied']==1
    assert all(a['status']=='known' for a in state['assignments'])
    assert len(data['candidate_taxonomy']['tasks'])==1
    assert state['validated_taxonomy_version']==data['candidate_taxonomy']['version']


def test_disjoint_pairs_audit_in_parallel_and_commit_without_shared_leaves(setup):
    from threading import Barrier, Lock
    barrier=Barrier(2,timeout=5);lock=Lock();active=0;peak=0
    class Parallel(LLM):
        def chat_json(self,prompt,user,schema,**kwargs):
            nonlocal active,peak
            if schema is MergeProposal:
                with lock:
                    active+=1;peak=max(peak,active)
                barrier.wait()
                with lock:active-=1
            return super().chat_json(prompt,user,schema,**kwargs)
    data,vectors,reporter=seed(4)
    vectors[:4]=[1.,0.];vectors[4:]=[0.,1.]
    result,_=merge_leaves(setup,data,Parallel(),2,reporter,lambda:None,vectors=vectors)
    assert peak==2 and result['applied']==2
    assert len(data['candidate_taxonomy']['tasks'])==2
    assert len(data['task_aliases'])==2


def test_persist_failure_does_not_leave_in_memory_partial_merge(setup):
    data,vectors,reporter=seed();old=copy.deepcopy(data)
    def fail():raise PermissionError('save failed')
    result,_=merge_leaves(setup,data,LLM(),2,reporter,fail,vectors=vectors)
    assert result['status']=='failed' and result['applied']==0
    assert data['candidate_taxonomy']==old['candidate_taxonomy']
    assert [a['cluster_id'] for a in data['intents']]==[a['cluster_id'] for a in old['intents']]
    assert 'task_aliases' not in data


def test_budget_is_bounded_and_cached_decisions_allow_next_candidates(setup):
    setup['atomic_discovery']['leaf_merge_max_pairs']=1
    data,vectors,reporter=seed(3);llm=LLM('keep')
    first,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert first['logical_requests']==1 and first['deferred_candidates']==2
    second,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert second['logical_requests']==1 and second['cached']==1 and second['deferred_candidates']==1


@pytest.mark.parametrize('legacy_status', [None, 'ambiguous', 'discovery'])
def test_unresolved_conflicts_are_not_silently_relabelled_from_old_membership(setup,monkeypatch,legacy_status):
    import intent_engine.workflows.pipeline as pipeline
    from intent_engine.models import SemanticResult, SemanticIntent
    from intent_engine.workflows.hierarchy_iteration import run_hierarchy_iterations,hierarchy_iteration_steps
    data,vectors,_=seed();setup['atomic_discovery']['iteration_scope']='unresolved_only'
    previous=initial_membership_assignments(data['intents'],data,Taxonomy.model_validate(data['candidate_taxonomy']))
    previous[0].update(status='ambiguous',task_id=None,conflicting_task_ids=list(data['task_topic_nodes']),assignment_basis='llm_classification')
    if legacy_status:
        previous[0]['status']=legacy_status
        previous[0].pop('conflicting_task_ids')
    data['iteration']={'assignments':previous}
    batches=[]
    class Pipeline:
        def __init__(self,*args,**kwargs):self.llm=LLM('keep')
        def classify_dataframe(self,frame,taxonomy,number):
            batches.append(frame['_atomic_text'].tolist())
            return [SemanticResult(raw_text=text,intents=[SemanticIntent(status='ambiguous',span=text,
                        object='material',action='query',conflicting_task_ids=[task.task_id for task in taxonomy.tasks])])
                    for text in frame['_atomic_text']]
    monkeypatch.setattr(pipeline,'IterativePipeline',Pipeline)
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(setup,data,1,2,reporter,lambda:None,vectors)
    assert state['status']=='completed'
    assert batches==[[data['intents'][0]['text']]]
    assert state['assignments'][0]['status']=='ambiguous'
    assert len(data['candidate_taxonomy']['tasks'])==2


def test_parent_review_failure_is_partial_not_reported_as_clean_success(setup,monkeypatch):
    from intent_engine.atomic import cluster_refinement
    def rebuild(texts,vectors,labels,options):
        topics=sorted(set(labels)-{-1});parent=max(topics)+1
        keys={topic:['material'] for topic in topics}
        return keys,build_topic_tree(labels,keys,[{'Parent_ID':parent,'Child_Left_ID':topics[0],'Child_Right_ID':topics[1],'Distance':.3}])
    monkeypatch.setattr(cluster_refinement,'rebuild_hierarchy',rebuild)
    class ParentFailure(LLM):
        def chat_json(self,prompt,user,schema,**kwargs):
            if schema is NodeDecision and json.loads(user)['kind']=='parent':raise RuntimeError('parent unavailable')
            return super().chat_json(prompt,user,schema,**kwargs)
    data,vectors,reporter=seed(3)
    result,_=merge_leaves(setup,data,ParentFailure(),2,reporter,lambda:None,vectors=vectors)
    assert result['applied']==1 and result['status']=='completed_with_errors'
    assert len(data['candidate_taxonomy']['tasks'])==2
    assert any('parent unavailable' in error['error'] for error in data['errors'])


def test_completed_unchanged_pass_skips_embedding_and_audits(setup, monkeypatch):
    data,vectors,reporter=seed()
    llm=LLM(decision='keep')
    first,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert first['logical_requests']==1
    from intent_engine.atomic.atomic_discovery import SemanticEmbeddings
    monkeypatch.setattr(SemanticEmbeddings,'encode',lambda *a:pytest.fail('unchanged pass must not encode'))
    calls=len(llm.calls)
    second,_=merge_leaves(setup,data,llm,2,reporter,lambda:None)
    assert second['skipped'] and second['logical_requests']==0
    assert len(llm.calls)==calls


def test_completed_pass_invalidated_by_definition_and_new_conflicts(setup):
    data,vectors,reporter=seed()
    llm=LLM(decision='keep')
    merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    data['candidate_taxonomy']['tasks'][0]['description']+=' new boundary'
    second,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert not second.get('skipped') and second['logical_requests']==1
    ids=[t['task_id'] for t in data['candidate_taxonomy']['tasks']]
    third,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors,
                        conflicts=[dict(intent_id='r1:i0',text='conflicting case',status='ambiguous',conflicting_task_ids=ids)])
    assert not third.get('skipped') and third['logical_requests']==1


def test_deferred_pass_is_not_marked_complete(setup):
    data,vectors,reporter=seed(3)
    data['parameters']['leaf_merge_max_pairs']=1
    llm=LLM(decision='keep')
    result,_=merge_leaves(setup,data,llm,2,reporter,lambda:None,vectors=vectors)
    assert result['deferred_candidates']>0
    assert 'completed_identity' not in data['leaf_merging']


def test_parent_regroup_preserves_leaf_tasks_and_members(setup,monkeypatch):
    from intent_engine.atomic.parent_regroup import regroup,Proposal,Verification
    from intent_engine.atomic.atomic_discovery import SemanticEmbeddings
    from intent_engine.atomic.topic_hierarchy import index_topic_tree
    data,vectors,reporter=seed()
    original=[(a['intent_id'],a['cluster_id']) for a in data['intents']]
    tasks=copy.deepcopy(data['candidate_taxonomy']['tasks'])
    data['parameters'].update(parent_regroup_enabled=True,parent_regroup_rounds=1)
    monkeypatch.setattr(SemanticEmbeddings,'encode',lambda self,texts,progress:np.ones((len(texts),2)))
    monkeypatch.setattr('intent_engine.atomic.atomic_discovery.embedding_settings',lambda cfg:{})
    monkeypatch.setattr(SemanticEmbeddings,'__init__',lambda *a:None)
    class ParentLLM(LLM):
        def chat_json(self,prompt,user,schema,**kw):
            if schema is Proposal:
                value=json.loads(user)
                return Proposal(reason='shared material knowledge',label='材料知识',description='材料知识问答',target_id=value['peer_roots'][0]['node_id'],operation='create_parent')
            return Verification(reason='compatible children',approved=True)
    regroup(data,setup,ParentLLM(),2)
    index_topic_tree(data['topic_hierarchy'])
    assert len(data['topic_hierarchy']['root_ids'])==1
    assert data['parent_regroup']['passes'][-1]['applied']==1
    assert original==[(a['intent_id'],a['cluster_id']) for a in data['intents']]
    assert tasks==data['candidate_taxonomy']['tasks']


@pytest.mark.parametrize('decision',['common_parent','normalize'])
def test_semantic_relation_operations_are_applied_after_independent_validation(setup,decision):
    data,vectors,reporter=seed();members=[a['cluster_id'] for a in data['intents']]
    class Relation(LLM):
        def chat_json(self,system,user,schema,**kwargs):
            if schema is MergeProposal:
                return schema(reason='语义审核',label='制作材料知识',description='物品材料相关请求',
                    object='物品材料',action='查询',decision=decision)
            return super().chat_json(system,user,schema,**kwargs)
    record,_=merge_leaves(setup,data,Relation(),2,reporter,lambda:None,vectors=vectors)
    if decision=='common_parent':
        assert record['parent_applied']==1
        assert [a['cluster_id'] for a in data['intents']]==members
        assert len(data['candidate_taxonomy']['tasks'])==2
        assert len(data['topic_hierarchy']['root_ids'])==1
        assert any(n['label']=='制作材料知识' and not n['is_leaf'] for n in data['topic_hierarchy']['nodes'])
    else:
        assert record['applied']==1 and len(data['candidate_taxonomy']['tasks'])==1


def test_invalid_common_parent_does_not_change_tree(setup):
    data,vectors,reporter=seed();tree=copy.deepcopy(data['topic_hierarchy'])
    class Invalid(LLM):
        def chat_json(self,system,user,schema,**kwargs):
            if schema is MergeProposal:return schema(reason='关系建议',parent_node_id='missing',decision='common_parent')
            return super().chat_json(system,user,schema,**kwargs)
    record,_=merge_leaves(setup,data,Invalid(),2,reporter,lambda:None,vectors=vectors)
    assert record['parent_applied']==0 and data['topic_hierarchy']==tree
    assert record['pairs'][0]['status']=='error'


def test_flat_mode_retains_descriptions_ids_and_members_when_removing_parents():
    from intent_engine.atomic.topic_mode import flatten_existing
    from intent_engine.atomic.topic_hierarchy import index_topic_tree
    data,_,_=seed()
    leaves=copy.deepcopy(data['topic_hierarchy']['nodes'])
    data['topic_hierarchy']['nodes'].append({'node_id':'p','label':'父主题','description':'上层归纳','is_leaf':False,'distance':0.0,
        'children':[n['node_id'] for n in leaves],'semantic_status':'approved'})
    index_topic_tree(data['topic_hierarchy'])
    before=[(a['intent_id'],a['cluster_id']) for a in data['intents']]
    tasks=set(data['task_topic_nodes'])
    prior=initial_membership_assignments(data['intents'],data,Taxonomy.model_validate(data['candidate_taxonomy']))
    flatten_existing(data)
    for atom,assignment in zip(data['intents'],prior):
        assert reusable_assignment(atom,assignment,data,Taxonomy.model_validate(data['candidate_taxonomy'])) is not None
    assert set(data['task_topic_nodes'])==tasks
    assert [(a['intent_id'],a['cluster_id']) for a in data['intents']]==before
    assert all(n['is_leaf'] and n['description'] for n in data['topic_hierarchy']['nodes'])
    assert data['topic_hierarchy']['max_depth']==1
    assert all(len(a['topic_path'])==1 for a in data['intents'])


def test_flat_review_never_requests_parent_naming_or_regroup():
    data,_,reporter=seed()
    data['topic_hierarchy']['nodes'].append({'node_id':'p','label':'父级','is_leaf':False,
        'children':data['topic_hierarchy']['root_ids'][:],'description':'上层','semantic_status':'approved'})
    llm=LLM()
    review_hierarchy(data,llm,2,reporter,lambda:None,cfg={'atomic_discovery':{'flat_topics':True,'parent_regroup_enabled':True}})
    assert not any(schema is NodeDecision for schema,_ in llm.calls)
    assert data['hierarchy_review']['accepted_parents']==0
    assert all(n['is_leaf'] for n in data['topic_hierarchy']['nodes'])


def test_flat_merge_only_accepts_cluster_operations(setup):
    from intent_engine.atomic.leaf_merge import FlatMergeProposal
    data,vectors,reporter=seed();setup['atomic_discovery']['flat_topics']=True
    class Flat(LLM):
        def chat_json(self,prompt,user,schema,**kwargs):
            if schema is FlatMergeProposal:
                assert json.loads(user)['topic_mode']=='flat'
                return schema(reason='不同类别',decision='keep')
            return super().chat_json(prompt,user,schema,**kwargs)
    record,_=merge_leaves(setup,data,Flat(),2,reporter,lambda:None,vectors=vectors)
    assert record['applied']==0
    with pytest.raises(ValueError):FlatMergeProposal(reason='相关',label='父级',description='说明',decision='common_parent')
