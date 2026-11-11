"""One repair task reads all feedback while immutable case constraints stay local."""
from __future__ import annotations

from copy import deepcopy

from . import JUDGES
from .schemas import REPAIR_SCHEMA
from .validators import validate_dialogue, validate_schema


def build_repair_payload(original_case: dict, revision: dict, history: list, policy: dict,
                         repair_round: int = 1, max_rounds: int = 3) -> dict:
    return {
        **{key: deepcopy(original_case[key]) for key in ("case_id", "scenario", "seed_qa", "factors", "persona")},
        "original_dialogue": deepcopy(original_case["dialogue"]),
        "current_dialogue": deepcopy(revision["dialogue"]),
        "latest_evaluation": deepcopy(revision["evaluation"]),
        "repair_history": deepcopy(history), "repair_round": repair_round,
        "max_rounds": max_rounds, "decision_policy": deepcopy(policy),
    }


def _resolve_feedback(pointer: str, evaluation: dict):
    parts = pointer.split("/")
    if len(parts) < 3 or parts[0] or parts[1] != "judge_results" or parts[2] not in JUDGES:
        raise ValueError("feedback_refs must point into latest_evaluation.judge_results")
    value = evaluation
    for raw in parts[1:]:
        # Reject malformed JSON Pointer escapes and non-canonical array indices.
        remaining = raw.replace("~0", "").replace("~1", "")
        if "~" in remaining:
            raise ValueError("Malformed JSON Pointer escape")
        part = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, list):
            if not part.isdigit() or str(int(part)) != part:
                raise ValueError("Invalid feedback array index")
            value = value[int(part)]
        elif isinstance(value, dict):
            value = value[part]
        else:
            raise ValueError("feedback_refs descend beyond a leaf")
    return value


def validate_repair_response(response: dict, original_case: dict, revision: dict) -> dict:
    validate_schema(response, REPAIR_SCHEMA)
    if response["action"] != "revised":
        if response["dialogue"] is not None or response["change_summary"]:
            raise ValueError("unchanged/blocked require null dialogue and empty change_summary")
        return deepcopy(original_case)
    if response["dialogue"] is None or not response["change_summary"]:
        raise ValueError("revised requires a complete dialogue and nonempty change_summary")
    for change in response["change_summary"]:
        for pointer in change["feedback_refs"]:
            try:
                _resolve_feedback(pointer, revision["evaluation"])
            except (KeyError, IndexError, ValueError) as exc:
                raise ValueError(f"Invalid feedback_refs: {pointer}") from exc
    candidate = deepcopy(original_case)
    candidate["dialogue"] = validate_dialogue(response["dialogue"], renumber=True)
    return candidate


def dialogue_diff(before, after, path="/dialogue") -> list:
    """Describe actual content changes independently of model-authored summaries."""
    if type(before) is type(after) and isinstance(before, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            escaped = key.replace("~", "~0").replace("/", "~1")
            child = f"{path}/{escaped}"
            if key not in before:
                changes.append({"op": "add", "path": child, "before": None, "after": after[key]})
            elif key not in after:
                changes.append({"op": "remove", "path": child, "before": before[key], "after": None})
            else:
                changes.extend(dialogue_diff(before[key], after[key], child))
        return changes
    if type(before) is type(after) and isinstance(before, list):
        changes = []
        for index in range(max(len(before), len(after))):
            child = f"{path}/{index}"
            if index >= len(before):
                changes.append({"op": "add", "path": child, "before": None, "after": after[index]})
            elif index >= len(after):
                changes.append({"op": "remove", "path": child, "before": before[index], "after": None})
            else:
                changes.extend(dialogue_diff(before[index], after[index], child))
        return changes
    return [] if type(before) is type(after) and before == after else [{"op": "replace", "path": path, "before": before, "after": after}]
