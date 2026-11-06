import json

import pytest

from intent_engine.workflows.workflow_diagnostics import workflow_diagnostics


def data():
    return {'run_id': 'test', 'status': 'completed', 'completed_stages': ['extract', 'cluster', 'review', 'domains'],
            'intents': [{'status': 'valid', 'cluster_id': 0}],
            'clusters': [{'cluster_id': 0, 'review': None}], 'candidate_taxonomy': {'domains': [], 'tasks': []}}


def test_legacy_completed_flags_do_not_hide_review_failure():
    source = data()
    source['clusters'][0]['review_error'] = 'failed'
    diagnosis = workflow_diagnostics(source)
    assert diagnosis['next_stage'] == 'review'
    assert diagnosis['review']['failed'] == 1
    assert next(s for s in diagnosis['stages'] if s['key'] == 'review')['status'] == 'failed'
    assert next(s for s in diagnosis['stages'] if s['key'] == 'domains')['status'] == 'blocked'


def test_skipped_single_stage_and_running_results_explain_next_action():
    source = data()
    source['requested_stage'] = 'cluster'
    assert '单阶段' in workflow_diagnostics(source)['message']
    source['stage_outcomes'] = {'review': {'status': 'skipped'}}
    assert '关闭了审核' in workflow_diagnostics(source)['message']
    source['status'] = 'running'
    assert workflow_diagnostics(source)['next_stage'] is None
    assert '正在运行' in workflow_diagnostics(source)['message']


def test_ready_and_domains_failure():
    source = data()
    source['clusters'][0]['review'] = {'verdict': 'consistent', 'object': '应用', 'action': '打开'}
    source['errors'] = [{'stage': 'domains', 'error': 'bad schema'}]
    assert workflow_diagnostics(source)['code'] == 'domains_failed'
    source['candidate_taxonomy'] = {'domains': [{'name': '应用'}], 'tasks': [{'action': '打开'}]}
    assert workflow_diagnostics(source)['code'] == 'ready'
    assert workflow_diagnostics(source)['tasks'] == 1


def test_diagnostics_endpoint_uses_latest_scoped_report(tmp_path, monkeypatch):
    import web_server as web
    from fastapi import HTTPException
    report = tmp_path / 'report.json'
    report.write_text(json.dumps(data()))
    manifest = {'run_id': 'test', 'requested_stage': 'cluster', 'artifacts': {'report': str(report)}}
    (tmp_path / 'coldstart_test_workflow.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    result = web.api_workflow_diagnostics('test')
    assert result['review']['pending'] == 1 and '单阶段' in result['message']
    with pytest.raises(HTTPException):
        web.api_workflow_diagnostics('../test')
