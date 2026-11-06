import json
from pathlib import Path

import pytest
from fastapi import HTTPException

import web_server as web
from intent_engine.workflows.workflow_artifacts import workflow_artifact


@pytest.fixture
def workflow_files(tmp_path, monkeypatch):
    reports = tmp_path / 'reports'
    reports.mkdir()
    source = reports / 'atoms.json'
    source.write_text(json.dumps({'mode': 'atomic_actions', 'run_id': 'parent', 'stage': 'extract',
        'completed_stages': ['extract'], 'rows': [{'row_id': 1, 'raw_text': '打开应用'}],
        'intents': [{'intent_id': 'r1:i0', 'row_id': 1, 'raw_text': '打开应用', 'span': '打开应用',
                     'text': '打开应用', 'status': 'valid', 'start': 0, 'end': 4, 'slots': {}}]}))
    manifest = {'run_id': 'parent', 'artifacts': {'extract': str(source)}, 'completed_stages': ['extract']}
    (reports / 'coldstart_parent_workflow.json').write_text(json.dumps(manifest))
    monkeypatch.setattr(web, '_reports_dir', lambda: reports)
    monkeypatch.setattr(web, 'ROOT', tmp_path)
    calls = []
    monkeypatch.setattr(web, '_start_job', lambda kind, cmd: calls.append((kind, cmd)) or {'job_id': 'test'})
    return reports, source, calls


def test_stage_selection_is_scoped_to_existing_run(workflow_files):
    reports, source, calls = workflow_files
    web.api_job_workflow_stage(web.WorkflowStageRequest(stage='export', run_id='parent', artifact='extract'))
    assert str(source) in calls[0][1]
    assert '--source' in calls[0][1]
    assert '--input' not in calls[0][1]
    for req in [web.WorkflowStageRequest(stage='export'),
                web.WorkflowStageRequest(stage='export', run_id='missing', artifact='extract'),
                web.WorkflowStageRequest(stage='review', run_id='parent', artifact='extract'),
                web.WorkflowStageRequest(stage='export', run_id='parent', artifact='extract', external_source=str(source))]:
        with pytest.raises(HTTPException):
            web.api_job_workflow_stage(req)
    assert len(calls) == 1


def test_external_file_requires_explicit_override(workflow_files):
    _, source, calls = workflow_files
    web.api_job_workflow_stage(web.WorkflowStageRequest(stage='export', use_external=True, external_source=str(source)))
    assert len(calls) == 1


def test_manifest_cannot_escape_report_directory(workflow_files, tmp_path):
    reports, _, _ = workflow_files
    outside = tmp_path / 'outside.json'
    outside.write_text('{}')
    path = reports / 'coldstart_parent_workflow.json'
    manifest = json.loads(path.read_text())
    manifest['artifacts']['extract'] = str(outside)
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match='不属于'):
        workflow_artifact(reports, 'parent', 'extract')
    with pytest.raises(ValueError, match='编号'):
        workflow_artifact(reports, '../outside', 'extract')
