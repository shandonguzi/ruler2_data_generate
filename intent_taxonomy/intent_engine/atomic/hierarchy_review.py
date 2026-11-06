"""Bottom-up, level-parallel semantic review of the BERTopic merge tree."""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intent_engine.atomic.topic_hierarchy import build_topic_tree, index_topic_tree, attach_topic_memberships


from intent_engine.settings.prompt_settings import prompt_ref


NODE_PROMPT = prompt_ref("hierarchy_review.NODE_PROMPT")
LEAF_PROMPT = prompt_ref("hierarchy_review.LEAF_PROMPT")


class NodeDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)
    description: str = Field(min_length=1, max_length=1000)
    label: str = Field(min_length=1, max_length=160)
    decision: Literal['group', 'separate', 'uncertain']



class LeafNamingDecision(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra='forbid')
    reason: str = Field(min_length=1, max_length=1000)
    description: str = Field(min_length=1, max_length=1000)
    label: str = Field(min_length=1, max_length=160)
    semantic_issue: Literal['none','missing_context','ambiguous_object','ambiguous_action','conflicting_semantics'] = 'none'
    semantic_clear: bool

    @model_validator(mode='after')
    def concrete_semantic_issue_required(self):
        if self.semantic_clear != (self.semantic_issue == 'none'):
            raise ValueError('语义不明确必须说明具体语义问题，支持量或稳定性不足不是语义问题')
        return self


