from __future__ import annotations

import json
from types import SimpleNamespace

import numpy as np
import pytest
from pydantic import ValidationError

import intent_engine.atomic.atomic_discovery as discovery
from intent_engine.atomic.hierarchy_review import NodeDecision, LeafNamingDecision
from intent_engine.infrastructure.progress import ProgressReporter


def extraction(intents, **kwargs):
    intents=[dict({'object':'对象','action':'查询'} if i.get('status','valid')=='valid' else {},**i) for i in intents]
    return discovery.AtomicExtraction.model_validate({'status': 'valid', 'intents': intents, **kwargs})


def test_explicit_actions_preserve_source_and_order():
    raw = '打开支付宝，帮我交电费'
    parsed = extraction([{'span': '打开支付宝', 'text': '打开支付宝'},
                         {'span': '帮我交电费', 'text': '帮我交电费'}],
                        relations=[{'source': 0, 'target': 1, 'kind': 'before', 'reason': '原文先后动作'}])
    assert discovery.validate_extraction(raw, parsed) == [(0, 5), (6, 11)]
    assert len(parsed.intents) == 2
    single = extraction([{'span': '用支付宝交电费', 'text': '用支付宝交电费'}])
    assert len(single.intents) == 1
    with pytest.raises(ValueError, match='原文片段'):
        discovery.validate_extraction('用支付宝交电费', parsed)


def test_coreference_requires_real_context_and_no_free_rewrite():
    with pytest.raises(ValidationError, match='上下文补全'):
        extraction([{'span': '查一下余额', 'text': '查询手机余额'}])
    with pytest.raises(ValidationError, match='上下文索引'):
        extraction([{'span': '取消它', 'text': '取消订单', 'context_indices': [9], 'reason': '订单'}])
    parsed = extraction([{'span': '查订单', 'text': '查订单'},
                         {'span': '取消它', 'text': '取消订单', 'context_indices': [0], 'reason': '它指代前面的订单'}])
    assert discovery.validate_extraction('查订单，再取消它', parsed) == [(0, 3), (5, 8)]
    with pytest.raises(ValidationError):
        discovery.AtomicExtraction(status='invalid', intents=parsed.intents)


def test_cluster_review_rejects_invented_or_overlapping_evidence():
    review = discovery.ClusterReview(verdict='mixed', reason='两种动作',
                                    split_groups=[{'label': '查询', 'evidence_ids': ['a']},
                                                  {'label': '充值', 'evidence_ids': ['b']}])
    discovery.check_evidence(review, {'a', 'b'})
    with pytest.raises(ValueError, match='未提供'):
        discovery.check_evidence(review, {'a'})
    review.split_groups[1].evidence_ids = ['a']
    with pytest.raises(ValueError, match='互不重复'):
        discovery.check_evidence(review, {'a', 'b'})


@pytest.mark.parametrize('values', [[[1, 0]], [[0, 0], [1, 0]], [[float('nan'), 1], [1, 0]], [[1], [2, 3]]])
def test_bad_embedding_responses_rejected(values):
    with pytest.raises((ValueError, TypeError)):
        discovery.SemanticEmbeddings._validate(values, 2)


