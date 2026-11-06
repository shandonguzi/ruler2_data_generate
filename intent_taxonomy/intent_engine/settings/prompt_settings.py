"""File-backed prompt catalog, runtime references and revision-checked editing."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from threading import RLock

import yaml

from intent_engine.paths import PROJECT_ROOT

PROMPT_ROOT = PROJECT_ROOT / 'prompts'
_STORE_LOCK = RLock()
_KEY = re.compile(r'[a-z][a-z0-9_]*\.[A-Z][A-Z0-9_]*')


def _relative_path(key):
    if not isinstance(key, str) or not _KEY.fullmatch(key):
        raise ValueError('Prompt 标识无效')
    module, name = key.split('.')
    return Path(module) / (name + '.txt')


def _read(path):
    try:
        value = path.read_text(encoding='utf-8')
    except OSError as exc:
        raise ValueError(f'无法读取 Prompt 文件：{path}') from exc
    if not value.strip() or len(value) > 32000:
        raise ValueError(f'Prompt 不能为空，且不能超过 32000 字符：{path}')
    return value


def prompt_catalog():
    """Read metadata and defaults afresh; no business-module imports or text matching."""
    catalog = yaml.safe_load((PROMPT_ROOT / 'catalog.yaml').read_text(encoding='utf-8'))
    if not isinstance(catalog, dict) or catalog.get('version') != 1 or not isinstance(catalog.get('templates'), list):
        raise ValueError('Prompt 目录格式无效')
    entries, seen = [], set()
    for item in catalog['templates']:
        if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not item[k].strip()
                                             for k in ('key', 'group', 'label')):
            raise ValueError('Prompt 目录条目无效')
        key = item['key']
        relative = _relative_path(key)
        if key in seen:
            raise ValueError('Prompt 目录标识重复')
        seen.add(key)
        default_path = PROMPT_ROOT / 'defaults' / relative
        entries.append({**{k:item[k] for k in ('key','group','label')},
                        'default':_read(default_path), 'default_path':str(default_path.resolve())})
    return tuple(entries)


class PromptRef(str):
    """Readable defaults for callers, stable keys for actual runtime resolution."""
    def __new__(cls, key):
        entry = next((e for e in prompt_catalog() if e['key'] == key), None)
        if entry is None:
            raise ValueError(f'未注册的 Prompt：{key}')
        obj = str.__new__(cls, entry['default'])
        obj.key = key
        return obj

    def __getnewargs__(self):
        return (self.key,)


def prompt_ref(key):
    return PromptRef(key)


def _override_path(config_path, key):
    root = Path(config_path).resolve().parent / 'prompts'
    path = root / _relative_path(key)
    if root.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('自定义 Prompt 文件必须位于当前配置的 prompts 目录内')
    return path


def _state(config_path):
    path = Path(config_path).resolve()
    raw = path.read_text(encoding='utf-8')
    cfg = yaml.safe_load(raw) or {}
    legacy = cfg.get('prompts') or {}
    if not isinstance(legacy, dict):
        raise ValueError('prompts 配置必须是映射')
    entries = []
    identity = [str(path), raw, (PROMPT_ROOT/'catalog.yaml').read_text(encoding='utf-8')]
    for entry in prompt_catalog():
        key, default = entry['key'], entry['default']
        override_path = _override_path(path, key)
        override = _read(override_path) if override_path.exists() else None
        old = legacy.get(key)
        if old is not None and (not isinstance(old, str) or not old.strip() or len(old)>32000):
            raise ValueError(f'旧配置中的 Prompt 无效：{key}')
        value = override if override is not None else old if old is not None else default
        source = 'file' if override is not None else 'legacy' if old is not None else 'default'
        entries.append({**entry, 'value':value, 'customized':value!=default,
                        'storage':source, 'override_path':str(override_path),
                        'source_path':str(override_path) if source=='file' else str(path) if source=='legacy' else entry['default_path']})
        identity.append([key, default, override])
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest()
    return {'revision':digest, 'prompts':entries}, raw, legacy


def prompt_settings_snapshot(path):
    with _STORE_LOCK:
        return _state(path)[0]


def effective_prompts(path):
    """A job snapshots all effective templates, so edits affect subsequent jobs."""
    return {entry['key']:entry['value'] for entry in prompt_settings_snapshot(path)['prompts']}


def resolve_prompt(default, overrides=None):
    key = getattr(default, 'key', None)
    if key is None:
        return default
    if overrides and overrides.get(key):
        return overrides[key]
    return effective_prompts(PROJECT_ROOT/'config'/'config.yaml')[key]


def _without_migrated_prompts(raw, remaining):
    tree = yaml.compose(raw)
    for key, value in tree.value:
        if key.value != 'prompts':
            continue
        if remaining:
            replacement = yaml.safe_dump({'prompts':remaining}, allow_unicode=True, sort_keys=False)
        else:
            replacement = ''
        end = value.end_mark.index
        if end < len(raw) and raw[end]=='\n':
            end += 1
        return raw[:key.start_mark.index] + replacement + raw[end:]
    return raw


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.prompt-', suffix='.tmp', delete=False) as file:
            temp = Path(file.name)
            if path.exists():
                os.chmod(temp, stat.S_IMODE(path.stat().st_mode))
            file.write(data)
            file.flush()
            os.fsync(file.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def save_prompt_settings(path, revision, updates):
    """Save individual text files; migrate registered YAML overrides without loss."""
    path = Path(path).resolve()
    with _STORE_LOCK:
        state, raw, legacy = _state(path)
        if state['revision'] != revision:
            raise ValueError('Prompt 或配置文件已变更，请重新加载后再保存')
        entries = {entry['key']:entry for entry in state['prompts']}
        if not updates or not set(updates)<=entries.keys():
            raise ValueError('请选择有效的 Prompt 模板')
        for value in updates.values():
            if value is not None and (not isinstance(value,str) or not value.strip() or len(value)>32000):
                raise ValueError('Prompt 不能为空，且不能超过 32000 字符')
        desired = {key:entry['value'] for key,entry in entries.items() if key in legacy}
        desired.update({key:entries[key]['default'] if value is None else value for key,value in updates.items()})
        writes = {}
        for key,value in desired.items():
            target = _override_path(path,key)
            writes[target] = None if value==entries[key]['default'] else value.encode('utf-8')
        if set(legacy)&entries.keys():
            remaining = {key:value for key,value in legacy.items() if key not in entries}
            migrated = _without_migrated_prompts(raw,remaining)
            yaml.safe_load(migrated)
            writes[path] = migrated.encode('utf-8')
        before = {target:target.read_bytes() if target.exists() else None for target in writes}
        # Catch edits to defaults, overrides or configuration made while preparing the save.
        if _state(path)[0]['revision'] != revision:
            raise ValueError('Prompt 或配置文件已变更，请重新加载后再保存')
        applied = []
        try:
            for target,data in writes.items():
                current = target.read_bytes() if target.exists() else None
                if current != before[target]:
                    raise ValueError('Prompt 文件已变更，请重新加载后再保存')
                if data is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target,data)
                applied.append(target)
        except Exception:
            for target in reversed(applied):
                current = target.read_bytes() if target.exists() else None
                if current != writes[target]:
                    continue  # Do not overwrite an external edit made after our write.
                if before[target] is None:
                    target.unlink(missing_ok=True)
                else:
                    _atomic_write(target,before[target])
            raise
        return _state(path)[0]