def review_hierarchy(data, llm, workers, reporter, persist, *, previous_hierarchy=None, cfg=None):
    """Cut rejected parent merges; retain raw topology and every source leaf."""
    from intent_engine.atomic.atomic_discovery import review_eligible
    from intent_engine.infrastructure.model_requests import USAGE, usage_since
    previous = previous_hierarchy if previous_hierarchy is not None else data.get('topic_hierarchy',{})
    previous_nodes = {n['node_id']:n for n in (previous or {}).get('nodes',[])
                      if n.get('is_leaf') and n.get('semantic_status')=='approved'
                      and n.get('semantic_clear') is True and n.get('semantic_issue')=='none'}
    member_texts = {}
    for atom in data.get('intents',[]):
        if atom.get('status')=='valid':
            member_texts.setdefault(atom.get('cluster_id'),set()).add(atom['text'])
    from intent_engine.atomic.review_reuse import model_identity, digest
    naming_settings = model_identity(llm, LEAF_PROMPT, 1000)
    previous_names = {}
    for node in previous_nodes.values():
        key = node.get('naming_fingerprint')
        content = {field:node.get(field) for field in ('label','description','review_reason')}
        if key and node.get('naming_result_fingerprint') == digest(content):
            saved = copy.deepcopy(node)
            saved['task_id'] = node.get('task_id') or next((task for task, node_id in data.get('task_topic_nodes',{}).items() if node_id==node['node_id']), None) or data.get('cluster_task_ids',{}).get(str(node.get('topic_id')))
            previous_names[key] = saved
    identities = {}
    reused_task_ids = {}
    raw = copy.deepcopy(data.get('bertopic_hierarchy') or data.get('topic_hierarchy'))
    if raw is None:
        raw = build_topic_tree([c['cluster_id'] for c in data['clusters']],
                               {c['cluster_id']:c.get('keywords',[]) for c in data['clusters']},
                               status='not_generated', message='历史结果缺少父主题，仅审核已有叶子')
    nodes, _ = index_topic_tree(raw)
    clusters = {c['cluster_id']: c for c in data['clusters']}
    # Native depth determines parallel waves; children finish before their parent.
    levels = {}
    for node in nodes.values():
        levels.setdefault(node['depth'], []).append(node['node_id'])
    active, frontiers, reviews = {}, {}, []
    state = {'status':'running', 'source':'BERTopic', 'levels':[], 'reviews':reviews,
             'raw_status':raw.get('status')}
    data['hierarchy_review'] = state
    data['bertopic_hierarchy'] = copy.deepcopy(raw)
    total, completed = len(nodes), 0
    child_limit = max(2, int(data.get('parameters', {}).get('hierarchy_child_limit', 16)))

    def payload_for(key):
        node = nodes[key]
        if node['is_leaf']:
            cluster = clusters.get(node['topic_id'], {})
            if not review_eligible(cluster):
                return None, 'leaf_not_eligible'
            review = cluster['review']
            return {'node_id':key, 'kind':'leaf',
                    'review':{field:review[field] for field in ('reason','object','action','semantic_clear','verdict') if field in review},
                    'members':[{'text':sample['text']} for sample in cluster['review_samples']]}, None
        children = [child for original in node['children'] for child in frontiers[original]]
        if len(children) < 2:
            return None, 'too_few_children'
        if len(children) > child_limit:
            return None, 'child_summary_limit'
        if any(active[c].get('semantic_status') != 'approved' for c in children):
            return None, 'unreviewed_child'
        return {'node_id':key, 'kind':'parent', 'children':[
            {'node_id':c, 'label':active[c]['label'], 'description':active[c]['description'],
             'reason':active[c]['review_reason'], 'leaf_count':len(active[c].get('leaf_topic_ids', []))}
            for c in children]}, None

    def request(payload):
        leaf = payload['kind']=='leaf'
        return llm.chat_json(LEAF_PROMPT if leaf else NODE_PROMPT, json.dumps(payload, ensure_ascii=False),
                             schema=LeafNamingDecision if leaf else NodeDecision, max_tokens=1000)

    for depth in sorted(levels, reverse=True):
        usage_before = USAGE.snapshot()
        keys = sorted(levels[depth])
        outcomes = {}
        level = {'native_level':depth+1, 'node_ids':keys, 'status':'running', 'requests':0,'reused_nodes':0}
        state['levels'].append(level)
        with ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}
            for key in keys:
                payload, skipped = payload_for(key)
                if skipped:
                    outcomes[key] = (None, skipped)
                else:
                    reused = None
                    topic = nodes[key].get('topic_id')
                    if nodes[key]['is_leaf']:
                        naming_review = payload['review']
                        if len(member_texts.get(topic,set()))==1:
                            # Singleton support metadata can switch consistent -> uncertain
                            # without changing the already clear object/action semantics.
                            naming_review = {field:payload['review'].get(field) for field in ('object','action')}
                            naming_review['semantic_clear'] = payload['review'].get('semantic_clear') is not False
                        identity = {'version':3, 'texts':sorted(member_texts.get(topic,set())),
                                    'review':naming_review,
                                    'samples':sorted({sample['text'] for sample in payload['members']}),
                                    'settings':naming_settings}
                        identities[key] = digest(identity)
                        saved = previous_names.get(identities[key],{})
                        if saved:
                            if saved.get('task_id'):
                                reused_task_ids[key] = saved['task_id']
                            try:
                                reused = LeafNamingDecision(reason=saved['review_reason'],description=saved['description'],
                                                            label=saved['label'],semantic_clear=True)
                            except (KeyError,ValueError,TypeError):
                                pass
                    if reused is not None:
                        outcomes[key] = (reused,'reused_singleton' if len(member_texts.get(topic,set()))==1 else 'reused_leaf')
                        level['reused_nodes'] += 1
                    else:
                        pending[pool.submit(request, payload)] = key
                        level['requests'] += 1
            for future in as_completed(pending):
                key = pending[future]
                try:
                    outcomes[key] = (future.result(), None)
                except Exception as exc:
                    outcomes[key] = (None, 'request_failed')
                    data['errors'].append({'stage':'domains', 'node_id':key, 'error':str(exc)})
                completed += 1
                reporter.update(completed, total, unit='个层级节点')
        # Apply in stable order only after the entire wave; workers never mutate data.
        for key in keys:
            node = copy.deepcopy(nodes[key])
            decision, skipped = outcomes[key]
            record = {'node_id':key, 'native_level':depth+1, 'skipped':skipped,
                      **(decision.model_dump() if decision else {})}
            reviews.append(record)
            approved = bool(decision and (decision.semantic_clear if node['is_leaf'] else decision.decision=='group'))
            if node['is_leaf']:
                record['decision'] = 'group' if approved else 'uncertain'
            if node['is_leaf']:
                cluster = clusters.get(node['topic_id'], {})
                support = 'single_example' if len(member_texts.get(node['topic_id'],set()))==1 else 'multiple_examples'
                cluster['support_level'] = support
                node.update(support_level=support,leaf_admission_version=1 if decision else None,
                            semantic_clear=bool(decision and decision.semantic_clear),
                            semantic_issue=decision.semantic_issue if decision else None,
                            label=decision.label if approved else node['label'],
                            description=decision.description if decision else '',
                            review_reason=decision.reason if decision else skipped,
                            semantic_status='approved' if approved else 'pending')
                node.pop('naming_fingerprint',None)
                node.pop('naming_result_fingerprint',None)
                node.pop('task_id',None)
                if approved and key in reused_task_ids:
                    node['task_id'] = reused_task_ids[key]
                if approved and key in identities:
                    node['naming_fingerprint'] = identities[key]
                    node['naming_result_fingerprint'] = digest({field:node.get(field) for field in ('label','description','review_reason')})
                node['naming_source'] = skipped if skipped in {'reused_singleton','reused_leaf'} else 'llm' if decision else 'pending'
                active[key] = node
                frontiers[key] = [key]
                cluster['hierarchy_eligible'] = approved
                cluster['topic_pending'] = not approved
            else:
                children = [child for original in node['children'] for child in frontiers[original]]
                if approved:
                    node.update(label=decision.label, description=decision.description,
                                review_reason=decision.reason, semantic_status='approved', children=children)
                    active[key] = node
                    frontiers[key] = [key]
                else:
                    frontiers[key] = children
            if skipped and skipped != 'request_failed':
                completed += 1
        level['status'] = 'completed_with_errors' if any(outcomes[k][1]=='request_failed' for k in keys) else 'completed'
        level['token_usage'] = usage_since(usage_before)
        state['completed_nodes'] = completed
        reporter.update(completed, total, unit='个层级节点')
        persist()
    roots = [c for key in raw['root_ids'] for c in frontiers[key]]
    tree = {**raw, 'source':'BERTopic+LLM', 'representation':'reviewed_c_tf_idf',
            'nodes':list(active.values()), 'status':'ready' if len(roots)==1 else 'forest' if roots else 'no_topics',
            'message':'保留审核通过的父主题；不合理的合并已展开为独立分支'}
    index_topic_tree(tree, set(clusters))
    data['topic_hierarchy'] = tree
    state['reused_nodes'] = sum(level['reused_nodes'] for level in state['levels'])
    state.update(status='completed_with_errors' if any(r['skipped']=='request_failed' for r in reviews) else 'completed',
                 accepted_parents=sum(not n['is_leaf'] for n in active.values()),
                 rejected_parents=sum(not nodes[r['node_id']]['is_leaf'] and r.get('decision')!='group' for r in reviews))
    for atom in data.get('intents', []):
        c = clusters.get(atom.get('cluster_id'))
        if c:
            atom['topic_pending'] = not (review_eligible(c) and c.get('hierarchy_eligible'))
    if cfg is not None:
        from intent_engine.atomic.parent_regroup import regroup
        regroup(data,cfg,llm,workers)
    attach_topic_memberships(data)
    derive_taxonomy(data)
    persist()
    return state


