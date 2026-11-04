from __future__ import annotations

import json
import logging
import re
import unicodedata
from typing import Literal

from pydantic import field_validator

from .config import Config
from .context import context_text, cosine
from .prompts import DEDUP_RULES, JUDGE_RULES
from .schema import Chunk, Context, Document, QA, Record, stable_id
from .synthesis import rules_for_context

log = logging.getLogger(__name__)


class JudgeResult(Record):
    """Minimal response for one dimension; normalization happens in Python."""

    score: Literal[1, 2, 3]

    @field_validator("score", mode="before")
    @classmethod
    def require_integer_category(cls, value):
        if type(value) is not int:
            raise ValueError("score must be an integer category: 1, 2 or 3")
        return value


SCORE_MAPPING = {1: 0.0, 2: 0.5, 3: 1.0}
DIMENSION_WEIGHT = 0.2


class DuplicateResult(Record):
    duplicate: bool
    reason: str


def numeric_literals(text: str) -> set[str]:
    # Normalize grouping commas, preserving decimals and ordinal/range endpoints.
    return set(
        re.findall(
            r"(?<!\w)\d+(?:\.\d+)?",
            re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", unicodedata.normalize("NFKC", text)),
        )
    )


def deterministic_checks(
    qa: QA, chunks: dict[str, Chunk], docs: dict[str, Document], context: Context | None
) -> list[str]:
    reasons = []
    if not qa.question.strip() or not qa.answer.strip():
        reasons.append("empty_question_or_answer")
    # Standalone references are assessed by the Judge. A word blacklist cannot
    # distinguish a missing referent from a pronoun defined in the question itself.
    if not qa.evidence:
        reasons.append("missing_evidence")
    allowed = set(context.chunk_ids + context.support_chunk_ids) if context else set()
    for ev in qa.evidence:
        chunk, doc = chunks.get(ev.chunk_id), docs.get(ev.document_id)
        if chunk is None or doc is None or context is None or ev.chunk_id not in allowed:
            reasons.append("unknown_evidence_reference")
            continue
        if chunk.document_id != ev.document_id or ev.source_file != doc.path:
            reasons.append("evidence_source_mismatch")
        if not (chunk.char_start <= ev.char_start < ev.char_end <= chunk.char_end):
            reasons.append("evidence_offset_out_of_chunk")
        if not ev.quote.strip() or doc.text[ev.char_start : ev.char_end] != ev.quote:
            reasons.append("evidence_quote_not_in_source")
        if ev.quote not in chunk.text:
            reasons.append("evidence_quote_not_in_chunk")
        if chunk.text != doc.text[chunk.char_start : chunk.char_end]:
            reasons.append("chunk_snapshot_mismatch")
    evidence = "\n".join(e.quote for e in qa.evidence)
    for ev in qa.evidence:
        chunk = chunks.get(ev.chunk_id)
        if (
            chunk
            and chunk.table_header
            and not any(
                chunk.table_header in other.quote
                for other in qa.evidence
                if other.document_id == ev.document_id
            )
        ):
            reasons.append("missing_table_column_evidence")
    if numeric_literals(qa.answer) - numeric_literals(evidence):
        reasons.append("unsupported_numeric_literal")
    for unit in re.findall(r"\b(?:EGP|USD|EUR|km/h|km|cm|million|billion)\b", qa.answer, re.I):
        if not re.search(r"(?<!\w)" + re.escape(unit) + r"(?!\w)", evidence, re.I):
            reasons.append("unsupported_unit_or_currency")
    actual_docs = sorted({e.document_id for e in qa.evidence})
    if sorted(qa.source_document_ids) != actual_docs:
        reasons.append("source_document_ids_mismatch")
    if context and len(context.document_ids) > 1:
        primary_docs_used = {
            ev.document_id for ev in qa.evidence if ev.chunk_id in context.chunk_ids
        }
        if primary_docs_used != set(context.document_ids):
            reasons.append("cross_document_evidence_incomplete")
    return sorted(set(reasons))


