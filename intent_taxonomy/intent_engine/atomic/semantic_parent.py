"""Apply independently verified common ancestors without changing leaf meanings."""
import copy
from intent_engine.atomic.review_reuse import digest


def apply_common_parents(data, entries, persist):
    from intent_engine.atomic.topic_hierarchy import index_topic_tree, attach_topic_memberships
    from intent_engine.atomic.hierarchy_review import derive_taxonomy
    from intent_engine.atomic.leaf_merge import resolve_task_alias
    applied=0
    for entry in entries:
        before=copy.deepcopy(data)
        try:
            tree=copy.deepcopy(data['topic_hierarchy']);nodes,_=index_topic_tree(tree)
            ids=[data['task_topic_nodes'][resolve_task_alias(data,t)] for t in entry['task_ids']]
            if len(set(ids))!=2:continue
            proposal=entry['proposal'];target=proposal.get('parent_node_id')
            if target:
                if target not in nodes or nodes[target]['is_leaf'] or nodes[target].get('semantic_status')!='approved':
                    raise ValueError('共同祖先必须是现有已审核父主题')
                if all(nodes[child].get('parent_id')==target for child in ids):
                    entry['status']='parent_reused';continue
            else:
                target='semantic:'+digest({'children':sorted(ids),'label':proposal['label']})[:20]
                tree['nodes'].append(dict(node_id=target,label=proposal['label'],description=proposal['description'],
                    children=[],is_leaf=False,semantic_status='approved',keywords=[],distance=0.0))
                nodes[target]=tree['nodes'][-1]
            for child in ids:
                old=nodes[child].get('parent_id')
                if old and old!=target:nodes[old]['children'].remove(child)
                if child not in nodes[target]['children']:nodes[target]['children'].append(child)
            # Remove empty former ancestors; all leaves and their IDs remain intact.
            while True:
                empty={k for k,n in nodes.items() if not n['is_leaf'] and not n['children']}
                if not empty:break
                nodes={k:n for k,n in nodes.items() if k not in empty}
                for n in nodes.values():n['children']=[c for c in n['children'] if c not in empty]
            tree['nodes']=list(nodes.values());tree['status']='forest'
            index_topic_tree(tree)
            tree['status']='ready' if len(tree['root_ids'])==1 else 'forest'
            data['topic_hierarchy']=tree;attach_topic_memberships(data);derive_taxonomy(data)
            persist();entry['status']='parent_applied';applied+=1
        except Exception as exc:
            data.clear();data.update(before)
            entry.update(status='parent_rejected',error=str(exc))
    return applied
