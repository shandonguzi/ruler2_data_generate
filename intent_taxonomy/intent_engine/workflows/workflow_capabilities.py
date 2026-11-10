"""Inspect real snapshots once per file revision, sharing execution validation."""
import copy
import json
import hashlib
import time
from functools import lru_cache
from pathlib import Path

from intent_engine.atomic.atomic_discovery import WORKFLOW_STAGES, validate_stage_source
from intent_engine.workflows.workflow_layout import check_no_symlinks


@lru_cache(maxsize=256)
def _inspect(path, revision):
    try:
        data = json.loads(Path(path).read_text(encoding='utf-8'))
    except (ValueError, OSError) as exc:
        return {stage: {'available': False, 'reason': f'文件无法读取：{exc}'} for stage in WORKFLOW_STAGES}
    results = {}
    for stage in WORKFLOW_STAGES:
        try:
            validate_stage_source(copy.deepcopy(data), stage)
            results[stage] = {'available': True, 'reason': ''}
        except (ValueError, OSError, KeyError, TypeError, IndexError, AttributeError) as exc:
            results[stage] = {'available': False, 'reason': str(exc) or '中间结果结构不完整'}
    return results


@lru_cache(maxsize=1)
def _validator_version():
    digest=hashlib.sha256(b'workflow-capabilities-v1')
    for path in sorted(Path(__file__).resolve().parents[1].rglob('*.py')):
        digest.update(str(path.relative_to(Path(__file__).resolve().parents[1])).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _persistent_inspect(path, revision, cache_dir):
    started=time.perf_counter()
    identity={'path':str(path),'revision':list(revision),'validator':_validator_version()}
    key=hashlib.sha256(str(path).encode()).hexdigest()
    target=cache_dir/(key+'.json')
    try:
        saved=json.loads(target.read_text(encoding='utf-8'))
        result=saved['result']
        if saved.get('identity')==identity and isinstance(result,dict) and set(result)==set(WORKFLOW_STAGES) and all(
            isinstance(v,dict) and type(v.get('available')) is bool and isinstance(v.get('reason'),str)
            for v in result.values()):
            print(f'[流程校验耗时] {path} | 本地缓存命中 | {time.perf_counter()-started:.4f}s',flush=True)
            return result
    except (OSError,ValueError,KeyError,TypeError):
        pass
    result=_inspect(str(path),revision)
    # Do not publish a result under a revision that changed while being inspected.
    stat=path.stat()
    current=(stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
    if current==revision:
        try:
            from intent_engine.infrastructure.atomic_json import write_json_atomically
            cache_dir.mkdir(parents=True,exist_ok=True)
            write_json_atomically(target,{'identity':identity,'result':result})
        except OSError:
            pass  # A read-only cache must never prevent execution or listing.
    print(f'[流程校验耗时] {path} | 校验完成 | {time.perf_counter()-started:.4f}s',flush=True)
    return result


def workflow_capabilities(reports, workflow):
    reports = Path(reports).resolve()
    artifacts = {}
    for key, value in workflow.get('artifacts', {}).items():
        if key not in {*WORKFLOW_STAGES, 'report'}:
            continue
        try:
            path = Path(value).absolute()
            check_no_symlinks(path, reports)
            stat = path.stat()
            if not path.is_file():
                raise ValueError('中间结果不是普通文件')
            revision = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            artifacts[key] = _persistent_inspect(path, revision, reports.parent/'cache'/'workflow_capabilities')
        except (ValueError, OSError, TypeError) as exc:
            artifacts[key] = {stage: {'available': False, 'reason': str(exc)} for stage in WORKFLOW_STAGES}
    capabilities = {}
    for stage in WORKFLOW_STAGES:
        sources = [key for key, statuses in artifacts.items() if statuses[stage]['available']]
        reasons = list(dict.fromkeys(statuses[stage]['reason'] for statuses in artifacts.values() if not statuses[stage]['available']))
        capabilities[stage] = {'available': bool(sources), 'sources': sources,
                               'reason': '' if sources else '；'.join(reasons[:3]) or '没有可读取的流程快照'}
    return {'capabilities': capabilities, 'artifact_capabilities': artifacts}
