import copy
import json

import numpy as np
import pytest

import intent_engine.atomic.cluster_refinement as refinement
from intent_engine.atomic.atomic_discovery import ClusterReview, topic_vectorizer
from intent_engine.infrastructure.progress import ProgressReporter
from intent_engine.atomic.topic_hierarchy import build_topic_tree, attach_topic_memberships, index_topic_tree
from intent_engine.exporting.sentence_results import build_sentence_results


def dataset():
    texts = ['查询电费', '查看账单', '缴纳电费', '支付水费', '查询电费']
    atoms = [dict(intent_id=f'r{i}:i0', row_id=i, raw_text=t, text=t, span=t, slots={},
                  status='valid', cluster_id=0, text_index=min(i, 0) if i == 4 else i) for i, t in enumerate(texts)]
    cluster = dict(cluster_id=0, keywords=['账单'], review={'verdict': 'mixed'}, review_samples=[])
    data = dict(rows=[{'row_id': i, 'raw_text': t} for i,t in enumerate(texts)], intents=atoms,
                clusters=[cluster], errors=[], neighbor_pairs=[], pair_reviews=[],
                topic_hierarchy=build_topic_tree([0], {0: ['账单']}))
    attach_topic_memberships(data)
    return data


class LLM:
    def chat_json(self, system, user, schema, **kwargs):
        payload = json.loads(user)
        if schema is refinement.FullPartition:
            assert all(set(m) == {'intent_id', 'text'} for m in payload['members'])
            ids = [m['intent_id'] for m in payload['members']]
            return schema(groups=[{'label':'查询', 'evidence_ids':ids[:2]}, {'label':'缴费', 'evidence_ids':ids[2:]}])
        assert 'review_samples' not in payload and 'review' not in payload
        assert all(set(m) == {'intent_id', 'text'} for m in payload['members'])
        return schema(verdict='consistent', object='账单', action='处理', reason='同种动作')


def patch_tree(monkeypatch):
    def tree(texts, vectors, labels, options):
        ids = sorted(set(labels) - {-1})
        keywords = {i: [f'主题{i}'] for i in ids}
        return keywords, build_topic_tree(labels, keywords, status='not_generated')
    monkeypatch.setattr(refinement, 'rebuild_hierarchy', tree)


def run(data, monkeypatch, llm=None, **options):
    patch_tree(monkeypatch)
    snapshots = []
    refinement.refine_clusters(data, np.eye(4), options, llm or LLM(), ProgressReporter(), lambda: snapshots.append(copy.deepcopy(data)))
    return snapshots


def test_local_split_reaudits_all_children_expands_duplicates_and_replaces_paths(monkeypatch):
    monkeypatch.setattr(refinement, 'cluster_atomic_texts', lambda texts,vectors,cfg: ([0,0,1,1], {0:['查询'],1:['缴费']}))
    data = dataset()
    snapshots = run(data, monkeypatch)
    assert [a['cluster_id'] for a in data['intents']] == [1,1,2,2,1]
    assert all(c['review']['verdict'] == 'consistent' for c in data['clusters'])
    assert data['adjustments_applied'] is True
    assert data['cluster_refinement']['history'][0]['parent']['cluster_id'] == 0
    assert data['cluster_refinement']['history'][0]['method'] == 'llm_full_partition'
    assert len(data['neighbor_pairs']) == 1
    assert data['rows'][0]['topic_paths'][0]['node_ids'] == ['topic:1']
    for snapshot in snapshots:
        if snapshot.get('topic_hierarchy'):
            attach_topic_memberships(snapshot, validate_existing=True)
    assert len(build_sentence_results(data)['records']) == 5


def test_no_vector_split_uses_validated_full_llm_partition(monkeypatch):
    monkeypatch.setattr(refinement, 'cluster_atomic_texts', lambda *args: ([-1]*4, {}))
    data = dataset()
    run(data, monkeypatch)
    assert len(data['clusters']) == 2
    assert data['cluster_refinement']['history'][0]['method'] == 'llm_full_partition'


