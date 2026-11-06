from __future__ import annotations

from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field, field_validator, model_validator


Status = Literal["known", "discovery", "ambiguous", "invalid"]
Operation = Literal["ADD", "MERGE", "SPLIT", "KEEP"]
RouteMode = Literal["hard_rule", "llm_judge", "fallback_llm", "unrouted"]


def _as_str_list(value):
    """Normalize common local-LLM JSON deviations to list[str]."""
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, (tuple, set)):
        return list(value)
    return value


def _as_list(value):
    """Wrap a single JSON object/value as a one-element list."""
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    return [value]


class DomainDef(BaseModel):
    name: str
    description: str = ""
    # Optional parent metadata for the workflow-local hierarchy adapter.
    parent_domain: str = ""

    @field_validator("name", "description", "parent_domain", mode="before")
    @classmethod
    def normalize_nullable_domain_strings(cls, value):
        return "" if value is None else str(value)


class TaskDef(BaseModel):
    task_id: str
    domain: str
    object: str
    action: str
    description: str = ""
    positive_examples: List[str] = Field(default_factory=list)
    boundary_notes: List[str] = Field(default_factory=list)
    # Optional aliases used when recalling workflow-local leaf topics.
    domain_aliases: List[str] = Field(default_factory=list)
    object_aliases: List[str] = Field(default_factory=list)
    action_aliases: List[str] = Field(default_factory=list)
    # Concrete variants that live under one Atomic Task without receiving
    # their own task_id. Example: 话费/充值 -> 自定义缴费金额、指定号码+自定义缴费金额。
    subtasks: List[str] = Field(default_factory=list)
    created_in_version: int = 1

    @field_validator(
        "positive_examples", "boundary_notes",
        "domain_aliases", "object_aliases", "action_aliases", "subtasks",
        mode="before",
    )
    @classmethod
    def normalize_text_lists(cls, value):
        return _as_str_list(value)


class Taxonomy(BaseModel):
    version: int = 1
    domains: List[DomainDef]
    tasks: List[TaskDef] = Field(default_factory=list)


class SkillRoute(BaseModel):
    primary_skill_id: Optional[str] = None
    candidate_skill_ids: List[str] = Field(default_factory=list)
    route_mode: RouteMode = "unrouted"
    confidence: float = 0.0
    reason: str = ""
    matched_rules: List[str] = Field(default_factory=list)
    rejected_skills: List[Dict[str, str]] = Field(default_factory=list)


class SemanticIntent(BaseModel):
    status: Status = "discovery"
    domain: str = "其他"
    object: str = ""
    action: str = ""
    span: str = ""
    slots: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    matched_task_id: Optional[str] = None
    match_reason: str = ""
    # Operational failures are retryable and must not become discovery evidence.
    match_error: str = ""
    skill_route: Optional[SkillRoute] = None

    candidate_task_ids: List[str] = Field(default_factory=list)
    conflicting_task_ids: List[str] = Field(default_factory=list)

    @field_validator("status", mode="before")
    @classmethod
    def migrate_legacy_status(cls, value):
        # 向后兼容 v2 checkpoint / JSON。
        # old unknown = 语义明确但 taxonomy 未覆盖 -> discovery
        # old general 只是“通用能力”标签，不应该占用 classification status -> discovery
        if value in {"unknown", "general", None, ""}:
            return "discovery"
        return value


class SemanticResult(BaseModel):
    intents: List[SemanticIntent] = Field(default_factory=list)
    raw_text: str = ""


class PerIntentSemanticDecision(BaseModel):
    """Independent semantic re-parse for one already segmented Atomic Intent."""
    object: str = ""
    action: str = ""
    slots: Dict[str, Any] = Field(default_factory=dict)
    confidence: float = 0.0
    reason: str = ""

    @field_validator("object", "action", "reason", mode="before")
    @classmethod
    def normalize_nullable_strings(cls, value):
        return "" if value is None else str(value)

    @field_validator("slots", mode="before")
    @classmethod
    def normalize_nullable_slots(cls, value):
        return {} if value is None else value

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_nullable_confidence(cls, value):
        return 0.0 if value is None or value == "" else value


