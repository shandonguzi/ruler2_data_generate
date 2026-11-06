import json

from intent_engine.exporting.catalog import export_catalog


def test_catalog_separates_deliverables_and_process_with_legacy_s4(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    snapshot = run / 'report.json'
    snapshot.write_text(json.dumps({'rows': [], 'intents': [{'status': 'valid'}]}))
    s4 = run / 's4_mapping.csv'
    s4.write_text('S4,任务\n')
    detail = run / 'atomic_s4_details.csv'
    detail.write_text('intent_id\n')
    (run / 'workflow.json').write_text(json.dumps({'run_id': 'run', 'artifacts': {
        'report': str(snapshot), 'extract': str(snapshot), 'workbook': str(s4),
        'sentence_workbook': str(run / 'missing.xlsx')}}))
    result = export_catalog(tmp_path, 'run', 'extract')
    assert result['results'][0]['available']
    assert not result['results'][1]['available']
    assert result['results'][3]['filename'] == 's4_mapping.csv'
    assert result['results'][3]['available'] and result['results'][4]['available']
    assert [i['label'] for i in result['process']] == ['原子意图分析', '完整过程报告']
    # Changing snapshot affects the dynamic query export, not saved final files.
    snapshot.write_text(json.dumps({'intents': []}))
    assert not export_catalog(tmp_path, 'run', 'extract')['results'][0]['available']


def test_catalog_never_offers_outside_files(tmp_path):
    run = tmp_path / 'run'
    run.mkdir()
    snapshot = run / 'report.json'
    snapshot.write_text('{}')
    (run / 'workflow.json').write_text(json.dumps({'run_id': 'run', 'artifacts': {
        'report': str(snapshot), 'sentence_results': '/etc/hosts'}}))
    item = export_catalog(tmp_path, 'run')['results'][2]
    assert not item['available'] and item['url'] is None
