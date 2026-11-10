import copy
import json

import numpy as np
import pytest

from intent_engine.atomic.atomic_discovery import ClusterReview
from intent_engine.atomic.hierarchy_review import NodeDecision, LeafNamingDecision, review_hierarchy
from intent_engine.atomic.topic_hierarchy import build_topic_tree, attach_topic_memberships
from intent_engine.infrastructure.progress import ProgressReporter
from intent_engine.models import SemanticIntent, SemanticResult
from intent_engine.workflows.hierarchy_iteration import hierarchy_iteration_steps, run_hierarchy_iterations
from intent_engine.exporting.sentence_results import build_sentence_results


class LLM:
    def chat_json(self,prompt,user,schema,**kwargs):
        p=json.loads(user)
        if schema in {NodeDecision,LeafNamingDecision}:
            return schema(reason='shared subject',description='definition '+p['node_id'],label='name '+p['node_id'],**({'semantic_clear':True} if schema is LeafNamingDecision else {'decision':'group'}))
        if schema is ClusterReview:
            return schema(verdict='consistent',object='O'+str(p['cluster_id']),action='query',reason='same task')
        raise AssertionError(schema)


def make_tree(labels,keywords,options=None):
    topics=sorted(set(labels)-{-1});merges=[];parent=max(topics,default=-1)+1
    if topics:
        left=topics[0]
        for right in topics[1:]:
            merges.append(dict(Parent_ID=parent,Child_Left_ID=left,Child_Right_ID=right,Distance=.5))
            left=parent;parent+=1
    return keywords,build_topic_tree(labels,keywords,merges)


def dataset():
    labels=[0,1,-1,-1];texts=['A','B','C','D']
    atoms=[dict(intent_id=f'r{i}:i0',row_id=i,raw_text=t,text=t,span=t,slots={},status='valid',cluster_id=label)
           for i,(t,label) in enumerate(zip(texts,labels))]
    clusters=[dict(cluster_id=i,unique_text_count=1,keywords=[str(i)],review_samples=[copy.deepcopy(atoms[i])],
                   review=dict(verdict='consistent',object='O'+str(i),action='query',reason='same task')) for i in (0,1)]
    _,tree=make_tree(labels,{0:['0'],1:['1']})
    data=dict(run_id='test',parameters={'refinement_rounds':0},
              rows=[dict(row_id=i,raw_text=t) for i,t in enumerate(texts)],intents=atoms,clusters=clusters,
              errors=[],topic_hierarchy=tree)
    attach_topic_memberships(data)
    reporter=ProgressReporter();reporter.plan([{'id':'domains','label':'review'}]);reporter.start('domains')
    review_hierarchy(data,LLM(),2,reporter,lambda:None)
    return data


@pytest.fixture
def setup(tmp_path,monkeypatch):
    import intent_engine.workflows.pipeline as pipeline
    import intent_engine.atomic.atomic_discovery as discovery
    import intent_engine.atomic.cluster_refinement as refinement
    calls=[]
    class Pipeline:
        def __init__(self,cfg,progress):self.llm=LLM()
        def classify_dataframe(self,df,taxonomy,number):
            calls.append((number,copy.deepcopy(taxonomy)))
            tasks={t.object:t.task_id for t in taxonomy.tasks}
            out=[]
            for text in df['_atomic_text']:
                obj='O0' if text=='A' else 'O1' if text=='B' else 'O2'
                task=tasks.get(obj)
                out.append(SemanticResult(raw_text=text,intents=[SemanticIntent(status='known' if task else 'discovery',
                    object=obj,action='query',span=text,matched_task_id=task,confidence=.95)]))
            return out
    monkeypatch.setattr(pipeline,'IterativePipeline',Pipeline)
    monkeypatch.setattr(discovery,'cluster_atomic_texts',lambda texts,vectors,options: ([0]*len(texts),{0:['new']}))
    monkeypatch.setattr(refinement,'rebuild_hierarchy',lambda texts,vectors,labels,options:
                        make_tree(labels,{i:[str(i)] for i in set(labels) if i>=0}))
    return {'atomic_discovery':{'iteration_scope':'all','leaf_merge_enabled':False},'pipeline':{},'llm':{},'paths':{'checkpoint_dir':str(tmp_path)}},calls


