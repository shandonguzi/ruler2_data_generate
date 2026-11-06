import json

from intent_engine.web.app import read_request_records


def test_request_review_filters_user_payload_and_pages(tmp_path):
    path = tmp_path / 'requests.jsonl'
    records = [
        {'request_id': str(i), 'stage_id': 'review', 'input': {'messages': [
            {'role': 'system', 'content': 'unrelated'},
            {'role': 'user', 'content': json.dumps({'members': [{'intent_id': 'r42:i0', 'text': 'pay bill'}]})},
        ]}} for i in range(55)
    ]
    path.write_text('\n'.join(json.dumps(r) for r in records) + '\ninvalid partial', encoding='utf-8')
    assert read_request_records(path, terms=['r42:i0'])['total'] == 55
    assert len(read_request_records(path, terms=['pay bill'], offset=50)['records']) == 5
    assert read_request_records(path, terms=['unrelated'])['total'] == 0
    assert read_request_records(path, stage='extract')['total'] == 0
    assert read_request_records(tmp_path / 'absent')['records'] == []


def test_request_review_newest_first_across_pages(tmp_path):
    path = tmp_path / 'requests.jsonl'
    # Deliberately reverse completion order to represent parallel requests.
    records = [{'request_id': str(i), 'started_at': i} for i in range(54, -1, -1)]
    path.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
    first = read_request_records(path)
    second = read_request_records(path, offset=50)
    assert [r['request_id'] for r in first['records']] == [str(i) for i in range(54, 44, -1)]
    assert [r['request_id'] for r in second['records']] == ['4', '3', '2', '1', '0']
    assert first['total'] == second['total'] == 55


def test_request_review_old_records_reverse_file_order(tmp_path):
    path = tmp_path / 'requests.jsonl'
    path.write_text('\n'.join(json.dumps({'request_id': str(i)}) for i in range(3)), encoding='utf-8')
    assert [r['request_id'] for r in read_request_records(path)['records']] == ['2', '1', '0']


def test_embedding_input_is_recorded_and_can_be_reviewed(tmp_path, monkeypatch, capsys):
    from intent_engine.infrastructure.model_requests import tracked_request
    monkeypatch.setenv('INTENT_PROGRESS', '1')
    response = {'data': [{'embedding': [0.1, 0.2]}], 'usage': {'prompt_tokens': 3, 'total_tokens': 3}}
    tracked_request('embedding', {'model': 'test'}, lambda **kw: response, model='test', input=['pay bill'])
    lines = capsys.readouterr().out.splitlines()
    trace = json.loads(next(line.removeprefix('@@INTENT_TRACE@@') for line in lines if line.startswith('@@INTENT_TRACE@@')))
    assert trace['input']['input'] == ['pay bill']
    assert trace['output']['dimensions'] == [2]
    assert trace['status'] == 'success'
    assert 'embedding' not in trace['output']
    path = tmp_path / 'requests.jsonl'
    path.write_text(json.dumps(trace), encoding='utf-8')
    assert read_request_records(path, terms=['pay bill'])['total'] == 1


def test_cluster_request_tree_preserves_ancestry_and_stage_scope(tmp_path):
    def record(stage, cluster, parent, when):
        return {'stage_id': stage, 'started_at': when, 'request_id': str(when), 'input': {'messages': [
            {'role': 'user', 'content': json.dumps({'cluster_id': cluster, 'parent_cluster_id': parent, 'members': []})}]}}
    path = tmp_path / 'requests.jsonl'
    records = [record('review', 1, None, 1), record('review', 2, 1, 2), record('review', 3, 2, 3),
               record('review', 2, 1, 4), record('round_1:review', 2, None, 5)]
    path.write_text('\n'.join(json.dumps(r) for r in records), encoding='utf-8')
    result = read_request_records(path, view='tree')
    assert result['total'] == 2
    assert result['nodes'][0]['stage_id'] == 'round_1:review'
    root = result['nodes'][1]
    assert root['cluster_id'] == 1
    assert root['children'][0]['request_count'] == 2
    assert root['children'][0]['children'][0]['cluster_id'] == 3
    assert len(read_request_records(path, cluster=2, stage='review')['records']) == 2


def test_tree_missing_parent_unlinked_and_root_pagination():
    from intent_engine.infrastructure.request_trace_tree import cluster_tree
    records = [{'input': {'messages': [{'role': 'user', 'content': json.dumps(
        {'cluster_id': i, 'parent_cluster_id': 100+i, 'members': []})}]}, 'started_at': i} for i in range(12)]
    records.append({'input': {}})
    first = cluster_tree(records)
    assert first['total'] == 12
    assert len(first['nodes']) == 10
    assert first['unlinked'] == 1
    assert first['nodes'][0]['cluster_id'] == 111
    assert first['nodes'][0]['request_count'] == 0
    assert len(cluster_tree(records, 10)['nodes']) == 2


def test_index_prepares_metadata_once_and_reads_only_current_page(tmp_path, monkeypatch):
    from intent_engine.infrastructure import request_trace_store as store
    path = tmp_path / 'requests.jsonl'
    path.write_text(''.join(json.dumps({'request_id': str(i), 'input': {'input': ['hello']}}) + '\n' for i in range(30)))
    store.prepare_request_index(path)
    assert path.with_name('requests.jsonl.sqlite3').exists()
    def unexpected(*args, **kwargs):
        raise AssertionError('Existing payloads must not be reparsed for indexing')
    monkeypatch.setattr(store, '_values', unexpected)
    monkeypatch.setattr(store, 'cluster_context', unexpected)
    assert read_request_records(path, terms=['hello'], offset=10)['records'][0]['request_id'] == '19'
    assert read_request_records(path)['total'] == 30


def test_index_incremental_append_partial_tail_and_replacement(tmp_path):
    from intent_engine.infrastructure.request_trace_store import prepare_request_index
    path = tmp_path / 'requests.jsonl'
    path.write_text('{"request_id":"a"}')
    prepare_request_index(path)
    with path.open('a') as stream:
        stream.write('\n{"request_id":"b"')
    assert read_request_records(path)['total'] == 1
    with path.open('a') as stream:
        stream.write('}\n')
    assert [r['request_id'] for r in read_request_records(path)['records']] == ['b', 'a']
    replacement = tmp_path / 'new.jsonl'
    replacement.write_text('{"request_id":"replacement"}\n')
    replacement.replace(path)
    assert read_request_records(path)['records'] == [{'request_id': 'replacement'}]
    path.write_text('{"request_id":"c"}\n')
    assert read_request_records(path)['records'] == [{'request_id': 'c'}]


def test_tree_cache_persists_and_is_invalidated_on_append(tmp_path, monkeypatch):
    from intent_engine.infrastructure import request_trace_store as store
    path = tmp_path / 'requests.jsonl'
    def record(cluster):
        return json.dumps({'input': {'messages': [{'role': 'user', 'content': json.dumps({'members': [], 'cluster_id': cluster})}]}}) + '\n'
    path.write_text(record(1))
    assert read_request_records(path, view='tree')['total'] == 1
    original = store.cluster_tree
    def unexpected(*args, **kwargs):
        raise AssertionError('Unchanged tree should come from persistent cache')
    monkeypatch.setattr(store, 'cluster_tree', unexpected)
    assert read_request_records(path, view='tree')['total'] == 1
    monkeypatch.setattr(store, 'cluster_tree', original)
    with path.open('a') as stream:
        stream.write(record(2))
    assert read_request_records(path, view='tree')['total'] == 2
