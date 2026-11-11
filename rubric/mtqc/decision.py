"""Deterministic aggregation of validated LLM results."""
from __future__ import annotations

from . import JUDGES


def decide(results: dict, min_coherence: int = 3, min_persona: int = 3) -> str:
    if set(results) != set(JUDGES):
        raise ValueError("All four judge slots are required")
    if type(min_coherence) is not int or type(min_persona) is not int or min_coherence != 3 or min_persona != 3:
        raise ValueError("All judge thresholds must be 3")
    scores = {}
    for name in JUDGES:
        result = results[name]
        status, score = result["status"], result["score"]
        if status == "COMPLETED":
            if type(score) is not int or score not in ((1, 3) if name == JUDGES[0] else (1, 2, 3)):
                raise ValueError("COMPLETED judge requires a valid score")
        elif status not in ("ERROR", "SKIPPED") or score is not None:
            raise ValueError("ERROR/SKIPPED judges require null scores")
        scores[name] = score
    f, q, c, p = (scores[name] for name in JUDGES)
    if q == 1:
        return "REJECT"
    if f == 1 or (c is not None and c < min_coherence) or (p is not None and p < min_persona):
        return "REPAIR"
    if any(results[name]["status"] != "COMPLETED" for name in JUDGES):
        return "REVIEW"
    return "REVIEW" if q == 2 else "PASS"


def summarize_results(judge_results: dict, policy: dict) -> dict:
    scores = {name: judge_results[name]["score"] for name in JUDGES}
    thresholds = (3, 3, policy["min_coherence_score"], policy["min_persona_score"])
    errors = [name for name in JUDGES if judge_results[name]["status"] == "ERROR"]
    skipped = [name for name in JUDGES if judge_results[name]["status"] == "SKIPPED"]
    return {
        "judge_results": judge_results, "scores": scores,
        "dimension_feedback": [{key: judge_results[name][key] for key in ("judge_name", "status", "score", "reason", "details")} for name in JUDGES],
        "blocking_dimensions": [name for name, threshold in zip(JUDGES, thresholds) if scores[name] is not None and scores[name] < threshold],
        "failed_factors": judge_results[JUDGES[0]]["details"]["failed_factors"] if judge_results[JUDGES[0]]["status"] == "COMPLETED" else None,
        "technical_failures": errors, "skipped_dimensions": skipped,
        "quality_decision": decide(judge_results, policy["min_coherence_score"], policy["min_persona_score"]),
        "evaluation_status": "ERROR" if errors else "PARTIAL" if skipped else "COMPLETED",
    }
