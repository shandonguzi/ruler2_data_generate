"""One catalog for user deliverables and saved workflow process files."""
import json
from pathlib import Path
from urllib.parse import urlencode

from intent_engine.workflows.workflow_artifacts import load_workflow, workflow_artifact
from intent_engine.workflows.workflow_layout import check_no_symlinks


def export_catalog(reports, run_id, artifact='report'):
    workflow = load_workflow(reports, run_id)
    snapshot = workflow_artifact(reports, run_id, artifact)
    if artifact not in {'report', 'extract', 'cluster', 'review', 'pairs', 'domains', 'iterate', 'export'}:
        raise ValueError('请选择 JSON 结果快照')
    data = json.loads(snapshot.read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('结果快照格式无效')
    artifacts = workflow.get('artifacts', {})

    def file_item(label, description, value):
        path = Path(value) if value else None
        available = False
        if path:
            try:
                check_no_symlinks(path, reports)
                available = path.is_file()
            except ValueError:
                pass
        return dict(label=label, description=description, filename=path.name if path else '',
                    available=available, reason='尚未生成或文件不存在',
                    url='/api/download?' + urlencode({'path': str(path)}) if available else None)

    results = [dict(kind='query_tasks', label='query 与原子意图（CSV）',
                    description='按所选快照：原始 query 每行一条，任务名称去重、换行拼接；无名称留空。',
                    filename='query_atomic_intents.csv',
                    available=any(a.get('status') == 'valid' for a in data.get('intents', [])),
                    reason='所选快照尚无有效原子意图')]
    for key, label in [('sentence_workbook', '原句与主题（Excel）'), ('sentence_results', '原句与主题（JSON）')]:
        results.append(file_item(label, '该流程保存的最终主题及层级路径。', artifacts.get(key)))

    # Older S4 runs stored their CSV under "workbook"; distinguish by the real file name.
    workbook = artifacts.get('workbook')
    is_s4 = bool(workbook and Path(workbook).name == 's4_mapping.csv')
    results.append(file_item('S4 场景与任务汇总（CSV）', '该流程已生成的 S4 结果，每个场景一行，任务列拼接任务名称。',
                             artifacts.get('s4_mapping') or (workbook if is_s4 else None)))
    details = artifacts.get('s4_details') or (str(Path(workbook).with_name('atomic_s4_details.csv')) if is_s4 else None)
    results.append(file_item('S4 关联明细（CSV）', '每条原子意图的场景选择与关联状态。', details))
    labels = dict(extract='原子意图分析', cluster='无监督聚类', review='簇一致性审核', pairs='跨簇边界复核',
                  domains='主题树审核', iterate='迭代结果', export='统计与导出快照', report='完整过程报告', workbook='过程审核工作簿')
    process = [file_item(label, '保存的过程文件，可用于审查或继续处理。', artifacts[key])
               for key, label in labels.items() if artifacts.get(key) and not (key == 'workbook' and
               (is_s4 or Path(artifacts[key]).name == 'query_atomic_intents.csv'))]
    return dict(run_id=run_id, artifact=artifact, results=results, process=process)
