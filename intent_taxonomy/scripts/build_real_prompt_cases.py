"""Offline fixture selection from two saved runs. Never invokes a model."""
import copy
import hashlib
import json
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
RUNS=['20261104_222248_4b638fba','20261105_195042_057d470e']


def build():
    from intent_engine.atomic.topic_context import topic_context,merge_verification_payload
    from intent_engine.workflows.scene_mapping import load_scenes
    selected={}
    fields={'atomic_discovery.EXTRACT_PROMPT':'status','atomic_discovery.REVIEW_PROMPT':'verdict',
        'atomic_discovery.PAIR_PROMPT':'decision','hierarchy_review.LEAF_PROMPT':'semantic_clear',
        'hierarchy_review.NODE_PROMPT':'decision','classifier.PAIRWISE_SYSTEM':'match',
        'classifier.FINAL_MATCH_SYSTEM':'status','cluster_refinement.SPLIT_PROMPT':'groups'}
    for run in reversed(RUNS):
        trace=ROOT/'data'/'reports'/run/'requests'/'requests.jsonl'
        for number,line in enumerate(trace.open(encoding='utf-8'),1):
            record=json.loads(line);key=record.get('prompt_key');parsed=record.get('parsed')
            if key not in fields or record.get('status')!='success' or not isinstance(parsed,dict):continue
            try:payload=json.loads(next(m['content'] for m in reversed(record['input']['messages']) if m['role']=='user'))
            except (KeyError,ValueError,StopIteration):continue
            if len(payload.get('members',[]))>8 or len(payload.get('children',[]))>8:continue
            if key=='atomic_discovery.EXTRACT_PROMPT':
                if parsed.get('status')!='valid':continue
                category='single' if len(parsed.get('intents',[]))==1 else 'multi'
            elif key=='cluster_refinement.SPLIT_PROMPT':
                if len(payload.get('members',[]))<2:continue
                category='two_members' if len(payload['members'])==2 else 'small_cluster'
            else:category=str(parsed.get(fields[key]))
            wanted={'atomic_discovery.EXTRACT_PROMPT':['single','multi'],
                'atomic_discovery.REVIEW_PROMPT':['consistent','mixed'],
                'atomic_discovery.PAIR_PROMPT':['merge','keep'],
                'hierarchy_review.LEAF_PROMPT':['True','False'],
                'hierarchy_review.NODE_PROMPT':['group','separate'],
                'classifier.PAIRWISE_SYSTEM':['True','False'],
                'classifier.FINAL_MATCH_SYSTEM':['known','ambiguous'],
                'cluster_refinement.SPLIT_PROMPT':['two_members','small_cluster']}[key]
            if category not in wanted or (key,category) in selected:continue
            selected[key,category]={'id':key+':real_'+category,'prompt':key,'name':'real_'+category,
                'input':payload,'expected':{},'manual_review':True,'focus':'复核真实请求，历史响应仅作对照，不作正确答案',
                'source':{'kind':'historical_request','run_id':run,'file':str(trace.relative_to(ROOT)),
                          'line':number,'request_id':record.get('request_id'),'input_sha256':hashlib.sha256(json.dumps(payload,sort_keys=True,ensure_ascii=False).encode()).hexdigest()},
                'historical_response':parsed}
    data=json.loads((ROOT/'data/reports'/RUNS[1]/'process/discovery.json').read_text())
    old=json.loads((ROOT/'data/reports'/RUNS[0]/'process/discovery.json').read_text())
    atoms={a['intent_id']:a for a in data['intents']};oldatoms={a['intent_id']:a for a in old['intents']}
    nodes={n['node_id']:n for n in data['topic_hierarchy']['nodes']}
    leaves={n['topic_id']:n for n in nodes.values() if n['is_leaf']}
    report='data/reports/'+RUNS[1]+'/process/discovery.json'
    fixtures=list(selected.values())
    def add(key,name,payload,ids,focus):
        fixtures.append({'id':key+':real_'+name,'prompt':key,'name':'real_'+name,'input':copy.deepcopy(payload),
            'expected':{},'manual_review':True,'focus':focus,
            'source':{'kind':'assembled_from_real_results','run_id':RUNS[1],'file':report,'intent_ids':ids,
            'origins':[{'intent_id':i,'row_id':atoms[i]['row_id'],'raw_text':atoms[i]['raw_text'],'text':atoms[i]['text'],
                        'previous_cluster_id':oldatoms[i].get('cluster_id'),'cluster_id':atoms[i].get('cluster_id'),
                        'topic_context':topic_context(data,atoms[i].get('cluster_id'))} for i in ids]}})
    def side(i):
        a=atoms[i];n=leaves[a['cluster_id']];c=next(c for c in data['clusters'] if c['cluster_id']==a['cluster_id'])
        # Preserve all actual members, not just the selected query, up to the production sample limit.
        return {'task_id':n.get('task_id'),'label':n['label'],'description':n.get('description',''),
                'object':(c.get('review')or{}).get('object',''),'action':(c.get('review')or{}).get('action',''),
                'topic_context':topic_context(data,a['cluster_id']),
                'members':[{'text':m['text']} for m in c['review_samples'][:8]]}
    pairs=[('religions',['r344:i0','r1411:i0'],'两个国家官方宗教：判断统一叶子或共用祖先，检查重复类别'),
           ('diwaniya',['r21:i0','r21:i1'],'同一句拆出社会角色和主要目的：判断近义重复还是相关叶子')]
    for name,ids,focus in pairs:
        payload={'left':side(ids[0]),'right':side(ids[1]),'conflict_texts':[]}
        add('leaf_merge.MERGE_PROMPT',name,payload,ids,focus)
        # Recheck a real existing shared ancestor, or an actual leaf definition as a merge hypothesis.
        paths=[]
        for i in ids:
            key=leaves[atoms[i]['cluster_id']]['node_id'];path=[]
            while key:
                path.append(key);key=nodes[key].get('parent_id')
            paths.append(path)
        shared=next((k for k in paths[0] if k in paths[1] and not nodes[k]['is_leaf']),None)
        n=nodes[shared] if shared else leaves[atoms[ids[0]]['cluster_id']]
        definition={'decision':'common_parent' if shared else 'merge','label':n['label'],'description':n.get('description',''),
                    'parent_node_id':shared,'object':payload['left']['object'],'action':payload['left']['action']}
        add('leaf_merge.VERIFY_PROMPT',name,merge_verification_payload(data,payload,definition),ids,'真实已有定义作为待审假设，不预设验证通过')
    for name,i in [('singleton','r14:i0'),('material','r27:i0')]:
        a=atoms[i]
        add('classifier.ATOMIC_CLASSIFY_SYSTEM',name,{'atomic_text':a['text'],'slots':{}},[i],'真实原子意图补解析，核对对象动作是否合理')
        add('scene_mapping.NAME_PROMPT',name,{'atomic_intent':a['text']},[i],'真实原子意图生成简短任务名称')
        add('scene_mapping.MAP_PROMPT',name,{'atomic_intent':a['text'],'scenes':load_scenes()},[i],'从真实完整S4目录选择；具体正确性人工核对')
    parents=[n for n in nodes.values() if not n['is_leaf'] and n.get('semantic_status')=='approved']
    for name,i in [('religion','r344:i0'),('diwaniya','r21:i0')]:
        n=leaves[atoms[i]['cluster_id']]
        branch={k:n.get(k) for k in ['node_id','label','description']};branch['children']=[]
        # Choose real candidates by shared labels, not inventing category definitions.
        ranked=sorted(parents,key=lambda p:(-len(set(p['label'])&set(n['label'])),p['node_id']))[:3]
        existing=[{'node_id':p['node_id'],'label':p['label'],'description':p.get('description',''),'children':[
            {'node_id':c,'label':nodes[c]['label'],'description':nodes[c].get('description','')} for c in p['children'][:16]]} for p in ranked]
        payload={'branch':branch,'existing_parents':existing,'peer_roots':[]}
        add('parent_regroup.PROPOSE',name,payload,[i],'真实叶子与真实父主题：判断挂入或保持独立')
        proposal={'operation':'attach','target_id':existing[0]['node_id'],'label':'','description':'','reason':'候选挂载假设，待独立复核'}
        add('parent_regroup.VERIFY',name,{'input':payload,'proposal':proposal},[i],'待审挂载假设，不将候选召回当作正确归属')
    def input_refs(value,ids,texts):
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='intent_id' and isinstance(item,str):ids.add(item)
                if key in {'text','span','user_text','atomic_text','intent_text'} and isinstance(item,str):texts.add(item)
                input_refs(item,ids,texts)
        elif isinstance(value,list):
            for item in value:input_refs(item,ids,texts)
    for case in fixtures:
        if case['source']['kind']!='historical_request':continue
        ids,texts=set(),set();input_refs(case['input'],ids,texts)
        matches=[a for a in atoms.values() if a['intent_id'] in ids or a['text'] in texts or a['raw_text'] in texts]
        case['source']['comparison_run_id']=RUNS[1]
        case['source']['intent_ids']=[a['intent_id'] for a in matches]
        case['source']['comparison_origins']=[{'intent_id':a['intent_id'],'row_id':a['row_id'],'raw_text':a['raw_text'],
            'text':a['text'],'previous_cluster_id':oldatoms[a['intent_id']].get('cluster_id'),
            'cluster_id':a.get('cluster_id'),'topic_context':topic_context(data,a.get('cluster_id'))} for a in matches]
    # Missing leaf negative examples should be reported, never substituted with invented data.
    from intent_engine.atomic.topic_context import pair_review_payload
    for case in fixtures:
        if case['prompt']=='atomic_discovery.PAIR_PROMPT':
            original=case['input'];contexts={}
            for side in ('left','right'):
                ids={m['intent_id'] for m in original[side]}
                topics={atoms[i].get('cluster_id') for i in ids if i in atoms}
                # Historical groups can now span several leaves: do not invent one current path.
                contexts[side]=next(iter(topics)) if len(topics)==1 else None
            case['source']['kind']='projected_historical_request'
            case['input']=pair_review_payload(data,original['left'],original['right'],contexts['left'],contexts['right'])
    fixtures.sort(key=lambda c:(c['prompt'],c['name']))
    return fixtures


if __name__=='__main__':
    fixtures=build();target=ROOT/'tests/fixtures/real_prompt_cases.json'
    target.write_text(json.dumps({'description':'真实请求重放和真实结果组装；预期语义待人工确认','cases':fixtures},ensure_ascii=False,indent=2)+'\n')
    print(f'已离线挑选{len(fixtures)}条；真实模型请求0次；文件：{target}')
