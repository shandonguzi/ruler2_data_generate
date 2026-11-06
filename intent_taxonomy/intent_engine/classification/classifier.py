from __future__ import annotations

import json
import re
from difflib import SequenceMatcher

from intent_engine.infrastructure.llm_client import LocalLLM
from intent_engine.models import (
    FinalTaskDecision,
    PairwiseTaskDecision,
    PerIntentSemanticDecision,
    SemanticIntent,
    SemanticResult,
    Taxonomy,
)


from intent_engine.settings.prompt_settings import prompt_ref


ATOMIC_CLASSIFY_SYSTEM = prompt_ref("classifier.ATOMIC_CLASSIFY_SYSTEM")


PAIRWISE_SYSTEM = prompt_ref("classifier.PAIRWISE_SYSTEM")

FINAL_MATCH_SYSTEM = prompt_ref("classifier.FINAL_MATCH_SYSTEM")


class IntentClassifier:
    def __init__(
        self,
        llm: LocalLLM,
        taxonomy: Taxonomy,
        known_threshold: float = 0.72,
        matching_cfg: dict | None = None,
        task_retriever=None,
    ):
        self.llm = llm
        self.task_retriever = task_retriever
        self.taxonomy = taxonomy
        self.known_threshold = known_threshold
        self.matching_cfg = matching_cfg or {}

        self.task_top_k = max(1, min(3, int(self.matching_cfg.get("candidate_top_k", 3))))
        self.max_final_candidates = max(1, int(self.matching_cfg.get("max_final_candidates", 4)))
        self.pairwise_min_confidence = float(self.matching_cfg.get("pairwise_min_confidence", 0.50))
        self.final_judge_always = bool(self.matching_cfg.get("final_judge_always", True))

    @staticmethod
    def _intent_payload(it: SemanticIntent) -> dict:
        out = {
            "object": it.object,
            "action": it.action,
            "span": it.span,
            "slots": it.slots,
            "confidence": it.confidence,
        }
        return out

    @staticmethod
    def _isolated_intent_text(result: SemanticResult, intent: SemanticIntent) -> str:
        """Return text safe for per-intent downstream semantic/matching stages."""
        return (intent.span or "").strip() or (result.raw_text or "").strip()

    @staticmethod
    def _norm(value: str) -> str:
        value = (value or "").strip().lower()
        return re.sub(r"[\s\-_./·，。；;、:：()（）]+", "", value)

    @classmethod
    def _similarity(cls, a: str, b: str) -> float:
        a, b = cls._norm(a), cls._norm(b)
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0
        if a in b or b in a:
            return 0.88
        return SequenceMatcher(None, a, b).ratio()

    def _recall_score(self, intent: SemanticIntent, task) -> float:
        score = 0.0
        obj_candidates = [task.object, *(getattr(task, "object_aliases", []) or [])]
        act_candidates = [task.action, *(getattr(task, "action_aliases", []) or [])]

        obj_sim = max(self._similarity(intent.object, x) for x in obj_candidates if x)
        act_sim = max(self._similarity(intent.action, x) for x in act_candidates if x)
        score += 5.0 * obj_sim
        score += 5.0 * act_sim
        joint_sim = max(
            self._similarity(f"{intent.object}{intent.action}", f"{o}{a}")
            for o in obj_candidates if o
            for a in act_candidates if a
        )
        score += 2.5 * joint_sim

        intent_text = f"{intent.object} {intent.action} {intent.span}"
        score += 0.8 * self._similarity(intent_text, task.description)
        if task.positive_examples:
            score += 0.6 * max(
                self._similarity(intent.span, ex) for ex in task.positive_examples[:4]
            )
        subtasks = getattr(task, "subtasks", []) or []
        if subtasks:
            # Subtasks are concrete variants below the Atomic Task. They should
            # improve recall, but never outweigh canonical Object/Action.
            score += 1.2 * max(self._similarity(intent.span, st) for st in subtasks[:12])
        return score

    def _recall_tasks(self, intent: SemanticIntent, intent_text: str = ""):
        if not self.taxonomy.tasks:
            return []
        if self.task_retriever is not None:
            return self.task_retriever.recall(
                self.taxonomy, intent_text or intent.span, self.task_top_k)
        ranked = sorted(
            ((self._recall_score(intent, task), task) for task in self.taxonomy.tasks),
            key=lambda x: (-x[0], x[1].task_id),
        )
        return [task for _, task in ranked[: self.task_top_k]]

    @staticmethod
    def _task_payload(task, compact: bool = False) -> dict:
        data = {
            "task_id": task.task_id,
            "topic_path": [part for part in task.domain.split(" / ") if part],
            "object": task.object,
            "action": task.action,
            "description": task.description,
            "object_aliases": (getattr(task, "object_aliases", []) or [])[:8],
            "action_aliases": (getattr(task, "action_aliases", []) or [])[:8],
            "subtasks": (getattr(task, "subtasks", []) or [])[:12],
        }
        if not compact:
            data["positive_examples"] = task.positive_examples[:4]
            data["boundary_notes"] = task.boundary_notes[:4]
        return data

    def _pairwise_judge(
        self, result: SemanticResult, intent: SemanticIntent, task
    ) -> PairwiseTaskDecision:
        payload = {
            "intent_text": self._isolated_intent_text(result, intent),
            "atomic_intent": self._intent_payload(intent),
            "candidate_task": self._task_payload(task),
        }
        decision = self.llm.chat_json(
            PAIRWISE_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            PairwiseTaskDecision,
            max_tokens=int(self.matching_cfg.get("pairwise_max_tokens", 320)),
        )
        decision.task_id = task.task_id
        decision.confidence = max(0.0, min(1.0, float(decision.confidence or 0.0)))
        return decision

    def _final_judge(
        self,
        result: SemanticResult,
        intent: SemanticIntent,
        shortlisted: list[tuple[object, PairwiseTaskDecision]],
    ) -> FinalTaskDecision:
        payload = {
            "intent_text": self._isolated_intent_text(result, intent),
            "atomic_intent": self._intent_payload(intent),
            "shortlist": [
                {
                    "task": self._task_payload(task),
                    "pairwise_match": pair.match,
                    "pairwise_confidence": pair.confidence,
                    "pairwise_reason": pair.reason,
                }
                for task, pair in shortlisted
            ],
        }
        return self.llm.chat_json(
            FINAL_MATCH_SYSTEM,
            json.dumps(payload, ensure_ascii=False),
            FinalTaskDecision,
            max_tokens=int(self.matching_cfg.get("final_max_tokens", 520)),
        )

    def match(self, result: SemanticResult) -> SemanticResult:
        if not self.taxonomy.tasks or not result.intents:
            for it in result.intents:
                if it.status != "invalid":
                    it.status = "discovery"
                    it.matched_task_id = None
                    it.match_reason = "taxonomy_has_no_equivalent_task_yet"
            return result

        for it in result.intents:
            it.match_error = ""
            if it.status == "invalid":
                it.matched_task_id = None
                continue

            recalled = self._recall_tasks(it, self._isolated_intent_text(result, it))
            it.candidate_task_ids = [t.task_id for t in recalled]
            if not recalled:
                it.status = "discovery"
                it.matched_task_id = None
                it.confidence = 0.0
                it.match_reason = "task_recall_empty"
                continue

            pairwise = []
            failures = []
            for task in recalled:
                try:
                    decision = self._pairwise_judge(result, it, task)
                except Exception as exc:
                    failures.append(f"{task.task_id}: {exc}")
                    decision = PairwiseTaskDecision(
                        task_id=task.task_id,
                        match=False,
                        confidence=0.0,
                        reason=f"pairwise_request_failed: {exc}",
                    )
                pairwise.append((task, decision))

            if failures:
                # A failed comparison is not a semantic rejection. Keep the
                # intent for inspection, exclude it from discovery, and retry
                # this row on the next run instead of checkpointing it.
                it.status = "ambiguous"
                it.matched_task_id = None
                it.confidence = 0.0
                it.match_error = "pairwise_request_failed: " + "; ".join(failures)
                it.match_reason = it.match_error
                continue

            shortlisted = [
                (task, decision)
                for task, decision in pairwise
                if decision.match and decision.confidence >= self.pairwise_min_confidence
            ]
            shortlisted.sort(key=lambda x: x[1].confidence, reverse=True)
            shortlisted = shortlisted[: self.max_final_candidates]

            if not shortlisted:
                it.status = "discovery"
                it.matched_task_id = None
                it.confidence = 0.0
                it.match_reason = (
                    "pairwise_no_equivalent_task; recalled="
                    + ",".join(it.candidate_task_ids)
                )
                continue

            if len(shortlisted) == 1 and not self.final_judge_always:
                task, decision = shortlisted[0]
                it.matched_task_id = task.task_id
                it.domain = task.domain
                it.confidence = decision.confidence
                it.status = (
                    "known" if decision.confidence >= self.known_threshold else "ambiguous"
                )
                it.match_reason = "pairwise_direct: " + decision.reason
                continue

            try:
                final = self._final_judge(result, it, shortlisted)
            except Exception as exc:
                task, decision = shortlisted[0]
                it.matched_task_id = task.task_id
                it.confidence = decision.confidence
                it.status = "ambiguous"
                it.match_error = f"final_judge_failed: {exc}"
                it.match_reason = (
                    f"final_judge_failed; best_pairwise={task.task_id}: {exc}"
                )
                continue

            valid_shortlist = {task.task_id for task, _ in shortlisted}
            selected = (
                final.selected_task_id
                if final.selected_task_id in valid_shortlist
                else None
            )
            confidence = max(0.0, min(1.0, float(final.confidence or 0.0)))

            if final.status == "discovery":
                it.status = "discovery"
                it.matched_task_id = None
                it.confidence = confidence
                it.match_reason = final.reason or "final_judge_rejected_shortlist"
                continue

            if final.status == "ambiguous":
                it.conflicting_task_ids = [key for key in final.candidate_task_ids if key in valid_shortlist]
                if len(it.conflicting_task_ids) < 2:
                    it.conflicting_task_ids = sorted(valid_shortlist) if len(valid_shortlist)>1 else []
            if final.status == "ambiguous" and not selected:
                it.status = "ambiguous"
                it.matched_task_id = None
                it.confidence = confidence
                it.match_reason = final.reason or "final_judge_ambiguous"
                continue

            if not selected:
                it.status = "discovery"
                it.matched_task_id = None
                it.confidence = confidence
                it.match_reason = "final_judge_selected_invalid_task; treated_as_discovery"
                continue

            it.matched_task_id = selected
            it.domain = next(task.domain for task, _ in shortlisted if task.task_id == selected)
            it.confidence = confidence
            if final.status == "ambiguous" or confidence < self.known_threshold:
                it.status = "ambiguous"
                prefix = (
                    "final_ambiguous: "
                    if final.status == "ambiguous"
                    else "low_confidence_final_match: "
                )
                it.match_reason = prefix + (final.reason or "")
            else:
                it.status = "known"
                it.match_reason = final.reason or "final_match"

        return result


    def classify(self, text: str) -> SemanticResult:
        text = "" if text is None else str(text).strip()
        if not text:
            return SemanticResult(raw_text=text, intents=[])
        from intent_engine.atomic.atomic_discovery import (
            EXTRACT_PROMPT, AtomicExtraction, validate_extraction, extraction_issues,
        )
        parsed = self.llm.chat_json(EXTRACT_PROMPT, json.dumps({"user_text": text}, ensure_ascii=False),
                                    schema=AtomicExtraction, max_tokens=2400)
        validate_extraction(text, parsed)
        issues = extraction_issues(text, parsed)
        if issues:
            parsed = self.llm.chat_json(EXTRACT_PROMPT, json.dumps({
                "user_text": text, "previous": parsed.model_dump(), "validation_feedback": issues,
            }, ensure_ascii=False), schema=AtomicExtraction, max_tokens=2400)
            validate_extraction(text, parsed)
            if extraction_issues(text, parsed):
                raise ValueError("原子意图提取未通过上下文检查")
        if len(parsed.intents) == 1:
            atom = parsed.intents[0]
            if not atom.context_indices and atom.text == atom.span:
                atom.span = atom.text = text
        intents = []
        for atom in parsed.intents:
            if atom.status != "valid":
                intents.append(SemanticIntent(status="ambiguous", span=atom.text))
                continue
            classified = self.classify_atomic(atom.text, semantics={"text":atom.text,"object":atom.object,"action":atom.action})
            intents.extend(classified.intents)
        return SemanticResult(raw_text=text, intents=intents)


    def classify_atomic(self, text: str, slots: dict | None = None, semantics: dict | None = None) -> SemanticResult:
        """Classify one already segmented action without consolidating sibling actions."""
        if (isinstance(semantics,dict) and semantics.get('text')==text
                and isinstance(semantics.get('object'),str) and semantics['object'].strip()
                and isinstance(semantics.get('action'),str) and semantics['action'].strip()):
            # Initial extraction already resolved semantics; do not request them again.
            intent = SemanticIntent(object=semantics['object'].strip(),action=semantics['action'].strip(),
                                    slots=slots or {},span=text)
            return self.match(SemanticResult(raw_text=text,intents=[intent]))
        prompt = ATOMIC_CLASSIFY_SYSTEM
        parsed = self.llm.chat_json(prompt, json.dumps({"atomic_text": text, "slots": slots or {}}, ensure_ascii=False),
            PerIntentSemanticDecision)
        if not parsed.object.strip() or not parsed.action.strip():
            raise ValueError("原子意图语义解析缺少对象或动作")
        intent = SemanticIntent(domain="", object=parsed.object,
            action=parsed.action, slots={**(slots or {}), **parsed.slots}, span=text,
            confidence=max(0., min(1., parsed.confidence)))
        return self.match(SemanticResult(raw_text=text, intents=[intent]))
