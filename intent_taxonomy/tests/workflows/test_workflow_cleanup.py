import json
from pathlib import Path

import pytest

from intent_engine.workflows.workflow_cleanup import delete_workflow, workflow_delete_plan


def version(reports, name='old', source=None):
    artifacts = {}
    for stage in ['extract', 'cluster', 'discovery']:
        path = reports / f'coldstart_{name}_{stage}.json'
        path.write_text(json.dumps({'run_id': name, 'rows': [], 'intents': []}))
        artifacts['report' if stage == 'discovery' else stage] = str(path)
    workbook = reports / f'coldstart_{name}_review.xlsx'
    workbook.write_bytes(b'test workbook')
    artifacts['workbook'] = str(workbook)
    manifest = reports / f'coldstart_{name}_workflow.json'
    manifest.write_text(json.dumps({'run_id': name, 'artifacts': artifacts, 'source': source}))
    return artifacts, manifest


def test_delete_only_owned_outputs_and_manifest(tmp_path):
    files, manifest = version(tmp_path)
    other, _ = version(tmp_path, 'old_suffix')
    input_file = tmp_path / 'input.csv'
    input_file.write_text('text\nhello')
    orphan = tmp_path / 'coldstart_old_domains.json'
    orphan.write_text('{}')  # Failed-stage output not yet registered by the manifest.
    unknown = tmp_path / 'coldstart_old_user_notes.txt'
    unknown.write_text('keep')
    plan = workflow_delete_plan(tmp_path, 'old')
    assert len(plan['files']) == 6
    result = delete_workflow(tmp_path, 'old', plan['revision'])
    assert result['deleted'] == 6
    assert not manifest.exists() and not orphan.exists()
    assert all(not Path(path).exists() for path in files.values())
    assert all(Path(path).exists() for path in other.values())
    assert input_file.exists() and unknown.exists()


def test_keep_other_versions_references(tmp_path):
    files, _ = version(tmp_path)
    child, child_manifest = version(tmp_path, 'child', source={'run_id': 'old', 'path': files['extract']})
    plan = workflow_delete_plan(tmp_path, 'old')
    assert plan['retained'][0]['name'] == 'coldstart_old_extract.json'
    assert plan['retained'][0]['referenced_by'] == ['child']
    delete_workflow(tmp_path, 'old', plan['revision'])
    assert Path(files['extract']).exists()
    assert child_manifest.exists() and all(Path(path).exists() for path in child.values())


@pytest.mark.parametrize('change', ['file', 'reference'])
def test_changed_plan_requires_new_preview(tmp_path, change):
    files, manifest = version(tmp_path)
    plan = workflow_delete_plan(tmp_path, 'old')
    if change == 'file':
        Path(files['extract']).write_text('new contents')
    else:
        version(tmp_path, 'new', source={'path': files['extract']})
    with pytest.raises(ValueError, match='重新预览'):
        delete_workflow(tmp_path, 'old', plan['revision'])
    assert manifest.exists() and all(Path(path).exists() for path in files.values())


def test_path_and_symlink_scope(tmp_path):
    reports = tmp_path / 'reports'
    reports.mkdir()
    files, manifest = version(reports)
    outside = tmp_path / 'outside.json'
    outside.write_text('keep')
    data = json.loads(manifest.read_text())
    data['artifacts']['report'] = str(outside)
    manifest.write_text(json.dumps(data))
    Path(files['cluster']).unlink()
    Path(files['cluster']).symlink_to(outside)
    with pytest.raises(ValueError, match='符号链接'):
        workflow_delete_plan(reports, 'old')
    with pytest.raises(ValueError, match='编号'):
        workflow_delete_plan(reports, '../outside')
    assert outside.read_text() == 'keep'


def test_move_failure_rolls_back_before_unlink(tmp_path, monkeypatch):
    import intent_engine.workflows.workflow_cleanup as cleanup
    files, manifest = version(tmp_path)
    plan = workflow_delete_plan(tmp_path, 'old')
    original = cleanup.os.replace
    count = 0
    def move(source, destination):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError('simulated failure')
        original(source, destination)
    monkeypatch.setattr(cleanup.os, 'replace', move)
    with pytest.raises(OSError):
        delete_workflow(tmp_path, 'old', plan['revision'])
    assert manifest.exists() and all(Path(path).exists() for path in files.values())
    assert not list(tmp_path.glob('.delete_*'))


def test_api_blocks_active_jobs_and_checks_revision(tmp_path, monkeypatch):
    import web_server as web
    files, _ = version(tmp_path)
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    manager = web.JobManager()
    monkeypatch.setattr(web, 'jobs', manager)
    manager.jobs['active'] = web.Job(job_id='active', job_type='discover', command=[], status='running')
    with pytest.raises(web.HTTPException) as exc:
        web.api_workflow_delete_preview('old')
    assert exc.value.status_code == 409
    with pytest.raises(web.HTTPException):
        web.api_delete_workflow('old', web.WorkflowDeleteRequest(revision='0'*64))
    manager.jobs.clear()
    plan = web.api_workflow_delete_preview('old')
    with pytest.raises(web.HTTPException):
        web.api_delete_workflow('old', web.WorkflowDeleteRequest(revision='0'*64))
    result = web.api_delete_workflow('old', web.WorkflowDeleteRequest(revision=plan['revision']))
    assert result['deleted'] == 5
    assert all(not Path(path).exists() for path in files.values())


