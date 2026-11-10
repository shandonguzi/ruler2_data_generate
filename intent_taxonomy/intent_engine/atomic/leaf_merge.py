"""Audited, bounded leaf consolidation; sampled evidence and original IDs stay traceable."""
from __future__ import annotations

import copy
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from itertools import combinations
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, ConfigDict, Field, model_validator

from intent_engine.atomic.review_reuse import digest, model_identity
from intent_engine.settings.prompt_settings import prompt_ref

MERGE_PROMPT = prompt_ref('leaf_merge.MERGE_PROMPT')
VERIFY_PROMPT = prompt_ref('leaf_merge.VERIFY_PROMPT')


class MergeProposal(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)
    description: str = Field(default='', max_length=1000)
    label: str = Field(default='', max_length=160)
    object: str = Field(default='', max_length=500)
    action: str = Field(default='', max_length=500)
    parent_node_id: str | None = None
    decision: Literal['merge', 'normalize', 'common_parent', 'keep', 'uncertain']

    @model_validator(mode='after')
    def definition_required(self):
        if self.decision in {'merge','normalize'} and not all((self.label,self.description,self.object,self.action)):
            raise ValueError('合并方案必须包含完整且非空的通用定义')
        if self.decision=='common_parent' and not self.parent_node_id and not (self.label and self.description):
            raise ValueError('新建共同祖先需要名称和定义')
        return self


class FlatMergeProposal(MergeProposal):
    decision: Literal['merge','normalize','keep','uncertain']


