from intent_engine.classification.classifier import IntentClassifier
from intent_engine.models import (DomainDef, FinalTaskDecision, PairwiseTaskDecision,
                                  SemanticIntent, SemanticResult, TaskDef, Taxonomy)


class FakeLLM:
    def __init__(self, match=True):
        self.matches = match
    def chat_json(self, system, user, schema, **kwargs):
        if schema is PairwiseTaskDecision:
            return schema(task_id='video', match=self.matches, confidence=.95, reason='checked')
        assert schema is FinalTaskDecision
        return schema(selected_task_id='video', status='known', confidence=.95)


def test_matching_preserves_semantics_and_rejection_enters_discovery():
    tax = Taxonomy(domains=[DomainDef(name='影音娱乐')], tasks=[
        TaskDef(task_id='video', domain='影音娱乐', object='影视内容', action='播放')])
    for accepted in (True, False):
        source = SemanticResult(raw_text='播放太平年', intents=[SemanticIntent(
            domain='影音娱乐', object='影视内容', action='播放', span='播放太平年',
            slots={'title': '太平年'})])
        out = IntentClassifier(FakeLLM(accepted), tax).match(source).intents[0]
        assert (out.domain, out.object, out.action, out.slots) == (
            '影音娱乐', '影视内容', '播放', {'title': '太平年'})
        assert out.status == ('known' if accepted else 'discovery')
        assert out.matched_task_id == ('video' if accepted else None)
