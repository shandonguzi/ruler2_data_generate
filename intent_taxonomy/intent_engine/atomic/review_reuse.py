"""Content identities for reusing successful reviews without regenerating definitions."""
import copy
import hashlib
import json

from intent_engine.settings.prompt_settings import resolve_prompt


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def model_identity(llm, prompt, max_tokens):
    cfg = getattr(llm, 'cfg', {}) or {}
    return {**{key: cfg.get(key) for key in ('base_url','model','extra_body','thinking_mode','thinking_parameter')},
            'temperature': cfg.get('temperature', .1), 'use_json_mode': cfg.get('use_json_mode', True),
            'endpoints':[{k:v for k,v in endpoint.items() if k not in {'api_key'}} for endpoint in cfg.get('endpoints',[])],
            'max_tokens': max_tokens, 'prompt': resolve_prompt(prompt, cfg.get('prompt_overrides', {}))}


def review_identity(cluster, members, llm, options):
    from intent_engine.atomic.atomic_discovery import REVIEW_PROMPT
    return digest({'version':1, 'texts': sorted({m['text'] for m in members}),
                   'samples': sorted({s['text'] for s in cluster['review_samples']}),
                   'sample_limit': max(2, int(options.get('review_examples', 8))),
                   'settings': model_identity(llm, REVIEW_PROMPT, 2400)})


def remember_review(cluster, members, llm, options):
    cluster['review_input_fingerprint'] = review_identity(cluster, members, llm, options)
    cluster['review_result_fingerprint'] = digest(cluster['review'])


def review_cache(clusters, atoms, llm, options):
    grouped = {}
    for atom in atoms:
        if atom.get('status') == 'valid':
            grouped.setdefault(atom.get('cluster_id'), []).append(atom)
    cache = {}
    for cluster in clusters:
        if cluster.get('review_error') or (cluster.get('review') or {}).get('verdict') != 'consistent':
            continue  # Pending or failed decisions remain eligible for another review.
        members = grouped.get(cluster['cluster_id'], [])
        key = cluster.get('review_input_fingerprint')
        if (members and key and key == review_identity(cluster, members, llm, options)
                and cluster.get('review_result_fingerprint') == digest(cluster['review'])):
            cache[key] = cluster
    return cache


def reuse_review(cluster, members, cache, llm, options):
    from intent_engine.atomic.atomic_discovery import ClusterReview, check_evidence
    saved = cache.get(review_identity(cluster, members, llm, options))
    if saved is None:
        return False
    old_text = {s['intent_id']: s['text'] for s in saved['review_samples']}
    new_ids = {s['text']: s['intent_id'] for s in cluster['review_samples']}
    review = copy.deepcopy(saved['review'])
    try:
        review['boundary_ids'] = [new_ids[old_text[key]] for key in review.get('boundary_ids', [])]
        for group in review.get('split_groups', []):
            group['evidence_ids'] = [new_ids[old_text[key]] for key in group.get('evidence_ids', [])]
        check_evidence(ClusterReview.model_validate(review), set(new_ids.values()))
    except (KeyError, ValueError, TypeError):
        return False
    cluster.update(review=review, review_source='reused_review', reused_from_cluster_id=saved['cluster_id'])
    remember_review(cluster, members, llm, options)
    return True
