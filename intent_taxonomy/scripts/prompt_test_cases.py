"""Small, hand-curated fixtures. No live model calls on import."""
import copy


def cases():
    result=[]
    def add(key,name,payload,expected,focus):
        result.append(dict(id=f'{key}:{name}',prompt=key,name=name,input=copy.deepcopy(payload),expected=expected,focus=focus,source='人工挑选的英文代表性测试，不是实际运行结果'))
    members=[{'intent_id':'a','text':'What is the official religion of Jordan?'},{'intent_id':'b','text':'What is the official religion of Morocco?'}]
    mixed=[{'intent_id':'a','text':'What material is a jambiya made from?'},{'intent_id':'b','text':'How is a jambiya made?'}]
    context={'source':'current_cluster','path':[{'label':'文化知识','is_leaf':False},{'label':'宗教知识','is_leaf':True}]}
    review=lambda m:{'cluster_id':0,'sample_count':len(m),'unique_text_count':len(m),'keywords':[],'review_scope':'all_unique_texts','topic_context':context,'members':m}
    key='atomic_discovery.EXTRACT_PROMPT'
    add(key,'single',{'user_text':members[0]['text']},{'status':['valid'],'intent_count':1},'单意图不补造额外动作')
    add(key,'multi',{'user_text':'Open Alipay and pay my electricity bill.'},{'status':['valid'],'intent_count':2},'显式打开与缴费分别保留')
    key='atomic_discovery.REVIEW_PROMPT'
    add(key,'same',review(members),{'verdict':['consistent']},'同类请求可统一')
    add(key,'mixed',review(mixed),{'verdict':['mixed']},'观察模型是否区分材料与过程')
    key='atomic_discovery.PAIR_PROMPT'
    for name,other,decision in [('same',[members[1]],'merge'),('different',[mixed[1]],'keep')]:
        left=[members[0]] if name=='same' else [mixed[0]]
        add(key,name,{'left':left,'right':other,'left_topic_context':context,'right_topic_context':context},{'decision':[decision]},'检查证据引用和跨簇关系')
    key='cluster_refinement.SPLIT_PROMPT'
    for name,m,decision in [('retain',members,'keep'),('split',mixed,'split')]:
        add(key,name,{**review(m),'review':{'verdict':'mixed','reason':'之前建议分开，现独立复核'},'candidate_groups':[]},{'decision':[decision]},'允许推翻旧mixed结论，不强制拆分')
    key='hierarchy_review.LEAF_PROMPT'
    add(key,'singleton',{'kind':'leaf','node_id':'topic:0','review':{'object':'国家官方宗教','action':'查询','semantic_clear':True,'verdict':'consistent','reason':'语义明确'},'members':[{'text':members[0]['text']}]},{'semantic_clear':[True]},'单样本语义明确仍可命名')
    add(key,'missing_context',{'kind':'leaf','node_id':'topic:0','review':{},'members':[{'text':'What about that one?'}]},{'semantic_clear':[False]},'缺少上下文不可猜测')
    children=[{'node_id':'a','label':'弯刀材料查询','description':'查询制作材料','reason':'已审核'}, {'node_id':'b','label':'弯刀制作方法','description':'查询制作过程','reason':'已审核'}]
    key='hierarchy_review.NODE_PROMPT'
    add(key,'related',{'kind':'parent','node_id':'p','children':children},{'decision':['group']},'共用父级，不合并叶子')
    add(key,'unrelated',{'kind':'parent','node_id':'p','children':[children[0],{'node_id':'b','label':'航班改签','description':'更改航班行程','reason':'已审核'}]},{'decision':['separate']},'避免空泛共同父级')
    def pair(left,right):
        return {'left':{'task_id':'a','label':'已有类别A','description':'以真实样本为准','members':[{'text':left}],'topic_context':context},'right':{'task_id':'b','label':'已有类别B','description':'以真实样本为准','members':[{'text':right}],'topic_context':context},'conflict_texts':[]}
    key='leaf_merge.MERGE_PROMPT'
    add(key,'normalize',pair('What material is this garment made from?','Which materials are used to make this garment?'),{'decision':['normalize','merge']},'近义描述统一')
    add(key,'ancestor',pair(mixed[0]['text'],mixed[1]['text']),{'decision':['common_parent']},'相关但不同的叶子共用祖先')
    key='leaf_merge.VERIFY_PROMPT'
    add(key,'valid_parent',{'definition':{'decision':'common_parent','label':'弯刀制作知识','description':'弯刀制作涉及的材料和过程'},'left':[{'text':mixed[0]['text']}],'right':[{'text':mixed[1]['text']}]},{'valid':[True]},'复核共同祖先覆盖双方')
    add(key,'wrong_merge',{'definition':{'decision':'merge','label':'材料查询','description':'只查询材料','object':'材料','action':'查询'},'left':[{'text':mixed[0]['text']}],'right':[{'text':mixed[1]['text']}]},{'valid':[False]},'拒绝不能覆盖制作过程的统一叶子')
    branch={'node_id':'leaf','label':'弯刀材料查询','description':'查询弯刀材料','children':[]}
    parent={'node_id':'p','label':'弯刀制作知识','description':'弯刀制作的材料和过程','children':[children[1]]}
    key='parent_regroup.PROPOSE'
    add(key,'attach',{'branch':branch,'existing_parents':[parent],'peer_roots':[]},{'operation':['attach']},'复用已有父主题')
    add(key,'independent',{'branch':branch,'existing_parents':[{'node_id':'p','label':'航空票务','description':'机票业务','children':[]}],'peer_roots':[]},{'operation':['keep_independent']},'无合理祖先可保持独立')
    key='parent_regroup.VERIFY'
    for name,p,approved in [('valid',parent,True),('invalid',{'node_id':'p','label':'航空票务','description':'机票业务','children':[]},False)]:
        add(key,name,{'input':{'branch':branch,'existing_parents':[p],'peer_roots':[]},'proposal':{'reason':'挂入该父级','operation':'attach','target_id':'p','label':'','description':''}},{'approved':[approved]},'独立审核挂载，不能盲信提议')
    key='classifier.ATOMIC_CLASSIFY_SYSTEM'
    add(key,'knowledge',{'atomic_text':members[0]['text'],'slots':{}},{'nonempty':['object','action']},'知识问答是有效语义')
    add(key,'operation',{'atomic_text':'Set an alarm for 7 AM.','slots':{}},{'nonempty':['object','action']},'解析动作并保留时间')
    task={'task_id':'t','domain':'宗教知识','object':'国家官方宗教','action':'查询','description':'查询国家官方宗教','positive_examples':[members[0]['text']]}
    atomic={'object':'国家官方宗教','action':'查询','span':members[0]['text'],'slots':{},'confidence':1}
    key='classifier.PAIRWISE_SYSTEM'
    add(key,'match',{'intent_text':members[0]['text'],'atomic_intent':atomic,'candidate_task':task},{'match':[True]},'候选任务匹配')
    other={**task,'object':'飞机票','action':'购买','description':'购买机票'}
    add(key,'reject',{'intent_text':members[0]['text'],'atomic_intent':atomic,'candidate_task':other},{'match':[False]},'不强行匹配无关任务')
    key='classifier.FINAL_MATCH_SYSTEM'
    add(key,'known',{'intent_text':members[0]['text'],'atomic_intent':atomic,'shortlist':[{'task':task,'pairwise_match':True,'pairwise_confidence':.99,'pairwise_reason':'同类请求'}]},{'status':['known'],'selected_task_id':['t']},'只选候选中的任务')
    add(key,'uncovered',{'intent_text':'Book a flight.','atomic_intent':{'object':'机票','action':'预订','span':'Book a flight.','slots':{},'confidence':1},'shortlist':[]},{'status':['discovery'],'selected_task_id':[None]},'无候选时返回未覆盖')
    from intent_engine.workflows.scene_mapping import load_scenes
    scenes=load_scenes()
    key='scene_mapping.MAP_PROMPT'
    add(key,'knowledge',{'atomic_intent':members[0]['text'],'scenes':scenes},{'scene_from_catalog':True},'完整36项场景中选真实ID')
    add(key,'alarm',{'atomic_intent':'Set an alarm for 7 AM.','scenes':scenes},{'scene_from_catalog':True},'操作类场景匹配，人工核对具体选择')
    key='scene_mapping.NAME_PROMPT'
    for name,text in [('knowledge',members[0]['text']),('alarm','Set an alarm for 7 AM.')]:
        add(key,name,{'atomic_intent':text},{'nonempty':['task_name']},'只生成任务名称，不重新选择S4')
    skill={'skill_id':'alarm','name':'闹钟','description':'设置闹钟','routing_priority':100,'include_rules':['设置或取消闹钟'],'exclude_rules':['知识问答'],'examples':['Set an alarm for 7 AM.'],'counterexamples':[members[0]['text']]}
    for key,field in [('skill_router.JUDGE_SYSTEM','candidate_skills'),('skill_router.FALLBACK_SYSTEM','skills')]:
        for name,text,expected in [('match','Set an alarm for 7 AM.','alarm'),('reject',members[0]['text'],'')]:
            add(key,name,{'atomic_intent':{'raw_text':text,'span':text,'domain':'','object':'闹钟' if name=='match' else '国家官方宗教','action':'设置' if name=='match' else '查询','slots':{},'matched_task_id':None},'routing_policy':{},field:[skill]},{'primary_skill_id':[expected]},'可选Skill路由，未启用时也可单测')
    return result


