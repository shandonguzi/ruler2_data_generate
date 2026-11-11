from __future__ import annotations

import json
import threading
from collections import Counter
from copy import deepcopy

import pytest
import yaml

from rubric.mtqc import JUDGES
from rubric.mtqc.config import load_config
from rubric.mtqc.validators import parse_json


@pytest.fixture
def case():
    return {
        "case_id": "case-1", "scenario": {key: "test" for key in ("s1", "s2", "s3", "s4")},
        "seed_qa": {"question": "A/B在哪换乘？", "answer": "Z站", "source_id": "seed-1"},
        "factors": {"general": {"history_turn_count": {"value": 0, "definition": "不包含当前轮"}}, "test": {}},
        "persona": {"description": "表达简洁"},
        "dialogue": {"history": [], "current": {"turn_id": 1, "user": "A/B在哪换乘？", "assistant": "Z站"}},
    }


@pytest.fixture
def settings(tmp_path, case):
    def build(cases=None, **overrides):
        source = tmp_path / "cases.jsonl"
        source.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in (cases if cases is not None else [case])), encoding="utf-8")
        raw = {
            "input": {"format": "jsonl", "path": "cases.jsonl"},
            "output": "output",
            "llm": {"api_url": "https://example.com/v1/chat/completions", "max_attempts": 3,
                    "concurrency": 4, "retry_backoff_seconds": 0},
            "judges": {name: {"model": "mock", "version": "v1"} for name in JUDGES},
            "repair": {"model": "repair-mock"},
        }
        for key, value in overrides.items():
            raw[key] = {**raw[key], **value} if isinstance(raw.get(key), dict) and isinstance(value, dict) else value
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw, allow_unicode=True), encoding="utf-8")
        return load_config(path)
    return build


def business_result(name, payload, score=3):
    turns = payload["dialogue"]["history"] + [payload["dialogue"]["current"]]
    ids = [turn["turn_id"] for turn in turns]
    result = {"score": score, "reason": "reason", "evidence_turn_ids": ids}
    if name == JUDGES[0]:
        failed = []
        for section, factors in payload["factors"].items():
            for key, spec in factors.items():
                if spec["required"] and score != 3 and not failed:
                    failed.append({"key": f"{section}.{key}", "expected": spec["value"], "reason": "needs repair"})
        result["failed_factors"] = failed
    else:
        result.pop("evidence_turn_ids")
    return result


def repair_response(payload, answer="Z站", action="revised"):
    dialogue = deepcopy(payload["current_dialogue"])
    dialogue["current"]["assistant"] = answer
    return {"action": action, "reason": "repair reason", "dialogue": dialogue if action == "revised" else None,
            "change_summary": [{"scope": "current", "reason": "修正答案", "feedback_refs": ["/judge_results/final_qa_validation/reason"]}] if action == "revised" else []}


class FakeAPI:
    def __init__(self, config, callback=None):
        self.prompts = {spec["prompt_content"]: name for name, spec in {**config["judges"], "dialogue_repair": config["repair"]}.items()}
        self.callback = callback
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, url, key, body, timeout):
        name = self.prompts[body["messages"][0]["content"]]
        payload = parse_json(body["messages"][1]["content"])
        with self.lock:
            self.calls.append((name, deepcopy(payload), deepcopy(body)))
            number = sum(call[0] == name for call in self.calls)
        value = self.callback(name, payload, number) if self.callback else business_result(name, payload)
        if isinstance(value, BaseException):
            raise value
        if isinstance(value, str):
            return value
        return json.dumps({"model": "mock-response", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(value, ensure_ascii=False)}}],
                           "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}}, ensure_ascii=False)

    @property
    def counts(self):
        return Counter(call[0] for call in self.calls)