def test_delete_includes_request_log_and_derived_index(tmp_path):
    from intent_engine.infrastructure.request_trace_store import prepare_request_index
    directory = tmp_path / 'indexed'
    requests = directory / 'requests'
    requests.mkdir(parents=True)
    (directory / 'workflow.json').write_text(json.dumps({'run_id': 'indexed', 'artifacts': {}}))
    trace = requests / 'requests.jsonl'
    trace.write_text('{"request_id":"a"}\n')
    prepare_request_index(trace)
    plan = workflow_delete_plan(tmp_path, 'indexed')
    assert {item['name'] for item in plan['files']} == {
        'indexed/workflow.json', 'indexed/requests/requests.jsonl', 'indexed/requests/requests.jsonl.sqlite3'}
    delete_workflow(tmp_path, 'indexed', plan['revision'])
    assert not directory.exists()


def test_unrelated_running_version_does_not_block_failed_version_deletion(tmp_path, monkeypatch):
    import intent_engine.web.app as web
    base, _ = version(tmp_path, 'base')
    old, old_manifest = version(tmp_path, 'failed_one', source={'run_id':'base','path':base['extract']})
    other, other_manifest = version(tmp_path, 'running_two', source={'run_id':'base','path':base['extract']})
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    manager = web.JobManager()
    monkeypatch.setattr(web, 'jobs', manager)
    manager.jobs['active'] = web.Job(job_id='active', job_type='workflow:iterate',
        command=['python','main.py','discover','--source',base['extract']], status='running', workflow_run_id='running_two')
    plan = web.api_workflow_delete_preview('failed_one')
    # Ongoing progress of version two must not invalidate version one's confirmation.
    content = json.loads(other_manifest.read_text())
    content['iteration'] = {'rounds_completed':2}
    other_manifest.write_text(json.dumps(content))
    result = web.api_delete_workflow('failed_one', web.WorkflowDeleteRequest(revision=plan['revision']))
    assert result['deleted'] == 5
    assert not old_manifest.exists()
    assert other_manifest.exists() and all(Path(path).exists() for path in other.values())
    assert manager.jobs['active'].status == 'running'


@pytest.mark.parametrize('status', ['queued','running'])
def test_active_input_dependency_is_blocked_at_preview_and_confirmation(tmp_path, monkeypatch, status):
    import intent_engine.web.app as web
    files, manifest = version(tmp_path)
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    manager = web.JobManager()
    monkeypatch.setattr(web, 'jobs', manager)
    plan = web.api_workflow_delete_preview('old')
    # A new dependency can appear after preview but before confirmation.
    manager.jobs['active'] = web.Job(job_id='active', job_type='workflow:iterate',
        command=['python','main.py','discover','--source',files['extract']], status=status)
    with pytest.raises(web.HTTPException) as error:
        web.api_workflow_delete_preview('old')
    assert error.value.status_code == 409 and '任务输入' in error.value.detail
    with pytest.raises(web.HTTPException) as error:
        web.api_delete_workflow('old', web.WorkflowDeleteRequest(revision=plan['revision']))
    assert error.value.status_code == 409
    assert manifest.exists() and all(Path(path).exists() for path in files.values())


@pytest.mark.parametrize('identified_by', ['manifest','trace'])
def test_active_output_is_blocked_even_before_first_model_request(tmp_path, monkeypatch, identified_by):
    import intent_engine.web.app as web
    files, manifest = version(tmp_path)
    if identified_by == 'manifest':
        content = json.loads(manifest.read_text())
        content['job_id'] = 'active'
        manifest.write_text(json.dumps(content))
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    manager = web.JobManager()
    monkeypatch.setattr(web, 'jobs', manager)
    manager.jobs['active'] = web.Job(job_id='active', job_type='discover',
        command=['python','main.py','discover','--input',str(tmp_path/'input.csv')], status='running',
        workflow_run_id='old' if identified_by == 'trace' else None)
    with pytest.raises(web.HTTPException) as error:
        web.api_workflow_delete_preview('old')
    assert error.value.status_code == 409 and '生成或更新' in error.value.detail
    assert manifest.exists()


def test_new_reference_still_invalidates_confirmation_while_other_job_runs(tmp_path, monkeypatch):
    import intent_engine.web.app as web
    files, manifest = version(tmp_path)
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    manager = web.JobManager()
    monkeypatch.setattr(web, 'jobs', manager)
    manager.jobs['active'] = web.Job(job_id='active',job_type='discover',status='running',
        command=['python','main.py','discover','--input',str(tmp_path/'input.csv')])
    plan = web.api_workflow_delete_preview('old')
    version(tmp_path, 'new_reference', source={'path':files['extract']})
    with pytest.raises(web.HTTPException) as error:
        web.api_delete_workflow('old', web.WorkflowDeleteRequest(revision=plan['revision']))
    assert error.value.status_code == 400 and '重新预览' in error.value.detail
    assert manifest.exists()
