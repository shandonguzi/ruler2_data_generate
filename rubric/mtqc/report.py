"""Initial/final quality and repair benefit statistics with explicit denominators."""
from __future__ import annotations

import html
import json
from collections import Counter, defaultdict
from pathlib import Path

from . import JUDGES
from .records import atomic_write
from .schemas import required_factors
from .validators import parse_json


def _ratio(numerator, denominator):
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def batch_statistics(results: list, records) -> dict:
    stats = {"case_count": len(results), "input_errors": sum(result["overall_decision"] == "INPUT_ERROR" for result in results),
             "final_decisions": dict(Counter(result["overall_decision"] for result in results)),
             "stop_reasons": dict(Counter(result["stop_reason"] for result in results))}
    for stage, position in (("initial", 0), ("final", -1)):
        evaluations = [result["revisions"][position]["evaluation"] for result in results if result["revisions"]]
        complete = [evaluation for evaluation in evaluations if evaluation["evaluation_status"] == "COMPLETED"]
        factor_results = [evaluation["judge_results"][JUDGES[0]] for evaluation in evaluations
                          if evaluation["judge_results"][JUDGES[0]]["status"] == "COMPLETED"]
        q_results = [evaluation["judge_results"][JUDGES[1]] for evaluation in evaluations
                     if evaluation["judge_results"][JUDGES[1]]["status"] == "COMPLETED"]
        factors = defaultdict(lambda: [0, 0])
        for result in results:
            if not result["revisions"]:
                continue
            factor_result = result["revisions"][position]["evaluation"]["judge_results"][JUDGES[0]]
            if factor_result["status"] != "COMPLETED":
                continue
            failed_keys = {entry["key"] for entry in factor_result["details"]["failed_factors"]}
            for key in required_factors(result["original_case"]):
                factors[key][0] += key in failed_keys
                factors[key][1] += 1
        stats[stage] = {
            "evaluated_cases": len(evaluations), "complete_cases": len(complete),
            "partial_or_error_cases": len(evaluations) - len(complete),
            "technical_failure_rate": _ratio(sum(evaluation["evaluation_status"] == "ERROR" for evaluation in evaluations), len(evaluations)),
            "score_distributions": {name: {
                "complete_cases": dict(Counter(str(evaluation["scores"][name]) for evaluation in complete)),
                "partial_or_error_cases": dict(Counter("null" if evaluation["scores"][name] is None else str(evaluation["scores"][name]) for evaluation in evaluations if evaluation["evaluation_status"] != "COMPLETED")),
            } for name in JUDGES},
            "factor_pass_rate": _ratio(sum(result["score"] == 3 for result in factor_results), len(factor_results)),
            "factor_failure_rates": {key: _ratio(*counts) for key, counts in factors.items()},
            "final_qa_pass_rate": _ratio(sum(result["score"] == 3 for result in q_results), len(q_results)),
        }
        stats[stage]["cohort_rates"] = {}
        for cohort, members in (("complete", complete), ("partial_or_error", [entry for entry in evaluations if entry["evaluation_status"] != "COMPLETED"])):
            factors_in_cohort = [entry["judge_results"][JUDGES[0]] for entry in members if entry["judge_results"][JUDGES[0]]["status"] == "COMPLETED"]
            answers_in_cohort = [entry["judge_results"][JUDGES[1]] for entry in members if entry["judge_results"][JUDGES[1]]["status"] == "COMPLETED"]
            stats[stage]["cohort_rates"][cohort] = {
                "case_count": len(members),
                "factor_pass_rate": _ratio(sum(entry["score"] == 3 for entry in factors_in_cohort), len(factors_in_cohort)),
                "final_qa_pass_rate": _ratio(sum(entry["score"] == 3 for entry in answers_in_cohort), len(answers_in_cohort)),
            }
    repaired = [result for result in results if result["repair_attempt_count"]]
    valid_count = sum(result["original_case"] is not None for result in results)
    stats["repair"] = {
        "trigger_rate": _ratio(len(repaired), valid_count),
        "success_rate": _ratio(sum(result["eligible_for_ingestion"] for result in repaired), len(repaired)),
        "successes_by_round": dict(Counter(str(rev["repair_round"]) for result in results for rev in result["revisions"]
                                          if rev["source"] == "repair" and rev["evaluation"]["quality_decision"] == "PASS")),
        "effective_modification_counts": dict(Counter(str(result["repair_count"]) for result in results)),
        "budget_exhausted_rate": _ratio(sum(result["stop_reason"] == "MAX_REPAIR_ROUNDS" for result in repaired), len(repaired)),
        "blocked_rate": _ratio(sum(result["stop_reason"] == "REPAIR_BLOCKED" for result in repaired), len(repaired)),
        "no_change_rate": _ratio(sum(result["stop_reason"] == "NO_CHANGE" for result in repaired), len(repaired)),
    }
    requests = {}
    for event in records.select("attempts"):
        requests.setdefault(event["attempt_id"], {}).update({event["event"]: event})
    costs = {}
    for kind in ("judge", "repair"):
        selected = [events for events in requests.values() if (events.get("START", {}).get("task_name") == "dialogue_repair") == (kind == "repair")]
        tokens = Counter()
        for events in selected:
            try:
                usage = parse_json(events.get("RESPONSE", {}).get("raw_response", "{}" )).get("usage", {})
                tokens.update({key: value for key, value in usage.items() if type(value) is int})
            except (ValueError, AttributeError):
                pass
        costs[kind] = {"requests": len(selected), "tokens": dict(tokens),
                       "elapsed_seconds": sum(events.get("FINISH", {}).get("elapsed_seconds", 0) for events in selected)}
    stats["api"] = costs
    return stats


def write_report(path, results, records):
    stats = batch_statistics(results, records)
    rows = "".join("<tr>" + "".join(f"<td>{html.escape(str(result[key]))}</td>" for key in
                                   ("case_id", "overall_decision", "stop_reason", "repair_attempt_count", "repair_count", "eligible_for_ingestion")) + "</tr>" for result in results)
    details = "".join(f"<details><summary>{html.escape(str(result['case_id'] or result['input_id']))}</summary><pre>{html.escape(json.dumps(result, ensure_ascii=False, indent=2))}</pre></details>" for result in results)
    document = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>多轮 QA 质检报告</title>
<style>body{{font:15px system-ui;margin:32px;color:#18324b}}table{{border-collapse:collapse}}td,th{{border:1px solid #ddd;padding:8px}}pre{{white-space:pre-wrap;overflow-wrap:anywhere}}details{{margin:12px 0}}</style>
<h1>多轮 QA 质检报告</h1><p>初评与最终结果按 Case 分开统计；技术失败与部分评估使用独立分母，null 不作为低分。</p>
<pre>{html.escape(json.dumps(stats, ensure_ascii=False, indent=2))}</pre>
<table><tr><th>Case</th><th>最终决定</th><th>停止原因</th><th>修复任务</th><th>有效修改</th><th>可入库</th></tr>{rows}</table>
<h2>完整版本与失败因子详情</h2>{details}</html>'''
    atomic_write(Path(path), document)
