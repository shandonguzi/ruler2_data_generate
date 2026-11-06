import json

import pytest

from intent_engine.web.app import Job, JobManager
from intent_engine.infrastructure.progress import PROGRESS_PREFIX
from intent_engine.infrastructure.model_requests import USAGE_PREFIX


@pytest.mark.parametrize('status', ['succeeded', 'failed', 'cancelled', 'running', 'queued'])
def test_job_history_restores_progress_usage_logs_and_status(tmp_path, status):
    manager = JobManager(tmp_path)
    job = Job(job_id='123456abcdef', job_type='workflow', command=['python', 'main.py'])
    manager.jobs[job.job_id] = job
    manager._append(job, PROGRESS_PREFIX + json.dumps({'event': 'plan', 'steps': [{'id': 'review', 'label': '审核'}]}))
    manager._append(job, PROGRESS_PREFIX + json.dumps({'event': 'start', 'id': 'review'}))
    manager._append(job, USAGE_PREFIX + json.dumps({'kind': 'llm', 'model': 'test', 'reported': True, 'prompt_tokens': 12, 'completion_tokens': 3, 'total_tokens': 15}))
    manager._append(job, '历史日志')
    job.status = status
    manager._save(job)
    restored = JobManager(tmp_path).get(job.job_id)
    assert restored.status == ('failed' if status in {'running', 'queued'} else status)
    assert not restored.logs_loaded
    assert restored.to_dict(include_logs=True)['logs'] == ['历史日志']
    assert restored.progress.current_id == 'review'
    assert restored.token_usage.snapshot()['total_tokens'] == 15
    assert restored.stage_usage['review'].snapshot()['requests'] == 1
    assert restored.process is None
    if status in {'running', 'queued'}:
        assert '服务已重启' in restored.error
        assert restored.finished_at
    assert JobManager(tmp_path).recent()[0].job_id == job.job_id


def test_damaged_history_does_not_break_startup(tmp_path):
    directory = tmp_path / 'damaged'
    directory.mkdir()
    (directory / 'state.json').write_text('{broken', encoding='utf-8')
    assert JobManager(tmp_path).recent() == []


def test_finished_history_startup_does_not_read_logs_or_write_state(tmp_path,monkeypatch):
    manager=JobManager(tmp_path)
    job=Job(job_id='123456abcdef',job_type='test',command=[],status='succeeded')
    manager._append(job,'保存的日志')
    path=tmp_path/job.job_id/'state.json'
    stamp=path.stat().st_mtime_ns
    from pathlib import Path
    original=Path.open
    def guarded(self,*args,**kwargs):
        if self.name=='logs.jsonl':raise AssertionError('startup read logs')
        return original(self,*args,**kwargs)
    with monkeypatch.context() as m:
        m.setattr(Path,'open',guarded)
        restored=JobManager(tmp_path).get(job.job_id)
        assert restored.to_dict()['log_count']==1
    assert path.stat().st_mtime_ns==stamp
    assert restored.to_dict(include_logs=True)['logs']==['保存的日志']


def test_log_tail_keeps_utf8_and_skips_corrupt_lines(tmp_path):
    from intent_engine.infrastructure.log_tail import read_log_tail
    path=tmp_path/'logs.jsonl'
    path.write_text(''.join(json.dumps('日志'+str(i),ensure_ascii=False)+'\n' for i in range(30000))+'{bad}\n',encoding='utf-8')
    result=read_log_tail(path,limit=3)
    assert result==['日志29998','日志29999']


def test_log_response_is_bounded_and_cursor_still_reaches_end():
    job=Job(job_id='123456abcdef',job_type='test',command=[],logs=[str(i) for i in range(20000)])
    payload=job.to_dict(include_logs=True)
    assert len(payload['logs'])==500
    assert payload['logs'][0]=='19500' and payload['next_offset']==20000
    assert payload['logs_skipped']==19500
    assert job.to_dict(include_logs=True,offset=20000)['logs']==[]


def test_tail_limits_bytes_even_for_a_giant_line(tmp_path):
    from intent_engine.infrastructure.log_tail import read_log_tail
    path=tmp_path/'logs.jsonl'
    path.write_bytes(json.dumps('x'*100000).encode()+b'\n'+json.dumps('last').encode()+b'\n')
    assert read_log_tail(path,max_bytes=128)==['last']
