import pytest
import yaml
from intent_engine.atomic.atomic_discovery import topic_vectorizer, english_tokens
from intent_engine.settings.clustering_settings import clustering_settings_snapshot, save_clustering_settings


def test_english_features_preserve_negation_actions_and_phrases():
    v = topic_vectorizer({'topic_language':'english'})
    tokens = v.build_analyzer()("Please don't call the bank; I can't turn off notifications")
    assert {'not','call','bank','off','notifications','not call','call bank'} <= set(tokens)
    assert not {'please','the','i','can'} & set(tokens)
    assert 'not' in english_tokens("won’t cancel")
    assert 'not' in english_tokens("doesn't cancel")
    assert v.tokenizer is english_tokens and v.ngram_range == (1,2)


def test_languages_keep_independent_ngrams_and_stop_words():
    cfg = {'topic_ngram_ranges':{'english':[1,2], 'chinese':[1,1]},
           'topic_stop_words_by_language':{'english':['please','hello'], 'chinese':['麻烦']}}
    en = topic_vectorizer({**cfg,'topic_language':'english'}).build_analyzer()('hello check account balance')
    zh = topic_vectorizer({**cfg,'topic_language':'chinese'}).build_analyzer()('麻烦查询电费账单')
    assert 'hello' not in en and 'account balance' in en
    assert '麻烦' not in zh and '查询' in zh and '电费' in zh and not any(' ' in t for t in zh)


def test_save_roundtrip_preserves_model_and_other_clustering_options(tmp_path):
    path = tmp_path/'config.yaml'
    path.write_text('# comment\nllm: {model: test}\nembedding: {}\natomic_discovery:\n  min_cluster_size: 7\n')
    first = clustering_settings_snapshot(path)
    options = first['languages']
    options['english']['stop_words'] = ['hello']
    options['chinese']['ngram_range'] = [1,2]
    saved = save_clustering_settings(path,first['revision'],'english',options)
    assert saved['language'] == 'english'
    assert saved['languages'] == options
    cfg = yaml.safe_load(path.read_text())
    assert cfg['atomic_discovery']['min_cluster_size'] == 7 and cfg['llm']['model']=='test'
    assert '# comment' in path.read_text()
    with pytest.raises(ValueError,match='已变更'):
        save_clustering_settings(path,first['revision'],'chinese',options)
    bad = saved['languages']
    bad['english']['ngram_range'] = [2,1]
    before = path.read_bytes()
    with pytest.raises(ValueError):
        save_clustering_settings(path,saved['revision'],'english',bad)
    assert path.read_bytes() == before


def test_real_rebuilt_english_hierarchy_uses_phrases_and_keeps_original_ids():
    import numpy as np
    pytest.importorskip('bertopic')
    from intent_engine.atomic.cluster_refinement import rebuild_hierarchy
    texts = ['check account balance', 'view account balance', 'show account balance',
             'cancel my flight booking', 'cancel flight reservation', 'cancel the flight ticket']
    keywords, tree = rebuild_hierarchy(texts,np.eye(6),[4,4,4,9,9,9],{'topic_language':'english'})
    assert set(keywords) == {4,9}
    assert any(' ' in word for values in keywords.values() for word in values)
    assert tree['status']=='ready' and tree['leaf_count']==2


def test_flat_switch_roundtrips_without_replacing_language_settings(tmp_path):
    path=tmp_path/'config.yaml';path.write_text('atomic_discovery: {}\n')
    first=clustering_settings_snapshot(path)
    saved=save_clustering_settings(path,first['revision'],first['language'],first['languages'],True)
    assert saved['flat_topics'] is True
    assert saved['languages']==first['languages']
    saved=save_clustering_settings(path,saved['revision'],saved['language'],saved['languages'],False)
    assert saved['flat_topics'] is False
