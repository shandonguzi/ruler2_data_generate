"""Remove completed task histories together with their workflow, reversibly staged."""
import json
import re
import shutil
import uuid
from contextlib import contextmanager
from pathlib import Path

from intent_engine.infrastructure.atomic_json import replace_with_retry


def associated_jobs(manager, run_id, manifest_job_id, trace_root):
    matches = []
    for job in manager.jobs.values():
        if not job.workflow_run_id and re.fullmatch(r'[a-f0-9]{12}', job.job_id):
            path = Path(trace_root) / job.job_id / 'requests.jsonl'
            if path.is_file() and not path.is_symlink():
                with path.open(encoding='utf-8') as stream:
                    for line in stream:
                        try:
                            event = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(event, dict) and event.get('run_id'):
                            job.workflow_run_id = event['run_id']
                            break
        if job.workflow_run_id == run_id or (not job.workflow_run_id and job.job_id == manifest_job_id):
            matches.append(job)
    return matches


@contextmanager
def remove_histories(manager, jobs, trace_root):
    """Caller holds manager.lock. Roll back staged directories if workflow deletion fails."""
    moved = []
    result = {'deleted_jobs': [], 'cleanup_warnings': []}
    try:
        for job in jobs:
            if job.status in {'queued', 'running', 'paused'} or job.process is not None or job.finished_at is None:
                raise ValueError(f'任务 {job.job_id} 尚未结束，不能删除任务历史')
            if not re.fullmatch(r'[a-f0-9]{12}', job.job_id):
                raise ValueError('任务编号无效，无法安全清理历史文件')
            for root in (manager.history_dir, trace_root):
                if root is None:
                    continue
                root = Path(root)
                source = root / job.job_id
                if root.is_symlink() or source.is_symlink():
                    raise ValueError('任务历史目录为符号链接，不能安全清理')
                if not source.exists():
                    continue
                if not source.is_dir():
                    raise ValueError('任务历史目录格式错误')
                destination = root / ('.delete_' + job.job_id + '_' + uuid.uuid4().hex)
                replace_with_retry(source, destination)
                moved.append((source, destination))
        yield result
    except Exception:
        for source, destination in reversed(moved):
            replace_with_retry(destination, source)
        raise
    else:
        for job in jobs:
            manager.jobs.pop(job.job_id, None)
            result['deleted_jobs'].append(job.job_id)
        for _, destination in moved:
            try:
                shutil.rmtree(destination)
            except OSError as exc:
                result['cleanup_warnings'].append(f'任务已移除，暂存目录清理失败：{destination}：{exc}')
