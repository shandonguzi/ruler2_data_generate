"""Complete four-judge evaluation → one repair → complete re-evaluation."""
from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy

from . import JUDGES
from .decision import summarize_results
from .llm_client import PermanentAPIError, TaskError
from .records import digest, now
from .repair import build_repair_payload, dialogue_diff, validate_repair_response
from .schemas import REPAIR_SCHEMA, judge_schema
from .validators import validate_judge_result


def build_judge_payload(case: dict, judge: str) -> dict:
    keys = {
        JUDGES[0]: ("factors", "dialogue"), JUDGES[1]: ("seed_qa", "dialogue"),
        JUDGES[2]: ("dialogue",), JUDGES[3]: ("persona", "dialogue"),
    }
    if judge not in keys:
        raise ValueError(f"Unknown judge: {judge}")
    payload = {key: deepcopy(case[key]) for key in keys[judge]}
    if judge == JUDGES[0]:
        payload["factors"] = {
            section: {key: spec for key, spec in factors.items() if spec.get("required", True)}
            for section, factors in payload["factors"].items()
        }
    return payload


def empty_judge(name: str, status: str, reason: str) -> dict:
    return {"judge_name": name, "status": status, "score": None, "reason": reason,
            "evidence_turn_ids": [], "details": {}}


def commit_revision(candidate: dict, parent, repair_attempt) -> dict:
    return {
        "revision_id": f"r{int(parent['revision_id'][1:]) + 1}" if parent else "r0",
        "parent_revision_id": parent["revision_id"] if parent else None,
        "source": "repair" if parent else "original",
        "repair_round": repair_attempt["repair_round"] if repair_attempt else None,
        "dialogue": deepcopy(candidate["dialogue"]), "dialogue_hash": digest(candidate["dialogue"]),
        "created_at": now(), "dialogue_diff": dialogue_diff(parent["dialogue"], candidate["dialogue"]) if parent else [],
        "repair_attempt_id": repair_attempt["repair_attempt_id"] if repair_attempt else None,
    }


