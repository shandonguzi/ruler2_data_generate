"""Read-only query/task export shared by HTTP, CLI and S4 workflows."""
import csv
import io

def query_task_csv(records, source_rows=None):
    # Source row identity, not query text, preserves the original input table.
    groups={}
    def key(record):
        return ('row',record['row_id']) if record.get('row_id') is not None else ('text',record.get('raw_text',''))
    if source_rows is not None:
        for row in source_rows:
            groups[key(row)]={'query':row.get('raw_text',''),'names':[],'seen':set()}
    for record in records:
        identity=key(record)
        if source_rows is None:
            groups.setdefault(identity,{'query':record.get('raw_text',''),'names':[],'seen':set()})
        group=groups.get(identity)
        if group is None or group['query']!=record.get('raw_text',''):continue
        title=(record.get('task_name') or '').strip()
        if record.get('status')=='matched' and title and title not in group['seen']:
            group['seen'].add(title);group['names'].append(title)
    stream=io.StringIO(newline='');writer=csv.DictWriter(stream,fieldnames=['query','原子意图']);writer.writeheader()
    for group in groups.values():writer.writerow({'query':group['query'],'原子意图':'\n'.join(group['names'])})
    return stream.getvalue()


def saved_task_records(source):
    from intent_engine.atomic.atomic_discovery import review_eligible
    from intent_engine.workflows.atomic_tasks import TaskName
    saved={r.get('intent_id'):r for r in source.get('atomic_task_names',{}).get('records',[]) if r.get('intent_id')}
    assignments={r.get('intent_id'):r for r in source.get('iteration',{}).get('assignments',[]) if r.get('intent_id')}
    nodes={n['node_id']:n for n in source.get('topic_hierarchy',{}).get('nodes',[])}
    tasks={t['task_id'] for t in source.get('candidate_taxonomy',{}).get('tasks',[])}
    clusters={c['cluster_id']:c for c in source.get('clusters',[])}
    version=source.get('candidate_taxonomy',{}).get('version')
    def leaf_name(task_id):
        node=nodes.get(source.get('task_topic_nodes',{}).get(task_id),{})
        if task_id not in tasks or not node.get('is_leaf') or node.get('semantic_status')!='approved':return ''
        return node.get('label','')
    def same_origin(record,atom):
        return all(record.get(k)==atom.get(k) for k in ('row_id','raw_text','text'))
    records=[]
    for atom in source.get('intents',[]):
        if atom.get('status')!='valid':continue
        record={k:atom.get(k) for k in ('intent_id','row_id','raw_text','text')}
        title=atom.get('task_name');name_source='saved_task_name'
        previous=saved.get(atom.get('intent_id'))
        if not title and previous and previous.get('status')=='matched' and same_origin(previous,atom):
            title=previous.get('task_name')
        assignment=assignments.get(atom.get('intent_id'))
        if not title and assignment is not None:
            # Explicit unresolved matches must not inherit a label from mere membership.
            if (assignment.get('status')=='known' and not assignment.get('error') and same_origin(assignment,atom)
                    and assignment.get('taxonomy_version',version)==version):
                title=leaf_name(assignment.get('task_id'));name_source='matched_leaf'
        elif not title:
            cluster=clusters.get(atom.get('cluster_id'))
            if cluster and review_eligible(cluster) and not atom.get('topic_pending'):
                title=leaf_name(source.get('cluster_task_ids',{}).get(str(atom.get('cluster_id'))))
                name_source='audited_cluster_leaf'
        try:title=TaskName(task_name=title).task_name
        except (ValueError,TypeError):title=''
        record.update(task_name=title,status='matched' if title else 'missing_name',name_source=name_source if title else None)
        records.append(record)
    return records

