"""Resolve intermediate artifacts within their owning workflow."""
from __future__ import annotations

import json
import re
import copy
import hashlib
import uuid
from datetime import datetime
from pathlib import Path

from intent_engine.atomic.atomic_discovery import WORKFLOW_STAGES
from intent_engine.workflows.workflow_layout import find_manifest, manifest_paths, valid_manifest_location, check_no_symlinks, create_workflow_directory


def load_workflow(reports: Path, run_id: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
        raise ValueError("流程编号无效")
    manifest = find_manifest(reports, run_id)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    if data.get("run_id") != run_id:
        raise ValueError("流程记录不匹配")
    return data


def list_workflows(reports: Path) -> list[dict]:
    runs = []
    for path in sorted(manifest_paths(reports), key=lambda p: p.lstat().st_mtime, reverse=True):
        try:
            check_no_symlinks(path, reports)
            data = json.loads(path.read_text(encoding="utf-8"))
            if valid_manifest_location(path, reports, data["run_id"]):
                runs.append(data)
        except (ValueError, KeyError, OSError):
            continue
    return runs


def workflow_artifact(reports: Path, run_id: str, artifact: str) -> Path:
    if artifact not in {*WORKFLOW_STAGES, "report", "workbook", "sentence_results", "sentence_workbook", "query_atomic_intents", "s4_mapping", "s4_details"}:
        raise ValueError("中间结果类型无效")
    workflow = load_workflow(reports, run_id)
    value = workflow.get("artifacts", {}).get(artifact)
    if not value:
        raise ValueError("该流程尚未生成所选中间结果")
    path = Path(value).resolve()
    if reports.resolve() not in path.parents:
        raise ValueError("中间结果不属于当前报告目录")
    if not path.is_file():
        raise ValueError("中间结果文件不存在")
    return path


def load_workflow_taxonomy(reports: Path, run_id: str, artifact: str) -> dict:
    from intent_engine.models import Taxonomy
    from intent_engine.atomic.atomic_discovery import read_stage_source
    path = workflow_artifact(reports, run_id, artifact)
    data = read_stage_source(path, "domains")
    if "domains" not in data["completed_stages"]:
        raise ValueError("请选择已生成候选层级的中间结果")
    taxonomy = Taxonomy.model_validate(data["candidate_taxonomy"])
    return {**taxonomy.model_dump(), "path": str(path), "run_id": run_id, "artifact": artifact,
            "revision": hashlib.sha256(path.read_bytes()).hexdigest()}


def save_workflow_taxonomy(reports: Path, run_id: str, artifact: str, revision: str, tasks: list, domains: list) -> dict:
    from intent_engine.models import Taxonomy
    from intent_engine.taxonomies.taxonomy_manager import build_managed_taxonomy
    from intent_engine.atomic.atomic_discovery import _write_json
    current = load_workflow_taxonomy(reports, run_id, artifact)
    if current["revision"] != revision:
        raise ValueError("中间结果已变更，请重新选择后再保存")
    projected, changes = build_managed_taxonomy(Taxonomy.model_validate(current), tasks, domains)
    if not changes["changed"]:
        return {"run_id": run_id, "changes": changes}
    path = Path(current["path"])
    result = copy.deepcopy(json.loads(path.read_text(encoding="utf-8")))
    if result.get("topic_hierarchy", {}).get("source") == "BERTopic+LLM":
        original = {task["task_id"]:task for task in current["tasks"]}
        if set(original) != {task.task_id for task in projected.tasks} or projected.domains != Taxonomy.model_validate(current).domains:
            raise ValueError("当前层级由 BERTopic 树统一管理；新增、删除或调整父主题请通过聚类与层级审核阶段执行")
        nodes = {node["node_id"]:node for node in result["topic_hierarchy"]["nodes"]}
        for task in projected.tasks:
            previous = original[task.task_id]
            if any(getattr(task,key)!=previous[key] for key in ("domain","object","action")):
                raise ValueError("叶子类别与层级结构需通过簇审核调整；此处可编辑任务说明、示例与别名")
            leaf = nodes[result["task_topic_nodes"][task.task_id]]
            leaf["description"] = task.description
            leaf["manual_definition"] = True
    new_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid.uuid4().hex[:8]
    paths = create_workflow_directory(reports, new_id)
    snapshot, report = paths["domains"], paths["report"]
    result.update(run_id=new_id, created_at=datetime.now().isoformat(), stage="domains",
                  status="completed_with_errors" if result.get("errors") else "completed",
                  source={"run_id": run_id, "stage": artifact, "path": str(path)},
                  candidate_taxonomy=projected.model_dump(), manual_changes=changes,
                  completed_stages=[s for s in result["completed_stages"] if s not in {"iterate", "export"}],
                  artifacts={"domains": str(snapshot), "report": str(report)})
    result.pop("summary", None)
    result.pop("delivery_topics", None)
    result.pop("iteration", None)
    result["stage_outcomes"] = {key: value for key, value in result.get("stage_outcomes", {}).items() if key not in {"iterate", "export"}}
    result["stage_outcomes"]["domains"] = {"status": "completed", "message": "候选层级已人工编辑并保存"}
    from intent_engine.infrastructure.model_requests import UsageLedger
    result["token_usage"] = UsageLedger().snapshot()
    _write_json(snapshot, result)
    _write_json(report, result)
    manifest = {k: result.get(k) for k in ("run_id", "created_at", "status", "stage", "input", "source", "completed_stages", "stage_outcomes", "artifacts", "token_usage")}
    manifest["requested_stage"] = "manual_taxonomy"
    _write_json(paths["manifest"], manifest)
    return {"run_id": new_id, "changes": changes}