@pytest.mark.parametrize('ids', [['a'], ['a','a','b'], ['a','invented']])
def test_partition_rejects_omissions_duplicates_and_unknown_ids(ids):
    partition = refinement.FullPartition(groups=[{'label':'A','evidence_ids':[ids[0]]},
                                                  {'label':'B','evidence_ids':ids[1:] or ['invented']}])
    with pytest.raises(ValueError, match='完整且不重复'):
        refinement.validate_partition(partition, [{'intent_id':'a'}, {'intent_id':'b'}])


def test_repeated_mixed_stops_at_branch_limit_and_keeps_raw_sentences(monkeypatch):
    monkeypatch.setattr(refinement, 'cluster_atomic_texts', lambda texts,vectors,cfg: ([0 if i < len(texts)//2 else 1 for i in range(len(texts))], {}))
    class Mixed(LLM):
        def chat_json(self, system, user, schema, **kwargs):
            if schema is refinement.FullPartition:return super().chat_json(system,user,schema,**kwargs)
            ids = [m['intent_id'] for m in json.loads(user)['members']]
            return schema(verdict='mixed', reason='不同动作',
                          split_groups=[{'label':'A','evidence_ids':ids[:1]}, {'label':'B','evidence_ids':ids[1:]}])
    data = dataset()
    run(data, monkeypatch, Mixed(), refinement_rounds=1)
    assert len(data['cluster_refinement']['history']) == 1
    assert all(c['refinement_stop'] == 'max_rounds' for c in data['clusters'])
    assert all(a['topic_pending'] for a in data['intents'])
    assert all(not level['topics'] for r in build_sentence_results(data)['records'] for level in r['topics_by_level'])


def test_large_unseparated_cluster_never_assigns_from_sample_suggestions(monkeypatch):
    monkeypatch.setattr(refinement, 'cluster_atomic_texts', lambda *args: ([0]*4, {}))
    data = dataset()
    run(data, monkeypatch, refinement_llm_limit=2)
    assert len(data['clusters']) == 1
    assert data['clusters'][0]['refinement_stop'] == 'llm_limit'
    assert data['cluster_refinement']['history'][0]['applied'] is False


def test_failed_semantic_review_preserves_original_cluster_pending(monkeypatch):
    monkeypatch.setattr(refinement, 'cluster_atomic_texts', lambda *args: ([0,0,1,-1], {}))
    class Failing(LLM):
        def chat_json(self, *args, **kwargs):
            raise RuntimeError('offline')
    data = dataset()
    run(data, monkeypatch, Failing())
    assert all(a['cluster_id']==0 for a in data['intents'])
    assert data['clusters'][0]['refinement_stop']=='no_reliable_split'
    assert data['intents'][3]['topic_pending'] is True
    assert all(a['topic_pending'] for a in data['intents'])
    assert not data['errors']
    assert data['cluster_refinement']['history'][0]['errors']


def test_stop_words_filter_topic_features_without_removing_actions_or_negation():
    texts = ['Please 查询 the 账单', '帮我 打开 应用 的 页面', '不要 取消 订单', 'via 支付 水费']
    vocab = topic_vectorizer({'topic_stop_words':['页面']}).fit(texts).vocabulary_
    assert not {'please','the','帮我','的','页面','via'} & set(vocab)
    assert {'查询','账单','打开','取消','订单'} <= set(vocab)
    assert '不要' in vocab
    no_stop = topic_vectorizer({'topic_english_stop_words':False, 'topic_chinese_stop_words':False}).fit(texts).vocabulary_
    assert 'the' in no_stop and '的' in no_stop
    with pytest.raises(ValueError):
        topic_vectorizer({'topic_stop_words':'the'})


def test_real_rebuilt_hierarchy_preserves_non_contiguous_workflow_ids():
    pytest.importorskip('bertopic')
    texts = ['please 查询电费','the 查看电费账单','查询账单','via 支付水费','缴纳水费','支付账单']
    labels = [4,4,4,9,9,9]
    keywords, tree = refinement.rebuild_hierarchy(texts, np.eye(6), labels, {})
    assert set(keywords) == {4,9}
    words = {word for values in keywords.values() for word in values}
    assert any(any('\u4e00' <= ch <= '\u9fff' for ch in word) for word in words)
    assert not {'please', 'the', 'via', 'emptydoc'} & words
    assert tree['status'] == 'ready'
    assert tree['leaf_count'] == 2
    _, paths = index_topic_tree(tree, {4,9})
    assert all(len(path) == 2 for path in paths.values())


def test_singleton_dominated_vector_split_uses_semantic_partition(monkeypatch):
    monkeypatch.setattr(refinement,'cluster_atomic_texts',lambda *args:([0,1,2,3],{}))
    data=dataset()
    run(data,monkeypatch)
    record=data['cluster_refinement']['history'][0]
    assert record['local_rejected']=='singleton_dominated'
    assert record['method']=='llm_full_partition'
    assert len(data['clusters'])==2


def test_iteration_refinement_single_text_children_reuse_parsing_without_audit(monkeypatch):
    patch_tree(monkeypatch)
    monkeypatch.setattr(refinement,'cluster_atomic_texts',lambda *args: ([0,1,2,3],{}))
    data=dataset()
    records=[{**{k:a[k] for k in ('intent_id','row_id','raw_text','span','text')},
              'object':'账单','action':'处理','error':None} for a in data['intents']]
    class PartitionOnly:
        def chat_json(self,prompt,user,schema,**kwargs):
            assert schema is refinement.FullPartition
            return schema(groups=[{'label':m['text'],'evidence_ids':[m['intent_id']]} for m in json.loads(user)['members']])
    refinement.refine_clusters(data,np.eye(4),{},PartitionOnly(),ProgressReporter(),lambda:None,semantic_records=records)
    assert len(data['clusters'])==4
    assert all(c['review_source']=='singleton_skip' for c in data['clusters'])
    assert all(c['review']['semantic_clear'] for c in data['clusters'])
    assert data['clusters'][0]['sample_count']==2  # Repeated identical text still skips.


def test_singleton_skip_requires_matching_semantic_evidence():
    from intent_engine.atomic.atomic_discovery import singleton_review,review_eligible
    members=dataset()['intents'][:1]
    review=singleton_review(members,[])
    assert review is not None and review.semantic_clear is False
    assert not review_eligible({'unique_text_count':1,'review':review.model_dump()})
    assert singleton_review(dataset()['intents'][:2],[]) is None
    assert singleton_review(members,None) is None  # Initial discovery keeps existing behavior.


def test_model_can_keep_even_when_vector_split_proposes_children(monkeypatch):
    monkeypatch.setattr(refinement,'cluster_atomic_texts',lambda *a:([0,0,1,1],{}))
    data=dataset();before=[a['cluster_id'] for a in data['intents']]
    class Keep:
        def chat_json(self,system,user,schema,**kwargs):
            payload=json.loads(user)
            assert schema is refinement.FullPartition and len(payload['candidate_groups'])==2
            return schema(reason='完整语义支持同类',object='账单',action='处理',
                groups=[{'label':'账单处理','evidence_ids':[m['intent_id'] for m in payload['members']]}],decision='keep')
    run(data,monkeypatch,llm=Keep())
    assert [a['cluster_id'] for a in data['intents']]==before
    assert len(data['clusters'])==1 and data['clusters'][0]['review']['verdict']=='consistent'
    assert data['clusters'][0]['refinement_stop']=='semantic_keep'


def test_keep_cannot_hide_omitted_or_uncertain_members():
    partition=refinement.FullPartition(object='账单',action='查询',groups=[{'label':'账单','evidence_ids':['a']}],decision='keep')
    with pytest.raises(ValueError):refinement.validate_partition(partition,[{'intent_id':'a'},{'intent_id':'b'}])