def validate_qa(
    qa: QA,
    context: Context | None,
    chunks: dict[str, Chunk],
    docs: dict[str, Document],
    config: Config,
    remote=None,
) -> QA:
    qa = qa.model_copy(deep=True)
    qa.quality_scores = {}
    qa.judge_result = None
    qa.duplicate_of = None
    qa.dedup_group_id = None
    qa.reasons = [r for r in qa.reasons if r.startswith("offline_smoke")]
    failures = deterministic_checks(qa, chunks, docs, context)
    if failures:
        qa.review_status = "rejected"
        qa.reasons.extend(failures)
        return qa
    if remote is None or qa.generation_metadata.get("engine") == "offline_smoke":
        qa.review_status = "pending_review"
        qa.reasons.append("llm_judge_not_run")
        return qa
    try:
        payload = json.dumps(
            {
                "question": qa.question,
                "answer": qa.answer,
                "relation_type": context.relation_type,
                "primary_chunk_ids": context.chunk_ids,
                "primary_document_ids": context.document_ids,
                "language": config.language,
                "task_rules": rules_for_context(context, docs, config.synthesis),
                "evidence": context_text(context, chunks),
                "citations": [{"chunk_id": ev.chunk_id, "quote": ev.quote} for ev in qa.evidence],
            },
            ensure_ascii=False,
        )
        raw_scores = {}
        for dimension, rules in JUDGE_RULES.items():
            result = remote.structured(rules, payload, JudgeResult)
            raw_scores[dimension] = result.score
        # Publish scores only after all five requests succeed. Partial results
        # must never become an acceptance decision.
        qa.judge_result = raw_scores
        qa.quality_scores = {
            dimension: SCORE_MAPPING[score] for dimension, score in raw_scores.items()
        }
        overall = round(sum(qa.quality_scores.values()) * DIMENSION_WEIGHT, 10)
        qa.quality_scores["overall"] = overall
        if overall < config.quality.overall_threshold:
            qa.review_status = "rejected"
            qa.reasons.append("below_threshold_overall")
        else:
            qa.review_status = "auto_pass"
    except Exception as exc:
        log.exception("Judge failed: qa_id=%s", qa.qa_id)
        qa.review_status = "pending_review"
        qa.reasons.append("judge_error:" + type(exc).__name__)
    return qa


def normalize_question(text: str) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", text).casefold()))


def scope_signature(text: str) -> tuple:
    # Preserve entity-like phrases and every number before semantic adjudication.
    capitals = re.findall(r"\b[A-Z][a-z]+(?:[ -][A-Z][a-z]+)*\b", text)
    ignore = {"What", "Which", "How", "According", "Does", "Is", "Are", "The", "In"}
    entities = tuple(sorted(p.casefold() for p in capitals if p not in ignore))
    return tuple(sorted(numeric_literals(text))), entities


def deduplicate(records: list[QA], config: Config, remote=None) -> list[QA]:
    # Prefer an auto-passed canonical record over a pending/rejected identical question.
    priority = {"auto_pass": 0, "pending_review": 1, "rejected": 2, "duplicate": 3}
    ordered = sorted(
        enumerate(records), key=lambda item: (priority[item[1].review_status], item[0])
    )
    canonical = {}
    for _, qa in ordered:
        key = normalize_question(qa.question)
        if not key or qa.review_status == "rejected":
            continue
        if key in canonical:
            other = canonical[key]
            if normalize_question(qa.answer) != normalize_question(other.answer):
                qa.review_status = other.review_status = "pending_review"
                qa.reasons.append("conflicting_answers_for_same_question")
                other.reasons.append("conflicting_answers_for_same_question")
            else:
                mark_duplicate(qa, other)
        else:
            canonical[key] = qa
    candidates = [q for q in canonical.values() if q.review_status == "auto_pass"]
    if remote and config.quality.semantic_dedup and len(candidates) > 1:
        try:
            vectors = remote.embed([q.question for q in candidates], config.context.embedding_model)
            for i, qa in enumerate(candidates):
                if qa.review_status != "auto_pass":
                    continue
                for j in range(i):
                    other = candidates[j]
                    if other.review_status != "auto_pass" or numeric_literals(
                        qa.question
                    ) != numeric_literals(other.question):
                        continue
                    known_a = {
                        e.casefold()
                        for e in config.context.entities
                        if re.search(r"(?<!\w)" + re.escape(e) + r"(?!\w)", qa.question, re.I)
                    }
                    known_b = {
                        e.casefold()
                        for e in config.context.entities
                        if re.search(r"(?<!\w)" + re.escape(e) + r"(?!\w)", other.question, re.I)
                    }
                    if known_a != known_b:
                        continue
                    if (
                        cosine(vectors[i], vectors[j])
                        >= config.quality.semantic_candidate_threshold
                    ):
                        verdict = remote.structured(
                            DEDUP_RULES,
                            json.dumps(
                                {
                                    "a": {"question": other.question, "answer": other.answer},
                                    "b": {"question": qa.question, "answer": qa.answer},
                                },
                                ensure_ascii=False,
                            ),
                            DuplicateResult,
                        )
                        if verdict.duplicate:
                            mark_duplicate(qa, other)
                            break
        except Exception as exc:
            log.exception("Semantic deduplication failed")
            # A failed semantic check cannot silently result in a fully approved dataset.
            for qa in candidates:
                if qa.review_status == "auto_pass":
                    qa.review_status = "pending_review"
                    qa.reasons.append("semantic_dedup_error:" + type(exc).__name__)
    return records


def mark_duplicate(qa: QA, canonical: QA):
    group = canonical.dedup_group_id or stable_id("dedup", canonical.qa_id)
    canonical.dedup_group_id = qa.dedup_group_id = group
    qa.duplicate_of = canonical.qa_id
    qa.review_status = "duplicate"
    qa.reasons.append("duplicate_question")
