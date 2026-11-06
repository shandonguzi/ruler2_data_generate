import json
from pathlib import Path

import pytest

from intent_engine.web.app import Job, JobManager, WorkflowDeleteRequest
from intent_engine.workflows.workflow_layout import create_workflow_directory


def setup_history(tmp_path, monkeypatch, *, legacy=False):
    import intent_engine.web.app as web
    reports = tmp_path / 'data' / 'reports'
    history = tmp_path / 'data' / 'job_history'
    trace = tmp_path / 'data' / 'request_traces'
    manager = JobManager(history)
    monkeypatch.setattr(web, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(web, '_reports_dir', lambda: reports)
    monkeypatch.setattr(web, 'jobs', manager)
    job = Job(job_id='123456abcdef', job_type='discover', command=[], status='failed', finished_at=1,
              workflow_run_id=None if legacy else 'run')
    manager.jobs[job.job_id] = job
    manager._save(job)
    (trace / job.job_id).mkdir(parents=True)
    (trace / job.job_id / 'requests.jsonl').write_text(json.dumps({'run_id':'run'})+'\n')
    paths = create_workflow_directory(reports, 'run')
    paths['report'].write_text('{}')
    paths['manifest'].write_text(json.dumps({'run_id':'run','status':'failed','artifacts':{'report':str(paths['report'])},
                                            **({} if legacy else {'job_id':job.job_id})}))
    return web, manager, job, paths, trace


@pytest.mark.parametrize('legacy', [False, True])
def test_workflow_deletion_removes_only_associated_finished_history_and_survives_restart(tmp_path, monkeypatch, legacy):
    web, manager, job, paths, trace = setup_history(tmp_path, monkeypatch, legacy=legacy)
    unrelated = Job(job_id='abcdef123456',job_type='discover',command=['python','main.py','discover','--input',str(tmp_path/'input.csv')],status='running')
    manager.jobs[unrelated.job_id] = unrelated
    plan = web.api_workflow_delete_preview('run')
    assert plan['history_jobs'] == [{'job_id':job.job_id,'status':'failed'}]
    result = web.api_delete_workflow('run',WorkflowDeleteRequest(revision=plan['revision']))
    assert result['deleted_jobs'] == [job.job_id]
    assert job.job_id not in manager.jobs and unrelated.job_id in manager.jobs
    assert not (manager.history_dir/job.job_id).exists()
    assert not (trace/job.job_id).exists()
    assert not paths['manifest'].exists()
    assert job.job_id not in JobManager(manager.history_dir).jobs


def test_failed_workflow_deletion_restores_history_and_requests(tmp_path, monkeypatch):
    web, manager, job, paths, trace = setup_history(tmp_path, monkeypatch)
    plan = web.api_workflow_delete_preview('run')
    paths['report'].write_text('{"changed":true}')
    with pytest.raises(web.HTTPException):
        web.api_delete_workflow('run',WorkflowDeleteRequest(revision=plan['revision']))
    assert paths['manifest'].exists()
    assert (manager.history_dir/job.job_id/'state.json').exists()
    assert (trace/job.job_id/'requests.jsonl').exists()
    assert job.job_id in manager.jobs
    assert not list(manager.history_dir.glob('.delete_*'))


def test_old_orphan_history_can_be_deleted_without_deleting_workflow(tmp_path, monkeypatch):
    web, manager, job, paths, trace = setup_history(tmp_path, monkeypatch)
    result = web.api_delete_job(job.job_id)
    assert result['deleted_jobs'] == [job.job_id]
    assert paths['manifest'].exists() and paths['report'].exists()
    assert job.job_id not in JobManager(manager.history_dir).jobs
    assert not (trace/job.job_id).exists()


def test_running_history_cannot_be_removed(tmp_path, monkeypatch):
    web, manager, job, paths, trace = setup_history(tmp_path, monkeypatch)
    job.status = 'running'
    with pytest.raises(web.HTTPException) as error:
        web.api_delete_job(job.job_id)
    assert error.value.status_code == 409
    assert (manager.history_dir/job.job_id/'state.json').exists()


def test_history_move_failure_preserves_flow_and_memory(tmp_path, monkeypatch):
    web, manager, job, paths, trace = setup_history(tmp_path, monkeypatch)
    import intent_engine.workflows.job_history_cleanup as cleanup
    original = cleanup.replace_with_retry
    count = 0
    def fail_second(source, target):
        nonlocal count
        count += 1
        if count == 2:
            raise PermissionError('locked')
        return original(source, target)
    monkeypatch.setattr(cleanup,'replace_with_retry',fail_second)
    plan = web.api_workflow_delete_preview('run')
    with pytest.raises(web.HTTPException):
        web.api_delete_workflow('run',WorkflowDeleteRequest(revision=plan['revision']))
    assert paths['manifest'].exists()
    assert (manager.history_dir/job.job_id/'state.json').exists()
    assert (trace/job.job_id).exists()
    assert job.job_id in manager.jobs
