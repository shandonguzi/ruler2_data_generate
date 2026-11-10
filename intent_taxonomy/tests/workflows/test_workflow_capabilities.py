import copy
import json
from pathlib import Path

import pytest

from intent_engine.atomic.atomic_discovery import read_stage_source, validate_stage_source
from intent_engine.atomic.topic_hierarchy import build_topic_tree
from intent_engine.workflows.workflow_capabilities import workflow_capabilities, _inspect


def snapshot():
    atom = dict(intent_id='r1:i0', row_id=1, raw_text='pay bill', text='pay bill', span='pay bill',
                start=0, end=8, status='valid', cluster_id=0)
    return dict(mode='atomic_actions', run_id='run', completed_stages=[],
                rows=[{'row_id': 1, 'raw_text': 'pay bill'}], intents=[atom],
                clusters=[dict(cluster_id=0, unique_text_count=1, review_samples=[copy.deepcopy(atom)],
                               review={'verdict':'consistent','object':'bill','action':'pay','reason':'same'})])


def save(tmp_path, data):
    path = tmp_path / 'snapshot.json'
    path.write_text(json.dumps(data))
    return path, {'artifacts': {'review': str(path)}}


def test_uses_actual_inputs_even_without_completion_labels(tmp_path):
    path, workflow = save(tmp_path, snapshot())
    capabilities = workflow_capabilities(tmp_path, workflow)['capabilities']
    assert capabilities['cluster']['available']
    assert capabilities['review']['sources'] == ['review']
    assert capabilities['domains']['available']
    assert not capabilities['iterate']['available']
    assert not capabilities['pairs']['available']
    assert '至少需要两个' in capabilities['pairs']['reason']
    assert read_stage_source(path, 'review')['clusters']


def test_completion_labels_do_not_make_missing_inputs_usable(tmp_path):
    data = snapshot()
    data.update(completed_stages=['extract', 'cluster', 'review', 'domains'], intents=[], clusters=[])
    path, workflow = save(tmp_path, data)
    result = workflow_capabilities(tmp_path, workflow)
    assert result['capabilities']['extract']['available']
    for stage in ['cluster', 'review', 'domains', 'iterate']:
        assert not result['capabilities'][stage]['available']
        with pytest.raises(ValueError):
            read_stage_source(path, stage)


def test_cache_reuses_inspection_and_invalidates_on_change_or_delete(tmp_path, monkeypatch):
    import intent_engine.workflows.workflow_capabilities as module
    path, workflow = save(tmp_path, snapshot())
    _inspect.cache_clear()
    calls = []
    original = module.validate_stage_source
    def track(data, stage):
        calls.append(stage)
        return original(data, stage)
    monkeypatch.setattr(module, 'validate_stage_source', track)
    workflow_capabilities(tmp_path, workflow)
    count = len(calls)
    workflow_capabilities(tmp_path, workflow)
    assert len(calls) == count
    path.write_text('{}')
    assert not workflow_capabilities(tmp_path, workflow)['capabilities']['extract']['available']
    assert len(calls) > count
    path.unlink()
    assert not workflow_capabilities(tmp_path, workflow)['capabilities']['extract']['available']


def test_corrupt_and_external_files_have_reasons_without_breaking_list(tmp_path):
    path, workflow = save(tmp_path, snapshot())
    path.write_text('{')
    result = workflow_capabilities(tmp_path, workflow)
    assert '文件无法读取' in result['capabilities']['extract']['reason']
    workflow['artifacts']['review'] = '/outside/snapshot.json'
    assert '不属于' in workflow_capabilities(tmp_path, workflow)['capabilities']['extract']['reason']


def test_iteration_requires_real_approved_leaf_mapping():
    data = snapshot()
    data['topic_hierarchy'] = build_topic_tree([0], {0:['pay']})
    data['topic_hierarchy']['source'] = 'BERTopic+LLM'
    node = data['topic_hierarchy']['nodes'][0]
    node['semantic_status'] = 'approved'
    data['candidate_taxonomy'] = {'version':1, 'domains':[], 'tasks':[
        {'task_id':'t', 'domain':'billing', 'object':'bill', 'action':'pay', 'description':'pay bill'}]}
    data['task_topic_nodes'] = {'t': node['node_id']}
    data['cluster_task_ids'] = {'0':'t'}
    stale = copy.deepcopy(data)
    stale['neighbor_pairs'] = [[0,999]]
    stale['pair_reviews'] = [{'left':0,'right':999}]
    repaired = validate_stage_source(stale,'iterate')
    assert repaired['neighbor_pairs'] == [] and repaired['pair_reviews'] == []
    assert repaired['input_repairs'][-1]['kind'] == 'stale_neighbor_graph'
    with pytest.raises(ValueError,match='相邻簇引用无效'):
        validate_stage_source({**copy.deepcopy(data),'neighbor_pairs':[[0,999]]},'pairs')
    validate_stage_source(copy.deepcopy(data), 'iterate')
    data['task_topic_nodes'] = {}
    with pytest.raises(ValueError, match='已批准'):
        validate_stage_source(copy.deepcopy(data), 'iterate')


def test_api_exposes_inspected_capabilities_and_rechecks_changed_file(tmp_path, monkeypatch):
    import intent_engine.web.app as web
    import intent_engine.workflows.workflow_artifacts as artifacts
    path, workflow = save(tmp_path, snapshot())
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    monkeypatch.setattr(artifacts, 'list_workflows', lambda reports: [workflow])
    assert web.api_workflows()['runs'][0]['capabilities']['review']['available']
    monkeypatch.setattr(artifacts, 'workflow_artifact', lambda *args: path)
    monkeypatch.setattr(web, '_start_job', lambda *args: pytest.fail('invalid input must never start'))
    path.write_text('{}')
    with pytest.raises(web.HTTPException) as error:
        web.api_job_workflow_stage(web.WorkflowStageRequest(stage='review',run_id='run',artifact='review'))
    assert error.value.status_code == 400


def test_upstream_repair_does_not_require_valid_downstream_outputs():
    data = snapshot()
    data['topic_hierarchy'] = {'nodes': 'broken downstream tree'}
    validate_stage_source(copy.deepcopy(data), 'cluster')
    data.pop('topic_hierarchy')
    data['clusters'][0]['review']['boundary_ids'] = ['missing-id']
    validate_stage_source(copy.deepcopy(data), 'review')
    with pytest.raises(ValueError):
        validate_stage_source(copy.deepcopy(data), 'domains')


def test_disk_cache_survives_memory_clear_and_corruption(tmp_path, monkeypatch):
    import intent_engine.workflows.workflow_capabilities as module
    path, workflow = save(tmp_path, snapshot())
    first=workflow_capabilities(tmp_path,workflow)
    module._inspect.cache_clear()
    original=module.validate_stage_source
    def unexpected(*args):
        raise AssertionError('disk cache should avoid validation')
    monkeypatch.setattr(module,'validate_stage_source',unexpected)
    assert workflow_capabilities(tmp_path,workflow)==first
    monkeypatch.setattr(module,'validate_stage_source',original)
    cache=tmp_path.parent/'cache'/'workflow_capabilities'
    for entry in cache.glob('*.json'):
        entry.write_text('{')
    module._inspect.cache_clear()
    assert workflow_capabilities(tmp_path,workflow)==first