class Orchestrator:
    def __init__(self, config: dict, records, client):
        self.config, self.records, self.client = config, records, client

    def _judge(self, case: dict, revision: dict, name: str, binding: dict) -> dict:
        spec = self.config["judges"][name]
        payload = build_judge_payload(case, name)
        schema = judge_schema(name, case)
        common_fields = ("score", "reason", "evidence_turn_ids") if name == JUDGES[0] else ("score", "reason")
        identity = self.client.identity(case["case_id"], name, payload, spec, schema)
        task_id = digest(identity)
        saved = self.records.select("judge_results", **binding, judge_name=name)
        if saved and saved[-1].get("task_id") == task_id and saved[-1]["result"]["status"] == "COMPLETED":
            result = saved[-1]["result"]
            validate_judge_result(name, {key: result[key] for key in common_fields}
                                  | result["details"], case)
            return result
        audit = {"prompt_version": spec["version"], "prompt_sha256": spec["prompt_sha256"],
                 "response_schema_version": "mtqc-judge-v1", "response_schema_hash": digest(schema),
                 "model": spec["model"], "payload_hash": digest(payload), "task_id": task_id}
        try:
            outcome = self.client.call(identity, payload, spec, schema,
                                       lambda value: validate_judge_result(name, value, case), binding)
            value = outcome["result"]
            result = {"judge_name": name, "status": "COMPLETED", "score": value["score"],
                      "reason": value["reason"], "evidence_turn_ids": value.get("evidence_turn_ids", []),
                      "details": {key: item for key, item in value.items() if key not in common_fields},
                      **audit, "response_model": outcome["response_model"], "attempt_ids": outcome["attempt_ids"], "completed_at": now()}
        except TaskError as exc:
            result = {**empty_judge(name, "ERROR", str(exc)), **audit, "response_model": "", "completed_at": now(),
                      "permanent_error": isinstance(exc, PermanentAPIError),
                      "attempt_ids": [event["attempt_id"] for event in self.records.select("attempts", task_id=task_id) if event["event"] == "START"]}
        if not saved or saved[-1].get("result") != result:
            self.records.append("judge_results", {**binding, "judge_name": name, "task_id": task_id, "result": result})
        return result

    def evaluate_revision(self, original: dict, revision: dict, input_id: str) -> dict:
        case = {**original, "dialogue": revision["dialogue"]}
        binding = {"run_id": self.records.manifest["run_id"], "input_id": input_id,
                   "case_id": case["case_id"], "revision_id": revision["revision_id"]}
        results = {}
        if self.config["evaluation"]["fail_fast"]:
            stopped = False
            for name in JUDGES:
                if stopped:
                    result = empty_judge(name, "SKIPPED", "EARLY_STOP after a validated score of 1")
                    previous = self.records.select("judge_results", **binding, judge_name=name)
                    if not previous:
                        self.records.append("judge_results", {**binding, "judge_name": name, "task_id": None, "result": result})
                else:
                    result = self._judge(case, revision, name, binding)
                    stopped = result["status"] == "COMPLETED" and result["score"] == 1
                results[name] = result
        else:
            with ThreadPoolExecutor(max_workers=min(4, self.config["llm"]["concurrency"])) as executor:
                futures = {name: executor.submit(self._judge, case, revision, name, binding) for name in JUDGES}
                for name in JUDGES:
                    results[name] = futures[name].result()
        return {"case_id": case["case_id"], "revision_id": revision["revision_id"],
                **summarize_results(results, self.config["decision"])}

    def _revisions(self, input_id: str) -> list:
        return [event["revision"] for event in self.records.select("revisions", input_id=input_id)]

    def _repairs(self, input_id: str) -> list:
        latest = {}
        for event in self.records.select("repair_attempts", input_id=input_id):
            latest[event["repair_attempt_id"]] = event
        return list(latest.values())

    def _apply_saved_revisions(self, input_id: str):
        saved = {revision["revision_id"] for revision in self._revisions(input_id)}
        for repair in self._repairs(input_id):
            if repair.get("status") == "APPLIED" and repair["revision"]["revision_id"] not in saved:
                self.records.append("revisions", {"input_id": input_id, "case_id": repair["case_id"], "revision": repair["revision"]})
                saved.add(repair["revision"]["revision_id"])

    def repair_dialogue(self, original: dict, revision: dict, input_id: str) -> dict:
        repairs = self._repairs(input_id)
        pending = [repair for repair in repairs if repair["event"] == "START"]
        spec = self.config["repair"]
        if pending:
            start = pending[-1]
            payload = start["payload"]
        else:
            history = []
            for repair in repairs:
                source = next(item for item in self._revisions(input_id) if item["revision_id"] == repair["base_revision_id"])
                old_results = self._saved_evaluation(input_id, source["revision_id"])
                new_results = self._saved_evaluation(input_id, repair.get("result_revision_id"))
                history.append({"repair_round": repair["repair_round"], "status": repair["status"],
                                "reason": repair["reason"], "change_summary": repair["change_summary"],
                                "before_scores": old_results["scores"] if old_results else None,
                                "after_scores": new_results["scores"] if new_results else None,
                                "unresolved_dimensions": new_results["blocking_dimensions"] if new_results else [],
                                "unresolved_feedback": [entry for entry in new_results["dimension_feedback"]
                                                        if entry["judge_name"] in new_results["blocking_dimensions"]] if new_results else []})
            round_no = len(repairs) + 1
            payload = build_repair_payload(original, revision, history, self.config["decision"], round_no, spec["max_rounds"])
            start = self.records.append("repair_attempts", {
                "event": "START", "input_id": input_id, "case_id": original["case_id"],
                "repair_attempt_id": uuid.uuid4().hex, "repair_round": round_no,
                "base_revision_id": revision["revision_id"], "result_revision_id": None,
                "payload": payload, "input_hash": digest(payload), "prompt_version": spec["version"],
                "prompt_sha256": spec["prompt_sha256"], "model": spec["model"],
                "task_id": digest(self.client.identity(original["case_id"], "dialogue_repair", payload, spec, REPAIR_SCHEMA)),
            })
        low_scores = [f"{label} {revision['evaluation']['scores'][name]}"
                      for name, label in zip(JUDGES, ("因子", "问答", "连贯", "画像"))
                      if revision["evaluation"]["scores"][name] is not None
                      and revision["evaluation"]["scores"][name] < 3]
        action = "继续修复" if pending else "触发修复"
        print(f"      ↳ {action} {start['repair_round']}/{spec['max_rounds']}：{'、'.join(low_scores)}（低于 3 分）", flush=True)
        identity = self.client.identity(original["case_id"], "dialogue_repair", payload, spec, REPAIR_SCHEMA)
        binding = {"input_id": input_id, "case_id": original["case_id"], "revision_id": revision["revision_id"],
                   "repair_attempt_id": start["repair_attempt_id"], "run_id": self.records.manifest["run_id"]}
        end = {**start, "event": "END", "change_summary": [], "attempt_ids": [], "task_id": digest(identity)}
        try:
            outcome = self.client.call(identity, payload, spec, REPAIR_SCHEMA,
                                       lambda value: validate_repair_response(value, original, revision), binding)
            value = outcome["result"]
            end.update(output=value, reason=value["reason"], change_summary=value["change_summary"],
                       attempt_ids=outcome["attempt_ids"], response_model=outcome["response_model"])
            if value["action"] == "blocked":
                end["status"] = "BLOCKED"
            elif value["action"] == "unchanged":
                end["status"] = "NO_CHANGE"
            else:
                candidate = validate_repair_response(value, original, revision)
                candidate_hash = digest(candidate["dialogue"])
                duplicate = next((old for old in self._revisions(input_id) if old["dialogue_hash"] == candidate_hash), None)
                if candidate_hash == revision["dialogue_hash"]:
                    end["status"] = "NO_CHANGE"
                elif duplicate:
                    end.update(status="DUPLICATE", duplicate_revision_id=duplicate["revision_id"])
                else:
                    new_revision = commit_revision(candidate, revision, start)
                    end.update(status="APPLIED", result_revision_id=new_revision["revision_id"], revision=new_revision)
        except TaskError as exc:
            end.update(status="ERROR", reason=str(exc), permanent_error=isinstance(exc, PermanentAPIError), attempt_ids=[
                event["attempt_id"] for event in self.records.select("attempts", task_id=digest(identity)) if event["event"] == "START"])
        # END contains the full candidate, enabling recovery across the two-file commit.
        self.records.append("repair_attempts", end)
        self._apply_saved_revisions(input_id)
        if end.get("permanent_error"):
            raise PermanentAPIError(end["reason"])
        return end

    def _saved_evaluation(self, input_id: str, revision_id):
        results = {}
        for event in self.records.select("judge_results", input_id=input_id, revision_id=revision_id):
            results[event["judge_name"]] = event["result"]
        if not results:
            return None
        for name in JUDGES:
            results.setdefault(name, empty_judge(name, "ERROR", "Evaluation interrupted; no result saved"))
        item = next(item for item in self.records.select("original_cases", input_id=input_id))
        return {"case_id": item["case_id"], "revision_id": revision_id,
                **summarize_results(results, self.config["decision"])}

    def run_case(self, item: dict) -> str:
        original, input_id = item["original_case"], item["input_id"]
        if original is None:
            return "INPUT_ERROR"
        self._apply_saved_revisions(input_id)
        if not self._revisions(input_id):
            self.records.append("revisions", {"input_id": input_id, "case_id": original["case_id"],
                                              "revision": commit_revision(original, None, None)})
        while True:
            revision = deepcopy(self._revisions(input_id)[-1])
            revision["evaluation"] = self.evaluate_revision(original, revision, input_id)
            permanent = [result["reason"] for result in revision["evaluation"]["judge_results"].values()
                         if result.get("permanent_error")]
            if permanent:
                # All dimensions of this version have settled; don't repeat a bad API
                # configuration across the rest of the batch.
                raise PermanentAPIError(permanent[0])
            stop = self.stop_reason(input_id, revision)
            if stop:
                return stop
            outcome = self.repair_dialogue(original, revision, input_id)
            if outcome["status"] != "APPLIED":
                return self._repair_stop(outcome["status"])

    @staticmethod
    def _repair_stop(status):
        return {"BLOCKED": "REPAIR_BLOCKED", "NO_CHANGE": "NO_CHANGE", "DUPLICATE": "DUPLICATE_REVISION", "ERROR": "REPAIR_ERROR"}[status]

    def stop_reason(self, input_id: str, revision: dict):
        evaluation = revision["evaluation"]
        if evaluation["quality_decision"] == "PASS":
            return "PASS_INITIAL" if revision["revision_id"] == "r0" else "PASS_AFTER_REPAIR"
        if evaluation["technical_failures"]:
            return "JUDGE_ERROR"
        if evaluation["skipped_dimensions"]:
            return "EARLY_STOP"
        repairs = self._repairs(input_id)
        if repairs and repairs[-1].get("status") in ("BLOCKED", "NO_CHANGE", "DUPLICATE", "ERROR"):
            return self._repair_stop(repairs[-1]["status"])
        spec = self.config["repair"]
        if not spec["enabled"] or spec["max_rounds"] == 0:
            return "REPAIR_DISABLED"
        if repairs and repairs[-1]["event"] == "START":
            return None
        if len(repairs) >= spec["max_rounds"]:
            return "MAX_REPAIR_ROUNDS"
        return None