def test_new_leaf_is_rebuilt_into_same_tree_then_validated_with_updated_labels(setup):
    cfg,calls=setup;data=dataset();before=copy.deepcopy(data['topic_hierarchy'])
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(cfg,data,1,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1]]))
    assert state['status']=='completed' and state['stop_reason']=='max_rounds'
    assert len(data['candidate_taxonomy']['tasks'])==3 and data['candidate_taxonomy']['version']==2
    assert state['validated_taxonomy_version']==2
    assert [a['cluster_id'] for a in data['intents']]==[0,1,2,2]
    assert calls[-1][1].version==2
    result=build_sentence_results(data)
    assert any(level['topics']==['name topic:2'] for level in result['records'][2]['topics_by_level'])
    assert data['topic_hierarchy']!=before and data['topic_hierarchy']['source']=='BERTopic+LLM'
    assert data['task_topic_nodes'][state['assignments'][2]['task_id']]=='topic:2'
    assert data['hierarchy_review']['reused_nodes']==2


def test_assignment_failure_rolls_back_to_committed_tree(setup,monkeypatch):
    cfg,calls=setup;data=dataset();before=copy.deepcopy(data)
    import intent_engine.workflows.pipeline as pipeline
    monkeypatch.setattr(pipeline.IterativePipeline,'classify_dataframe',lambda *args: (_ for _ in ()).throw(RuntimeError('offline')))
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(2))
    state=run_hierarchy_iterations(cfg,data,2,2,reporter,lambda:None)
    assert state['status']=='failed' and not state['converged']
    assert data['topic_hierarchy']==before['topic_hierarchy']
    assert data['candidate_taxonomy']==before['candidate_taxonomy']
    assert [(a['text'],a['cluster_id']) for a in data['intents']]==[(a['text'],a['cluster_id']) for a in before['intents']]


def test_unresolved_only_retries_only_unmatched_noise_and_keeps_full_export(setup, monkeypatch):
    cfg, calls = setup
    cfg['atomic_discovery'] = {'iteration_scope':'unresolved_only','leaf_merge_enabled':False}
    data = dataset()
    import intent_engine.workflows.pipeline as pipeline
    import intent_engine.atomic.atomic_discovery as discovery
    original = pipeline.IterativePipeline.classify_dataframe
    batches = []
    def track(self, frame, taxonomy, number):
        batches.append((number, frame['_atomic_text'].tolist()))
        return original(self, frame, taxonomy, number)
    monkeypatch.setattr(pipeline.IterativePipeline,'classify_dataframe',track)
    # First round discovers a new leaf for C, while D stays noise.
    monkeypatch.setattr(discovery,'cluster_atomic_texts',lambda texts,vectors,options: ([0,-1],{0:['new']}))
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(3))
    state=run_hierarchy_iterations(cfg,data,3,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1]]))
    assert state['status']=='completed'
    assert batches[0] == (1,['C','D'])
    assert batches[1] == (2,['D'])
    assert all(len(texts) < 4 for number,texts in batches[1:])
    assert state['rounds'][1]['classified_count']==1
    assert state['rounds'][1]['reused_count']==3
    progress = next(s for s in reporter.state.steps if s['id']=='round_2:classify')
    assert progress['total']==progress['completed']==1
    assert progress['reused']==3 and progress['cached']==0
    assert progress['selection_scope']=='unresolved_only'
    baseline = next(s for s in reporter.state.steps if s['id']=='round_1:classify')
    assert baseline['total']==2 and baseline['selection_scope']=='unresolved_only'
    assert baseline['reused']==2

    assert len(state['assignments'])==4
    assert all(r['status']=='known' for r in state['assignments'])
    assert state['assignments'][2]['assignment_source']=='retained_assignment' or state['assignments'][2]['assignment_source']=='audited_cluster'
    assert state['validated_taxonomy_version']==data['candidate_taxonomy']['version']
    results=build_sentence_results(data)
    assert all(any(level['topics'] for level in row['topics_by_level']) for row in results['records'])


def test_unresolved_only_without_noise_reuses_results_after_tree_update(setup, monkeypatch):
    cfg,calls=setup
    cfg['atomic_discovery']={'iteration_scope':'unresolved_only','leaf_merge_enabled':False}
    data=dataset();reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(2))
    state=run_hierarchy_iterations(cfg,data,2,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1]]))
    assert state['status']=='completed'
    assert len(calls)==1  # No all-data classification hidden in final validation.
    assert state['rounds'][1]['classified_count']==0
    assert state['rounds'][1]['reused_count']==4
    progress = next(s for s in reporter.state.steps if s['id']=='round_2:classify')
    assert progress['total']==progress['completed']==0
    assert progress['reused']==4

    assert all(r['status']=='known' for r in state['assignments'])


