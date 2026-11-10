"""Editable language-specific topic feature settings."""
import hashlib
from pathlib import Path
import yaml

from intent_engine.atomic.atomic_discovery import topic_vectorizer


def clustering_settings_snapshot(path):
    raw = Path(path).read_text(encoding='utf-8')
    cfg = (yaml.safe_load(raw) or {}).get('atomic_discovery') or {}
    ranges = cfg.get('topic_ngram_ranges') or {}
    stops = cfg.get('topic_stop_words_by_language') or {}
    return {'revision':hashlib.sha256(raw.encode()).hexdigest(),
            'language':cfg.get('topic_language', 'chinese'),'flat_topics':cfg.get('flat_topics',False),
            'languages':{lang:{'ngram_range':ranges.get(lang, default),
                               'stop_words':stops.get(lang, cfg.get('topic_stop_words', [])),
                               'use_stop_words':cfg.get('topic_'+lang+'_stop_words', True)}
                         for lang, default in [('english',[1,2]),('chinese',[1,1])]}}


def save_clustering_settings(path, revision, language, languages, flat_topics=None):
    from intent_engine.settings.config import save_model_settings
    if language not in {'english','chinese'} or set(languages) != {'english','chinese'}:
        raise ValueError('请选择中文或英文并提供两种语言的配置')
    updates = {'topic_language':language,
               'topic_ngram_ranges':{lang:settings['ngram_range'] for lang,settings in languages.items()},
               'topic_stop_words_by_language':{lang:settings['stop_words'] for lang,settings in languages.items()},
               **{'topic_'+lang+'_stop_words':settings['use_stop_words'] for lang,settings in languages.items()}}
    if flat_topics is not None:
        if type(flat_topics) is not bool:raise ValueError('flat_topics 必须为布尔值')
        updates['flat_topics']=flat_topics
    for lang in languages:
        topic_vectorizer({**updates, 'topic_language':lang})
    save_model_settings(path, revision, {'atomic_discovery':updates})
    return clustering_settings_snapshot(path)
