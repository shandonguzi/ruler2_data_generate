"""Compact, read-only hierarchy context for semantic review requests."""

def topic_context(data, cluster_id, parent_cluster_id=None):
    tree=(data or {}).get('topic_hierarchy') or {}
    nodes={n['node_id']:n for n in tree.get('nodes',[])}
    parents={child:n['node_id'] for n in nodes.values() for child in n.get('children',[])}
    leaf=next((n for n in nodes.values() if n.get('is_leaf') and n.get('topic_id')==cluster_id),None)
    origin='current_cluster'
    if leaf is None and parent_cluster_id is not None:
        leaf=next((n for n in nodes.values() if n.get('is_leaf') and n.get('topic_id')==parent_cluster_id),None)
        origin='parent_cluster_before_split'
    path=[];seen=set()
    while leaf and leaf['node_id'] not in seen:
        seen.add(leaf['node_id'])
        path.append({'node_id':leaf['node_id'],'label':leaf.get('label',''),'is_leaf':bool(leaf.get('is_leaf'))})
        leaf=nodes.get(parents.get(leaf['node_id']))
    return {'source':origin if path else 'unavailable','path':list(reversed(path))}


def pair_review_payload(data,left,right,left_id,right_id):
    def samples(values):return [{k:m[k] for k in ('intent_id','text')} for m in values]
    return {'left':samples(left),'right':samples(right),
            'left_topic_context':topic_context(data,left_id),'right_topic_context':topic_context(data,right_id)}


def merge_verification_payload(data,payload,proposal):
    result={'definition':proposal,'left':payload['left']['members'],'right':payload['right']['members'],
            'left_topic_context':payload['left']['topic_context'],'right_topic_context':payload['right']['topic_context']}
    target=proposal.get('parent_node_id')
    if proposal.get('decision')=='common_parent' and target:
        ancestors={n['node_id'] for side in ('left','right') for n in payload[side]['topic_context']['path'] if not n['is_leaf']}
        if target not in ancestors:raise ValueError('选中祖先不在两侧主题路径中')
        node=next((n for n in data['topic_hierarchy']['nodes'] if n['node_id']==target),None)
        if not node or node.get('is_leaf') or node.get('semantic_status')!='approved':
            raise ValueError('选中祖先不是已审核父主题')
        result['selected_ancestor']={k:node.get(k,'') for k in ('node_id','label','description')}
    return result