def response_schema(key):
    from intent_engine.atomic.atomic_discovery import AtomicExtraction,ClusterReview,PairReview
    from intent_engine.atomic.cluster_refinement import FullPartition
    from intent_engine.atomic.hierarchy_review import LeafNamingDecision,NodeDecision
    from intent_engine.atomic.leaf_merge import MergeProposal,MergeValidation
    from intent_engine.atomic.parent_regroup import Proposal,Verification
    from intent_engine.models import PerIntentSemanticDecision,PairwiseTaskDecision,FinalTaskDecision
    from intent_engine.workflows.atomic_tasks import TaskName
    from intent_engine.workflows.scene_mapping import SceneChoice
    mapping=dict(zip(['atomic_discovery.EXTRACT_PROMPT','atomic_discovery.REVIEW_PROMPT','atomic_discovery.PAIR_PROMPT','cluster_refinement.SPLIT_PROMPT','hierarchy_review.LEAF_PROMPT','hierarchy_review.NODE_PROMPT','leaf_merge.MERGE_PROMPT','leaf_merge.VERIFY_PROMPT','parent_regroup.PROPOSE','parent_regroup.VERIFY','classifier.ATOMIC_CLASSIFY_SYSTEM','classifier.PAIRWISE_SYSTEM','classifier.FINAL_MATCH_SYSTEM','scene_mapping.MAP_PROMPT','scene_mapping.NAME_PROMPT'],[AtomicExtraction,ClusterReview,PairReview,FullPartition,LeafNamingDecision,NodeDecision,MergeProposal,MergeValidation,Proposal,Verification,PerIntentSemanticDecision,PairwiseTaskDecision,FinalTaskDecision,SceneChoice,TaskName]))
    return mapping.get(key)


