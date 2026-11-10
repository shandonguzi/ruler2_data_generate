"""Bounded local refinement; sampled review evidence never assigns unseen members."""
from __future__ import annotations
from intent_engine.atomic.topic_context import topic_context

import copy
import json
from collections import defaultdict

from pydantic import Field
from typing import Literal

from intent_engine.atomic.atomic_discovery import (StrictModel, SplitGroup, ClusterReview, REVIEW_PROMPT,
                               check_evidence, review_sample, cluster_atomic_texts, neighbor_pairs, review_eligible, consistency_review_payload)


class FullPartition(StrictModel):
    reason: str = ''
    object: str = ''
    action: str = ''
    groups: list[SplitGroup] = Field(min_length=1)
    uncertain_ids: list[str] = Field(default_factory=list)
    decision: Literal['keep','split'] = 'split'


from intent_engine.settings.prompt_settings import prompt_ref


SPLIT_PROMPT = prompt_ref("cluster_refinement.SPLIT_PROMPT")


def validate_partition(partition, members):
    if partition.decision=='keep' and (len(partition.groups)!=1 or partition.uncertain_ids or not partition.object.strip() or not partition.action.strip()):
        raise ValueError('保留原簇需提供统一对象动作、一组完整成员且无待确认项')
    if partition.decision=='split' and len(partition.groups)<2:
        raise ValueError('选择拆分时至少需要两个分组')
    ids = [i for group in partition.groups for i in group.evidence_ids] + partition.uncertain_ids
    if len(ids) != len(set(ids)) or set(ids) != {m['intent_id'] for m in members}:
        raise ValueError('拆分必须完整且不重复地覆盖所有真实样本')


def rebuild_hierarchy(texts, vectors, labels, options):
    """Recompute native c-TF-IDF hierarchy for fixed, audited memberships."""
    if vectors is None:
        raise ValueError("语义向量不可用，保留细分叶子，待恢复向量服务后重建层级")
    import numpy as np
    from bertopic import BERTopic
    from bertopic.dimensionality import BaseDimensionalityReduction
    from scipy.cluster.hierarchy import linkage
    from intent_engine.atomic.atomic_discovery import topic_vectorizer
    from intent_engine.atomic.topic_hierarchy import build_topic_tree

    class FixedClusters:
        def fit(self, X, y=None):
            self.labels_ = np.asarray(labels)
            return self

    model = BERTopic(language="multilingual", embedding_model=None, umap_model=BaseDimensionalityReduction(),
                    hdbscan_model=FixedClusters(),
                    vectorizer_model=topic_vectorizer(options))
    mapped, _ = model.fit_transform(texts, embeddings=vectors)
    back = dict(zip(mapped, labels))  # BERTopic renumbers by frequency; restore workflow IDs.
    keywords = {back[label]: [word for word, _ in model.get_topic(label) or [] if word.strip()]
                for label in set(mapped) if label >= 0}
    method = options.get('hierarchy_linkage', 'average')
    merges = []
    if len(keywords) > 1 and options.get('hierarchical_topics', True) and not options.get('flat_topics',False):
        table = model.hierarchical_topics(texts, use_ctfidf=True,
            linkage_function=lambda d: linkage(d, method=method, optimal_ordering=True))
        parent_map = {int(row.Parent_ID): max(labels) + 1 + i for i, row in enumerate(table.itertuples())}
        for row in table.to_dict(orient='records'):
            row['Parent_ID'] = parent_map[int(row['Parent_ID'])]
            for key in ('Child_Left_ID', 'Child_Right_ID'):
                child = int(row[key])
                row[key] = back[child] if child in back and child >= 0 else parent_map[child]
            merges.append(row)
    status = 'disabled' if len(keywords) > 1 and (not options.get('hierarchical_topics', True) or options.get('flat_topics',False)) else None
    return keywords, build_topic_tree(labels, keywords, merges, status=status, linkage=method)


