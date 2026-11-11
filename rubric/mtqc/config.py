"""Validated YAML configuration; all relative paths are config-relative."""
from __future__ import annotations

import math
from pathlib import Path
from urllib.parse import urlparse

import yaml

from . import JUDGES
from .prompt_loader import load_prompt


def _mapping(value, fields, name):
    if not isinstance(value, dict) or set(value) - set(fields):
        raise ValueError(f"{name}: expected mapping with keys {sorted(fields)}")
    return dict(value)


def _integer(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ValueError(f"{name} must be an integer >= {minimum}")
    return value


def _boolean(value, name):
    if type(value) is not bool:
        raise ValueError(f"{name} must be boolean")
    return value


def _text(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def load_config(path) -> dict:
    path = Path(path).expanduser().resolve()
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML configuration: {path}") from exc
    raw = _mapping(document,
                   {"pipeline", "rubric_version", "input", "output", "llm", "evaluation", "judges", "repair", "decision"}, "config")
    if raw.get("pipeline", "multi_turn_qa_qc") != "multi_turn_qa_qc":
        raise ValueError("pipeline must be multi_turn_qa_qc")
    config = {"pipeline": "multi_turn_qa_qc", "rubric_version": _text(raw.get("rubric_version", "mtqc-v1.1"), "rubric_version")}

    def resolve(value, name):
        return (path.parent / Path(_text(value, name)).expanduser()).resolve()

    source = _mapping(raw.get("input"), {"format", "path", "columns", "sheet_name"}, "input")
    source["format"] = source.get("format", "jsonl")
    if source["format"] not in ("jsonl", "excel"):
        raise ValueError("input.format must be jsonl or excel")
    source["path"] = str(resolve(source.get("path"), "input.path"))
    if source["format"] == "excel":
        columns = source.setdefault("columns", {"case": "case_json"})
        if not isinstance(columns, dict) or set(columns) not in ({"case"}, {"case_id", "scenario", "seed_qa", "factors", "persona", "dialogue"}):
            raise ValueError("Excel columns must map case, or all six Case fields")
        for name in columns.values():
            _text(name, "input.columns")
        if len(columns) != len(set(columns.values())):
            raise ValueError("Excel column names must be unique")
        if not isinstance(source.get("sheet_name", ""), str):
            raise ValueError("input.sheet_name must be a string")
    config["input"] = source
    output_root = resolve(raw.get("output"), "output")
    if output_root.exists() and not output_root.is_dir():
        raise ValueError("output must be a directory path")
    output = {key: str(output_root / name) for key, name in {
        "results_jsonl": "results.jsonl", "excel_path": "results.xlsx",
        "report_path": "report.html", "records_dir": "records",
    }.items()}
    files = [source["path"], output["results_jsonl"], output["excel_path"], output["report_path"]]
    if len(files) != len(set(files)):
        raise ValueError("Input and output file paths must be distinct")
    if source["format"] == "excel" and Path(source["path"]).suffix.lower() != ".xlsx":
        raise ValueError("Excel input must be .xlsx")
    records = Path(output["records_dir"])
    if any(records == Path(file) or records in Path(file).parents for file in files):
        raise ValueError("Input and exported files must be outside records_dir")
    config["output"] = output
    llm = _mapping(raw.get("llm"), {"api_url", "api_key", "timeout_seconds", "max_attempts", "concurrency",
                                    "temperature", "response_format", "request_parameters", "retry_backoff_seconds", "requests_per_second"}, "llm")
    url = urlparse(_text(llm.get("api_url"), "llm.api_url"))
    if url.scheme not in ("http", "https") or not url.netloc or url.username or url.password or url.query or url.fragment:
        raise ValueError("llm.api_url must be an HTTP(S) endpoint without credentials/query/fragment")
    llm["api_url"] = llm["api_url"].rstrip("/")
    if url.path.rstrip("/").endswith("/v1"):
        llm["api_url"] += "/chat/completions"
    if "api_key" in llm:
        llm["api_key"] = _text(llm["api_key"], "llm.api_key").strip()
    for key, default in (("max_attempts", 3), ("concurrency", 4)):
        llm[key] = _integer(llm.get(key, default), f"llm.{key}", 1)
    for key, default, positive in (("timeout_seconds", 120, True), ("retry_backoff_seconds", 2, False), ("requests_per_second", 0, False)):
        value = llm.get(key, default)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0 or (positive and value == 0):
            raise ValueError(f"llm.{key} invalid")
        llm[key] = value
    llm.setdefault("temperature", 0)
    if llm["temperature"] is not None and (type(llm["temperature"]) not in (int, float) or not math.isfinite(llm["temperature"]) or not 0 <= llm["temperature"] <= 2):
        raise ValueError("llm.temperature must be null or a number in [0, 2]")
    llm.setdefault("response_format", "auto")
    if llm["response_format"] not in ("auto", "json_schema", "json_object"):
        raise ValueError("llm.response_format must be auto/json_schema/json_object")
    params = llm.setdefault("request_parameters", {})
    allowed_params = {"max_tokens", "max_completion_tokens", "top_p", "seed", "frequency_penalty", "presence_penalty", "reasoning_effort"}
    _mapping(params, allowed_params, "llm.request_parameters")
    for key in ("max_tokens", "max_completion_tokens"):
        if key in params:
            _integer(params[key], key, 1)
    if "seed" in params and type(params["seed"]) is not int:
        raise ValueError("llm.request_parameters.seed must be an integer")
    for key, minimum, maximum in (("top_p", 0, 1), ("frequency_penalty", -2, 2), ("presence_penalty", -2, 2)):
        if key in params and (type(params[key]) not in (int, float) or not math.isfinite(params[key]) or not minimum <= params[key] <= maximum):
            raise ValueError(f"llm.request_parameters.{key} must be in [{minimum}, {maximum}]")
    if "reasoning_effort" in params:
        _text(params["reasoning_effort"], "llm.request_parameters.reasoning_effort")
    config["llm"] = llm
    evaluation = _mapping(raw.get("evaluation", {}), {"fail_fast"}, "evaluation")
    evaluation["fail_fast"] = _boolean(evaluation.get("fail_fast", False), "evaluation.fail_fast")
    config["evaluation"] = evaluation
    repair = _mapping(raw.get("repair", {}), {"enabled", "max_rounds", "model", "prompt", "version"}, "repair")
    repair["enabled"] = _boolean(repair.get("enabled", True), "repair.enabled")
    repair["max_rounds"] = _integer(repair.get("max_rounds", 3), "repair.max_rounds")
    if repair["enabled"] and evaluation["fail_fast"]:
        raise ValueError("repair.enabled=true is incompatible with evaluation.fail_fast=true")
    judges = _mapping(raw.get("judges"), set(JUDGES), "judges")
    if set(judges) != set(JUDGES):
        raise ValueError("All four judges must be configured")
    for name in JUDGES:
        judges[name] = _mapping(judges[name], {"model", "prompt", "version"}, f"judges.{name}")
    for name, spec in list(judges.items()) + [("dialogue_repair", repair)]:
        if name == "dialogue_repair" and not repair["enabled"] and not spec.get("model"):
            spec["model"] = judges[JUDGES[0]]["model"]
        _text(spec.get("model"), f"{name}.model")
        spec["version"] = _text(spec.get("version", "v1"), f"{name}.version")
        spec["prompt"] = str(resolve(spec["prompt"], f"{name}.prompt")) if spec.get("prompt") else str(Path(__file__).parent / "prompts" / f"{name}.txt")
        prompt = load_prompt(Path(spec["prompt"]))
        spec.update(prompt_content=prompt["content"], prompt_sha256=prompt["sha256"])
    config.update(judges=judges, repair=repair)
    protected = {path, Path(source["path"]), *[Path(spec["prompt"]) for spec in list(judges.values()) + [repair]]}
    if any(Path(file) in protected for file in files[1:]) or any(records == file or records in file.parents for file in protected):
        raise ValueError("Outputs must not overwrite config, input, or prompt files")
    decision = _mapping(raw.get("decision", {}), {"require_factor_score", "require_final_qa_score", "min_coherence_score", "min_persona_score"}, "decision")
    for key, default, allowed in (("require_factor_score", 3, (3,)), ("require_final_qa_score", 3, (3,)),
                                  ("min_coherence_score", 3, (3,)), ("min_persona_score", 3, (3,))):
        value = decision.get(key, default)
        if type(value) is not int or value not in allowed:
            raise ValueError(f"decision.{key} must be one of {allowed}")
        decision[key] = value
    config["decision"] = decision
    return config