def check_response(case,response):
    from intent_engine.atomic.atomic_discovery import check_evidence
    from intent_engine.atomic.cluster_refinement import validate_partition
    schema=response_schema(case['prompt']);parsed=schema.model_validate(response) if schema else None
    data=parsed.model_dump() if parsed else response
    if not isinstance(data,dict):raise ValueError('响应必须为JSON对象')
    key=case['prompt'];payload=case['input']
    if key=='atomic_discovery.REVIEW_PROMPT':check_evidence(parsed,{m['intent_id'] for m in payload['members']})
    if key=='cluster_refinement.SPLIT_PROMPT':validate_partition(parsed,payload['members'])
    if key=='atomic_discovery.PAIR_PROMPT':
        for side in ['left','right']:
            if not set(data[side+'_evidence_ids'])<={m['intent_id'] for m in payload[side]}:raise ValueError('引用未提供证据')
    if key=='classifier.FINAL_MATCH_SYSTEM' and data.get('selected_task_id') is not None:
        if data['selected_task_id'] not in {s['task']['task_id'] for s in payload['shortlist']}:raise ValueError('选择非候选任务')
    if key.startswith('skill_router.'):
        allowed={s['skill_id'] for s in payload.get('candidate_skills',payload.get('skills',[]))}
        if data.get('primary_skill_id') not in allowed|{''}:raise ValueError('选择未提供Skill')
    if key=='scene_mapping.MAP_PROMPT' and data['scene_id'] not in {s['scene_id'] for s in payload['scenes']}:raise ValueError('场景ID不在输入目录')
    if key=='leaf_merge.VERIFY_PROMPT' and not set(data['rejected_texts'])<={m['text'] for side in ['left','right'] for m in payload[side]}:raise ValueError('拒绝文本不在输入样本中')
    failures=[]
    for field,expected in case['expected'].items():
        if field=='intent_count':passed=len(data.get('intents',[]))==expected
        elif field=='nonempty':passed=all(str(data.get(k,'')).strip() for k in expected)
        elif field=='scene_from_catalog':passed=data.get('scene_id') in {s['scene_id'] for s in payload['scenes']}
        else:passed=data.get(field) in expected
        if not passed:failures.append(field)
    return data,failures
