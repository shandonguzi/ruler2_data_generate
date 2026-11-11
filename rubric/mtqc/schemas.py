"""Strict wire schemas. Semantic decisions belong exclusively to the LLM judges."""
from __future__ import annotations

from copy import deepcopy
from typing import Any

from . import JUDGES


def obj(properties: dict, required=None) -> dict:
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required,
            "additionalProperties": False}


def array(items: dict, **kwargs) -> dict:
    return {"type": "array", "items": items, **kwargs}


TEXT = {"type": "string", "minLength": 1, "pattern": r"\S"}
IDS = array({"type": "integer", "minimum": 1}, uniqueItems=True)
TURN = obj({"turn_id": {"type": "integer", "minimum": 1}, "user": TEXT, "assistant": TEXT})
DIALOGUE = obj({"history": array(TURN), "current": TURN})
FACTOR_SPEC = obj({"value": {}, "definition": TEXT, "scope": TEXT, "unit": TEXT,
                   "required": {"type": "boolean"}}, required=["value", "definition"])
CASE_SCHEMA = obj({
    "case_id": TEXT,
    "scenario": obj({key: TEXT for key in ("s1", "s2", "s3", "s4")}),
    "seed_qa": obj({"question": TEXT, "answer": TEXT, "source_id": TEXT}, ["question", "answer"]),
    "factors": obj({section: {"type": "object", "additionalProperties": FACTOR_SPEC}
                    for section in ("general", "test")}),
    "persona": obj({"description": TEXT, "must_express": array(TEXT), "must_not_conflict": array(TEXT)},
                   ["description"]),
    "dialogue": DIALOGUE,
})


def value_schema(value: Any) -> dict:
    """Preserve JSON types while avoiding open-ended objects in strict API schemas."""
    if value is None:
        return {"type": "null"}
    if type(value) is bool:
        return {"type": "boolean"}
    if type(value) is int:
        return {"type": "integer"}
    if type(value) is float:
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        variants = {str(value_schema(item)): value_schema(item) for item in value}
        return array({"anyOf": list(variants.values())} if variants else {"type": "string"})
    return obj({key: value_schema(item) for key, item in value.items()})


def required_factors(case: dict) -> dict:
    return {f"{section}.{key}": spec for section, values in case["factors"].items()
            for key, spec in values.items() if spec["required"]}


def judge_schema(name: str, case: dict) -> dict:
    if name not in JUDGES:
        raise ValueError(f"Unknown judge: {name}")
    fields = {"score": {"type": "integer", "enum": [1, 3] if name == JUDGES[0] else [1, 2, 3]},
              "reason": TEXT}
    if name == JUDGES[0]:
        fields["evidence_turn_ids"] = IDS
        # Factor values may be composite JSON; each branch retains its own exact shape.
        variants = []
        for key, spec in required_factors(case).items():
            variants.append(obj({"key": {"type": "string", "enum": [key]},
                                 "expected": value_schema(spec["value"]),
                                 "reason": TEXT}))
        fields["failed_factors"] = array({"anyOf": variants}, uniqueItems=True)
    return obj(fields)


REPAIR_SCHEMA = obj({
    "action": {"type": "string", "enum": ["revised", "unchanged", "blocked"]},
    "reason": TEXT, "dialogue": {"anyOf": [DIALOGUE, {"type": "null"}]},
    "change_summary": array(obj({"scope": {"type": "string", "enum": ["history", "current"]},
                                 "reason": TEXT, "feedback_refs": array(TEXT, minItems=1)})),
})


def response_schema(name: str, case: dict) -> dict:
    return deepcopy(REPAIR_SCHEMA) if name == "dialogue_repair" else judge_schema(name, case)
