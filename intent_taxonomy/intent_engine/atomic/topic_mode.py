"""Flat cluster mode: keep leaf identity and memberships, omit parent structures."""
import copy


def apply_topic_mode(options, cfg=None):
    current=(cfg or {}).get('atomic_discovery',{})
    options['flat_topics']=bool(current.get('flat_topics',options.get('flat_topics',False)))
    return options


def flat_tree(tree):
    from intent_engine.atomic.topic_hierarchy import index_topic_tree
    result=copy.deepcopy(tree)
    result['nodes']=[n for n in result['nodes'] if n['is_leaf']]
    result.update(status='disabled',message='仅一级簇：不生成父主题',topic_mode='flat')
    index_topic_tree(result)
    return result


def flatten_existing(data):
    from intent_engine.atomic.topic_hierarchy import attach_topic_memberships
    from intent_engine.atomic.hierarchy_review import derive_taxonomy
    if not data.get('topic_hierarchy'):return
    previous_tasks={t['task_id']:copy.deepcopy(t) for t in data.get('candidate_taxonomy',{}).get('tasks',[])}
    data['topic_hierarchy']=flat_tree(data['topic_hierarchy'])
    if data.get('bertopic_hierarchy'):data['bertopic_hierarchy']=flat_tree(data['bertopic_hierarchy'])
    attach_topic_memberships(data);derive_taxonomy(data)
    data['candidate_taxonomy']['tasks']=[{**previous_tasks.get(t['task_id'],{}),**t} for t in data['candidate_taxonomy']['tasks']]
    data.setdefault('parent_regroup',{}).update(status='disabled',reason='flat_topics')
