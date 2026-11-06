from __future__ import annotations

import json
from pathlib import Path
from typing import List
import pandas as pd

from intent_engine.models import CandidateCluster, SemanticResult, TaxonomyDecision
from intent_engine.infrastructure.utils import ensure_dir


def save_candidate_report(candidates: List[CandidateCluster], path: str):
    rows = [c.model_dump() for c in candidates]
    df = pd.DataFrame(rows)
    if not df.empty:
        df["examples"] = df["examples"].apply(lambda x: "\n".join(x))
    df.to_excel(path, index=False)


def save_decisions(decisions: List[TaxonomyDecision], path: str):
    p = Path(path)
    ensure_dir(p.parent)
    with open(p, "w", encoding="utf-8") as f:
        json.dump([d.model_dump() for d in decisions], f, ensure_ascii=False, indent=2)


def save_summary(results: List[SemanticResult], path: str):
    rows = []
    for r in results:
        for i in r.intents:
            route = i.skill_route
            rows.append({
                "raw_text": r.raw_text,
                "span": i.span,
                "status": i.status,
                "domain": i.domain,
                "object": i.object,
                "action": i.action,
                "matched_task_id": i.matched_task_id or "",
                "confidence": i.confidence,
                "has_existing_match": bool(i.matched_task_id),
                "candidate_task_ids": ",".join(i.candidate_task_ids),
                "primary_skill_id": route.primary_skill_id if route else "",
                "skill_route_mode": route.route_mode if route else "",
                "skill_route_confidence": route.confidence if route else 0.0,
                "skill_candidates": ",".join(route.candidate_skill_ids) if route else "",
                "skill_reason": route.reason if route else "",
            })
    df = pd.DataFrame(rows)
    if df.empty:
        pd.DataFrame(columns=["metric", "value"]).to_excel(path, index=False)
        return

    status = df.groupby("status").size().rename("count").reset_index()
    task_dist = (
        df.groupby(["domain", "object", "action", "matched_task_id"], dropna=False)
        .size().rename("count").reset_index().sort_values("count", ascending=False)
    )
    skill_dist = (
        df.groupby(["primary_skill_id", "skill_route_mode"], dropna=False)
        .size().rename("count").reset_index().sort_values("count", ascending=False)
    )
    task_skill = (
        df.groupby(["matched_task_id", "domain", "object", "action", "primary_skill_id"], dropna=False)
        .size().rename("count").reset_index().sort_values("count", ascending=False)
    )
    with pd.ExcelWriter(path) as writer:
        status.to_excel(writer, sheet_name="status", index=False)
        task_dist.to_excel(writer, sheet_name="task_distribution", index=False)
        skill_dist.to_excel(writer, sheet_name="skill_distribution", index=False)
        task_skill.to_excel(writer, sheet_name="task_skill_distribution", index=False)
        df.to_excel(writer, sheet_name="all_intents", index=False)
