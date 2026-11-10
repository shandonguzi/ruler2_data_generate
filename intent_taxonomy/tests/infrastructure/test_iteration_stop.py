import pytest
from intent_engine.infrastructure.iteration_stop import parse_rounds,resolve_rounds,cluster_counts,counts_stable


def test_auto_counts_use_members_not_ids_and_ignore_invalid():
    atoms=[{'status':'valid','cluster_id':n} for n in [0,1,1,-1,-1]]+[{'status':'invalid','cluster_id':9}]
    current=cluster_counts(atoms)
    assert current=={'singleton_clusters':1,'noise_intents':2}
    assert not counts_stable(None,current)
    assert counts_stable({'singleton_clusters':1,'noise_intents':2},current)
    assert not counts_stable({'singleton_clusters':1,'noise_intents':1},current)


def test_round_limits_and_auto_safety_cap():
    assert parse_rounds('auto')==-1
    assert resolve_rounds(-1,{})==(30,True)
    assert resolve_rounds('auto',{'iteration_auto_max_rounds':8})==(8,True)
    assert resolve_rounds(3,{})==(3,False)
    for value in ['invalid',11,-2]:
        with pytest.raises(ValueError):parse_rounds(value)
    with pytest.raises(ValueError):resolve_rounds('auto',{'iteration_auto_max_rounds':101})
