"""Preview and delete only a version's owned outputs, preserving live references."""
import hashlib
import json
import os
from pathlib import Path

from intent_engine.atomic.atomic_discovery import WORKFLOW_STAGES
from intent_engine.workflows.workflow_artifacts import load_workflow
from intent_engine.workflows.workflow_layout import find_manifest, manifest_paths, valid_manifest_location, workflow_paths, check_no_symlinks


def workflow_delete_plan(reports: Path, run_id: str) -> dict:
    load_workflow(reports, run_id)  # Validate identity before constructing any paths.
    reports = reports.resolve()
    references = {}
    for path in sorted(manifest_paths(reports)):
        check_no_symlinks(path, reports)
        if path.is_symlink():
            raise ValueError("流程记录是符号链接，无法安全清理")
        content = path.read_text(encoding="utf-8")
        data = json.loads(content)
        if not valid_manifest_location(path, reports, data.get("run_id")):
            raise ValueError("流程记录损坏，无法确认文件引用关系")
        if data["run_id"] == run_id:
            continue
        values = [*data.get("artifacts", {}).values(), (data.get("source") or {}).get("path"),
                  (data.get("input") or {}).get("path")]
        for value in values:
            if isinstance(value, str) and value:
                references.setdefault(str(Path(value).resolve()), set()).add(data["run_id"])
    manifest_path = find_manifest(reports, run_id)
    legacy = manifest_path.parent == reports
    owned = workflow_paths(reports, run_id, legacy=legacy)
    if not legacy:
        trace = manifest_path.parent / 'requests' / 'requests.jsonl'
        owned.update(request_trace=trace, request_index=Path(str(trace) + '.sqlite3'))
    files, retained, fingerprint = [], [], []
    for path in sorted(owned.values()):
        name = str(path.relative_to(reports))
        check_no_symlinks(path, reports)
        if not path.exists():
            continue
        if not path.is_file():
            raise ValueError(f"产物 {name} 不是普通文件")
        stat = path.stat()
        fingerprint.append([name, stat.st_size, stat.st_mtime_ns, stat.st_ino])
        item = {"name": name, "size": stat.st_size}
        users = sorted(references.get(str(path), []))
        if users:
            if path == manifest_path:
                raise ValueError("该流程记录本身仍被其他流程引用，请先处理引用版本")
            retained.append({**item, "referenced_by": users})
        else:
            files.append(item)
    # Only relevant ownership/reference changes invalidate confirmation. Unrelated
    # running workflows may update their manifests continuously.
    revision = hashlib.sha256(json.dumps([fingerprint, files, retained], ensure_ascii=False).encode()).hexdigest()
    return {"run_id": run_id, "revision": revision, "files": files, "retained": retained,
            "bytes": sum(item["size"] for item in files), "directory": None if legacy else run_id}


def delete_workflow(reports: Path, run_id: str, revision: str) -> dict:
    plan = workflow_delete_plan(reports, run_id)
    if revision != plan["revision"]:
        raise ValueError("流程文件或引用关系已变化，请重新预览删除清单")
    # Renames are reversible until all files are staged. Keep the manifest last.
    import uuid
    from intent_engine.infrastructure.atomic_json import replace_with_retry
    reports = reports.resolve()
    staging = reports / (".delete_" + uuid.uuid4().hex)
    staging.mkdir()
    moved = []
    try:
        files = sorted(plan["files"], key=lambda item: Path(item["name"]).name in {"workflow.json", f"coldstart_{run_id}_workflow.json"})
        for index, item in enumerate(files):
            source = reports / item["name"]
            destination = staging / str(index)
            replace_with_retry(source, destination)
            moved.append((source, destination))
    except OSError:
        for source, destination in reversed(moved):
            replace_with_retry(destination, source)
        staging.rmdir()
        raise
    for _, destination in moved:
        destination.unlink()
    staging.rmdir()
    # Remove only empty output directories, preserving referenced or unknown files.
    parents = {parent for source, _ in moved for parent in source.parents if parent != reports and reports in parent.parents}
    if plan["directory"]:
        directory = reports / plan["directory"]
        parents.update([directory, *(directory / name for name in ("stages", "process", "results"))])
    for parent in sorted(parents, key=lambda p: len(p.parts), reverse=True):
        try:
            parent.rmdir()
        except OSError:
            pass
    return {"run_id": run_id, "deleted": len(moved), "bytes": plan["bytes"], "retained": plan["retained"]}


class WorkflowInUseError(ValueError):
    pass


def ensure_workflow_not_in_use(reports, run_id, active_jobs):
    """Protect active outputs and their inputs, without locking unrelated versions."""
    workflow = load_workflow(reports, run_id)
    reports = Path(reports).resolve()
    manifest = find_manifest(reports, run_id)
    legacy = manifest.parent == reports
    owned = {path.resolve() for path in workflow_paths(reports, run_id, legacy=legacy).values()}
    directory = None if legacy else manifest.parent.resolve()
    for job in active_jobs:
        if job.status not in {'queued', 'running'}:
            continue
        reason = None
        if workflow.get('job_id') == job.job_id or getattr(job, 'workflow_run_id', None) == run_id:
            reason = '该版本正在生成或更新'
        inputs = []
        for index, value in enumerate(job.command):
            if value in {'--source', '--input'} and index + 1 < len(job.command):
                path = Path(job.command[index + 1])
                if not path.is_absolute():
                    path = Path(__file__).resolve().parents[2] / path
                inputs.append(path.resolve())
        if any(path in owned or directory is not None and (path == directory or directory in path.parents) for path in inputs):
            reason = '该版本的文件正在作为任务输入使用'
        # Older running workers may not have emitted a run identity yet.
        if not reason and not workflow.get('job_id') and not getattr(job, 'workflow_run_id', None) and workflow.get('status') == 'running':
            reason = '该版本仍标记为运行中，暂时无法确认其写入任务'
        if not reason and not inputs and not getattr(job, 'workflow_run_id', None):
            reason = '无法确定运行任务的输入及输出范围'
        if reason:
            raise WorkflowInUseError(f'任务 {job.job_id} ({job.job_type}) 正在运行：{reason}，完成后可删除')
