import copy

import pytest

from intent_engine.atomic.review_reuse import remember_review, review_cache, reuse_review


class LLM:
    cfg = {'model':'test','temperature':.1}


def fixture():
    members = [{'intent_id':str(i),'text':text,'status':'valid','cluster_id':1} for i,text in enumerate(['pay bill','settle bill','pay invoice'])]
    cluster = {'cluster_id':1,'review_samples':copy.deepcopy(members[:2]),'review':{
        'verdict':'consistent','object':'bill','action':'pay','reason':'same action',
        'boundary_ids':['1'],'split_groups':[{'label':'pay','evidence_ids':['0','1']}]}}
    remember_review(cluster,members,LLM(),{})
    return cluster,members


def test_reuse_remaps_evidence_and_ignores_ids_order_duplicates_and_cluster_number():
    old,members=fixture()
    cache=review_cache([old],members,LLM(),{})
    current=copy.deepcopy(old)
    current['cluster_id']=9
    current['review']=None
    current['review_samples'].reverse()
    for sample in current['review_samples']:
        sample['intent_id']='new-'+sample['intent_id']
    new_members=[dict(m,cluster_id=9,intent_id='new-'+m['intent_id']) for m in reversed(members)]
    new_members.append(dict(new_members[0],intent_id='duplicate'))
    assert reuse_review(current,new_members,cache,LLM(),{})
    assert current['review_source']=='reused_review'
    assert current['review']['boundary_ids']==['new-1']
    assert current['review']['split_groups'][0]['evidence_ids']==['new-0','new-1']
    assert current['review']['object']==old['review']['object']
    assert old['review']['boundary_ids']==['1']


@pytest.mark.parametrize('change',['full_members','samples','model','prompt','limit','review_changed','missing_fingerprint','uncertain','error'])
def test_review_reuse_invalidates_semantic_input_and_untrusted_results(change):
    old,members=fixture();llm=LLM();llm.cfg=copy.deepcopy(LLM.cfg);options={}
    current=copy.deepcopy(old);current['review']=None
    if change=='full_members':
        members.append({'text':'unsampled different intent','status':'valid','cluster_id':1,'intent_id':'extra'})
    elif change=='samples':current['review_samples']=copy.deepcopy(members[1:])
    elif change=='model':llm.cfg['model']='changed'
    elif change=='prompt':llm.cfg['prompt_overrides']={'atomic_discovery.REVIEW_PROMPT':'changed prompt'}
    elif change=='limit':options['review_examples']=10
    elif change=='review_changed':old['review']['object']='modified'
    elif change=='missing_fingerprint':old.pop('review_input_fingerprint')
    elif change=='uncertain':old['review']['verdict']='uncertain'
    elif change=='error':old['review_error']='failure'
    cache=review_cache([old],members,llm,options)
    assert not reuse_review(current,members,cache,llm,options)
