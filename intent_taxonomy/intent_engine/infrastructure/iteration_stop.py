"""Automatic iteration stops compare counts, not cluster IDs or semantic accuracy."""
from collections import Counter


def parse_rounds(value):
    if value=='auto':return -1
    try:count=int(value)
    except (ValueError,TypeError):raise ValueError('迭代轮数应为 auto 或 0–10 的整数') from None
    if count not in {-1,*range(11)}:raise ValueError('迭代轮数应为 auto 或 0–10 的整数')
    return count


def resolve_rounds(value,options):
    count=parse_rounds(value)
    if count!=-1:return count,False
    limit=int(options.get('iteration_auto_max_rounds',30))
    if not 1<=limit<=100:raise ValueError('自动迭代安全上限需要为1–100轮')
    return limit,True


def cluster_counts(intents):
    members=Counter(a.get('cluster_id') for a in intents if a.get('status','valid')=='valid' and isinstance(a.get('cluster_id'),int))
    return {'singleton_clusters':sum(n==1 for cluster,n in members.items() if cluster>=0),
            'noise_intents':members.get(-1,0)}


def counts_stable(previous,current):
    return previous is not None and all(previous.get(k)==current[k] for k in ('singleton_clusters','noise_intents'))