def refine_clusters(data, vectors, options, llm, reporter, persist, *, semantic_records=None, cached_reviews=None):
    from intent_engine.atomic.topic_hierarchy import attach_topic_memberships, build_topic_tree
    from intent_engine.atomic.atomic_discovery import singleton_review
    atoms = [a for a in data['intents'] if a['status'] == 'valid']
    indices = {text: i for i, text in enumerate(dict.fromkeys(a['text'] for a in atoms))}
    for atom in atoms:
        atom['text_index'] = indices[atom['text']]
    by_cluster = defaultdict(list)
    for atom in atoms:
        if atom.get('cluster_id', -1) >= 0:
            by_cluster[atom['cluster_id']].append(atom)
    rounds = max(0, min(5, int(options.get('refinement_rounds', 2))))
    limit = max(2, min(200, int(options.get('refinement_llm_limit', 80))))
    sample_limit = max(2, int(options.get('review_examples', 8)))
    budget = max(1, min(200, int(options.get('refinement_max_splits', 64))))
    history = data.setdefault('cluster_refinement', {'history': []})
    history.update(max_rounds=rounds, status='running')
    next_id = max(by_cluster, default=-1) + 1
    queue = [(cluster, 0) for cluster in list(data['clusters']) if (cluster.get('review') or {}).get('verdict') == 'mixed']
    changed = False
    attempts = 0
    while queue:
        cluster, depth = queue.pop(0)
        members = by_cluster[cluster['cluster_id']]
        distinct = {}
        for member in members:
            distinct.setdefault(member['text'], member)
        unique = list(distinct.values())
        if depth >= rounds or attempts >= budget or len(unique) < 2:
            cluster['refinement_stop'] = 'max_rounds' if depth >= rounds else 'split_budget' if attempts >= budget else 'too_few_samples'
            continue
        attempts += 1
        reporter.update(attempts, attempts + len(queue) + 1, unit="次局部细分")
        event = {'parent_cluster_id': cluster['cluster_id'], 'round': depth + 1,
                 'parent': copy.deepcopy(cluster), 'applied': False, 'errors': []}
        local = {**options, 'min_cluster_size': max(2, min(int(options.get('min_cluster_size', 5)) // (2 ** (depth + 1)), len(unique) // 2)),
                 'min_samples': 1, 'n_neighbors': max(2, min(5, len(unique) - 1)), 'hierarchical_topics': False}
        event['parameters'] = local
        groups, noise, names = [], [], []
        try:
            indices = [m['text_index'] for m in unique]
            result = cluster_atomic_texts([m['text'] for m in unique], vectors[indices], local)
            labels, keywords = result
            if len(labels) != len(unique):
                raise ValueError('局部聚类返回的样本数不匹配')
            sizes = [labels.count(value) for value in set(labels) - {-1}]
            fragmented = len(unique) >= 4 and sizes and sum(size == 1 for size in sizes) / len(sizes) > .5
            if fragmented:
                event['local_rejected'] = 'singleton_dominated'
            if len(set(labels) - {-1}) >= 2 and not fragmented:
                groups = [[m['intent_id'] for m, label in zip(unique, labels) if label == value] for value in sorted(set(labels) - {-1})]
                names = [keywords.get(value, []) for value in sorted(set(labels) - {-1})]
                noise = [m['intent_id'] for m, label in zip(unique, labels) if label == -1]
                event['method'] = 'local_bertopic'
        except Exception as exc:
            event['errors'].append(str(exc))
        if len(unique) <= limit:
            try:
                partition = llm.chat_json(SPLIT_PROMPT, json.dumps({'cluster_id': cluster['cluster_id'],
                    'parent_cluster_id': cluster.get('parent_cluster_id'), 'refinement_round': depth + 1,
                    'topic_context':topic_context(data,cluster['cluster_id'],cluster.get('parent_cluster_id')),
                    'members': [
                    {k: m[k] for k in ('intent_id', 'text')} for m in unique],
                    'candidate_groups':[{'label':' / '.join(names[j]),'evidence_ids':ids} for j,ids in enumerate(groups)],
                    'review': cluster['review']}, ensure_ascii=False), schema=FullPartition, max_tokens=6000)
                validate_partition(partition, unique)
                if partition.decision=='keep':
                    cluster['review']={'reason':partition.reason,'object':partition.object,'action':partition.action,
                        'semantic_clear':True,'verdict':'consistent','boundary_ids':[],'split_groups':[]}
                    cluster['refinement_stop']='semantic_keep'
                    cluster['semantic_label']=partition.groups[0].label
                    event.update(method='llm_full_partition',decision='keep',reason=partition.reason)
                    history['history'].append(event);persist()
                    continue
                groups = [g.evidence_ids for g in partition.groups]
                names = [[g.label] for g in partition.groups]
                noise = partition.uncertain_ids
                event['method'] = 'llm_full_partition'
            except Exception as exc:
                groups,noise,names=[],[],[]
                event['errors'].append(str(exc))
        if not groups:
            cluster['refinement_stop'] = 'llm_limit' if len(unique) > limit else 'no_reliable_split'
            history['history'].append(event)
            persist()
            continue
        # Expand text-level decisions to all original occurrences, never just sampled IDs.
        text_by_id = {m['intent_id']: m['text'] for m in unique}
        assignment = {text_by_id[i]: next_id + j for j, ids in enumerate(groups) for i in ids}
        assignment.update({text_by_id[i]: -1 for i in noise})
        children = []
        for j, ids in enumerate(groups):
            child_id = next_id + j
            child_members = [m for m in members if assignment[m['text']] == child_id]
            samples = review_sample(child_members, vectors, sample_limit) if vectors is not None else [
                {k: m[k] for k in ('intent_id', 'text', 'span', 'raw_text')}
                for m in list({m['text']: m for m in child_members}.values())[:sample_limit]]
            child = {'cluster_id': child_id, 'parent_cluster_id': cluster['cluster_id'], 'refinement_round': depth + 1,
                     'sample_count': len(child_members), 'unique_text_count': len(ids), 'keywords': names[j],
                     'reviewed_ids': [s['intent_id'] for s in samples], 'review_samples': samples,
                     'review_scope': 'all_unique_texts' if len(ids) <= sample_limit else 'sampled', 'review': None}
            try:
                from intent_engine.atomic.review_reuse import reuse_review
                if cached_reviews and reuse_review(child,child_members,cached_reviews,llm,options):
                    decision = ClusterReview.model_validate(child['review'])
                else:
                    decision = singleton_review(child_members, semantic_records)
                    if decision is None:
                        decision = llm.chat_json(REVIEW_PROMPT, json.dumps(consistency_review_payload(child, data), ensure_ascii=False),
                                                 schema=ClusterReview, max_tokens=2400)
                    else:
                        child.update(review_source='singleton_skip',support_level='single_example')
                check_evidence(decision, set(child['reviewed_ids']))
                child['review'] = decision.model_dump()
                if child['review'].get('verdict')=='consistent':
                    from intent_engine.atomic.review_reuse import remember_review
                    remember_review(child,child_members,llm,options)
            except Exception as exc:
                child['review_error'] = str(exc)
                data['errors'].append({'stage': 'review', 'cluster_id': child_id, 'error': str(exc)})
            children.append(child)
            by_cluster[child_id] = child_members
        for atom in members:
            atom['cluster_id'] = assignment[atom['text']]
            if atom['cluster_id'] == -1:
                atom['topic_pending'] = True
        next_id += len(children)
        data['clusters'].remove(cluster)
        data['clusters'].extend(children)
        queue.extend((child, depth + 1) for child in children if (child.get('review') or {}).get('verdict') == 'mixed')
        event.update(applied=True, child_cluster_ids=[c['cluster_id'] for c in children], uncertain_ids=noise)
        history['history'].append(event)
        changed = True
        # Invalidate paths immediately: checkpoints must never carry the old tree with new labels.
        data.pop('topic_hierarchy', None)
        data.pop('bertopic_hierarchy', None)
        data.pop('hierarchy_review', None)
        data.pop('task_topic_nodes', None)
        for atom in data['intents']:
            atom.pop('topic_path', None)
        for row in data['rows']:
            row.pop('topic_paths', None)
            row.pop('topic_memberships', None)
        data['neighbor_pairs'] = []
        data['pair_reviews'] = []
        persist()
    for cluster in data['clusters']:
        pending = not review_eligible(cluster)
        cluster['topic_pending'] = pending
        cluster['support_level'] = 'single_example' if cluster.get('unique_text_count', len({a['text'] for a in by_cluster[cluster['cluster_id']]})) == 1 else 'multiple_examples'
        for atom in by_cluster[cluster['cluster_id']]:
            atom['topic_pending'] = pending
    history.update(status='completed', attempts=attempts,
                   pending_clusters=sum(c['topic_pending'] for c in data['clusters']))
    if changed:
        labels = [-1] * (max((a['text_index'] for a in atoms), default=-1) + 1)
        texts = [''] * len(labels)
        for atom in atoms:
            labels[atom['text_index']], texts[atom['text_index']] = atom['cluster_id'], atom['text']
        keywords = {c['cluster_id']: c['keywords'] for c in data['clusters']}
        try:
            keywords, tree = rebuild_hierarchy(texts, vectors, labels, options)
            for cluster in data['clusters']:
                cluster['keywords'] = keywords[cluster['cluster_id']]
        except Exception as exc:
            tree = build_topic_tree(labels, keywords, status='failed', message=f'细分后的层级重建失败：{exc}')
            data['errors'].append({'stage': 'hierarchy', 'error': tree['message']})
        data['topic_hierarchy'] = tree
        if 'cluster' in data.get('stage_outcomes', {}):
            data['stage_outcomes']['cluster']['hierarchy_depth'] = tree['max_depth']
        data['adjustments_applied'] = True
        data['neighbor_pairs'] = neighbor_pairs({c['cluster_id']: by_cluster[c['cluster_id']] for c in data['clusters']}, vectors,
                                              top_k=max(1, int(options.get('neighbor_top_k', 3)))) if vectors is not None else []
        attach_topic_memberships(data)
    persist()
