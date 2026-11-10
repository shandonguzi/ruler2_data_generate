import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]


def test_real_cases_have_traceable_origin_without_invented_gold_labels():
    fixtures=json.loads((ROOT/'tests/fixtures/real_prompt_cases.json').read_text())['cases']
    assert len(fixtures)==30
    assert len({c['prompt'] for c in fixtures})==15
    for c in fixtures:
        assert c['manual_review'] and not c['expected']
        assert c['source']['file'].startswith('data/reports/2026110')
        assert c['source']['kind'] in {'historical_request','projected_historical_request','assembled_from_real_results'}
        if c['source']['kind'] in {'historical_request','projected_historical_request'}:
            assert c['source']['request_id'] and c['source']['line']>0
            digest=hashlib.sha256(json.dumps(c['input'],sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            if c['source']['kind']=='historical_request':assert digest==c['source']['input_sha256']
        else:
            assert c['source']['origins'] and c['source']['intent_ids']
            assert all(o['raw_text'] and o['text'] and 'previous_cluster_id' in o for o in c['source']['origins'])
