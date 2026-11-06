import json
from pathlib import Path

from openpyxl import load_workbook

from intent_engine.exporting.sentence_results import build_sentence_results, sentence_result_tables, write_sentence_results
from intent_engine.atomic.topic_hierarchy import attach_topic_memberships, build_topic_tree


def source():
    raw = '=打开支付宝，交电费'
    rows = [{'row_id': 1, 'raw_text': raw, 'status': 'valid', 'relations': [{'source': 0, 'target': 1, 'kind': 'before'}]},
            {'row_id': 2, 'raw_text': raw, 'status': 'valid'},
            {'row_id': 3, 'raw_text': '取消一下', 'status': 'ambiguous'},
            {'row_id': 4, 'raw_text': '模型失败', 'status': 'processing_error', 'reason': 'unavailable'},
            {'row_id': 5, 'raw_text': '未知动作', 'status': 'valid'}]
    atoms = [{'intent_id': 'r1:i0', 'row_id': 1, 'raw_text': raw, 'text': '打开支付宝', 'span': '打开支付宝', 'status': 'valid', 'cluster_id': 0},
             {'intent_id': 'r1:i1', 'row_id': 1, 'raw_text': raw, 'text': '交电费', 'span': '交电费', 'status': 'valid', 'cluster_id': 1},
             {'intent_id': 'r2:i0', 'row_id': 2, 'raw_text': raw, 'text': '交电费', 'span': '交电费', 'status': 'valid', 'cluster_id': 1},
             {'intent_id': 'r3:i0', 'row_id': 3, 'raw_text': '取消一下', 'text': '取消一下', 'status': 'ambiguous', 'cluster_id': None},
             {'intent_id': 'r5:i0', 'row_id': 5, 'raw_text': '未知动作', 'text': '未知动作', 'status': 'valid', 'cluster_id': -1}]
    tree = build_topic_tree([0, 1, -1], {0: ['打开应用'], 1: ['交电费']},
        [{'Parent_ID': 2, 'Parent_Name': '请求', 'Child_Left_ID': 0, 'Child_Right_ID': 1, 'Distance': .5}])
    assignments = [{'intent_id': 'r1:i0', 'domain': '应用', 'object': '应用', 'action': '打开', 'slots': {'应用名': '支付宝'},
                    'taxonomy_version': 3, 'task_id': 'open', 'status': 'known'},
                   {'intent_id': 'r1:i1', 'domain': '生活缴费', 'object': '电费', 'action': '缴纳', 'slots': {'金额': '50元'},
                    'taxonomy_version': 3, 'task_id': 'pay', 'status': 'known'}]
    data = {'run_id': 'sample', 'status': 'completed_with_errors', 'rows': rows, 'intents': atoms,
            'topic_hierarchy': tree, 'candidate_taxonomy': {'version': 3, 'tasks': [{'task_id': 'pay', 'domain': '生活缴费', 'object': '电费', 'action': '缴纳'}]},
            'cluster_task_ids': {'1': 'pay'}, 'iteration': {'validated_taxonomy_version': 3, 'assignments': assignments}}
    attach_topic_memberships(data)
    return data


def test_final_records_contain_only_original_sentence_and_topic_levels():
    import copy
    data = source()
    before = copy.deepcopy(data)
    output = build_sentence_results(data)
    assert set(output) == {'records'}
    assert len(output['records']) == 5
    assert all(set(record) == {'raw_text', 'topics_by_level'} for record in output['records'])
    first = output['records'][0]
    assert first['raw_text'] == data['rows'][0]['raw_text'] == output['records'][1]['raw_text']
    assert first['topics_by_level'] == [{'level': 1, 'topics': ['请求']},
                                      {'level': 2, 'topics': ['打开应用', '交电费']}]
    assert all(not level['topics'] for record in output['records'][2:] for level in record['topics_by_level'])
    assert data == before  # Process records and provenance stay intact.


def test_excel_exposes_only_raw_sentence_and_per_level_themes():
    tables = sentence_result_tables(build_sentence_results(source()))
    assert set(tables) == {'原句结果'}
    row = tables['原句结果'][0]
    assert list(row) == ['原始句子', '第1层主题', '第2层主题']
    assert row['第1层主题'] == '请求'
    assert row['第2层主题'] == '打开应用\n交电费'
    assert all('r1:i' not in str(value) and 'merge:' not in str(value) for value in row.values())


def test_old_flat_results_do_not_invent_parents_or_export_semantic_details():
    data = source()
    data.pop('topic_hierarchy')
    result = build_sentence_results(data)
    assert len(result['records'][0]['topics_by_level']) == 1
    assert set(result['records'][0]['topics_by_level'][0]) == {'level', 'topics'}
    assert set(sentence_result_tables(result)['原句结果'][0]) == {'原始句子', '第1层主题'}


def test_old_delivery_preview_strips_process_details_and_topic_provenance():
    old = {'run_id': 'old', 'records': [{'raw_text': '原句', 'intents': [{'slots': {'金额': 50}}],
           'topics_by_level': [{'level': 1, 'topics': [{'node_id': 'merge:2', 'label': '请求', 'intent_ids': ['r1:i0']}]}]}]}
    assert sentence_result_tables(old) == {'原句结果': [{'原始句子': '原句', '第1层主题': '请求'}]}


def test_export_json_and_xlsx_preserve_formula_like_raw_sentences(tmp_path):
    artifacts = write_sentence_results(tmp_path / 'coldstart_sample', source())
    data = json.loads(Path(artifacts['sentence_results']).read_text())
    book = load_workbook(artifacts['sentence_workbook'])
    sheet = book['原句结果']
    assert sheet.max_row == 6 and sheet.max_column == 3
    assert sheet['A2'].value == data['records'][0]['raw_text']
    assert sheet['A2'].data_type == 's'
    assert sheet.freeze_panes == 'B2'
    assert book.sheetnames == ['原句结果']
    assert set(data) == {'records'}


def test_business_outputs_are_scoped_and_included_in_workflow_cleanup(tmp_path, monkeypatch):
    import web_server as web
    from intent_engine.workflows.workflow_cleanup import workflow_delete_plan, delete_workflow
    artifacts = write_sentence_results(tmp_path / 'coldstart_sample', source())
    manifest = tmp_path / 'coldstart_sample_workflow.json'
    manifest.write_text(json.dumps({'run_id': 'sample', 'artifacts': artifacts}))
    monkeypatch.setattr(web, '_reports_dir', lambda: tmp_path)
    monkeypatch.setattr(web, 'ROOT', tmp_path)
    preview = web.api_workflow_artifact('sample', 'sentence_results')
    assert len(preview['tables']['原句最终结果']) == 5
    assert set(preview['tables']['原句最终结果'][0]) == {'原始句子', '第1层主题', '第2层主题'}
    plan = workflow_delete_plan(tmp_path, 'sample')
    assert len(plan['files']) == 3
    delete_workflow(tmp_path, 'sample', plan['revision'])
    assert all(not Path(path).exists() for path in artifacts.values())