def test_retained_known_assignment_invalidates_changed_leaf(setup):
    from intent_engine.models import Taxonomy
    from intent_engine.workflows.hierarchy_iteration import reusable_assignment
    cfg,_=setup;data=dataset();reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(cfg,data,1,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1]]))
    prior=state['assignments'][0]
    tax=Taxonomy.model_validate(data['candidate_taxonomy'])
    assert reusable_assignment(data['intents'][0],prior,data,tax) is not None
    next(t for t in tax.tasks if t.task_id==prior['task_id']).description='changed boundary'
    assert reusable_assignment(data['intents'][0],prior,data,tax) is None


def test_iteration_singleton_clusters_do_not_request_consistency(setup,monkeypatch):
    cfg,_=setup;data=dataset();audit_sizes=[]
    original=LLM.chat_json
    def audit(self,prompt,user,schema,**kwargs):
        if schema is ClusterReview:
            size=len(json.loads(user)['members'])
            audit_sizes.append(size)
            assert size>1
        return original(self,prompt,user,schema,**kwargs)
    monkeypatch.setattr(LLM,'chat_json',audit)
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(cfg,data,1,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1]]))
    assert state['status']=='completed'
    assert audit_sizes==[2]
    for cluster in data['clusters'][:2]:
        assert cluster['review_source']=='singleton_skip'
        assert cluster['review']['verdict']=='uncertain'
        assert cluster['review']['semantic_clear'] is True
        assert cluster['support_level']=='single_example'
    assert len(data['candidate_taxonomy']['tasks'])==3


def test_unresolved_only_first_round_reuses_all_approved_members_without_any_matching(setup,monkeypatch):
    cfg,calls=setup;cfg['atomic_discovery']['iteration_scope']='unresolved_only'
    data=dataset();data['intents']=data['intents'][:2];data['rows']=data['rows'][:2]
    import intent_engine.workflows.pipeline as pipeline
    monkeypatch.setattr(pipeline.IterativePipeline,'classify_dataframe',lambda *a:pytest.fail('no noise to match'))
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(2))
    state=run_hierarchy_iterations(cfg,data,2,2,reporter,lambda:None)
    assert state['status']=='completed' and state['converged']
    assert state['rounds'][0]['classified_count']==0
    assert state['rounds'][0]['reused_count']==2
    assert state['validated_taxonomy_version']==1
    assert all(a['assignment_basis']=='audited_cluster' for a in state['assignments'])
    assert all(a['status']=='known' for a in state['assignments'])
    records=build_sentence_results(data)['records']
    assert len(records)==2 and all(any(level['topics'] for level in row['topics_by_level']) for row in records)


@pytest.mark.parametrize('scope', ['unresolved_only', 'noise_only'])
def test_incremental_matches_pending_nonnoise_cluster_and_reuses_confirmed_members(setup, monkeypatch, scope):
    cfg, _ = setup
    cfg['atomic_discovery']['iteration_scope'] = scope
    data = dataset()
    data['intents'] = data['intents'][:2]
    data['rows'] = data['rows'][:2]
    extra = dict(data['intents'][1], intent_id='r2:i0', row_id=2, raw_text='E', text='E', span='E')
    data['intents'].append(extra)
    data['rows'].append({'row_id': 2, 'raw_text': 'E'})
    pending = data['clusters'][1]
    pending.update(unique_text_count=2, review_samples=[data['intents'][1], extra],
                   review={'verdict': 'uncertain', 'reason': 'unclear boundary', 'object': '', 'action': ''})
    reporter = ProgressReporter()
    reporter.plan([{'id': 'domains', 'label': 'review'}])
    reporter.start('domains')
    review_hierarchy(data, LLM(), 2, reporter, lambda: None)
    assert len(data['candidate_taxonomy']['tasks']) == 1
    import intent_engine.workflows.pipeline as pipeline
    batches = []
    def classify(self, frame, taxonomy, number):
        batches.append(frame['_atomic_text'].tolist())
        task = taxonomy.tasks[0]
        return [SemanticResult(raw_text=text, intents=[SemanticIntent(status='known', object=task.object,
                    action=task.action, span=text, matched_task_id=task.task_id, confidence=.95)])
                for text in frame['_atomic_text']]
    monkeypatch.setattr(pipeline.IterativePipeline, 'classify_dataframe', classify)
    reporter = ProgressReporter()
    reporter.plan(hierarchy_iteration_steps(2))
    state = run_hierarchy_iterations(cfg, data, 2, 2, reporter, lambda: None, np.array([[1,0],[0,1],[.9,1]]))
    assert state['status'] == 'completed'
    assert state['iteration_scope'] == data['parameters']['iteration_scope'] == 'unresolved_only'
    assert batches == [['B', 'E']]
    assert state['rounds'][0]['classified_count'] == 2
    assert state['rounds'][0]['reused_count'] == 1
    assert all(record['status'] == 'known' for record in state['assignments'])
    assert all(any(level['topics'] for level in row['topics_by_level']) for row in build_sentence_results(data)['records'])


