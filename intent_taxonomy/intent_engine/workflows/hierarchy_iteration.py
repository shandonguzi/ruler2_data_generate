"""Iterate memberships and unresolved leaves in the single BERTopic hierarchy."""
from __future__ import annotations

import copy
import json
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from intent_engine.infrastructure.model_requests import USAGE, usage_since


def hierarchy_iteration_steps(rounds):
    phases = [('classify','叶子主题向量召回与匹配'), ('cluster','未覆盖意图聚类'),
              ('review','成员一致性审核与细分'), ('domains','重建与逐层审核主题树'), ('merge','叶子实例泛化与归并')]
    return [{'id':'iteration_prepare','label':'迭代准备：叶子准入与归并'}] + [{'id':f'round_{r}:{key}','label':label,'round':r}
            for r in range(1,rounds+1) for key,label in phases] + [
            {'id':'iteration_validation','label':'验证最终叶子归属'},
            {'id':'iteration_reports','label':'保存层级迭代记录'}]


def hierarchy_signature(data):
    return json.dumps({
        'nodes':[{k:node.get(k) for k in ('node_id','label','description','children','semantic_status')}
                 for node in sorted(data['topic_hierarchy']['nodes'],key=lambda n:n['node_id'])],
        'members':[(a['intent_id'],a.get('cluster_id'),a.get('topic_pending')) for a in data['intents']],
    }, ensure_ascii=False, sort_keys=True)


def reusable_assignment(atom, previous, data, taxonomy):
    """Rebase a retained result only when its leaf still has trustworthy evidence."""
    from intent_engine.atomic.atomic_discovery import review_eligible
    from intent_engine.workflows.candidate_iteration import fingerprint
    if previous is None or previous.get('error'):
        return None
    if any(previous.get(k) != atom.get(k) for k in ('intent_id','row_id','text','raw_text','span')):
        return None
    tasks = {t.task_id:t for t in taxonomy.tasks}
    task_id = previous.get('task_id')
    cluster = next((c for c in data['clusters'] if c['cluster_id']==atom.get('cluster_id')), None)
    if previous['status']=='known' and cluster and cluster.get('review_source')=='leaf_merge' and task_id in cluster.get('merged_task_ids',[]):
        from intent_engine.atomic.leaf_merge import resolve_task_alias
        canonical = resolve_task_alias(data,task_id)
        if canonical in tasks and data.get('cluster_task_ids',{}).get(str(atom.get('cluster_id')))==canonical and review_eligible(cluster):
            migrated = copy.deepcopy(previous)
            migrated.update(status='ambiguous',task_id=None)
            migrated.setdefault('original_task_id',task_id)
            return reusable_assignment(atom,migrated,data,taxonomy)
    source = 'retained_assignment'
    if (previous['status'] in {'ambiguous','discovery'} and (previous.get('conflicting_task_ids') or previous.get('pending_match_required'))
            and (not cluster or cluster.get('review_source')!='leaf_merge')):
        return None  # Existing membership alone cannot resolve an explicit task conflict.
    if previous['status'] == 'known':
        task = tasks.get(task_id)
        node = next((n for n in data['topic_hierarchy']['nodes']
                     if n['node_id']==data.get('task_topic_nodes',{}).get(task_id)), None)
        if not task or not node or node.get('semantic_status')!='approved':
            return None
        # A changed leaf definition invalidates reuse; ancestor path changes do not.
        identity = {k:task.model_dump().get(k) for k in ('object','action','description','subtasks','object_aliases','action_aliases','boundary_notes')}
        if previous.get('leaf_identity') != identity:
            return None
    else:
        task_id = data.get('cluster_task_ids',{}).get(str(atom.get('cluster_id')))
        cluster = next((c for c in data['clusters'] if c['cluster_id']==atom.get('cluster_id')), None)
        if task_id in tasks and cluster and review_eligible(cluster):
            source = 'audited_cluster'
        else:
            return None  # Unconfirmed memberships must enter recall/matching again.
    result = copy.deepcopy(previous)
    result.setdefault('original_taxonomy_version', previous.get('taxonomy_version'))
    result.update(taxonomy_version=taxonomy.version, taxonomy_fingerprint=fingerprint(taxonomy),
                  topic_path=atom.get('topic_path',[]), assignment_source=source,
                  assignment_basis='audited_cluster' if source=='audited_cluster' else previous.get('assignment_basis','llm_classification'))
    if task_id in tasks:
        task = tasks[task_id]
        result.update(status='known',task_id=task_id,domain=task.domain,object=task.object,action=task.action)
        if source=='audited_cluster':
            result['confidence'] = None  # Cluster evidence is not an LLM match score.
        result['leaf_identity'] = {k:task.model_dump().get(k) for k in ('object','action','description','subtasks','object_aliases','action_aliases','boundary_notes')}
    return result