def derive_taxonomy(data):
    """Task/domain objects are lookup adapters to ONE tree, never a second taxonomy."""
    from intent_engine.atomic.atomic_discovery import review_eligible
    tree = data['topic_hierarchy']
    nodes, paths = index_topic_tree(tree)
    domains, tasks, mapping, task_nodes = {}, [], {}, {}
    version = data.get('candidate_taxonomy', {}).get('version', 1)
    for cluster in data['clusters']:
        topic = cluster['cluster_id']
        path = paths.get(topic, [])
        if not path or not review_eligible(cluster) or nodes[path[-1]].get('semantic_status') != 'approved':
            continue
        ancestors = path[:-1] or path[-1:]
        name = ' / '.join(nodes[key]['label'] for key in ancestors)
        domains[name] = {'name':name, 'description':nodes[ancestors[-1]].get('description','')}
        task_id = f"candidate_{data['run_id']}_{topic}"
        # Keep IDs stable across stage reruns and iterations when a leaf survives.
        task_id = nodes[path[-1]].get('task_id') or data.get('cluster_task_ids', {}).get(str(topic), task_id)
        nodes[path[-1]]['task_id'] = task_id
        review = cluster['review']
        tasks.append({'task_id':task_id, 'domain':name, 'object':review['object'], 'action':review['action'],
                      'description':nodes[path[-1]]['description'],
                      'positive_examples':[sample['text'] for sample in cluster['review_samples']]})
        mapping[str(topic)] = task_id
        task_nodes[task_id] = path[-1]
    data['candidate_taxonomy'] = {'version':version, 'domains':list(domains.values()), 'tasks':tasks}
    data['cluster_task_ids'], data['task_topic_nodes'] = mapping, task_nodes