class MergeValidation(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    reason: str = Field(min_length=1, max_length=1000)
    rejected_texts: list[str] = Field(default_factory=list)
    valid: bool

    @model_validator(mode='after')
    def no_rejected_when_valid(self):
        if self.valid and self.rejected_texts:
            raise ValueError('包含拒绝样本不能判定合并通过')
        return self


def resolve_task_alias(data, task_id):
    aliases = data.get('task_aliases', {})
    seen = set()
    while task_id in aliases and task_id not in seen:
        seen.add(task_id)
        task_id = aliases[task_id]
    if task_id in seen:
        raise ValueError('任务归并映射包含循环')
    return task_id


def completion_identity(cfg, data, llm, options, conflicts):
    """Only skip an exhausted pass with exactly the same semantic inputs."""
    return digest({'version':1,
        'tasks':sorted(data.get('candidate_taxonomy',{}).get('tasks',[]),key=lambda t:t['task_id']),
        'members':sorted((a['intent_id'],a.get('text'),a.get('cluster_id')) for a in data['intents'] if a.get('status')=='valid'),
        'hierarchy':[(n['node_id'],n.get('label'),n.get('description'),n.get('children')) for n in data['topic_hierarchy']['nodes']],
        'leaves':sorted((n['node_id'],n.get('topic_id'),n.get('label'),n.get('semantic_status'))
                        for n in data['topic_hierarchy']['nodes'] if n.get('is_leaf')),
        'mapping':data.get('task_topic_nodes',{}), 'aliases':data.get('task_aliases',{}),
        'clusters':sorted(({k:c.get(k) for k in ('cluster_id','review_samples','review','review_error')}
                           for c in data.get('clusters',[])), key=lambda c:c['cluster_id']),
        'conflicts':sorted((a.get('intent_id',''),a.get('text',''),tuple(sorted(a.get('conflicting_task_ids',[]))))
                           for a in conflicts if a.get('status')=='ambiguous' and not a.get('error')),
        'options':{k:options.get(k) for k in ('flat_topics','leaf_merge_top_k','leaf_merge_min_similarity','leaf_merge_max_pairs','leaf_merge_max_merges','review_examples')},
        'embedding':{k:cfg.get('embedding',{}).get(k) for k in ('base_url','model','dimensions','cache_version','extra_body')},
        'proposal':model_identity(llm,MERGE_PROMPT,1600),'verify':model_identity(llm,VERIFY_PROMPT,1000)})


def merge_leaves(cfg, data, llm, workers, reporter, persist, *, vectors=None, conflicts=(), phase='initial'):
    """Stage all changes; failed reconstruction never publishes new memberships."""
    from intent_engine.atomic.atomic_discovery import SemanticEmbeddings, embedding_settings, review_sample
    from intent_engine.atomic.cluster_refinement import rebuild_hierarchy
    from intent_engine.atomic.hierarchy_review import review_hierarchy
    from intent_engine.models import Taxonomy
    from intent_engine.workflows.candidate_iteration import fingerprint
    options = {**cfg.get('atomic_discovery', {}), **data.get('parameters', {})}
    # The current switch can enable semantic repair for historical snapshots.
    if 'leaf_merge_enabled' in cfg.get('atomic_discovery',{}):
        options['leaf_merge_enabled']=cfg['atomic_discovery']['leaf_merge_enabled']
    from intent_engine.atomic.topic_mode import apply_topic_mode
    apply_topic_mode(options,cfg)
    history = data.setdefault('leaf_merging', {'passes': [], 'cache': {}})
    record = {'phase':phase, 'status':'running', 'pairs':[], 'applied':0, 'logical_requests':0, 'cached':0}
    history.setdefault('passes',[]).append(record)
    if not options.get('leaf_merge_enabled', False):
        record['status']='disabled'
        return record, vectors
    conflicts = list(conflicts)
    completed_identity = completion_identity(cfg,data,llm,options,conflicts)
    if history.get('completed_identity') == completed_identity:
        record.update(status='completed', skipped=True, skip_reason='unchanged_completed_pass')
        reporter.update(0,0,unit='对候选叶子')
        return record,vectors
    tasks = {t['task_id']:t for t in data.get('candidate_taxonomy', {}).get('tasks', [])}
    if len(tasks)<2:
        record['status']='no_candidates'
        history['completed_identity']=completed_identity
        return record, vectors
    atoms = [a for a in data['intents'] if a.get('status')=='valid']
    texts = list(dict.fromkeys(a['text'] for a in atoms))
    indices = {text:index for index,text in enumerate(texts)}
    grouped = {}
    for atom in atoms:
        grouped.setdefault(atom.get('cluster_id'), []).append(atom)
    nodes = {n['node_id']:n for n in data['topic_hierarchy']['nodes']}
    clusters = {c['cluster_id']:c for c in data['clusters']}
    leaves = {}
    for task_id, task in tasks.items():
        node = nodes.get(data.get('task_topic_nodes', {}).get(task_id))
        if node and node.get('is_leaf') and node.get('semantic_status')=='approved' and grouped.get(node['topic_id']):
            leaves[task_id] = node['topic_id']
    if len(leaves)<2:
        record['status']='no_candidates'
        history['completed_identity']=completed_identity
        return record, vectors
    try:
        if vectors is None:
            vectors = SemanticEmbeddings(embedding_settings(cfg), Path(cfg['paths']['checkpoint_dir'])/'atomic_embeddings').encode(texts,reporter)
        vectors = np.asarray(vectors)
        if vectors.ndim!=2 or len(vectors)!=len(texts) or not np.isfinite(vectors).all():
            raise ValueError('叶子归并向量与完整原子文本不一致')
        for atom in atoms:
            atom['text_index']=indices[atom['text']]
        centers = {}
        for task_id, topic in leaves.items():
            matrix = vectors[sorted({indices[a['text']] for a in grouped[topic]})]
            norms=np.linalg.norm(matrix,axis=1,keepdims=True)
            center=(matrix/np.maximum(norms,1e-12)).mean(axis=0)
            centers[task_id]=center/max(float(np.linalg.norm(center)),1e-12)
        ids=sorted(leaves);scores=np.asarray([centers[key] for key in ids])@np.asarray([centers[key] for key in ids]).T
        candidates={}
        k=max(1,min(10,int(options.get('leaf_merge_top_k',3))))
        threshold=float(options.get('leaf_merge_min_similarity',.75))
        for index,left in enumerate(ids):
            for j in [j for j in np.argsort(-scores[index],kind='stable') if j!=index][:k]:
                if scores[index,j]>=threshold:
                    candidates[tuple(sorted((left,ids[j])))]=(0,float(scores[index,j]))
        conflict_texts={}
        for assignment in conflicts:
            if assignment.get('status')!='ambiguous' or assignment.get('error'):
                continue
            targets=sorted({resolve_task_alias(data,key) for key in assignment.get('conflicting_task_ids',[])})
            for pair in combinations([key for key in targets if key in leaves],2):
                candidates[pair]=(1,1.)
                conflict_texts.setdefault(pair,set()).add(assignment['text'])
        limit=max(1,min(8,int(options.get('review_examples',8))))
        settings={'proposal':model_identity(llm,MERGE_PROMPT,1600),'verify':model_identity(llm,VERIFY_PROMPT,1000)}
        side_payloads={}
        def side(task_id):
            if task_id in side_payloads:return side_payloads[task_id]
            task=tasks[task_id];members=grouped[leaves[task_id]]
            samples=review_sample(members,vectors,limit)
            from intent_engine.atomic.topic_context import topic_context
            side_payloads[task_id] = {'topic_context':topic_context(data,leaves[task_id]),'task_id':task_id,'label':nodes[data['task_topic_nodes'][task_id]]['label'],
                    **{key:task.get(key) for key in ('object','action','description','boundary_notes')},
                    'members':[{'text':text} for text in sorted({s['text'] for s in samples})]}
            return side_payloads[task_id]
        work=[];budget=max(1,int(options.get('leaf_merge_max_pairs',64)))
        cache=history.setdefault('cache',{})
        for pair in sorted(candidates,key=lambda pair:(-candidates[pair][0],-candidates[pair][1],pair)):
            payload={'topic_mode':'flat' if options['flat_topics'] else 'hierarchical','left':side(pair[0]),'right':side(pair[1]),'conflict_texts':sorted(conflict_texts.get(pair,set()))[:8]}
            identity=digest({'version':1,'payload':payload,'settings':settings,
                'full_members':{key:sorted({a['text'] for a in grouped[leaves[key]]}) for key in pair}})
            if identity in cache:
                record['cached']+=1
                record['pairs'].append({**copy.deepcopy(cache[identity]),'task_ids':list(pair),'cache_hit':True})
            elif len(work)<budget:
                work.append((pair,payload,identity))
        record['deferred_candidates']=max(0,len(candidates)-len(work)-record['cached'])
        reporter.update(0,len(work),unit='对候选叶子')
        def audit(item):
            pair,payload,identity=item
            entry={'task_ids':list(pair),'cache_hit':False,'sample_counts':[len(payload[side]['members']) for side in ('left','right')], 'logical_requests':1}
            try:
                proposal=llm.chat_json(MERGE_PROMPT,json.dumps(payload,ensure_ascii=False),schema=FlatMergeProposal if options['flat_topics'] else MergeProposal,max_tokens=1600)
                entry['proposal']=proposal.model_dump()
                if proposal.decision in {'merge','normalize','common_parent'}:
                    from intent_engine.atomic.topic_context import merge_verification_payload
                    verification=merge_verification_payload(data,payload,proposal.model_dump())
                    entry['logical_requests']+=1
                    result=llm.chat_json(VERIFY_PROMPT,json.dumps(verification,ensure_ascii=False),schema=MergeValidation,max_tokens=1000)
                    allowed={sample['text'] for side in ('left','right') for sample in payload[side]['members']}
                    if not set(result.rejected_texts)<=allowed:
                        raise ValueError('合并复核引用了未提供的样本')
                    entry['validation']=result.model_dump()
                entry['status']='verified' if entry.get('validation',{}).get('valid') else proposal.decision if proposal.decision in {'keep','uncertain'} else 'rejected'
            except Exception as exc:
                entry.update(status='error',error=str(exc))
            return entry
        outcomes={}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures={pool.submit(audit,item):item for item in work}
            for done,future in enumerate(as_completed(futures),1):
                pair,payload,identity=futures[future]
                try:
                    entry=future.result()
                    # Input fingerprints include both full memberships and definitions.
                    # A merged leaf therefore cannot reuse an old pair decision.
                    if entry['status'] in {'keep','rejected','uncertain','verified'}:
                        cache[identity]=copy.deepcopy(entry)
                    outcomes[pair]=entry
                    if entry['status']=='error':
                        data['errors'].append({'stage':'leaf_merge','task_ids':list(pair),'error':entry['error']})
                except Exception as exc:
                    outcomes[pair]={'task_ids':list(pair),'status':'error','error':str(exc)}
                    data['errors'].append({'stage':'leaf_merge','task_ids':list(pair),'error':str(exc)})
                reporter.update(done,len(work),unit='对候选叶子',failed=sum(e['status']=='error' for e in outcomes.values()))
        for pair,_,_ in work:
            record['pairs'].append(outcomes[pair])
        record['logical_requests']=sum(entry.get('logical_requests',0) for entry in outcomes.values())
        structural=[entry for entry in record['pairs'] if entry['status']=='verified' and entry['proposal']['decision']=='common_parent']
        accepted=[];used=set();max_merges=max(1,int(options.get('leaf_merge_max_merges',16)))
        for entry in record['pairs']:
            pair=entry['task_ids']
            if entry['status']!='verified' or entry in structural:continue
            if used.intersection(pair) or len(accepted)>=max_merges:
                entry['status']='deferred_overlap'
                continue
            used.update(pair);accepted.append(entry)
        selected_structural=[]
        for entry in structural:
            if used.intersection(entry['task_ids']) or len(accepted)+len(selected_structural)>=max_merges:
                entry['status']='deferred_overlap';continue
            used.update(entry['task_ids']);selected_structural.append(entry)
        structural=selected_structural
        if not accepted:
            from intent_engine.atomic.semantic_parent import apply_common_parents
            record['parent_applied']=apply_common_parents(data,structural,persist)
            record['status']='completed_with_errors' if any(e['status'] in {'error','parent_rejected'} for e in record['pairs']) else 'completed'
            if record['status']=='completed' and not record['deferred_candidates'] and not any(e['status']=='deferred_overlap' for e in record['pairs']):
                history['completed_identity']=completion_identity(cfg,data,llm,options,conflicts)
            data['leaf_merging']=history
            persist()
            return record,vectors
        history.pop('completed_identity',None)
        staged=copy.deepcopy(data)
        target_clusters={c['cluster_id']:c for c in staged['clusters']}
        aliases=staged.setdefault('task_aliases',{})
        definitions={}
        for entry in accepted:
            left,right=entry['task_ids']
            canonical=min((left,right),key=lambda key:(-len(grouped[leaves[key]]),key))
            removed=right if canonical==left else left
            keep_topic,drop_topic=leaves[canonical],leaves[removed]
            proposal=entry['proposal'];combined=[]
            for atom in staged['intents']:
                if atom.get('status')=='valid' and atom.get('cluster_id') in {keep_topic,drop_topic}:
                    atom['cluster_id']=keep_topic;atom.pop('topic_path',None);combined.append(atom)
            cluster=target_clusters[keep_topic]
            cluster.update(sample_count=len(combined),unique_text_count=len({a['text'] for a in combined}),
                review_samples=review_sample(combined,vectors,limit),review_source='leaf_merge',
                merged_task_ids=sorted(set(cluster.get('merged_task_ids',[]))|set(target_clusters[drop_topic].get('merged_task_ids',[]))|{left,right}),
                review={'reason':entry['validation']['reason'],'object':proposal['object'],'action':proposal['action'],
                        'verdict':'consistent','semantic_clear':True,'boundary_ids':[],'split_groups':[]})
            cluster['reviewed_ids']=[s['intent_id'] for s in cluster['review_samples']]
            cluster['review_scope']='sampled' if cluster['unique_text_count']>limit else 'all_unique_texts'
            for key in ('review_error','review_input_fingerprint','review_result_fingerprint'):
                cluster.pop(key,None)
            staged['clusters']=[c for c in staged['clusters'] if c['cluster_id']!=drop_topic]
            staged['cluster_task_ids'].pop(str(drop_topic),None)
            staged['cluster_task_ids'][str(keep_topic)]=canonical
            aliases[removed]=canonical
            for key,value in list(aliases.items()):
                if value==removed:aliases[key]=canonical
            definitions[keep_topic]=(proposal,canonical)
            entry.update(canonical_task_id=canonical,removed_task_id=removed,canonical_cluster_id=keep_topic,removed_cluster_id=drop_topic)
        # Rebuild the native tree from the actual merged memberships. A failure
        # here leaves the original workflow and all assignments untouched.
        labels=[-1]*len(texts)
        for atom in staged['intents']:
            if atom.get('status')=='valid':labels[indices[atom['text']]]=atom['cluster_id']
            atom.pop('topic_path',None)
        keywords,tree=rebuild_hierarchy(texts,vectors,labels,options)
        from intent_engine.atomic.review_reuse import remember_review
        from intent_engine.atomic.hierarchy_review import LEAF_PROMPT
        naming_settings=model_identity(llm,LEAF_PROMPT,1000)
        old_tree=copy.deepcopy(data['topic_hierarchy'])
        for node in tree['nodes']:
            if node.get('is_leaf') and node['topic_id'] in definitions:
                topic=node['topic_id'];proposal,canonical=definitions[topic];cluster=target_clusters[topic]
                cluster['keywords']=keywords.get(topic,[])
                review={key:cluster['review'][key] for key in ('reason','object','action','semantic_clear','verdict')}
                identity={'version':3,'texts':sorted({a['text'] for a in staged['intents'] if a.get('cluster_id')==topic and a.get('status')=='valid'}),
                          'review':review,'samples':sorted({s['text'] for s in cluster['review_samples']}),'settings':naming_settings}
                if len(identity['texts'])==1:
                    identity['review']={key:review[key] for key in ('object','action','semantic_clear')}
                node.update(label=proposal['label'],description=proposal['description'],review_reason=cluster['review']['reason'],
                            semantic_status='approved',semantic_clear=True,semantic_issue='none',leaf_admission_version=1,
                            task_id=canonical,naming_fingerprint=digest(identity))
                node['naming_result_fingerprint']=digest({key:node[key] for key in ('label','description','review_reason')})
                old_tree['nodes'].append(copy.deepcopy(node))
                remember_review(cluster,[a for a in staged['intents'] if a.get('status')=='valid' and a.get('cluster_id')==topic],llm,options)
        # Replace old leaf entries with the independently verified merged definition.
        old_tree['nodes']=[n for n in old_tree['nodes'] if n.get('topic_id') not in definitions or n.get('naming_fingerprint') in {node.get('naming_fingerprint') for node in tree['nodes'] if node.get('topic_id') in definitions}]
        staged['bertopic_hierarchy']=tree;staged['topic_hierarchy']=tree
        staged['neighbor_pairs']=[];staged['pair_reviews']=[]
        review_hierarchy(staged,llm,workers,reporter,lambda:None,previous_hierarchy=old_tree,cfg=cfg)
        before=Taxonomy.model_validate(data['candidate_taxonomy']);after=Taxonomy.model_validate(staged['candidate_taxonomy'])
        if not after.tasks:raise ValueError('归并重建后没有可用叶子')
        if fingerprint(before)!=fingerprint(after):staged['candidate_taxonomy']['version']=before.version+1
        for entry in accepted:entry['status']='applied'
        record['applied']=len(accepted)
        record['status']='completed_with_errors' if any(e['status']=='error' for e in record['pairs']) or staged.get('hierarchy_review',{}).get('status')=='completed_with_errors' else 'completed'
        from intent_engine.atomic.semantic_parent import apply_common_parents
        record['parent_applied']=apply_common_parents(staged,structural,lambda:None)
        staged['leaf_merging']=copy.deepcopy(history)
        before_publish=copy.deepcopy(data)
        data.clear();data.update(staged)
        try:
            persist()
        except Exception:
            data.clear();data.update(before_publish)
            data['leaf_merging']=history
            raise
        return record,vectors
    except Exception as exc:
        record.update(status='failed',error=str(exc),applied=0)
        for entry in record['pairs']:
            if entry['status'] in {'verified','applied'}:entry['status']='rolled_back'
        data['errors'].append({'stage':'leaf_merge','error':str(exc)})
        return record,vectors