def test_pending_assignment_is_not_reused_as_confirmed_membership(setup):
    from intent_engine.models import Taxonomy
    from intent_engine.workflows.hierarchy_iteration import reusable_assignment, initial_membership_assignments
    data = dataset()
    taxonomy = Taxonomy.model_validate(data['candidate_taxonomy'])
    atom = data['intents'][2]
    atom['cluster_id'] = 10  # A real cluster ID does not establish an approved leaf assignment.
    prior = initial_membership_assignments([atom], data, taxonomy)[0]
    assert prior['status'] == 'ambiguous'
    assert reusable_assignment(atom, prior, data, taxonomy) is None


def test_unchanged_multi_member_leaves_skip_review_naming_and_final_matching(setup,monkeypatch):
    from intent_engine.atomic.review_reuse import remember_review
    cfg,_=setup;cfg['atomic_discovery']['iteration_scope']='unresolved_only'
    data=dataset()
    # Seed two multi-member approved clusters and leave C/D for discovery.
    for index,text in [(0,'A alternative'),(1,'B alternative')]:
        row_id=len(data['rows'])
        atom=dict(data['intents'][index],intent_id=f'r{row_id}:i0',row_id=row_id,text=text,raw_text=text,span=text)
        data['intents'].append(atom);data['rows'].append({'row_id':row_id,'raw_text':text})
        cluster=data['clusters'][index]
        cluster['review_samples'].append(copy.deepcopy(atom));cluster['unique_text_count']=2
    for cluster in data['clusters']:
        remember_review(cluster,[a for a in data['intents'] if a['cluster_id']==cluster['cluster_id']],LLM(),data['parameters'])
    reporter=ProgressReporter();reporter.plan([{'id':'domains','label':'review'}]);reporter.start('domains')
    review_hierarchy(data,LLM(),2,reporter,lambda:None)
    original={t['task_id']:copy.deepcopy(t) for t in data['candidate_taxonomy']['tasks']}
    import intent_engine.workflows.pipeline as pipeline
    batches=[]
    original_classify=pipeline.IterativePipeline.classify_dataframe
    def classify(self,frame,taxonomy,number):
        batches.append(frame['_atomic_text'].tolist())
        return original_classify(self,frame,taxonomy,number)
    monkeypatch.setattr(pipeline.IterativePipeline,'classify_dataframe',classify)
    original_chat=LLM.chat_json
    def chat(self,prompt,user,schema,**kwargs):
        payload=json.loads(user)
        if schema is ClusterReview:
            assert payload['cluster_id'] not in {0,1}, 'Unchanged multi-member clusters must reuse their audit'
        if schema is LeafNamingDecision:
            assert payload['node_id'] not in {'topic:0','topic:1'}, 'Unchanged leaves must not be renamed'
        return original_chat(self,prompt,user,schema,**kwargs)
    monkeypatch.setattr(LLM,'chat_json',chat)
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(cfg,data,1,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1],[1,0],[0,1]]))
    assert state['status']=='completed'
    assert batches==[['C','D']]
    assert state['rounds'][0]['review_reuse']=={'reused_clusters':2,'requested_clusters':1}
    assert data['hierarchy_review']['reused_nodes']==2
    assert state['validation_classified_count']==0
    assert state['validation_reused_count']==6
    final={t['task_id']:t for t in data['candidate_taxonomy']['tasks']}
    for task_id,old in original.items():
        for field in ('object','action','description'):
            assert final[task_id][field]==old[field]