class PairwiseTaskDecision(BaseModel):
    task_id: str = ""
    match: bool = False
    confidence: float = 0.0
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def tolerate_pairwise_shapes(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "match" not in data:
            for key in ("is_match", "matched", "equivalent"):
                if key in data:
                    data["match"] = data[key]
                    break
        if not data.get("reason"):
            for key in ("rationale", "explanation", "match_reason"):
                if data.get(key):
                    data["reason"] = str(data[key])
                    break
        return data


FinalMatchStatus = Literal["known", "ambiguous", "discovery"]


class FinalTaskDecision(BaseModel):
    status: FinalMatchStatus = "discovery"
    selected_task_id: Optional[str] = None
    confidence: float = 0.0
    candidate_task_ids: List[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="before")
    @classmethod
    def tolerate_final_shapes(cls, value):
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "selected_task_id" not in data:
            for key in ("matched_task_id", "task_id", "selected"):
                if key in data:
                    data["selected_task_id"] = data[key]
                    break
        if "candidate_task_ids" not in data:
            for key in ("candidates", "ambiguous_task_ids"):
                if key in data:
                    data["candidate_task_ids"] = data[key]
                    break
        if not data.get("reason"):
            for key in ("rationale", "explanation", "match_reason"):
                if data.get(key):
                    data["reason"] = str(data[key])
                    break
        status = str(data.get("status", "")).strip().lower()
        aliases = {
            "match": "known", "matched": "known", "known": "known",
            "ambiguous": "ambiguous", "uncertain": "ambiguous",
            "no_match": "discovery", "unmatched": "discovery", "discovery": "discovery",
        }
        if status in aliases:
            data["status"] = aliases[status]
        elif data.get("selected_task_id"):
            data["status"] = "known"
        else:
            data["status"] = "discovery"
        return data

    @field_validator("candidate_task_ids", mode="before")
    @classmethod
    def normalize_final_candidates(cls, value):
        return _as_str_list(value)


class CandidateCluster(BaseModel):
    candidate_id: str
    source_status: str = "discovery"
    domain: str
    object: str
    action: str
    count: int
    examples: List[str] = Field(default_factory=list)


class ProposedTask(BaseModel):
    domain: str
    object: str
    action: str
    description: str = ""
    positive_examples: List[str] = Field(default_factory=list)
    boundary_notes: List[str] = Field(default_factory=list)
    subtasks: List[str] = Field(default_factory=list)

    @field_validator("positive_examples", "boundary_notes", "subtasks", mode="before")
    @classmethod
    def normalize_text_lists(cls, value):
        return _as_str_list(value)


class TaxonomyDecision(BaseModel):
    operation: Operation
    rationale: str
    candidate_ids: List[str] = Field(default_factory=list)
    target_task_ids: List[str] = Field(default_factory=list)
    new_tasks: List[ProposedTask] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def tolerate_local_llm_decision_shapes(cls, value):
        """Repair common local-LLM reviewer outputs conservatively.

        Reviewer output must never crash a completed classification round.  When
        the model omits an explicit operation, we infer SPLIT only from strong
        evidence (should_split=true or >=2 proposed new tasks for a task_id).
        Otherwise the safe default is KEEP, never ADD/MERGE.
        """
        if not isinstance(value, dict):
            return value

        data = dict(value)

        # Common field aliases.
        if "candidate_ids" not in data and "candidate_id" in data:
            data["candidate_ids"] = data.get("candidate_id")
        if "target_task_ids" not in data:
            if "target_task_id" in data:
                data["target_task_ids"] = data.get("target_task_id")
            elif "task_id" in data:
                data["target_task_ids"] = data.get("task_id")

        if "new_tasks" not in data:
            for key in ("proposed_tasks", "split_tasks", "replacement_tasks"):
                if key in data:
                    data["new_tasks"] = data.get(key)
                    break

        # Normalize / infer operation.
        op = data.get("operation", data.get("op", data.get("decision")))
        if isinstance(op, str):
            op_map = {
                "add": "ADD", "新增": "ADD",
                "merge": "MERGE", "合并": "MERGE",
                "split": "SPLIT", "拆分": "SPLIT",
                "keep": "KEEP", "保持": "KEEP", "保留": "KEEP",
                "no_split": "KEEP", "不拆分": "KEEP",
            }
            op = op_map.get(op.strip().lower(), op_map.get(op.strip(), op.strip().upper()))

        if op not in {"ADD", "MERGE", "SPLIT", "KEEP"}:
            should_split = data.get("should_split", data.get("need_split", data.get("split")))
            new_tasks = _as_list(data.get("new_tasks"))
            if should_split is True or (data.get("task_id") and len(new_tasks) >= 2):
                op = "SPLIT"
            else:
                # Conservative fallback: malformed reviewer output must not mutate taxonomy.
                op = "KEEP"
        data["operation"] = op

        # Rationale aliases; missing rationale is valid for a safe KEEP.
        if not data.get("rationale"):
            for key in ("reason", "analysis", "explanation", "notes", "conclusion", "boundary_analysis"):
                v = data.get(key)
                if v:
                    if isinstance(v, list):
                        v = "；".join(str(x) for x in v)
                    data["rationale"] = str(v)
                    break
        if not data.get("rationale"):
            data["rationale"] = "Reviewer 未给出完整 TaxonomyDecision；按安全策略 KEEP，不修改 taxonomy。"

        return data

    @field_validator("candidate_ids", "target_task_ids", mode="before")
    @classmethod
    def normalize_id_lists(cls, value):
        return _as_str_list(value)

    @field_validator("new_tasks", mode="before")
    @classmethod
    def normalize_new_tasks(cls, value):
        return _as_list(value)

    @model_validator(mode="after")
    def validate_mutation_is_actionable(self):
        """Downgrade incomplete mutation proposals to KEEP instead of failing/applying damage."""
        invalid = False
        why = ""
        if self.operation == "ADD" and not self.new_tasks:
            invalid, why = True, "ADD 缺少 new_tasks"
        elif self.operation == "MERGE" and (not self.target_task_ids or not self.new_tasks):
            invalid, why = True, "MERGE 缺少 target_task_ids 或 new_tasks"
        elif self.operation == "SPLIT" and (not self.target_task_ids or len(self.new_tasks) < 2):
            invalid, why = True, "SPLIT 必须包含 target_task_ids 且至少两个 new_tasks"

        if invalid:
            self.operation = "KEEP"
            self.rationale = f"{self.rationale}；{why}，已自动降级为 KEEP。"
        return self


class TaxonomyDecisionBatch(BaseModel):
    decisions: List[TaxonomyDecision] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def tolerate_batch_shapes(cls, value):
        if isinstance(value, list):
            return {"decisions": value}
        if not isinstance(value, dict):
            return value
        data = dict(value)
        if "decisions" not in data:
            if "decision" in data and isinstance(data["decision"], (dict, list)):
                data["decisions"] = data["decision"]
            elif any(k in data for k in ("operation", "task_id", "should_split", "new_tasks", "proposed_tasks", "split_tasks")):
                # Some local models return one decision object without the wrapper.
                data = {"decisions": [data]}
            else:
                data["decisions"] = []
        return data

    @field_validator("decisions", mode="before")
    @classmethod
    def normalize_decisions(cls, value):
        return _as_list(value)


class RoutingPolicy(BaseModel):
    principle: str = ""
    priority_order: List[str] = Field(default_factory=list)
    global_rules: List[str] = Field(default_factory=list)


class SkillDef(BaseModel):
    skill_id: str
    name: str
    description: str
    executor_type: str = ""
    routing_priority: int = 0
    include_rules: List[str] = Field(default_factory=list)
    exclude_rules: List[str] = Field(default_factory=list)
    examples: List[str] = Field(default_factory=list)
    counterexamples: List[str] = Field(default_factory=list)
    retrieval_keywords: List[str] = Field(default_factory=list)
    supported_task_ids: List[str] = Field(default_factory=list)


class SkillRegistry(BaseModel):
    version: int = 1
    routing_policy: RoutingPolicy = Field(default_factory=RoutingPolicy)
    skills: List[SkillDef] = Field(default_factory=list)
