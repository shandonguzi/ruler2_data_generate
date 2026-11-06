from __future__ import annotations

from pathlib import Path
import hashlib
import json
import os
import stat
import tempfile
import textwrap
import yaml


MODEL_FIELDS = ("base_url", "model", "api_key_env")


def model_settings_snapshot(path: str | Path) -> dict:
    raw = Path(path).read_text(encoding="utf-8")
    cfg = yaml.safe_load(raw)
    result = {"revision": hashlib.sha256(raw.encode()).hexdigest()}
    for section in ("llm", "embedding"):
        settings = cfg.get(section) or {}
        result[section] = {key: settings.get(key) or "" for key in MODEL_FIELDS}
        result[section]["api_key_configured"] = bool(settings.get("api_key") and settings["api_key"] != "EMPTY")
        result[section]["extra_body"] = settings.get("extra_body") or {}
        if section == "llm":
            result[section]["thinking_mode"] = settings.get("thinking_mode") or "default"
            result[section]["thinking_parameter"] = settings.get("thinking_parameter") or "chat_template_kwargs.enable_thinking"
    endpoints=(cfg.get("llm") or {}).get("endpoints",[])
    if endpoints:
        result["extra_llms"]=[{k:v for k,v in e.items() if k not in {"api_key"}} for e in endpoints]
    return result


def save_model_settings(path: str | Path, revision: str, updates: dict) -> dict:
    """Patch edited YAML values; preserve unrelated settings and comments."""
    path = Path(path)
    raw = path.read_text(encoding="utf-8")
    if hashlib.sha256(raw.encode()).hexdigest() != revision:
        raise ValueError("配置文件已变更，请重新加载后再保存")
    tree = yaml.compose(raw)
    sections = {key.value: value for key, value in tree.value}
    patches = []
    append = ""
    for name, values in updates.items():
        section = sections.get(name)
        if section is None:
            append += "\n" + yaml.safe_dump({name: values}, allow_unicode=True, sort_keys=False)
            continue
        if not isinstance(section, yaml.MappingNode) and section.tag != "tag:yaml.org,2002:null":
            raise ValueError(f"{name} 配置必须是映射")
        if not isinstance(section, yaml.MappingNode) or section.flow_style or not section.value:
            merged = (yaml.safe_load(raw).get(name) or {}) | values
            encoded = "\n" + textwrap.indent(yaml.safe_dump(merged, allow_unicode=True, sort_keys=False).rstrip(), "  ")
            patches.append((section.start_mark.index, section.end_mark.index, encoded))
            continue
        fields = {key.value: value for key, value in section.value}
        additions = ""
        for key, value in values.items():
            encoded = json.dumps(value, ensure_ascii=False)
            node = fields.get(key)
            if node is not None:
                end = node.end_mark.index
                if not isinstance(node, yaml.ScalarNode) and node.end_mark.line > node.start_mark.line:
                    end -= node.end_mark.column
                    if raw[node.start_mark.index:end].endswith("\n"):
                        encoded += "\n"
                if getattr(node, "style", None) in {"|", ">"} and raw[node.start_mark.index:node.end_mark.index].endswith("\n"):
                    encoded += "\n"
                patches.append((node.start_mark.index, end, encoded))
            else:
                additions += f"  {key}: {encoded}\n"
        if additions:
            # Insert before the first existing key so trailing comments retain location.
            index = section.value[0][0].start_mark.index - section.value[0][0].start_mark.column if section.value else section.end_mark.index
            patches.append((index, index, additions))
    for start, end, value in sorted(patches, key=lambda p: (p[0], p[1]), reverse=True):
        raw = raw[:start] + value + raw[end:]
    raw += append
    yaml.safe_load(raw)  # Verify the patched document before replacing the file.
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".model-config-", suffix=".tmp", delete=False) as file:
            temp_path = Path(file.name)
            os.chmod(temp_path, stat.S_IMODE(path.stat().st_mode))
            file.write(raw)
            file.flush()
            os.fsync(file.fileno())
        if hashlib.sha256(path.read_text(encoding="utf-8").encode()).hexdigest() != revision:
            raise ValueError("配置文件已变更，请重新加载后再保存")
        os.replace(temp_path, path)
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
    return model_settings_snapshot(path)


def load_config(path: str):
    with open(path, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    from intent_engine.settings.prompt_settings import effective_prompts
    cfg.setdefault("llm", {})["prompt_overrides"] = effective_prompts(path)

    root = Path(path).resolve().parent.parent
    cfg["_root"] = str(root)

    for key in ["reports_dir", "checkpoint_dir"]:
        value = cfg["paths"][key]
        p = Path(value)
        if not p.is_absolute():
            cfg["paths"][key] = str(root / p)

    return cfg
