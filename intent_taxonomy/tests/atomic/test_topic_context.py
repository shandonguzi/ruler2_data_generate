from intent_engine.atomic.topic_context import topic_context, pair_review_payload


def test_current_parent_fallback_and_missing_path():
    data={'topic_hierarchy':{'nodes':[
        {'node_id':'p','label':'交通','is_leaf':False,'children':['l']},
        {'node_id':'l','topic_id':2,'label':'航班查询','is_leaf':True,'children':[]}]}}
    current=topic_context(data,2)
    assert [n['label'] for n in current['path']]==['交通','航班查询']
    assert current['source']=='current_cluster'
    assert topic_context(data,3,2)['source']=='parent_cluster_before_split'
    assert topic_context({},3)=={'source':'unavailable','path':[]}
    payload=pair_review_payload(data,[{'intent_id':'a','text':'q','span':'q','raw_text':'q'}],[],2,3)
    assert set(payload['left'][0])=={'intent_id','text'}
    assert 'hierarchy_context' not in payload
    assert payload['left_topic_context']['path'][-1]['node_id']=='l'


def test_verification_only_includes_selected_path_ancestor():
    import pytest
    from intent_engine.atomic.topic_context import merge_verification_payload
    data={'topic_hierarchy':{'nodes':[
        {'node_id':'p','label':'主题','description':'父级定义','is_leaf':False,'semantic_status':'approved','children':['l']},
        {'node_id':'l','topic_id':2,'label':'叶子','is_leaf':True,'children':[]},
        {'node_id':'unrelated','label':'无关主题','description':'不应传入','is_leaf':False,'semantic_status':'approved','children':[]}]}}
    side={'members':[{'text':'问题'}],'topic_context':topic_context(data,2)}
    payload={'left':side,'right':side}
    verified=merge_verification_payload(data,payload,{'decision':'common_parent','parent_node_id':'p'})
    assert verified['selected_ancestor']['description']=='父级定义'
    assert 'unrelated' not in str(verified) and 'existing_parents' not in verified
    with pytest.raises(ValueError):merge_verification_payload(data,payload,{'decision':'common_parent','parent_node_id':'unrelated'})
