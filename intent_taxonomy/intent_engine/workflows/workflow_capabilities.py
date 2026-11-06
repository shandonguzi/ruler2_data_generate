"""Inspect real snapshots once per file revision, sharing execution validation."""
import copy
import json
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
            artifacts[key] = _inspect(str(path), revision)
        except (ValueError, OSError, TypeError) as exc:
            artifacts[key] = {stage: {'available': False, 'reason': str(exc)} for stage in WORKFLOW_STAGES}
    capabilities = {}
    for stage in WORKFLOW_STAGES:
        sources = [key for key, statuses in artifacts.items() if statuses[stage]['available']]
        reasons = list(dict.fromkeys(statuses[stage]['reason'] for statuses in artifacts.values() if not statuses[stage]['available']))
        capabilities[stage] = {'available': bool(sources), 'sources': sources,
                               'reason': '' if sources else '；'.join(reasons[:3]) or '没有可读取的流程快照'}
    return {'capabilities': capabilities, 'artifact_capabilities': artifacts}
