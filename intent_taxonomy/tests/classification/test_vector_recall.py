import json

import numpy as np
import pytest

from intent_engine.atomic import atomic_discovery
from intent_engine.classification.vector_recall import VectorTaskRecall
from intent_engine.classification.classifier import IntentClassifier
from intent_engine.models import DomainDef, TaskDef, Taxonomy, SemanticIntent


@pytest.fixture
def encoder(monkeypatch):
    class FakeEncoder:
        calls = []
        def __init__(self, cfg, cache_dir):
            pass
        _validate = staticmethod(atomic_discovery.SemanticEmbeddings._validate)
        def encode(self, texts, progress):
            self.calls.append(list(texts))
            return np.array([[0., 1.] if 'transfer' in text else [1., 0.] for text in texts])
    monkeypatch.setattr(atomic_discovery, 'SemanticEmbeddings', FakeEncoder)
    return FakeEncoder


def taxonomy():
    return Taxonomy(domains=[DomainDef(name='banking', parent_domain='finance')], tasks=[
        TaskDef(task_id='balance', domain='banking', object='account', action='check',
                description='available funds', positive_examples=['remaining money']),
        TaskDef(task_id='transfer', domain='banking', object='money', action='transfer'),
    ])


def test_semantic_recall_and_classifier_uses_atomic_text(tmp_path, encoder):
    tax = taxonomy()
    recall = VectorTaskRecall({'model': 'test'}, tmp_path)
    classifier = IntentClassifier(None, tax, matching_cfg={'enabled': True, 'candidate_top_k': 1},
                                  task_retriever=recall)
    intent = SemanticIntent(domain='wrong', object='money', action='transfer', span='available funds')
    assert [t.task_id for t in classifier._recall_tasks(intent, 'remaining money')] == ['balance']
    assert [t.task_id for t in recall.recall(tax, 'transfer funds', 1)] == ['transfer']
    payload = json.loads(recall.task_text(tax, tax.tasks[0]))
    assert payload['path'] == ['finance', 'banking', 'account', 'check']


def test_reuse_queries_refresh_only_changed_topic_and_service(tmp_path, encoder):
    tax = taxonomy()
    cfg = {'model': 'one'}
    first = VectorTaskRecall(cfg, tmp_path)
    first.prepare(tax, ['remaining money', 'remaining money'])
    assert sum(batch.count('remaining money') for batch in encoder.calls) == 1
    encoder.calls.clear()
    second = VectorTaskRecall(cfg, tmp_path)
    second.prepare(tax, ['remaining money'])
    assert encoder.calls == []
    tax.tasks[0].description = 'new definition'
    second.prepare(tax, ['remaining money'])
    assert len(encoder.calls) == 1 and len(encoder.calls[0]) == 1
    assert 'new definition' in encoder.calls[0][0]
    encoder.calls.clear()
    third = VectorTaskRecall({'model': 'two'}, tmp_path)
    third.prepare(tax, ['remaining money'])
    assert sum(map(len, encoder.calls)) == 3


def test_no_tasks_and_dimension_error(tmp_path, encoder):
    recall = VectorTaskRecall({'model': 'one'}, tmp_path)
    assert recall.recall(Taxonomy(domains=[], tasks=[]), 'query', 5) == []
    tax = taxonomy()
    recall.prepare(tax)
    recall.memory['query'] = np.array([1., 0., 0.])
    with pytest.raises(ValueError, match='维度不一致'):
        recall.recall(tax, 'query', 5)



def test_atomic_request_has_no_domain_step_or_domain_embedding(tmp_path, encoder, monkeypatch):
    from intent_engine.models import PerIntentSemanticDecision
    tax = taxonomy()
    seen=[]
    class LLM:
        def chat_json(self, system, user, schema):
            seen.append(json.loads(user))
            return schema(object='火箭',action='查询原理',slots={},confidence=.9)
    classifier=IntentClassifier(LLM(),tax,task_retriever=VectorTaskRecall({'model':'one'},tmp_path))
    monkeypatch.setattr(classifier,'match',lambda result:result)
    result=classifier.classify_atomic('How does a rocket work?',{'source':'explicit'})
    assert seen==[{'atomic_text':'How does a rocket work?','slots':{'source':'explicit'}}]
    assert encoder.calls==[]
    assert 'domain' not in PerIntentSemanticDecision.model_fields
    assert result.intents[0].domain==''


@pytest.mark.parametrize('configured,expected', [(None,3),(1,1),(3,3),(5,3)])
def test_leaf_candidate_recall_is_capped_at_three(configured,expected):
    cfg={} if configured is None else {'candidate_top_k':configured}
    tax=Taxonomy(domains=[DomainDef(name='bank')],tasks=[TaskDef(task_id=str(i),domain='bank',object='account',action='check') for i in range(6)])
    class Retriever:
        def recall(self,taxonomy,text,k):
            assert k==expected
            return taxonomy.tasks[:k]
    intent=SemanticIntent(object='account',action='check',span='check balance')
    for retriever in (None,Retriever()):
        classifier=IntentClassifier(None,tax,matching_cfg=cfg,task_retriever=retriever)
        assert len(classifier._recall_tasks(intent,'check balance'))==expected