def test_legacy_singleton_veto_is_rechecked_before_matching_without_membership_changes(setup,monkeypatch):
    cfg,_=setup;cfg['atomic_discovery']['iteration_scope']='unresolved_only'
    data=dataset();data['intents']=data['intents'][:2];data['rows']=data['rows'][:2]
    from intent_engine.atomic.hierarchy_review import derive_taxonomy
    leaf=next(node for node in data['topic_hierarchy']['nodes'] if node['node_id']=='topic:1')
    leaf.update(semantic_status='pending',review_reason='only one sample, insufficient stability')
    leaf.pop('leaf_admission_version',None)
    data['clusters'][1]['hierarchy_eligible']=False
    data['clusters'][1]['unique_text_count']=1
    derive_taxonomy(data)
    assert len(data['candidate_taxonomy']['tasks'])==1
    import intent_engine.workflows.pipeline as pipeline
    monkeypatch.setattr(pipeline.IterativePipeline,'classify_dataframe',lambda *args:pytest.fail('Clear singleton should be admitted before matching'))
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(cfg,data,1,2,reporter,lambda:None)
    assert state['status']=='completed'
    assert state['preparation']['admission_refresh']['clusters']==[1]
    assert len(data['candidate_taxonomy']['tasks'])==2
    assert all(record['status']=='known' for record in state['assignments'])
    assert data['clusters'][1]['support_level']=='single_example'
    assert data['clusters'][1]['hierarchy_eligible'] is True


def test_auto_stops_when_singleton_and_noise_counts_match_previous_round(setup):
    cfg,_=setup;data=dataset();data['parameters']['iteration_auto']=True
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(8))
    state=run_hierarchy_iterations(cfg,data,8,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1]]))
    assert state['stop_reason']=='counts_stable'
    assert len(state['rounds'])==2 and state['converged']
    assert state['rounds'][0]['previous_cluster_counts'] is None
    assert state['rounds'][1]['previous_cluster_counts']==state['rounds'][1]['cluster_counts']
    assert state['validated_taxonomy_version'] is not None


def test_auto_cap_is_not_reported_as_count_convergence(setup):
    cfg,_=setup;data=dataset();data['parameters']['iteration_auto']=True
    reporter=ProgressReporter();reporter.plan(hierarchy_iteration_steps(1))
    state=run_hierarchy_iterations(cfg,data,1,2,reporter,lambda:None,np.array([[1,0],[0,1],[1,1],[.9,1]]))
    assert state['stop_reason']=='auto_limit' and not state['converged']


def test_rejected_merge_does_not_stop_next_round(setup, monkeypatch):
    from intent_engine.atomic import leaf_merge

    cfg, _ = setup
    data = dataset()
    original = leaf_merge.merge_leaves

    def merge(*args, **kwargs):
        record, vectors = original(*args, **kwargs)
        if kwargs.get('phase') == 'round_1':
            args[1]['errors'].append({'stage': 'leaf_merge', 'error': 'invalid proposal'})
            record['status'] = 'completed_with_errors'
        return record, vectors

    monkeypatch.setattr(leaf_merge, 'merge_leaves', merge)
    reporter = ProgressReporter()
    reporter.plan(hierarchy_iteration_steps(2))
    state = run_hierarchy_iterations(
        cfg, data, 2, 2, reporter, lambda: None,
        np.array([[1, 0], [0, 1], [1, 1], [.9, 1]]),
    )
    assert len(state['rounds']) == 2
    assert state['rounds'][0]['processing_errors'] == 1
    assert state['rounds'][0]['blocking_errors'] == 0
    assert state['status'] == 'completed_with_errors'


def test_missing_model_response_keeps_membership_and_continues(setup, monkeypatch):
    from intent_engine.workflows import pipeline

    cfg, _ = setup
    data = dataset()
    original = pipeline.IterativePipeline.classify_dataframe

    def classify(self, frame, taxonomy, number):
        results = original(self, frame, taxonomy, number)
        if number == 1:
            results[0] = SemanticResult(raw_text='A', intents=[])
        return results

    monkeypatch.setattr(pipeline.IterativePipeline, 'classify_dataframe', classify)
    reporter = ProgressReporter()
    reporter.plan(hierarchy_iteration_steps(2))
    state = run_hierarchy_iterations(
        cfg, data, 2, 2, reporter, lambda: None,
        np.array([[1, 0], [0, 1], [1, 1], [.9, 1]]),
    )
    assert len(state['rounds']) == 2
    assert state['rounds'][0]['assignments'][0]['model_error']
    assert state['rounds'][0]['memberships'][0]['cluster_id'] == 0
    assert state['status'] == 'completed_with_errors'
