"""Explicit cluster relationships for request inspection (never inferred from text)."""
import json


def cluster_context(record):
    for message in record.get('input', {}).get('messages', []):
        if message.get('role') != 'user':
            continue
        try:
            payload = json.loads(message.get('content', ''))
        except (ValueError, TypeError):
            continue
        if isinstance(payload, dict) and 'members' in payload and isinstance(payload.get('cluster_id'), int):
            return {'cluster_id': payload['cluster_id'], 'parent_cluster_id': payload.get('parent_cluster_id'),
                    'stage_id': record.get('stage_id', '__unassigned__'),
                    'round': payload.get('refinement_round', 0)}
    return None


def cluster_tree(records, offset=0, *, precomputed=False, page_size=10):
    nodes = {}
    unlinked = 0
    for record in records:
        context = record.get("_cluster_context") if precomputed else cluster_context(record)
        if context is None:
            unlinked += 1
            continue
        stage, cluster = context['stage_id'], context['cluster_id']
        key = (stage, cluster)
        node = nodes.setdefault(key, {**context, 'request_count': 0, 'children': [], 'latest': 0})
        node['request_count'] += 1
        node['latest'] = max(node['latest'], record.get('started_at', 0) or 0)
        if context['parent_cluster_id'] is not None:
            node['parent_cluster_id'] = context['parent_cluster_id']
    # Preserve missing ancestors as context-only nodes, without inventing requests.
    for node in list(nodes.values()):
        parent = node['parent_cluster_id']
        if isinstance(parent, int):
            nodes.setdefault((node['stage_id'], parent), {'cluster_id': parent, 'stage_id': node['stage_id'],
                             'parent_cluster_id': None, 'round': 0, 'request_count': 0,
                             'children': [], 'latest': 0})
    roots = []
    for key, node in nodes.items():
        parent_key = (node['stage_id'], node['parent_cluster_id'])
        seen = {key}
        cursor = parent_key
        while cursor in nodes and cursor not in seen:
            seen.add(cursor)
            cursor = (cursor[0], nodes[cursor]['parent_cluster_id'])
        if parent_key in nodes and cursor not in seen:
            nodes[parent_key]['children'].append(node)
        else:
            roots.append(node)
    def order(node):
        for child in node['children']:
            order(child)
            node['latest'] = max(node['latest'], child['latest'])
        node['children'].sort(key=lambda n: n['latest'], reverse=True)
    for root in roots:
        order(root)
    roots.sort(key=lambda n: n['latest'], reverse=True)
    start = max(0, offset)
    return {'total': len(roots), 'request_total': len(records), 'unlinked': unlinked,
            'nodes': roots[start:] if page_size is None else roots[start:start + page_size], 'records': [], 'view': 'tree'}
