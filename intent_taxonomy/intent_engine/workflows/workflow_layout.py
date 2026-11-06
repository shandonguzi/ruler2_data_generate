"""Workflow output layout, including read-only compatibility with flat reports."""
import re
from pathlib import Path

STAGES = ('extract', 'cluster', 'review', 'pairs', 'domains', 'iterate', 'export')


def validate_run_id(run_id):
    if not isinstance(run_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', run_id):
        raise ValueError('流程编号无效')


def workflow_paths(reports, run_id, *, legacy=False):
    validate_run_id(run_id)
    reports = Path(reports)
    if legacy:
        prefix = reports / f'coldstart_{run_id}'
        suffixes = {**{key:key+'.json' for key in STAGES}, 'report':'discovery.json',
                    'manifest':'workflow.json', 'workbook':'review.xlsx',
                    'sentence_results':'sentence_results.json', 'sentence_workbook':'sentence_results.xlsx'}
        return {key:prefix.with_name(prefix.name+'_'+suffix) for key,suffix in suffixes.items()}
    directory = reports / run_id
    return {**{key:directory/'stages'/(key+'.json') for key in STAGES},
            'manifest':directory/'workflow.json', 'report':directory/'process'/'discovery.json',
            'workbook':directory/'process'/'review.xlsx',
            'sentence_results':directory/'results'/'sentence_results.json',
            'sentence_workbook':directory/'results'/'sentence_results.xlsx'}


def create_workflow_directory(reports, run_id):
    paths = workflow_paths(reports, run_id)
    directory = paths['manifest'].parent
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.mkdir()  # Never reuse or overwrite another run's directory.
    for name in ('stages','process','results'):
        (directory/name).mkdir()
    return paths


def check_no_symlinks(path, reports):
    path, reports = Path(path).absolute(), Path(reports).absolute()
    if path != reports and reports not in path.parents:
        raise ValueError('文件不属于报告目录')
    current = path
    while current != reports:
        if current.is_symlink():
            raise ValueError('流程记录或产物是符号链接，无法安全处理')
        current = current.parent


def manifest_paths(reports):
    reports = Path(reports)
    return [*reports.glob('coldstart_*_workflow.json'), *reports.glob('*/workflow.json')]


def valid_manifest_location(path, reports, run_id):
    validate_run_id(run_id)
    return Path(path) in (workflow_paths(reports,run_id)['manifest'], workflow_paths(reports,run_id,legacy=True)['manifest'])


def find_manifest(reports, run_id):
    choices = [workflow_paths(reports,run_id)['manifest'], workflow_paths(reports,run_id,legacy=True)['manifest']]
    for path in choices:
        check_no_symlinks(path,reports)
    existing = [path for path in choices if path.exists()]
    if len(existing) > 1:
        raise ValueError('存在重复流程编号，请检查报告目录')
    return existing[0] if existing else choices[0]
