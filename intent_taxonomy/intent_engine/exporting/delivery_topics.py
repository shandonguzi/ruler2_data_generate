"""Validated projection onto the reviewed BERTopic hierarchy only."""
from collections import Counter


def hierarchy_projection(data):
    if data.get('topic_hierarchy', {}).get('source') != 'BERTopic+LLM':
        return None
    taxonomy = data.get('candidate_taxonomy') or {}
    tasks = {task['task_id']:task for task in taxonomy.get('tasks', [])}
    if not tasks:
        return {'source':'reviewed_bertopic','assignments':{},'levels':[]}
    from intent_engine.atomic.atomic_discovery import review_eligible
    from intent_engine.workflows.candidate_iteration import fingerprint
    from intent_engine.models import Taxonomy
    parsed = Taxonomy.model_validate(taxonomy)
    digest = fingerprint(parsed)
    iteration = data.get('iteration') or {}
    validated = iteration.get('validated_taxonomy_version') == parsed.version
    atoms = {atom['intent_id']:atom for atom in data.get('intents', []) if atom.get('status') == 'valid'}
    assignments = {}
    if validated:
        # Duplicates, stale versions and changed input are never accepted as final classifications.
        records = iteration.get('assignments', [])
        counts = Counter(item['intent_id'] for item in records)
        for record in records:
            atom = atoms.get(record['intent_id'])
            if (not atom or counts[record['intent_id']] != 1 or record.get('status') != 'known' or record.get('error')
                    or record.get('taxonomy_version') != parsed.version or record.get('taxonomy_fingerprint') != digest
                    or record.get('text') != atom['text'] or record.get('row_id') != atom['row_id']
                    or record.get('raw_text') != atom.get('raw_text') or record.get('span') != atom.get('span')):
                continue
            if record.get('task_id') in tasks:
                assignments[atom['intent_id']] = (record['task_id'], 'audited_cluster' if record.get('assignment_basis')=='audited_cluster' else 'validated_classification')
    elif not iteration.get('rounds'):
        mapping = data.get('cluster_task_ids') or {}
        clusters = {cluster['cluster_id']:cluster for cluster in data.get('clusters', [])}
        for atom in atoms.values():
            cluster = clusters.get(atom.get('cluster_id'))
            task_id = mapping.get(str(atom.get('cluster_id')))
            if cluster and review_eligible(cluster) and task_id in tasks:
                assignments[atom['intent_id']] = (task_id, 'audited_cluster')
    from intent_engine.atomic.topic_hierarchy import index_topic_tree
    nodes, paths = index_topic_tree(data['topic_hierarchy'])
    projected = {}
    for intent_id, (task_id, evidence_source) in assignments.items():
        leaf = nodes.get(data.get('task_topic_nodes', {}).get(task_id))
        if not leaf or not leaf['is_leaf'] or leaf.get('semantic_status') != 'approved':
            continue
        projected[intent_id] = {'task_id':task_id, 'source':evidence_source, 'path':[
            {'node_id':key,'label':nodes[key]['label']} for key in paths[leaf['topic_id']]]}
    return {'source':'reviewed_bertopic', 'taxonomy_version':parsed.version,
            'taxonomy_fingerprint':digest, 'assignments':projected,
            'levels':['审核后的 BERTopic 主题路径']}


def prepare_delivery_topics(data):
    from intent_engine.exporting.sentence_results import build_sentence_results
    if 'rows' not in data or any('row_id' not in atom for atom in data.get('intents', [])):
        data['delivery_topics'] = {'source':'unknown', 'coverage':None, 'assignments':{},
                                   'warnings':['历史快照缺少原句关联，无法计算最终覆盖率']}
        return data['delivery_topics']
    projection = hierarchy_projection(data)
    final = build_sentence_results(data)
    rows = data.get('rows', [])
    covered = sum(any(level['topics'] for level in record['topics_by_level']) for record in final['records'])
    atoms = data.get('intents', [])
    coverage = {'rows':len(rows), 'covered_rows':covered, 'uncovered_rows':len(rows)-covered,
                'coverage_rate':round(covered/len(rows),4) if rows else 0,
                'invalid_rows':sum(row.get('status')=='invalid' for row in rows),
                'processing_error_rows':sum(row.get('status')=='processing_error' for row in rows),
                'pending_intents':sum(bool(atom.get('topic_pending')) for atom in atoms),
                'noise_intents':sum(atom.get('cluster_id')==-1 for atom in atoms),
                'semantic_assigned_intents':len(projection['assignments']) if projection else 0}
    warnings = []
    if coverage['invalid_rows']:
        warnings.append(f"{coverage['invalid_rows']} 条原句被判无效，请检查知识问答是否被误排除")
    if coverage['uncovered_rows']:
        warnings.append(f"{coverage['uncovered_rows']} / {len(rows)} 条原句尚无最终主题；流程完成不代表全量覆盖")
    source = projection or {'source':'bertopic', 'assignments':{}}
    data['delivery_topics'] = {**source, 'coverage':coverage, 'warnings':warnings}
    return data['delivery_topics']
