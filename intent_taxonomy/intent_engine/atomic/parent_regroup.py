"""Bounded semantic repair of an audited topic forest."""
import copy
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Literal

import numpy as np
from pydantic import BaseModel, Field
from intent_engine.atomic.review_reuse import digest, model_identity
from intent_engine.settings.prompt_settings import prompt_ref

PROPOSE = prompt_ref('parent_regroup.PROPOSE')
VERIFY = prompt_ref('parent_regroup.VERIFY')


class Proposal(BaseModel):
    reason: str
    label: str = ''
    description: str = ''
    target_id: str | None = None
    operation: Literal['attach', 'create_parent', 'keep_independent']


class Verification(BaseModel):
    reason: str
    approved: bool


def regroup(data, cfg, llm, workers):
    from intent_engine.atomic.atomic_discovery import SemanticEmbeddings, embedding_settings
    from intent_engine.atomic.topic_hierarchy import index_topic_tree
    options={**cfg.get('atomic_discovery',{}),**data.get('parameters',{})}
    if cfg.get('atomic_discovery',{}).get('flat_topics',options.get('flat_topics',False)) or not options.get('parent_regroup_enabled',False):return
    history=data.setdefault('parent_regroup',{'cache':{},'passes':[]})
    record={'status':'running','proposals':[],'applied':0}
    history['passes'].append(record)
    try:
        for wave in range(max(1,min(2,int(options.get('parent_regroup_rounds',2))))):
            tree=data['topic_hierarchy'];nodes={n['node_id']:n for n in tree['nodes']}
            roots=[k for k in tree['root_ids'] if nodes[k].get('semantic_status')=='approved' and len(nodes[k]['children'])<=16]
            if not roots:break
            ids=sorted(k for k,n in nodes.items() if n.get('semantic_status')=='approved' and len(n['children'])<=16)
            def summary(k):
                n=nodes[k]
                return {'node_id':k,'label':n['label'],'description':n.get('description',''),
                        'children':[{'node_id':c,'label':nodes[c]['label'],'description':nodes[c].get('description','')} for c in n['children'][:16]]}
            summaries={k:summary(k) for k in ids}
            matrix=SemanticEmbeddings(embedding_settings(cfg),Path(cfg['paths']['checkpoint_dir'])/'parent_embeddings').encode(
                [json.dumps(summaries[k],ensure_ascii=False) for k in ids],_SilentProgress())
            matrix=np.asarray(matrix);matrix=matrix/np.maximum(np.linalg.norm(matrix,axis=1,keepdims=True),1e-12)
            scores=matrix@matrix.T;positions={k:i for i,k in enumerate(ids)};work=[]
            for root in roots:
                descendants=set()
                def visit(k):
                    descendants.add(k)
                    for child in nodes[k]['children']:visit(child)
                visit(root)
                ranked=[ids[j] for j in np.argsort(-scores[positions[root]],kind='stable') if ids[j] not in descendants]
                parents=[k for k in ranked if not nodes[k]['is_leaf']][:3]
                peers=[k for k in ranked if k in roots][:5]
                candidates=list(dict.fromkeys(parents+peers))
                if not candidates:continue
                payload={'branch':summaries[root],'existing_parents':[summaries[k] for k in parents], 'peer_roots':[summaries[k] for k in peers]}
                key=digest({'payload':payload,'proposal':model_identity(llm,PROPOSE,1400),'verify':model_identity(llm,VERIFY,1000)})
                work.append((root,candidates,payload,key))
            budget=max(1,int(options.get('parent_regroup_max_requests',32)))
            selected=[];remaining=budget
            for item in work:
                if item[3] in history['cache']:selected.append(item)
                elif remaining>=2:selected.append(item);remaining-=2
            def audit(item):
                root,candidates,payload,key=item
                if key in history['cache']:return root,copy.deepcopy(history['cache'][key]),True
                try:
                    p=llm.chat_json(PROPOSE,json.dumps(payload,ensure_ascii=False),schema=Proposal,max_tokens=1400)
                    entry=p.model_dump()
                    if p.operation!='keep_independent':
                        if p.target_id not in candidates:raise ValueError('目标不在候选中')
                        if p.operation=='attach' and p.target_id not in [n['node_id'] for n in payload['existing_parents']]:raise ValueError('挂载目标不是父主题')
                        if p.operation=='create_parent' and (p.target_id not in roots or not p.label.strip() or not p.description.strip()):raise ValueError('新父主题缺少有效分支或定义')
                        v=llm.chat_json(VERIFY,json.dumps({'input':payload,'proposal':entry},ensure_ascii=False),schema=Verification,max_tokens=1000)
                        entry['verification']=v.model_dump()
                    return root,entry,False
                except Exception as e:return root,{'operation':'error','reason':str(e)},False
            with ThreadPoolExecutor(max_workers=workers) as pool:outcomes=list(pool.map(audit,selected))
            staged=copy.deepcopy(tree);byid={n['node_id']:n for n in staged['nodes']};used=set();applied=0
            for item,(root,entry,cached) in zip(selected,outcomes):
                if entry['operation']!='error':history['cache'][item[3]]=copy.deepcopy(entry)
                log={'wave':wave+1,'root':root,'cached':cached,**entry};record['proposals'].append(log)
                target=entry.get('target_id')
                if not entry.get('verification',{}).get('approved'):continue
                target_root=target
                while nodes[target_root].get('parent_id'):
                    target_root=nodes[target_root]['parent_id']
                if root in used or target in used or target_root in used:log['deferred']=True;continue
                if entry['operation']=='attach':byid[target]['children'].append(root);staged['root_ids'].remove(root)
                else:
                    key='regroup:'+digest({'children':sorted([root,target]),'label':entry['label']})[:20]
                    parent=dict(node_id=key,label=entry['label'],description=entry['description'],children=[root,target],is_leaf=False,semantic_status='approved',keywords=[],distance=0.0)
                    staged['nodes'].append(parent);byid[key]=parent
                    staged['root_ids']=[k for k in staged['root_ids'] if k not in {root,target}]+[key]
                used.update((root,target,target_root));applied+=1;log['applied']=True
            if not applied:break
            index_topic_tree(staged)
            staged['status']='ready' if len(staged['root_ids'])==1 else 'forest'
            data['topic_hierarchy']=staged;record['applied']+=applied
        record['status']='completed_with_errors' if any(p['operation']=='error' for p in record['proposals']) else 'completed'
    except Exception as e:record.update(status='failed',error=str(e))


class _SilentProgress:
    def update(self,*args,**kwargs):pass
