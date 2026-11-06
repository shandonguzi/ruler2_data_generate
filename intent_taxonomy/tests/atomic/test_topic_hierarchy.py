import copy

import pytest

from intent_engine.atomic.topic_hierarchy import attach_topic_memberships, build_topic_tree, hierarchy_export_tables, index_topic_tree
from intent_engine.workflows.workflow_visuals import visualization_page


@pytest.fixture
def hierarchical_data():
    merges = [{'Parent_ID': 3, 'Parent_Name': '缴费_充值', 'Child_Left_ID': 1, 'Child_Right_ID': 2, 'Distance': .3},
              {'Parent_ID': 4, 'Parent_Name': '操作_请求', 'Child_Left_ID': 0, 'Child_Right_ID': 3, 'Distance': .8}]
    tree = build_topic_tree([0, 1, 2, 1, -1], {0: ['打开应用'], 1: ['交电费'], 2: ['充话费']}, reversed(merges))
    rows = [{'row_id': i, 'raw_text': text, 'status': 'valid'} for i, text in enumerate(
        ['打开应用，交电费', '充话费', '交电费', '未知请求', '取消一下'], 1)]
    atoms = []
    for row_id, text, topic, status in [(1, '打开应用', 0, 'valid'), (1, '交电费', 1, 'valid'), (2, '充话费', 2, 'valid'),
                                      (3, '交电费', 1, 'valid'), (4, '未知请求', -1, 'valid'), (5, '取消一下', None, 'ambiguous')]:
        atoms.append({'intent_id': f'r{row_id}:i{sum(a["row_id"] == row_id for a in atoms)}', 'row_id': row_id,
                      'raw_text': rows[row_id-1]['raw_text'], 'text': text, 'status': status, 'cluster_id': topic})
    data = {'topic_hierarchy': tree, 'rows': rows, 'intents': atoms, 'completed_stages': ['extract', 'cluster']}
    attach_topic_memberships(data)
    return data


def test_multi_intent_sentence_inherits_multiple_paths_and_deduplicates_ancestors(hierarchical_data):
    data = hierarchical_data
    nodes = {node['node_id']: node for node in data['topic_hierarchy']['nodes']}
    assert data['topic_hierarchy']['max_depth'] == 3
    assert data['intents'][0]['topic_path'] == ['merge:4', 'topic:0']
    assert data['intents'][1]['topic_path'] == ['merge:4', 'merge:3', 'topic:1']
    row = data['rows'][0]
    assert len(row['topic_paths']) == 2 and len(row['topic_memberships']) == 4
    assert row['topic_memberships'][0]['intent_ids'] == ['r1:i0', 'r1:i1']
    assert nodes['merge:4']['intent_count'] == 4
    assert nodes['merge:4']['unique_text_count'] == 3
    assert nodes['merge:4']['row_count'] == 3
    assert data['intents'][-1]['topic_path'] == data['intents'][-2]['topic_path'] == []
    assert data['rows'][-1]['topic_memberships'] == []
    attach_topic_memberships(data, validate_existing=True)


def test_parent_filter_uses_all_descendants_and_preserves_sentence_context(hierarchical_data):
    data = visualization_page(hierarchical_data, view='hierarchy', topic='merge:3', page_size=1)
    assert data['total'] == 3 and data['pages'] == 3
    assert len(data['records'][0]['intents']) == 2
    assert visualization_page(hierarchical_data, view='clusters', topic='merge:3')['total'] == 3
    assert visualization_page(hierarchical_data, view='hierarchy', topic='topic:0')['total'] == 1
    assert visualization_page(hierarchical_data, view='hierarchy', topic='merge:3', query='不存在')['total'] == 0
    with pytest.raises(ValueError, match='不存在'):
        visualization_page(hierarchical_data, view='hierarchy', topic='merge:999')


def test_export_keeps_every_intent_level_and_sentence_origins(hierarchical_data):
    tables = hierarchy_export_tables(hierarchical_data)
    assert len(tables['主题层级']) == 5
    assert [r['level'] for r in tables['意图主题路径'] if r['intent_id'] == 'r1:i1'] == [1, 2, 3]
    root = next(row for row in tables['原句多层主题'] if row['row_id'] == 1 and row['node_id'] == 'merge:4')
    assert root['intent_ids'] == ['r1:i0', 'r1:i1']


def test_invalid_memberships_and_graphs_are_rejected(hierarchical_data):
    data = copy.deepcopy(hierarchical_data)
    data['intents'][0]['topic_path'] = ['topic:1']
    with pytest.raises(ValueError, match='路径'):
        attach_topic_memberships(data, validate_existing=True)
    tree = copy.deepcopy(hierarchical_data['topic_hierarchy'])
    next(node for node in tree['nodes'] if node['node_id'] == 'merge:4')['children'] = ['merge:3', 'merge:3']
    with pytest.raises(ValueError):
        index_topic_tree(tree)
    with pytest.raises(ValueError, match='不一致'):
        index_topic_tree(hierarchical_data['topic_hierarchy'], {0, 1})


def test_zero_single_topic_and_legacy_results_have_no_invented_parents():
    assert build_topic_tree([-1], {})['root_ids'] == []
    tree = build_topic_tree([0, 0, -1], {0: ['唯一主题']})
    assert tree['status'] == 'single_topic' and tree['max_depth'] == 1
    legacy = visualization_page({'rows': [], 'intents': []}, view='hierarchy')
    assert legacy['hierarchy']['status'] == 'not_generated'


def test_unbalanced_hierarchy_does_not_require_python_recursion():
    size = 1050
    merges = []
    previous = 0
    for index in range(1, size):
        parent = size + index - 1
        merges.append({'Parent_ID': parent, 'Child_Left_ID': previous, 'Child_Right_ID': index, 'Distance': float(index)})
        previous = parent
    tree = build_topic_tree(range(size), {}, merges)
    assert tree['max_depth'] == size
