import json
import pytest

from intent_engine.atomic.atomic_discovery import AtomicExtraction
from intent_engine.classification.classifier import IntentClassifier
from intent_engine.models import (DomainDef, FinalTaskDecision, PairwiseTaskDecision,
                                  PerIntentSemanticDecision, TaskDef, Taxonomy)


class IsolatedLLM:
    def __init__(self):
        self.calls = []
    def chat_json(self, system, user, schema, **kwargs):
        payload = json.loads(user)
        self.calls.append((schema, payload))
        if schema is AtomicExtraction:
            return schema(status='valid', intents=[
                {'span': '打开支付宝', 'text': '打开支付宝', 'object':'应用','action':'打开'},
                {'span': '交电费', 'text': '交电费', 'object':'电费','action':'缴纳'},
            ])
        if schema is PerIntentSemanticDecision:
            opening = payload['atomic_text'] == '打开支付宝'
            return schema(domain='生活', object='应用' if opening else '电费',
                          action='打开' if opening else '缴纳', slots={} if opening else {'utility':'electricity'}, confidence=.95)
        if schema is PairwiseTaskDecision:
            return schema(task_id=payload['candidate_task']['task_id'], match=True, confidence=.95)
        if schema is FinalTaskDecision:
            return schema(status='known', selected_task_id='open' if payload['intent_text']=='打开支付宝' else 'pay', confidence=.95)
        raise AssertionError(schema)


def test_raw_entry_uses_current_extractor_and_keeps_preparatory_action():
    llm = IsolatedLLM()
    tax = Taxonomy(domains=[DomainDef(name='生活')], tasks=[
        TaskDef(task_id='open', domain='生活', object='应用', action='打开'),
        TaskDef(task_id='pay', domain='生活', object='电费', action='缴纳')])
    result = IntentClassifier(llm, tax).classify('打开支付宝，交电费')
    assert [i.span for i in result.intents] == ['打开支付宝', '交电费']
    assert [i.matched_task_id for i in result.intents] == ['open', 'pay']
    assert result.intents[1].slots == {}
    assert not any(schema is PerIntentSemanticDecision for schema,_ in llm.calls)
    for schema, payload in llm.calls:
        if schema is AtomicExtraction:
            continue
        serialized = json.dumps(payload, ensure_ascii=False)
        assert '打开支付宝，交电费' not in serialized
        assert 'Consolidation' not in serialized


def test_no_legacy_prompts_or_methods_in_catalog():
    from intent_engine.settings.prompt_settings import prompt_catalog
    keys = {e['key'] for e in prompt_catalog()}
    assert {k for k in keys if k.startswith('classifier.')} == {
        'classifier.ATOMIC_CLASSIFY_SYSTEM', 'classifier.PAIRWISE_SYSTEM', 'classifier.FINAL_MATCH_SYSTEM'}
    assert not hasattr(IntentClassifier, 'match_legacy')
    assert not hasattr(IntentClassifier, 'consolidate')


def test_legacy_switches_removed_from_cli_api_and_page():
    from pathlib import Path
    import pytest
    from intent_engine.entrypoints.cli import build_parser
    from intent_engine.web.app import DiscoveryRequest
    for flag in ('--legacy-matcher', '--no-consolidation', '--no-doa-check'):
        with pytest.raises(SystemExit):
            build_parser().parse_args(['discover', '--input', 'example.csv', flag])
    for schema in (DiscoveryRequest,):
        assert not {'multi_stage', 'consolidation', 'doa_consistency'} & schema.model_fields.keys()
    root = Path(__file__).resolve().parents[2]
    page = (root / 'web/index.html').read_text()
    script = (root / 'web/static/js/console.js').read_text()
    for prefix in ('wf', 'iter'):
        for suffix in ('MultiStage', 'Consolidation', 'DoaCheck'):
            assert prefix + suffix not in page + script


def test_atomic_classifier_does_not_resegment_or_consolidate(monkeypatch):
    from intent_engine.classification.classifier import IntentClassifier
    from intent_engine.models import PerIntentSemanticDecision
    class LLM:
        def chat_json(self, prompt, payload, schema):
            assert schema is PerIntentSemanticDecision
            assert json.loads(payload)['atomic_text'] == '打开支付宝'
            return schema(object='应用', action='打开', slots={}, confidence=.9)
    taxonomy = Taxonomy.model_validate({'domains': [{'name': '应用'}], 'tasks': []})
    classifier = IntentClassifier(LLM(), taxonomy)
    monkeypatch.setattr(classifier, 'match', lambda result: result)
    monkeypatch.setattr(classifier, 'classify', lambda text: pytest.fail('must not resegment'))
    result = classifier.classify_atomic('打开支付宝', {'应用名': '支付宝'})
    assert len(result.intents) == 1
    assert result.intents[0].span == '打开支付宝'
    assert result.intents[0].slots == {'应用名': '支付宝'}


def test_precomputed_atomic_semantics_skip_parse_request(monkeypatch):
    class NoParse:
        def chat_json(self,*a,**kw):
            pytest.fail('semantics already extracted in main workflow')
    classifier=IntentClassifier(NoParse(),Taxonomy(domains=[],tasks=[]))
    result=classifier.classify_atomic('缴纳电费',semantics={'text':'缴纳电费','object':'电费','action':'缴纳'})
    assert result.intents[0].object=='电费' and result.intents[0].action=='缴纳'
    assert result.intents[0].status=='discovery'
    assert result.intents[0].slots=={}


def test_missing_or_stale_semantics_are_parsed_only_on_demand():
    calls=[]
    class Parser:
        def chat_json(self,prompt,user,schema,**kwargs):
            assert schema is PerIntentSemanticDecision
            calls.append(json.loads(user))
            return schema(object='电费',action='缴纳',slots={})
    classifier=IntentClassifier(Parser(),Taxonomy(domains=[],tasks=[]))
    classifier.classify_atomic('缴纳电费',semantics={'text':'旧文本','object':'电费','action':'查询'})
    classifier.classify_atomic('缴纳电费')
    assert len(calls)==2
