"""Structural and cross-field validation; no independent semantic scoring."""
from __future__ import annotations

import json
import math
from copy import deepcopy

from jsonschema import Draft202012Validator

from .schemas import CASE_SCHEMA, DIALOGUE, judge_schema, required_factors


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_json(text: str):
    def bad_constant(value):
        raise ValueError(f"Non-finite JSON value: {value}")
    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError(f"Non-finite JSON number: {value}")
        return number
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=bad_constant, parse_float=finite_float)


def validate_schema(value, schema) -> None:
    # jsonschema accepts 3.0 as an integer; the contract requires actual JSON integers.
    def strict_numbers(item, spec):
        if spec.get("type") == "integer" and type(item) is not int:
            raise ValueError("Expected a JSON integer (not boolean or decimal)")
        if isinstance(item, dict):
            for key, child in item.items():
                child_spec = spec.get("properties", {}).get(key, spec.get("additionalProperties", {}))
                if isinstance(child_spec, dict):
                    strict_numbers(child, child_spec)
        if isinstance(item, list):
            for child in item:
                strict_numbers(child, spec.get("items", {}))
    errors = sorted(Draft202012Validator(schema).iter_errors(value), key=lambda err: str(list(err.path)))
    if errors:
        error = errors[0]
        raise ValueError(f"{'/'.join(map(str, error.path)) or '$'}: {error.message}")
    strict_numbers(value, schema)
    canonical(value)


def validate_dialogue(dialogue: dict, renumber: bool = False) -> dict:
    validate_schema(dialogue, DIALOGUE)
    result = deepcopy(dialogue)
    for index, turn in enumerate(result["history"] + [result["current"]], 1):
        if type(turn["turn_id"]) is not int:
            raise ValueError("turn_id must be a JSON integer")
        if renumber:
            turn["turn_id"] = index
        elif turn["turn_id"] != index:
            raise ValueError("turn_id must be consecutive 1..N+1, excluding current from history")
    return result


def normalize_case(raw: dict) -> dict:
    validate_schema(raw, CASE_SCHEMA)
    case = deepcopy(raw)
    case["dialogue"] = validate_dialogue(case["dialogue"])
    for values in case["factors"].values():
        for key, spec in values.items():
            if not key.strip():
                raise ValueError("Factor key cannot be empty")
            spec.setdefault("required", True)
    if not required_factors(case):
        raise ValueError("At least one required factor is necessary")
    return case


def _evidence(ids: list, turns: dict) -> None:
    if any(type(turn_id) is not int or turn_id not in turns for turn_id in ids):
        raise ValueError("Evidence must reference existing turn_ids of this revision")


def validate_judge_result(name: str, result: dict, case: dict) -> None:
    validate_schema(result, judge_schema(name, case))
    if type(result["score"]) is not int:
        raise ValueError("score must be a JSON integer")
    if name != "factor_compliance":
        return
    turns = {turn["turn_id"]: turn for turn in case["dialogue"]["history"] + [case["dialogue"]["current"]]}
    _evidence(result["evidence_turn_ids"], turns)
    expected = required_factors(case)
    failed = result["failed_factors"]
    keys = [entry["key"] for entry in failed]
    if len(keys) != len(set(keys)) or not set(keys).issubset(expected):
        raise ValueError("failed_factors must contain unique required factor keys")
    for entry in failed:
        if canonical(entry["expected"]) != canonical(expected[entry["key"]]["value"]):
            raise ValueError("expected must preserve the input JSON value and type")
    if result["score"] != (1 if failed else 3):
        raise ValueError("Factor score contradicts failed_factors")