def test_embedding_cache_identity_order_and_indices(tmp_path, monkeypatch):
    import openai
    calls = []
    def create(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(data=[SimpleNamespace(index=i, embedding=[i + 1., 1.])
                                     for i in reversed(range(len(kwargs['input'])))])
    monkeypatch.setattr(openai, 'OpenAI', lambda **kwargs: SimpleNamespace(embeddings=SimpleNamespace(create=create)))
    settings = {'base_url': 'http://example.test/v1', 'model': 'embedding-a'}
    client = discovery.SemanticEmbeddings(settings, tmp_path)
    a = client.encode(['查余额', '充值'], ProgressReporter())
    assert a.tolist() == [[1, 1], [2, 1]]
    assert np.array_equal(a, client.encode(['查余额', '充值'], ProgressReporter()))
    assert len(calls) == 1
    client.encode(['充值', '查余额'], ProgressReporter())
    discovery.SemanticEmbeddings({**settings, 'model': 'embedding-b'}, tmp_path).encode(['查余额', '充值'], ProgressReporter())
    assert len(calls) == 3


class FakeLLM:
    def __init__(self, cfg):
        pass

    def chat_json(self, system, user, schema, **kwargs):
        payload = json.loads(user)
        if schema is discovery.AtomicExtraction:
            raw = payload['user_text']
            if raw == '失败':
                raise RuntimeError('service unavailable')
            if raw == '取消一下':
                return extraction([{'span': raw, 'text': raw, 'status': 'ambiguous'}], status='ambiguous')
            phrases = raw.split('，')
            return extraction([{'span': p, 'text': p} for p in phrases])
        if schema is discovery.ClusterReview:
            assert 'review_samples' not in payload and 'review' not in payload
            assert all(set(m) == {'intent_id', 'text'} for m in payload['members'])
            ids = [m['intent_id'] for m in payload['members']]
            return schema(verdict='consistent', object='对象', action=f"动作{payload['cluster_id']}",
                          reason='提供的样本属于同一动作')
        if schema is discovery.PairReview:
            return schema(decision='merge', reason='仅表达方式不同，建议人工复核',
                          left_evidence_ids=[payload['left'][0]['intent_id']],
                          right_evidence_ids=[payload['right'][0]['intent_id']])
        if schema in {NodeDecision,LeafNamingDecision}:
            return schema(reason='主题语义明确', description='当前主题边界', label='主题 '+payload['node_id'], **({'semantic_clear':True} if schema is LeafNamingDecision else {'decision':'group'}))
        raise AssertionError(schema)


@pytest.fixture
def coldstart(tmp_path, monkeypatch):
    path = tmp_path / 'input.csv'
    path.write_text('text\n打开支付宝，交电费\n打开支付宝\n失败\n取消一下\n查询天气\n', encoding='utf-8')
    cfg = {'embedding': {'base_url': 'http://example.test/v1', 'model': 'model'}, 'llm': {},
           'pipeline': {'text_column': 'text'}, 'atomic_discovery': {'iteration_rounds': 0,'iteration_scope':'all','leaf_merge_enabled':False},
           'paths': {'reports_dir': str(tmp_path / 'reports'), 'checkpoint_dir': str(tmp_path / 'cache'),
                     'taxonomy': '/does/not/exist', 'skill_registry': '/does/not/exist'}}
    monkeypatch.setattr(discovery, 'check_dependencies', lambda: None)
    monkeypatch.setattr(discovery, 'LocalLLM', FakeLLM)
    monkeypatch.setattr(discovery.SemanticEmbeddings, '__init__', lambda *args: None)
    monkeypatch.setattr(discovery.SemanticEmbeddings, 'encode', lambda self, texts, progress: np.array([[i + 1, 1.] for i in range(len(texts))]))
    def cluster(texts, embeddings, options):
        assert texts == ['打开支付宝', '交电费', '查询天气']
        return [0, 1, -1], {0: ['应用'], 1: ['电费']}
    monkeypatch.setattr(discovery, 'cluster_atomic_texts', cluster)
    return cfg, path


def test_discovery_keeps_duplicates_noise_errors_and_proposals_separate(coldstart):
    cfg, path = coldstart
    result = discovery.run_atomic_discovery(cfg, str(path), workers=2)
    assert result['status'] == 'completed_with_errors'
    assert {k:v for k,v in result['summary'].items() if k != 'coverage'} == {'rows': 5, 'atomic_intents': 5, 'unique_clustered_texts': 3,
                                 'clusters': 2, 'noise_intents': 1, 'ambiguous_intents': 1, 'processing_errors': 1}
    assert [a['cluster_id'] for a in result['intents']] == [0, 1, 0, None, -1]
    assert all('slots' not in atom for atom in result['intents'])
    assert all(atom['object'] and atom['action'] for atom in result['intents'] if atom['status']=='valid')
    assert all('slots' not in sample for c in result['clusters'] for sample in c['review_samples'])
    assert result['clusters'][0]['sample_count'] == 2
    assert result['clusters'][0]['unique_text_count'] == 1
    assert result['pair_reviews'][0]['decision'] == 'merge'
    assert len(result['candidate_taxonomy']['tasks']) == 2  # no automatic merge
    assert result['candidate_taxonomy']['domains'][0]['name'] == '主题 topic:0'
    assert result['adjustments_applied'] is False
    reports = list((path.parent / 'reports').rglob('*'))
    assert any(p.suffix == '.xlsx' for p in reports)
    assert any(p.suffix == '.json' for p in reports)


def test_no_review_does_not_invent_taxonomy(coldstart):
    cfg, path = coldstart
    result = discovery.run_atomic_discovery(cfg, str(path), review=False)
    assert result['candidate_taxonomy']['tasks'] == []
    assert result['pair_reviews'] == []


def test_invalid_review_and_domains_do_not_promote(coldstart, monkeypatch):
    cfg, path = coldstart
    original = FakeLLM.chat_json
    def invalid(self, system, user, schema, **kwargs):
        result = original(self, system, user, schema, **kwargs)
        if schema is discovery.ClusterReview:
            result.boundary_ids = ['invented']
        return result
    monkeypatch.setattr(FakeLLM, 'chat_json', invalid)
    result = discovery.run_atomic_discovery(cfg, str(path))
    assert result['candidate_taxonomy']['tasks'] == []
    assert all(c['review'] is None for c in result['clusters'])
    assert len([e for e in result['errors'] if e['stage'] == 'review']) == 2


def test_leaf_naming_failure_cannot_promote_unreviewed_topic(coldstart, monkeypatch):
    cfg, path = coldstart
    original = FakeLLM.chat_json
    def omit(self, system, user, schema, **kwargs):
        if schema in {NodeDecision,LeafNamingDecision}:
            raise RuntimeError('node naming failed')
        return original(self, system, user, schema, **kwargs)
    monkeypatch.setattr(FakeLLM, 'chat_json', omit)
    result = discovery.run_atomic_discovery(cfg, str(path))
    assert result['candidate_taxonomy']['tasks'] == []
    assert any(e['stage'] == 'domains' for e in result['errors'])


def test_embedding_failure_persists_extraction(coldstart, monkeypatch):
    cfg, path = coldstart
    def fail(*args):
        raise RuntimeError('embedding offline')
    monkeypatch.setattr(discovery.SemanticEmbeddings, 'encode', fail)
    with pytest.raises(RuntimeError, match='embedding offline'):
        discovery.run_atomic_discovery(cfg, str(path))
    saved = json.loads(next(path.parent.glob('reports/*/process/discovery.json')).read_text())
    assert saved['status'] == 'failed'
    assert saved['intents']
    assert saved['errors'][-1]['stage'] == 'embed'


def test_api_configuration_error_and_no_secret_leak(monkeypatch, tmp_path):
    import web_server as web
    cfg = {'embedding': {'base_url': '', 'model': '', 'api_key': 'SECRET'},
           'pipeline': {'text_column': 'text'}}
    monkeypatch.setattr(web, '_cfg', lambda: cfg)
    monkeypatch.setattr(discovery, 'check_dependencies', lambda: None)
    state = web.api_discovery_settings()
    assert not state['ready']
    assert 'SECRET' not in json.dumps(state)
    path = tmp_path / 'input.csv'
    path.write_text('text\nhello\n')
    with pytest.raises(web.HTTPException) as exc:
        web.api_job_discover(web.DiscoveryRequest(input=str(path)))
    assert exc.value.status_code == 400


def test_api_dispatch_preserves_column_and_review_choice(monkeypatch, tmp_path):
    import web_server as web
    path = tmp_path / 'input.csv'
    path.write_text('text\n打开应用\n')
    monkeypatch.setattr(web, '_cfg', lambda: {'embedding': {'base_url': 'http://example.test/v1', 'model': 'test'},
                                             'pipeline': {'text_column': 'text'}})
    monkeypatch.setattr(discovery, 'check_dependencies', lambda: None)
    calls = []
    monkeypatch.setattr(web, '_start_job', lambda kind, cmd: calls.append((kind, cmd)) or {'job_id': 'test'})
    web.api_job_discover(web.DiscoveryRequest(input=str(path), text_column='text', limit=10, workers=2, review=False))
    kind, command = calls[0]
    assert kind == 'discover'
    assert command[2] == 'discover'
    assert command[command.index('--text-column') + 1] == 'text'
    assert '--no-review' in command
    assert command[command.index('--limit') + 1] == '10'
    web.api_job_classification_workflow(web.DiscoveryRequest(input=str(path), text_column='text', review=False))
    assert '--no-review' not in calls[1][1]  # Main UI contract always includes audit and hierarchy.
    assert calls[1][1][calls[1][1].index('--iteration-rounds') + 1] == '0'  # Stable first draft by default.


def test_real_bertopic_with_precomputed_vectors():
    pytest.importorskip('bertopic')
    rng = np.random.default_rng(42)
    # Synthetic vectors test the adapter, not real semantic quality.
    vectors = np.vstack([np.eye(3, 12)[i] + rng.normal(0, .015, (15, 12)) for i in range(3)])
    texts = [f'{label}表达{i}' for label in ('打开应用', '查询余额', '充值话费') for i in range(15)]
    clustering = discovery.cluster_atomic_texts(texts, vectors, {'min_cluster_size': 5})
    labels, keywords = clustering
    assert len(labels) == len(texts)
    assert len(set(labels) - {-1}) >= 2
    assert all(keywords[label] for label in set(labels) - {-1})
    assert clustering.hierarchy['status'] == 'ready', clustering.hierarchy.get('message')
    assert clustering.hierarchy['leaf_count'] == len(set(labels) - {-1})
    assert clustering.hierarchy['max_depth'] >= 2
    assert len(clustering.hierarchy['root_ids']) == 1
    tiny_labels, _ = discovery.cluster_atomic_texts(texts[:2], vectors[:2], {})
    assert tiny_labels == [-1, -1]


def test_full_workflow_saves_each_stage_and_manifest(coldstart):
    cfg, path = coldstart
    result = discovery.run_atomic_discovery(cfg, str(path))
    expected = [stage for stage in discovery.WORKFLOW_STAGES if stage != 'iterate']
    assert result['completed_stages'] == expected
    for stage in expected:
        artifact = json.loads(__import__('pathlib').Path(result['artifacts'][stage]).read_text())
        assert artifact['stage'] == stage
        assert stage in artifact['completed_stages']
    from intent_engine.workflows.workflow_artifacts import list_workflows
    runs = list_workflows(__import__('pathlib').Path(cfg['paths']['reports_dir']))
    assert len(runs) == 1 and runs[0]['run_id'] == result['run_id']
    sentences = json.loads(__import__('pathlib').Path(result['artifacts']['sentence_results']).read_text())
    assert [record['raw_text'] for record in sentences['records']] == [row['raw_text'] for row in result['rows']]
    assert len(sentences['records']) == 5
    assert set(sentences['records'][0]) == {'raw_text', 'topics_by_level'}
    assert __import__('pathlib').Path(result['artifacts']['sentence_workbook']).exists()


def test_stage_cluster_reuses_atoms_without_llm_and_keeps_original(coldstart, monkeypatch):
    from pathlib import Path
    cfg, path = coldstart
    parent = discovery.run_atomic_discovery(cfg, str(path))
    source = Path(parent['artifacts']['extract'])
    original = source.read_bytes()
    def no_llm(*args, **kwargs):
        raise AssertionError('cluster stage must not call LLM')
    monkeypatch.setattr(discovery, 'LocalLLM', no_llm)
    path.unlink()  # intermediate processing does not depend on the original spreadsheet
    child = discovery.run_atomic_discovery(cfg, source_path=str(source), stage='cluster')
    assert child['source']['run_id'] == parent['run_id']
    assert child['run_id'] != parent['run_id']
    assert len(child['clusters']) == 2
    assert child['candidate_taxonomy']['tasks'] == []
    assert source.read_bytes() == original


def test_review_and_hierarchy_stages_do_not_need_embeddings(coldstart, monkeypatch):
    cfg, path = coldstart
    parent = discovery.run_atomic_discovery(cfg, str(path))
    cfg['embedding'] = {'base_url': '', 'model': ''}
    monkeypatch.setattr(discovery, 'check_dependencies', lambda: (_ for _ in ()).throw(AssertionError('must not import BERTopic')))
    child = discovery.run_atomic_discovery(cfg, source_path=parent['artifacts']['cluster'], stage='review')
    assert all(c['review']['verdict'] == 'consistent' for c in child['clusters'])
    assert child['candidate_taxonomy']['tasks'] == []
    hierarchy = discovery.run_atomic_discovery(cfg, source_path=child['artifacts']['review'], stage='domains')
    assert len(hierarchy['candidate_taxonomy']['tasks']) == 2
    with pytest.raises(ValueError, match='尚未完成'):
        discovery.run_atomic_discovery(cfg, source_path=parent['artifacts']['extract'], stage='review')


def test_taxonomy_edit_creates_derived_artifact_and_preserves_parent(coldstart):
    from pathlib import Path
    from intent_engine.workflows.workflow_artifacts import load_workflow_taxonomy, save_workflow_taxonomy, load_workflow
    cfg, path = coldstart
    parent = discovery.run_atomic_discovery(cfg, str(path))
    reports = Path(cfg['paths']['reports_dir'])
    current = load_workflow_taxonomy(reports, parent['run_id'], 'domains')
    original = Path(current['path']).read_bytes()
    current['tasks'][0]['description'] = '人工核对后的边界'
    saved = save_workflow_taxonomy(reports, parent['run_id'], 'domains', current['revision'], current['tasks'], current['domains'])
    assert saved['run_id'] != parent['run_id']
    assert Path(current['path']).read_bytes() == original
    child = load_workflow_taxonomy(reports, saved['run_id'], 'domains')
    assert child['tasks'][0]['description'] == '人工核对后的边界'
    assert child['version'] == current['version'] + 1
    assert 'workbook' not in load_workflow(reports, saved['run_id'])['artifacts']
    with pytest.raises(ValueError, match='已变更'):
        save_workflow_taxonomy(reports, parent['run_id'], 'domains', 'wrong', current['tasks'], current['domains'])
    with pytest.raises(ValueError, match='尚未完成|候选层级'):
        load_workflow_taxonomy(reports, parent['run_id'], 'extract')


def test_workflow_usage_is_per_run_and_persisted(coldstart, monkeypatch):
    from pathlib import Path
    from intent_engine.infrastructure.model_requests import USAGE
    from intent_engine.workflows.workflow_artifacts import load_workflow, load_workflow_taxonomy, save_workflow_taxonomy
    cfg, path = coldstart
    original = FakeLLM.chat_json
    calls = []
    def counted(self, *args, **kwargs):
        try:
            value = original(self, *args, **kwargs)
        except Exception:
            event = {'kind': 'llm', 'model': 'fake', 'failed': True}
            USAGE.apply(event)
            calls.append(event)
            raise
        event = {'kind': 'llm', 'model': 'fake', 'reported': True, 'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}
        USAGE.apply(event)
        calls.append(event)
        return value
    monkeypatch.setattr(FakeLLM, 'chat_json', counted)
    result = discovery.run_atomic_discovery(cfg, str(path))
    known = sum(not event.get('failed') for event in calls)
    assert result['token_usage']['requests'] == len(calls)
    assert result['token_usage']['total_tokens'] == known * 3
    assert result['token_usage']['unreported_requests'] == 1
    reports = Path(cfg['paths']['reports_dir'])
    assert load_workflow(reports, result['run_id'])['token_usage'] == result['token_usage']
    tax = load_workflow_taxonomy(reports, result['run_id'], 'domains')
    tax['tasks'][0]['description'] = 'changed manually'
    manual = save_workflow_taxonomy(reports, result['run_id'], 'domains', tax['revision'], tax['tasks'], tax['domains'])
    assert load_workflow(reports, manual['run_id'])['token_usage']['requests'] == 0


@pytest.mark.parametrize('scenario', ['review_failed', 'mixed', 'domains_failed', 'partial_review', 'noise'])
def test_main_workflow_audits_and_reports_missing_hierarchy(coldstart, monkeypatch, scenario):
    from pathlib import Path
    from intent_engine.workflows.workflow_diagnostics import workflow_diagnostics
    cfg, path = coldstart
    calls = []
    original = FakeLLM.chat_json

    def controlled(self, system, user, schema, **kwargs):
        calls.append(schema)
        if schema is discovery.ClusterReview:
            if scenario == 'review_failed' or (scenario == 'partial_review' and json.loads(user)['cluster_id'] == 1):
                raise RuntimeError('audit service failed')
            if scenario == 'mixed':
                members = json.loads(user)['members']
                return schema(verdict='uncertain', reason='上下文不足')
        if scenario == 'domains_failed' and schema in {NodeDecision,LeafNamingDecision}:
            raise RuntimeError('domain service failed')
        return original(self, system, user, schema, **kwargs)

    monkeypatch.setattr(FakeLLM, 'chat_json', controlled)
    if scenario == 'noise':
        monkeypatch.setattr(discovery, 'cluster_atomic_texts', lambda texts, embeddings, options: ([-1]*len(texts), {}))
    result = discovery.run_atomic_discovery(cfg, str(path))
    diagnosis = workflow_diagnostics(result)
    assert result['stage_outcomes']['review']['status'] == {
        'review_failed': 'failed', 'mixed': 'completed', 'domains_failed': 'completed',
        'partial_review': 'completed_with_errors', 'noise': 'blocked'}[scenario]
    if scenario != 'noise':
        assert calls.count(discovery.ClusterReview) == 2  # Happens inside the main workflow.
    if scenario == 'partial_review':
        assert len(result['candidate_taxonomy']['tasks']) == 1
        assert diagnosis['code'] == 'ready' and diagnosis['review']['failed'] == 1
        assert 'review' in result['completed_stages']
    else:
        assert result['candidate_taxonomy']['tasks'] == []
        assert 'domains' not in result['completed_stages']
        assert diagnosis['code'] == {'review_failed': 'needs_review', 'mixed': 'no_consistent',
                                     'domains_failed': 'domains_failed', 'noise': 'no_clusters'}[scenario]
    if scenario == 'review_failed':
        assert 'review' not in result['completed_stages']
        assert NodeDecision not in calls
    saved = json.loads(Path(result['artifacts']['export']).read_text())
    assert saved['stage_outcomes'] == result['stage_outcomes']
    manifest = json.loads(next(Path(cfg['paths']['reports_dir']).glob('*/workflow.json')).read_text())
    assert manifest['stage_outcomes'] == result['stage_outcomes']


def test_main_flow_and_stage_tool_integrate_candidate_iterations(coldstart, monkeypatch):
    import copy
    from pathlib import Path
    import intent_engine.workflows.hierarchy_iteration as candidate
    from intent_engine.workflows.workflow_artifacts import load_workflow_taxonomy
    cfg, path = coldstart
    cfg['atomic_discovery']['iteration_rounds'] = 2
    calls = []
    def iterate(cfg, data, rounds, workers, reporter, persist, vectors=None):
        atoms, seed = data['intents'], data['candidate_taxonomy']
        calls.append((copy.deepcopy(atoms), copy.deepcopy(seed), rounds))
        final = copy.deepcopy(seed)
        final['version'] += 1
        final['tasks'][0]['action'] = '优化后的动作'
        history = {'round': 1, 'task_count_before': 2, 'task_count_after': 2, 'changes': [],
                   'assignments': [], 'taxonomy_before': seed, 'taxonomy_after': final, 'evidence': {}}
        state = {'status': 'completed', 'rounds': [history], 'assignments': [], 'max_rounds': rounds,
                 'converged': False, 'stop_reason': 'max_rounds', 'stop_message': '达到上限'}
        data['iteration'], data['candidate_taxonomy'] = state, final
        persist()
        reporter.skip(s['id'] for s in candidate.hierarchy_iteration_steps(rounds) if s['id'] != 'iteration_reports')
        reporter.start('iteration_reports')
        return state
    monkeypatch.setattr(candidate, 'run_hierarchy_iterations', iterate)
    result = discovery.run_atomic_discovery(cfg, str(path))
    assert result['completed_stages'] == list(discovery.WORKFLOW_STAGES)
    assert calls[0][2] == 2 and len(calls[0][1]['tasks']) == 2
    assert result['candidate_taxonomy']['tasks'][0]['action'] == '优化后的动作'
    initial = json.loads(Path(result['artifacts']['domains']).read_text())
    assert initial['candidate_taxonomy']['tasks'][0]['action'] != '优化后的动作'
    current = load_workflow_taxonomy(Path(cfg['paths']['reports_dir']), result['run_id'], 'iterate')
    assert current['tasks'][0]['action'] == '优化后的动作'
    source = result['artifacts']['iterate']
    cfg['embedding'] = {'base_url': '', 'model': ''}
    path.unlink()
    child = discovery.run_atomic_discovery(cfg, stage='iterate', source_path=source, iteration_rounds=1)
    assert calls[-1][2] == 1 and child['source']['run_id'] == result['run_id']
    assert len(child['intents']) == len(result['intents'])


def test_iteration_is_blocked_when_main_audit_cannot_produce_seed(coldstart, monkeypatch):
    import intent_engine.workflows.candidate_iteration as candidate
    cfg, path = coldstart
    cfg['atomic_discovery']['iteration_rounds'] = 3
    original = FakeLLM.chat_json
    def no_consistent(self, system, user, schema, **kwargs):
        if schema is discovery.ClusterReview:
            return schema(verdict='uncertain', reason='证据不足')
        return original(self, system, user, schema, **kwargs)
    monkeypatch.setattr(FakeLLM, 'chat_json', no_consistent)
    result = discovery.run_atomic_discovery(cfg, str(path))
    assert result['iteration']['stop_reason'] == 'no_seed'
    assert result['stage_outcomes']['iterate']['status'] == 'blocked'
    assert 'iterate' not in result['completed_stages']


def test_hierarchy_is_persisted_exported_and_reused_by_downstream_stages(coldstart, monkeypatch):
    from pathlib import Path
    import pandas as pd
    from intent_engine.atomic.topic_hierarchy import ClusteringResult, build_topic_tree
    cfg, path = coldstart
    def clustering(texts, vectors, options):
        labels, keywords = [0, 1, 2], {0: ['打开'], 1: ['电费'], 2: ['天气']}
        merges = [{'Parent_ID': 3, 'Parent_Name': '打开_电费', 'Child_Left_ID': 0, 'Child_Right_ID': 1, 'Distance': .4},
                  {'Parent_ID': 4, 'Parent_Name': '操作_查询', 'Child_Left_ID': 3, 'Child_Right_ID': 2, 'Distance': .8}]
        return ClusteringResult(labels, keywords, build_topic_tree(labels, keywords, merges))
    monkeypatch.setattr(discovery, 'cluster_atomic_texts', clustering)
    result = discovery.run_atomic_discovery(cfg, str(path))
    saved = json.loads(Path(result['artifacts']['export']).read_text())
    assert saved['rows'][0]['topic_paths'] == result['rows'][0]['topic_paths']
    assert len(saved['rows'][0]['topic_paths']) == 2
    assert saved['topic_hierarchy']['max_depth'] == 3
    workbook = pd.ExcelFile(result['artifacts']['workbook'])
    assert {'主题层级', '意图主题路径', '原句多层主题'} <= set(workbook.sheet_names)
    cfg['embedding'] = {'base_url': '', 'model': ''}
    child = discovery.run_atomic_discovery(cfg, source_path=result['artifacts']['export'], stage='review')
    assert child['rows'][0]['topic_memberships'] == result['rows'][0]['topic_memberships']
    assert child['topic_hierarchy'] == result['topic_hierarchy']


def test_hierarchy_failure_preserves_flat_clusters_and_reports_error(coldstart, monkeypatch):
    from intent_engine.atomic.topic_hierarchy import ClusteringResult, build_topic_tree
    from intent_engine.workflows.workflow_diagnostics import workflow_diagnostics
    cfg, path = coldstart
    def clustering(texts, vectors, options):
        labels, keywords = [0, 1, -1], {0: ['打开'], 1: ['电费']}
        return ClusteringResult(labels, keywords, build_topic_tree(labels, keywords,
            status='failed', message='native hierarchy failed'))
    monkeypatch.setattr(discovery, 'cluster_atomic_texts', clustering)
    result = discovery.run_atomic_discovery(cfg, str(path))
    assert result['summary']['clusters'] == 2
    assert result['stage_outcomes']['cluster']['status'] == 'completed_with_errors'
    assert result['bertopic_hierarchy']['status'] == 'failed'
    assert result['topic_hierarchy']['status'] == 'forest'
    assert any(error['stage'] == 'hierarchy' for error in workflow_diagnostics(result)['errors'])


def test_mixed_clusters_are_refined_in_main_flow_and_resumable_review(coldstart, monkeypatch):
    import intent_engine.atomic.cluster_refinement as refinement
    cfg, path = coldstart
    monkeypatch.setattr(discovery, 'cluster_atomic_texts', lambda texts, vectors, options: ([0,0,1], {0:['请求'],1:['天气']}))
    monkeypatch.setattr(refinement, 'cluster_atomic_texts', lambda texts, vectors, options: ([0,1], {0:['打开'],1:['缴费']}))
    def rebuild(texts, vectors, labels, options):
        from intent_engine.atomic.topic_hierarchy import build_topic_tree
        keywords = {i:[f'主题{i}'] for i in set(labels) if i >= 0}
        return keywords, build_topic_tree(labels, keywords, status='not_generated')
    monkeypatch.setattr(refinement, 'rebuild_hierarchy', rebuild)
    original = FakeLLM.chat_json
    def controlled(self, system, user, schema, **kwargs):
        payload = json.loads(user)
        if schema is discovery.ClusterReview and payload['cluster_id'] == 0:
            ids = [m['intent_id'] for m in payload['members']]
            return schema(verdict='mixed', reason='打开与缴费不同',
                          split_groups=[{'label':'打开','evidence_ids':ids[:1]}, {'label':'缴费','evidence_ids':ids[1:]}])
        return original(self, system, user, schema, **kwargs)
    monkeypatch.setattr(FakeLLM, 'chat_json', controlled)
    parent = discovery.run_atomic_discovery(cfg, str(path), review=False)
    result = discovery.run_atomic_discovery(cfg, source_path=parent['artifacts']['cluster'], stage='review')
    assert result['adjustments_applied']
    assert len(result['clusters']) == 3
    assert result['cluster_refinement']['history'][0]['applied']
    discovery.read_stage_source(result['artifacts']['review'], 'domains')
    downstream = discovery.run_atomic_discovery(cfg, source_path=result['artifacts']['review'], stage='domains')
    assert len(downstream['candidate_taxonomy']['tasks']) == 3
    full = discovery.run_atomic_discovery(cfg, str(path))
    assert len(full['candidate_taxonomy']['tasks']) == 3
    assert full['cluster_refinement']['pending_clusters'] == 0


def test_cluster_reviews_overlap_and_failures_stay_with_their_cluster(coldstart, monkeypatch):
    import threading
    cfg, path = coldstart
    barrier = threading.Barrier(2, timeout=5)
    lock = threading.Lock()
    active = 0
    maximum = 0
    original = FakeLLM.chat_json

    def controlled(self, system, user, schema, **kwargs):
        nonlocal active, maximum
        if schema is not discovery.ClusterReview:
            return original(self, system, user, schema, **kwargs)
        label = json.loads(user)['cluster_id']
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait()
            if label == 1:
                raise RuntimeError('cluster-one-failed')
            return original(self, system, user, schema, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(FakeLLM, 'chat_json', controlled)
    result = discovery.run_atomic_discovery(cfg, str(path), workers=2)
    assert maximum == 2
    clusters = {c['cluster_id']: c for c in result['clusters']}
    assert clusters[0]['review']['verdict'] == 'consistent'
    assert clusters[1]['review'] is None
    assert clusters[1]['review_error'] == 'cluster-one-failed'
    assert result['stage_outcomes']['review']['status'] == 'completed_with_errors'
    saved = json.loads(__import__('pathlib').Path(result['artifacts']['review']).read_text())
    assert [(c['cluster_id'],c['review'],c.get('review_error')) for c in saved['clusters']] == [
        (c['cluster_id'],c['review'],c.get('review_error')) for c in result['clusters']]
    assert len(result['candidate_taxonomy']['tasks']) == 1


@pytest.mark.parametrize('workers', [1, 2])
def test_pair_audits_respect_concurrency_and_keep_evidence_errors_scoped(coldstart, monkeypatch, workers):
    import threading
    cfg, path = coldstart
    monkeypatch.setattr(discovery, 'cluster_atomic_texts', lambda texts, vectors, options: ([0,1,2], {0:['应用'],1:['电费'],2:['天气']}))
    barrier = threading.Barrier(2, timeout=5) if workers == 2 else None
    lock = threading.Lock()
    active = maximum = calls = 0
    original = FakeLLM.chat_json

    def controlled(self, system, user, schema, **kwargs):
        nonlocal active, maximum, calls
        if schema is not discovery.PairReview:
            return original(self, system, user, schema, **kwargs)
        with lock:
            calls += 1
            number = calls
            active += 1
            maximum = max(maximum, active)
        try:
            if barrier and number <= 2:
                barrier.wait()
            payload = json.loads(user)
            left_text = payload['left'][0]['text']
            right_text = payload['right'][0]['text']
            if left_text == '打开支付宝' and right_text == '查询天气':
                raise RuntimeError('weather-pair-failed')
            if left_text == '交电费':
                return schema(decision='keep', reason='invalid evidence', left_evidence_ids=['invented'],
                              right_evidence_ids=[payload['right'][0]['intent_id']])
            return original(self, system, user, schema, **kwargs)
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(FakeLLM, 'chat_json', controlled)
    result = discovery.run_atomic_discovery(cfg, str(path), workers=workers)
    assert maximum == workers and calls == 3
    reviews = result['pair_reviews']
    assert [(r['left_cluster_id'], r['right_cluster_id']) for r in reviews] == [(0,1),(0,2),(1,2)]
    assert reviews[0]['decision'] == 'merge'
    assert reviews[1]['error'] == 'weather-pair-failed'
    assert '未提供的样本' in reviews[2]['error']
    assert result['stage_outcomes']['pairs']['status'] == 'completed_with_errors'
    assert len(result['candidate_taxonomy']['tasks']) == 3
    saved = json.loads(__import__('pathlib').Path(result['artifacts']['pairs']).read_text())
    assert saved['pair_reviews'] == reviews


def test_new_workflow_directory_and_derived_export_are_independent(coldstart, monkeypatch):
    from pathlib import Path
    import web_server as web
    from intent_engine.workflows.workflow_artifacts import list_workflows
    from intent_engine.workflows.workflow_cleanup import workflow_delete_plan, delete_workflow
    cfg, path = coldstart
    parent = discovery.run_atomic_discovery(cfg, str(path))
    reports = Path(cfg['paths']['reports_dir'])
    directory = reports / parent['run_id']
    assert (directory/'workflow.json').is_file()
    assert set(p.name for p in directory.iterdir()) == {'workflow.json','stages','process','results'}
    assert all(Path(value).is_relative_to(directory) and Path(value).is_file() for value in parent['artifacts'].values())
    assert Path(parent['artifacts']['extract']) == directory/'stages'/'extract.json'
    assert Path(parent['artifacts']['sentence_workbook']) == directory/'results'/'sentence_results.xlsx'
    before = Path(parent['artifacts']['export']).read_bytes()
    child = discovery.run_atomic_discovery(cfg, source_path=parent['artifacts']['export'], stage='export')
    assert child['run_id'] != parent['run_id']
    assert Path(parent['artifacts']['export']).read_bytes() == before
    assert {r['run_id'] for r in list_workflows(reports)} == {parent['run_id'],child['run_id']}
    monkeypatch.setattr(web, '_reports_dir', lambda: reports)
    monkeypatch.setattr(web, 'ROOT', path.parent)
    preview = web.api_workflow_artifact(child['run_id'], 'sentence_results')
    assert len(preview['tables']['原句最终结果']) == 5
    plan = workflow_delete_plan(reports,parent['run_id'])
    assert any(item['name'].endswith('/stages/export.json') for item in plan['retained'])
    delete_workflow(reports,parent['run_id'],plan['revision'])
    assert Path(child['source']['path']).exists()
    assert (reports/child['run_id']/'workflow.json').is_file()


def test_knowledge_question_rechecked_and_single_context_preserved(coldstart, monkeypatch):
    cfg, path = coldstart
    raw = 'In Somali culture, what is a traditional dish?'
    path.write_text('text\n"'+raw+'"\n', encoding='utf-8')
    calls=[]
    def controlled(self, system, user, schema, **kwargs):
        payload=json.loads(user);calls.append(payload)
        if len(calls)==1:
            return discovery.AtomicExtraction(status='invalid',reason='信息性问题，没有可执行动作')
        return extraction([{'span':'what is a traditional dish?', 'text':'what is a traditional dish?'}])
    monkeypatch.setattr(FakeLLM,'chat_json',controlled)
    result=discovery.run_atomic_discovery(cfg,str(path),stage='extract')
    assert len(calls)==2 and 'validation_feedback' in calls[1]
    assert result['rows'][0]['extraction_rechecked']
    assert result['intents'][0]['text']==raw
    assert result['intents'][0]['span']==raw
    discovery.read_stage_source(result['artifacts']['extract'],'cluster')


def test_multi_intent_context_repair_is_bounded_and_grounded(coldstart,monkeypatch):
    cfg,path=coldstart
    raw="How is women's work viewed, and what is its value to families?"
    path.write_text('text\n"'+raw+'"\n',encoding='utf-8')
    calls=[]
    def controlled(self, system, user, schema, **kwargs):
        payload=json.loads(user);calls.append(payload)
        return extraction([{'span':"How is women's work viewed",'text':"How is women's work viewed"},
            {'span':'what is its value to families?',
             'text':"what is the value of women's work to families?" if len(calls)>1 else 'what is its value to families?',
             'context_indices':[0],'reason':'its refers to women\'s work'}])
    monkeypatch.setattr(FakeLLM,'chat_json',controlled)
    result=discovery.run_atomic_discovery(cfg,str(path),stage='extract')
    assert len(calls)==2
    assert result['intents'][1]['text']=="what is the value of women's work to families?"
    assert result['intents'][1]['span']=='what is its value to families?'
    discovery.read_stage_source(result['artifacts']['extract'],'cluster')


def test_low_support_semantically_clear_singleton_is_eligible(coldstart,monkeypatch):
    cfg,path=coldstart
    original=FakeLLM.chat_json
    def controlled(self,system,user,schema,**kwargs):
        if schema is discovery.ClusterReview:
            ids=[m['intent_id'] for m in json.loads(user)['members']]
            return schema(verdict='uncertain',semantic_clear=True,object='对象',action='查询',
                          reason='样本语义明确，类别仅有一条支持')
        return original(self,system,user,schema,**kwargs)
    monkeypatch.setattr(FakeLLM,'chat_json',controlled)
    result=discovery.run_atomic_discovery(cfg,str(path))
    assert len(result['candidate_taxonomy']['tasks'])==2
    assert all(c['support_level']=='single_example' and not c['topic_pending'] for c in result['clusters'])
    assert result['delivery_topics']['coverage']['covered_rows']==2


def test_cluster_review_omits_top_level_evidence():
    review = discovery.ClusterReview(verdict='consistent', object='账单', action='查询', reason='同类动作')
    assert 'evidence_ids' not in review.model_dump()
    discovery.check_evidence(review, {'a'})


def test_initial_extraction_has_no_slots_and_preserves_grounded_details():
    raw = '给张三转账100元'
    parsed = extraction([{'span':raw,'text':raw}])
    assert discovery.validate_extraction(raw,parsed)==[(0,len(raw))]
    assert 'slots' not in parsed.intents[0].model_dump()
    assert parsed.intents[0].text==raw
    assert 'slots' not in discovery.AtomicAction.model_fields
    atom={**parsed.intents[0].model_dump(),'intent_id':'r0:i0','raw_text':raw,'text_index':0}
    samples=discovery.review_sample([atom],np.array([[1.,0.]]),8)
    assert 'slots' not in samples[0]
    assert samples[0]['text']==raw


def test_initial_workflow_audits_leaf_merge_before_export(coldstart, monkeypatch):
    from intent_engine.atomic.leaf_merge import MergeProposal
    cfg,path=coldstart
    cfg['atomic_discovery']['leaf_merge_enabled']=True
    original=FakeLLM.chat_json
    calls=[]
    def chat(self,prompt,user,schema,**kwargs):
        if schema is MergeProposal:
            calls.append(json.loads(user))
            return schema(reason='打开应用与缴费是不同动作，不能合并',decision='keep')
        return original(self,prompt,user,schema,**kwargs)
    monkeypatch.setattr(FakeLLM,'chat_json',chat)
    result=discovery.run_atomic_discovery(cfg,str(path))
    assert calls and result['leaf_merging']['passes'][0]['phase']=='initial'
    assert result['leaf_merging']['passes'][0]['logical_requests']==1
    assert result['leaf_merging']['passes'][0]['applied']==0
    assert len(result['candidate_taxonomy']['tasks'])==2