def initial_membership_assignments(atoms, data, taxonomy):
    """Bootstrap reviewed memberships, without pretending an LLM matched each atom."""
    from intent_engine.workflows.candidate_iteration import fingerprint
    records = []
    for atom in atoms:
        record = {k:atom[k] for k in ('intent_id','row_id','raw_text','text','span')}
        record.update(status='discovery' if atom.get('cluster_id')==-1 else 'ambiguous',
                      object=atom.get('object',''),action=atom.get('action',''),slots=atom.get('slots',{}),
                      domain='',task_id=None,confidence=None,error=None,
                      taxonomy_version=taxonomy.version,taxonomy_fingerprint=fingerprint(taxonomy),
                      assignment_source='initial_semantics',assignment_basis='initial_semantics')
        records.append(reusable_assignment(atom,record,data,taxonomy) or record)
    return records


def run_hierarchy_iterations(cfg, data, rounds, workers, reporter, persist, vectors=None):
    import numpy as np
    import pandas as pd
    from intent_engine.atomic.atomic_discovery import (
        SemanticEmbeddings, embedding_settings, cluster_atomic_texts, review_sample,
        consistency_review_payload, REVIEW_PROMPT, ClusterReview, check_evidence, singleton_review,
    )
    from intent_engine.atomic.cluster_refinement import refine_clusters, rebuild_hierarchy
    from intent_engine.atomic.hierarchy_review import review_hierarchy
    from intent_engine.atomic.topic_hierarchy import build_topic_tree, attach_topic_memberships
    from intent_engine.workflows.pipeline import IterativePipeline
    from intent_engine.workflows.candidate_iteration import assignments, fingerprint, taxonomy_changes
    from intent_engine.models import Taxonomy
    atoms = [a for a in data['intents'] if a['status']=='valid']
    texts = list(dict.fromkeys(a['text'] for a in atoms))
    indices = {text:i for i,text in enumerate(texts)}
    for atom in atoms:
        atom['text_index'] = indices[atom['text']]
    from intent_engine.infrastructure.iteration_scope import normalize_iteration_scope
    scope = normalize_iteration_scope(data.get('parameters',{}).get('iteration_scope', cfg.get('atomic_discovery',{}).get('iteration_scope','unresolved_only')))
    data['parameters']['iteration_scope'] = scope
    resumed = data.pop('resume_iteration', {})
    completed_rounds = copy.deepcopy(resumed.get('rounds', []))
    # A round that ended with processing errors must be retried, not skipped.
    while completed_rounds and completed_rounds[-1].get('processing_errors'):
        completed_rounds.pop()
    start_round = len(completed_rounds) + 1
    prior_conflicts = copy.deepcopy((resumed or data.get('iteration') or {}).get('assignments', []))
    state = {'iteration_scope':scope, 'status':'running','max_rounds':rounds,'rounds':completed_rounds, 'assignments':copy.deepcopy(prior_conflicts) if resumed else [], 'converged':False,
             'stop_reason':None, 'mode':'bertopic_hierarchy', 'initial_taxonomy':copy.deepcopy(data['candidate_taxonomy'])}
    data['iteration'] = state
    local = copy.deepcopy(cfg)
    local.setdefault('pipeline', {}).update(atomic_input=True,text_column='_atomic_text')
    local.setdefault('llm', {})['max_workers'] = workers
    pipeline = IterativePipeline(local, progress=reporter)
    frame = pd.DataFrame([{'_atomic_text':a['text'], '_atomic_slots':a.get('slots', {}), '_atomic_semantics':{'text':a['text'],'object':a.get('object',''),'action':a.get('action','')}} for a in atoms])
    options = data['parameters']
    from intent_engine.atomic.leaf_merge import merge_leaves
    reporter.start('iteration_prepare')
    # Revisit legacy singleton vetoes even when matching does not change memberships.
    from intent_engine.atomic.atomic_discovery import review_eligible
    pending_nodes = {node['topic_id'] for node in data['topic_hierarchy']['nodes']
                     if node.get('is_leaf') and node.get('semantic_status')!='approved'
                     and node.get('leaf_admission_version')!=1}
    member_sets = {}
    for atom in atoms:
        member_sets.setdefault(atom.get('cluster_id'),set()).add(atom['text'])
    legacy_singletons = [cluster['cluster_id'] for cluster in data['clusters']
                         if cluster['cluster_id'] in pending_nodes and len(member_sets.get(cluster['cluster_id'],set()))==1
                         and review_eligible(cluster)]
    admission_refresh = None
    if legacy_singletons:
        prior_data = copy.deepcopy(data)
        old_taxonomy = Taxonomy.model_validate(data['candidate_taxonomy'])
        admission_refresh = review_hierarchy(data,pipeline.llm,workers,reporter,lambda:None,cfg=cfg)
        if not data['candidate_taxonomy']['tasks']:
            new_errors = copy.deepcopy(data['errors'][len(prior_data['errors']):])
            data.clear();data.update(prior_data);data['errors'].extend(new_errors)
            admission_refresh = {'status':'failed'}
        elif fingerprint(old_taxonomy)!=fingerprint(Taxonomy.model_validate(data['candidate_taxonomy'])):
            data['candidate_taxonomy']['version'] = old_taxonomy.version+1
    preparation,vectors = merge_leaves(cfg,data,pipeline.llm,workers,reporter,persist,vectors=vectors,
                                      conflicts=prior_conflicts,phase='iteration_prepare')
    if admission_refresh is not None:
        preparation['admission_refresh'] = {'clusters':legacy_singletons,'status':admission_refresh['status']}
        if admission_refresh['status'] in {'failed','completed_with_errors'}:
            preparation['status'] = 'completed_with_errors'
    state['preparation'] = copy.deepcopy(preparation)
    data['iteration'] = state
    state['prepared_taxonomy'] = copy.deepcopy(data['candidate_taxonomy'])
    # Consolidation publishes a staged copy; refresh atom references afterwards.
    atoms = [a for a in data['intents'] if a['status']=='valid']
    seen = {hierarchy_signature(data)}
    latest_fingerprint = None
    committed = copy.deepcopy(data)
    previous = initial_membership_assignments(atoms,data,Taxonomy.model_validate(data['candidate_taxonomy'])) if scope=='unresolved_only' else None
    if previous is not None:
        unresolved = {record['intent_id']:record for record in prior_conflicts
                      if record.get('status') in {'ambiguous','discovery'} and not record.get('error')}
        for index, atom in enumerate(atoms):
            record = unresolved.get(atom['intent_id'])
            if record and all(record.get(key)==atom.get(key) for key in ('row_id','text','raw_text','span')):
                previous[index] = copy.deepcopy(record)
                previous[index]['pending_match_required'] = True
    def classify(taxonomy, number):
        digest = fingerprint(taxonomy)
        retained, selected = {}, []
        for index, atom in enumerate(atoms):
            prior = previous[index] if previous is not None else None
            reused = reusable_assignment(atom, prior, data, taxonomy) if scope=='unresolved_only' else None
            if reused is not None:
                retained[index] = reused
            else:
                selected.append(index)
        reporter.update(0,len(selected),reused=len(atoms)-len(selected),
                        selection_scope='first_round_all' if previous is None else scope)
        fresh = []
        if selected:
            results = pipeline.classify_dataframe(frame.iloc[selected].reset_index(drop=True),taxonomy,number)
            fresh = assignments([atoms[i] for i in selected], results, taxonomy.version, digest)
        for index, record in zip(selected,fresh):
            record['assignment_source'] = 'llm_classification'
            record['assignment_basis'] = 'llm_classification'
            task = next((t for t in taxonomy.tasks if t.task_id==record.get('task_id')),None)
            if task:
                record['leaf_identity'] = {k:task.model_dump().get(k) for k in ('object','action','description','subtasks','object_aliases','action_aliases','boundary_notes')}
            retained[index] = record
        step = reporter.state.snapshot().get('current') or {}
        reporter.update(len(selected),len(selected),cached=step.get('cached',0),
                        failed=sum(bool(record.get('error')) for record in fresh))
        return [retained[i] for i in range(len(atoms))], selected
    try:
        if completed_rounds:
            reporter.skip(s['id'] for s in hierarchy_iteration_steps(rounds) if 0 < s.get('round', 0) < start_round)
        if start_round > rounds:
            state['stop_reason'] = resumed.get('stop_reason') if resumed.get('stop_reason') in {'no_changes','max_rounds','cycle_detected'} else 'max_rounds'
        for number in range(start_round,rounds+1):
            data['review_reuse'] = {'reused_clusters':0,'requested_clusters':0}
            before = Taxonomy.model_validate(data['candidate_taxonomy'])
            signature = hierarchy_signature(data)
            hierarchy_before = copy.deepcopy(data['topic_hierarchy'])
            from intent_engine.atomic.review_reuse import review_cache, reuse_review, remember_review
            cached_reviews = review_cache(data['clusters'],atoms,pipeline.llm,options)
            usage_before = USAGE.snapshot()
            error_count_before = len(data["errors"])
            reporter.start(f'round_{number}:classify')
            current, selected = classify(before, number)
            active = set(selected)
            state['assignments'] = current
            latest_fingerprint = fingerprint(before)
            if any(r['error'] for r in current):
                raise ValueError('原子意图匹配请求失败，保留上一版主题树，不视为已收敛')
            reporter.start(f'round_{number}:cluster')
            # Vector discovery changes only memberships; no independent domain ADD/MERGE.
            task_clusters = {task:int(topic) for topic,task in data['cluster_task_ids'].items()}
            unknown = list(dict.fromkeys(r['text'] for i,r in enumerate(current) if r['status']=='discovery' and i in active))
            has_membership_change = any(
                (task_clusters.get(r['task_id']) if r['status']=='known' else -1 if r['status']=='discovery' else a.get('cluster_id'))
                != a.get('cluster_id') for i,(a,r) in enumerate(zip(atoms,current)) if i in active)
            if unknown or has_membership_change:
                # Reuse clustering embeddings when available; derived iteration encodes once.
                if vectors is None:
                    vectors = SemanticEmbeddings(embedding_settings(cfg),
                        Path(cfg['paths']['checkpoint_dir'])/'atomic_embeddings').encode(texts, reporter)
                new_labels, new_keywords = [-1]*len(unknown), {}
                if len(unknown)>1:
                    clustered = cluster_atomic_texts(unknown, vectors[[indices[t] for t in unknown]], options)
                    new_labels, new_keywords = clustered
                    if len(new_labels)!=len(unknown):
                        raise ValueError('未覆盖意图聚类的样本数量不匹配')
                next_id = max((c['cluster_id'] for c in data['clusters']), default=-1)+1
                remap = {label:next_id+i for i,label in enumerate(sorted(set(new_labels)-{-1}))}
                new_by_text = {t:remap.get(label,-1) for t,label in zip(unknown,new_labels)}
                for index,(atom,record) in enumerate(zip(atoms,current)):
                    if index not in active:
                        continue
                    if record['status']=='known':
                        atom['cluster_id'] = task_clusters[record['task_id']]
                    elif record['status']=='discovery':
                        atom['cluster_id'] = new_by_text[atom['text']]
                keywords = {c['cluster_id']:c.get('keywords',[]) for c in data['clusters']}
                keywords.update({remap[k]:v for k,v in new_keywords.items() if k in remap})
                grouped = defaultdict(list)
                for atom in atoms:
                    if atom.get('cluster_id',-1)>=0:
                        grouped[atom['cluster_id']].append(atom)
                limit = max(2,int(options.get('review_examples',8)))
                clusters = []
                for topic,members in sorted(grouped.items()):
                    samples = review_sample(members,vectors,limit)
                    count = len({a['text'] for a in members})
                    clusters.append({'cluster_id':topic,'keywords':keywords.get(topic,[]),
                        'sample_count':len(members),'unique_text_count':count,'review_samples':samples,
                        'reviewed_ids':[s['intent_id'] for s in samples],
                        'review_scope':'sampled' if count>limit else 'all_unique_texts','review':None})
                data['clusters'] = clusters
                data['neighbor_pairs'] = []
                data['pair_reviews'] = []
                data.pop('bertopic_hierarchy',None)
                reporter.start(f'round_{number}:review')
                def audit(c):
                    decision = pipeline.llm.chat_json(REVIEW_PROMPT,
                        json.dumps(consistency_review_payload(c),ensure_ascii=False),schema=ClusterReview,max_tokens=2400)
                    check_evidence(decision,set(c['reviewed_ids']))
                    return decision.model_dump()
                needs_review = []
                for cluster in clusters:
                    if reuse_review(cluster,grouped[cluster['cluster_id']],cached_reviews,pipeline.llm,options):
                        continue
                    reused = singleton_review(grouped[cluster['cluster_id']], current)
                    if reused is not None:
                        cluster.update(review=reused.model_dump(), review_source='singleton_skip', support_level='single_example')
                    else:
                        needs_review.append(cluster)
                skipped = len(clusters)-len(needs_review)
                reporter.update(skipped,len(clusters),unit='个簇',reused=sum(c.get('review_source')=='reused_review' for c in clusters))
                data['review_reuse'] = {'reused_clusters':sum(c.get('review_source')=='reused_review' for c in clusters),'requested_clusters':len(needs_review)}
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    pending = {pool.submit(audit,c):c for c in needs_review}
                    for i,future in enumerate(as_completed(pending),1):
                        cluster = pending[future]
                        try:
                            cluster['review'] = future.result()
                            remember_review(cluster,grouped[cluster['cluster_id']],pipeline.llm,options)
                        except Exception as exc:
                            cluster['review_error'] = str(exc)
                            data['errors'].append({'stage':'iterate','cluster_id':cluster['cluster_id'],'error':str(exc)})
                        reporter.update(skipped+i,len(clusters),unit='个簇')
                # No persist until topology matches new memberships; readers never see stale paths.
                refine_clusters(data,vectors,options,pipeline.llm,reporter,lambda:None,semantic_records=current,cached_reviews=cached_reviews)
                reporter.start(f'round_{number}:domains')
                labels = [-1]*len(texts)
                for atom in atoms:
                    labels[atom['text_index']] = atom['cluster_id']
                try:
                    keys,tree = rebuild_hierarchy(texts,vectors,labels,options)
                    for cluster in data['clusters']:
                        cluster['keywords'] = keys.get(cluster['cluster_id'],[])
                except Exception as exc:
                    tree = build_topic_tree(labels,{c['cluster_id']:c['keywords'] for c in clusters},
                                            status='failed',message=str(exc))
                    data['errors'].append({'stage':'hierarchy','error':str(exc)})
                data['topic_hierarchy'] = tree
                attach_topic_memberships(data)
                review_hierarchy(data,pipeline.llm,workers,reporter,lambda:None,previous_hierarchy=hierarchy_before,cfg=cfg)
                after = Taxonomy.model_validate(data['candidate_taxonomy'])
                if not after.tasks:
                    raise ValueError('重建后没有审核通过的叶子主题，保留上一版层级')
            else:
                reporter.skip([f'round_{number}:review',f'round_{number}:domains'])
                after = before
            reporter.start(f'round_{number}:merge')
            merge_result,vectors = merge_leaves(cfg,data,pipeline.llm,workers,reporter,lambda:None,
                                                vectors=vectors,conflicts=current,phase=f'round_{number}')
            data['iteration'] = state
            atoms = [a for a in data['intents'] if a['status']=='valid']
            after = Taxonomy.model_validate(data['candidate_taxonomy'])
            if fingerprint(before)!=fingerprint(after):
                after.version = before.version+1
                data['candidate_taxonomy'] = after.model_dump()
            changed = signature!=hierarchy_signature(data)
            processing_errors = len(data['errors'])-error_count_before + int(number==1 and preparation['status'] in {'failed','completed_with_errors'})
            changes = taxonomy_changes(before,after)
            if changed:
                changes.append({'kind':'hierarchy','id':'topic_hierarchy','operation':'REBUILD',
                    'before':{'root_ids':hierarchy_before['root_ids'],'nodes':len(hierarchy_before['nodes'])},
                    'after':{'root_ids':data['topic_hierarchy']['root_ids'],'nodes':len(data['topic_hierarchy']['nodes'])}})
            state['rounds'].append({'round':number,'changed':changed,'taxonomy_before':before.model_dump(),
                'taxonomy_after':after.model_dump(),'changes':changes,
                'hierarchy_before':hierarchy_before,'hierarchy_after':copy.deepcopy(data['topic_hierarchy']),
                'memberships':[{k:a.get(k) for k in ('intent_id','cluster_id','topic_path','topic_pending')} for a in atoms],
                'classification_scope':scope,'classified_count':len(selected),'reused_count':len(atoms)-len(selected),
                'assignments':current,'task_count_before':len(before.tasks),'task_count_after':len(after.tasks),
                'leaf_merge':copy.deepcopy(merge_result),'processing_errors':processing_errors,'status_counts':{key:sum(r['status']==key for r in current) for key in ('known','discovery','ambiguous')},
                'token_usage':usage_since(usage_before),'review_reuse':copy.deepcopy(data.get('review_reuse')), 'hierarchy_review':copy.deepcopy(data.get('hierarchy_review'))})
            if processing_errors:
                state.update(stop_reason='processing_errors',converged=False)
            elif not changed and not merge_result.get('deferred_candidates') and not any(p.get('status')=='deferred_overlap' for p in merge_result.get('pairs',[])):
                state.update(stop_reason='no_changes',converged=True)
            elif changed and hierarchy_signature(data) in seen:
                state['stop_reason'] = 'cycle_detected'
            elif number==rounds:
                state['stop_reason'] = 'max_rounds'
            seen.add(hierarchy_signature(data))
            previous = copy.deepcopy(current)
            persist()
            committed = copy.deepcopy(data)
            if state['stop_reason']:
                reporter.skip(s['id'] for s in hierarchy_iteration_steps(rounds) if s.get('round',0)>number)
                break
        reporter.start('iteration_validation')
        validation_before = USAGE.snapshot()
        final = Taxonomy.model_validate(data['candidate_taxonomy'])
        if final.tasks and latest_fingerprint!=fingerprint(final):
            state['assignments'], validation_selected = classify(final, rounds+1)
            state['validation_classified_count'] = len(validation_selected)
            state['validation_reused_count'] = len(atoms)-len(validation_selected)
        state['validated_taxonomy_version'] = final.version if state['assignments'] and all(
            r['taxonomy_fingerprint']==fingerprint(final) and not r['error'] for r in state['assignments']) else None
        state['validation_mode'] = 'incremental_leaf_check' if scope=='unresolved_only' else 'full_classification'
        state['validation_token_usage'] = usage_since(validation_before)
        if state['validated_taxonomy_version'] is None:
            raise ValueError('最终叶子主题归属未通过验证')
        state['status'] = 'completed_with_errors' if state['stop_reason'] in {'cycle_detected','processing_errors'} else 'completed'
        state['stop_message'] = {'no_changes':'叶子归属与层级未变化，停止迭代',
            'cycle_detected':'层级出现循环，保留当前结果待确认',
            'processing_errors':'本轮审核或层级重建有错误，保留可用分支待复核，不视为收敛', 'max_rounds':'达到最大轮数，尚未确认收敛'}[state['stop_reason']]
    except Exception as exc:
        data.clear()
        data.update(committed)
        data["iteration"] = state
        state.update(status='completed_with_errors' if state['rounds'] else 'failed',
                     stop_reason='failed',stop_message=str(exc),converged=False)
        reporter.complete('failed')
        reporter.skip(s['id'] for s in hierarchy_iteration_steps(rounds) if s['id']!='iteration_reports')
    reporter.start('iteration_reports')
    persist()
    return state
