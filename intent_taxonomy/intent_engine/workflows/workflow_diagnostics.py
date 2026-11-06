"""Explain missing hierarchy using actual results, including older snapshots."""
from intent_engine.atomic.atomic_discovery import WORKFLOW_STAGES, review_eligible


def workflow_diagnostics(data):
    clusters = data.get("clusters", [])
    errors = data.get("errors", [])
    review = dict(total=len(clusters), consistent=0, mixed=0, uncertain=0, failed=0, pending=0)
    for cluster in clusters:
        verdict = (cluster.get("review") or {}).get("verdict")
        if verdict in {"consistent", "mixed", "uncertain"}:
            review[verdict] += 1
        elif cluster.get("review_error") or any(e.get("stage") == "review" and e.get("cluster_id") == cluster["cluster_id"] for e in errors):
            review["failed"] += 1
        else:
            review["pending"] += 1
    review["eligible"] = sum(review_eligible(c) for c in clusters)
    review["low_support_eligible"] = sum(review_eligible(c) and c.get("unique_text_count")==1 for c in clusters)
    review["low_support_approved"] = sum(c.get("support_level")=="single_example" and c.get("hierarchy_eligible") is True for c in clusters)
    completed = data.get("completed_stages", [])
    tasks = len(data.get("candidate_taxonomy", {}).get("tasks", []))
    outcomes = data.get("stage_outcomes", {})
    next_stage = None
    iteration = data.get("iteration", {})
    if tasks:
        code, message = "ready", f"已生成 {tasks} 个候选任务，可选择层级结果进行整理。"
        if "domains" in completed and not iteration.get("converged"):
            next_stage = "iterate"
    elif iteration.get("rounds"):
        code, message, next_stage = "iteration_empty", "迭代后的候选体系没有任务。请检查每轮变更，或基于初始簇审核重新归纳层级。", "domains"
    elif not any(a.get("status") == "valid" for a in data.get("intents", [])):
        code, message, next_stage = "needs_extract", "没有可聚类的明确原子意图，请检查原句、待确认意图或提取错误后重新分析。", "extract"
    elif "cluster" not in completed:
        code, message, next_stage = "needs_cluster", "尚未完成聚类，请先检查原子意图并执行无监督聚类。", "cluster" if "extract" in completed else "extract"
    elif not clusters:
        code, message, next_stage = "no_clusters", "没有可归纳的候选簇；有效意图可能全部为噪声。请检查意图与聚类配置。", "cluster"
    elif review["failed"] or review["pending"]:
        code, message, next_stage = "needs_review", f"簇审核失败 {review['failed']} 个、尚无结论 {review['pending']} 个。请检查错误后重试簇一致性审核。", "review"
        if outcomes.get("review", {}).get("status") == "skipped":
            message = "该历史流程显式关闭了审核，尚未执行簇审核与层级归纳；可复用已有聚类结果补跑审核。"
        elif data.get("requested_stage") in {"extract", "cluster"}:
            message = "这是单阶段处理生成的版本，只执行了所选阶段与导出，没有自动重跑下游审核。可继续执行簇一致性审核。"
    elif not review["eligible"]:
        code, message, next_stage = "no_consistent", "审核已执行，但所有簇均为混合或待确认，没有一致簇可生成层级。可补跑簇一致性审核进行局部细分；已达细分上限的簇需检查成员或调整配置。", "review"
    else:
        failed = any(e.get("stage") == "domains" for e in errors) or outcomes.get("domains", {}).get("status") == "failed"
        code = "domains_failed" if failed else "needs_domains"
        message = "层级归纳请求或结构校验失败，请查看错误详情并重试。" if failed else "已有一致簇，但尚未生成候选层级，请执行层级归纳。"
        next_stage = "domains"
    if data.get("status") == "running":
        message = "流程正在运行，后续审核或层级归纳可能尚未执行。" + message
        next_stage = None
    stages = []
    for key, label in WORKFLOW_STAGES.items():
        outcome = dict(outcomes.get(key, {}))
        if not outcome:
            status = "completed" if key in completed else "pending"
            if key == "review" and clusters:
                done = review["consistent"] + review["mixed"] + review["uncertain"]
                status = "failed" if review["failed"] == len(clusters) else "completed_with_errors" if done and (review["failed"] or review["pending"]) else "completed" if done == len(clusters) else "pending"
            if key == "domains" and not tasks and key in completed:
                status = "failed" if any(e.get("stage") == key for e in errors) else "blocked"
            if any(e.get("stage") == key for e in errors) and status == "pending":
                status = "failed"
            outcome = {"status": status, "message": "历史结果按实际数据推断"}
        stages.append({"key": key, "label": label, **outcome})
    iteration_summary = {key: iteration.get(key) for key in ("status", "max_rounds", "stop_reason", "stop_message", "converged", "validated_taxonomy_version", "iteration_scope", "validation_mode", "error")}
    iteration_summary["rounds"] = [{"round": record["round"], "before": record["task_count_before"], "after": record["task_count_after"],
        "changes": len(record["changes"]), "classified": record.get("classified_count"), "reused": record.get("reused_count"), "known": record.get("status_counts", {}).get("known", 0),
        "discovery": record.get("status_counts", {}).get("discovery", 0),
        "errors": record.get("classification_errors", 0) + record.get("model_errors", 0)} for record in iteration.get("rounds", [])]
    from intent_engine.exporting.delivery_topics import prepare_delivery_topics
    delivery_data = dict(data)
    delivery = prepare_delivery_topics(delivery_data)
    merge_history = data.get("leaf_merging", {}).get("passes", [])
    merge_summary = {"applied":sum(p.get("applied",0) for p in merge_history),
                     "passes":[{key:p.get(key) for key in ("phase","status","applied","cached","deferred_candidates","logical_requests","skipped","skip_reason","error","pairs")} for p in merge_history],
                     "aliases": data.get("task_aliases", {})}
    return {"append_input":data.get("append_input"), "parent_regroup": {"passes":data.get("parent_regroup",{}).get("passes",[])}, "leaf_merge":merge_summary, "delivery": {"source":delivery['source'], "coverage":delivery['coverage'], "warnings":delivery['warnings']}, "run_id": data.get("run_id"), "code": code, "message": message, "review": review, "iteration": iteration_summary,
            "refinement": {"status": data.get("cluster_refinement", {}).get("status"),
                "max_rounds": data.get("cluster_refinement", {}).get("max_rounds"),
                "pending_clusters": data.get("cluster_refinement", {}).get("pending_clusters", 0),
                "history": [{key: event.get(key) for key in ("parent_cluster_id", "round", "method", "applied", "child_cluster_ids", "errors")}
                            for event in data.get("cluster_refinement", {}).get("history", [])]},
            "tasks": tasks, "domains": len(data.get("candidate_taxonomy", {}).get("domains", [])),
            "noise": sum(a.get("cluster_id") == -1 for a in data.get("intents", [])),
            "next_stage": next_stage, "stages": stages,
            "topic_hierarchy": {key: data.get("topic_hierarchy", {}).get(key) for key in ("status", "max_depth", "leaf_count", "message")},
            "errors": [e for e in errors if e.get("stage") in {"extract", "embed", "review", "pairs", "domains", "cluster", "hierarchy", "iterate", "leaf_merge"} or str(e.get("stage", "")).startswith(("iteration_", "round_"))][:30]}
