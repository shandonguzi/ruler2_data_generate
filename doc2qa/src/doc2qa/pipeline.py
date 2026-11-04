from __future__ import annotations

import importlib.metadata
import json
import logging
import random
import time
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from .chunking import chunk_document
from .config import Config, require_models
from .context import build_contexts
from .document import ingest
from .prompts import PROMPT_VERSION, prompt_sha256
from .progress import RunProgress, context_scope
from .quality import deduplicate, validate_qa
from .remote import Remote
from .schema import Chunk, Context, Document, QA, digest
from .storage import export, read_jsonl, write_json, write_jsonl
from .synthesis import (
    align_docs_golden,
    generate_context,
    generate_docs,
    map_golden,
    require_contexts_for_rules,
    rules_for_context,
    smoke_candidates,
)

log = logging.getLogger(__name__)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def accrue_synthesis_cost(manifest: dict, cost: float | None):
    previous = manifest["deepeval_cost_usd"]
    # A total with any unpriced calls is unknown, including across resumes.
    manifest["deepeval_cost_usd"] = (
        previous + cost if previous is not None and cost is not None else None
    )


def version_info() -> dict:
    versions = {}
    for package in ("doc2qa", "deepeval", "openai", "tiktoken", "pypdf", "python-docx", "pydantic"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = "not_installed"
    return versions


def fingerprint(config: Config, docs: list[Document], smoke: bool, limit: int | None) -> str:
    code = {p.name: digest(p.read_bytes()) for p in Path(__file__).parent.glob("*.py")}
    return digest(
        json.dumps(
            {
                "config": config.model_dump(),
                "docs": [(d.document_id, d.path) for d in docs],
                "smoke": smoke,
                "limit": limit,
                "prompt": PROMPT_VERSION,
                "code": code,
                "versions": version_info(),
            },
            sort_keys=True,
        )
    )


def prepare(config: Config, directory: Path, remote=None):
    docs = ingest(Path(config.input), config.language)
    chunks = [chunk for doc in docs for chunk in chunk_document(doc, config.chunking)]
    contexts, index = build_contexts(chunks, config.context, remote)
    if not contexts:
        raise ValueError("No contexts fit the configured evidence/token constraints")
    directory.mkdir(parents=True, exist_ok=True)
    write_jsonl(directory / "documents.jsonl", docs)
    write_jsonl(directory / "chunks.jsonl", chunks)
    write_jsonl(directory / "contexts.jsonl", contexts)
    write_json(directory / "index.json", index)
    write_json(directory / "config.json", config.model_dump())
    summary = {
        "documents": len(docs),
        "chunks": len(chunks),
        "contexts": len(contexts),
        "cross_document_contexts": sum(len(c.document_ids) > 1 for c in contexts),
    }
    write_json(directory / "preparation.json", summary)
    return docs, chunks, contexts, summary


def load_bundle(directory: Path):
    docs = {d.document_id: d for d in read_jsonl(directory / "documents.jsonl", Document)}
    chunks = {c.chunk_id: c for c in read_jsonl(directory / "chunks.jsonl", Chunk)}
    contexts = {c.context_id: c for c in read_jsonl(directory / "contexts.jsonl", Context)}
    return docs, chunks, contexts


def generate(
    config: Config, smoke: bool = False, limit: int | None = None, resume: Path | None = None
) -> tuple[Path, dict]:
    random.seed(config.seed)
    if smoke and config.mode != "contexts":
        raise ValueError(
            "--smoke only supports --mode contexts; it does not impersonate DeepEval docs mode"
        )
    if config.mode == "docs" and config.context.strategy == "whole_document":
        raise ValueError(
            "Whole-document contexts require mode=contexts. "
            "For DeepEval docs mode set context.strategy=chunks (--context-strategy chunks)."
        )
    if config.mode == "docs":
        require_contexts_for_rules(config)
    if smoke and config.context.strategy == "chunks" and config.context.embedding == "openai":
        raise ValueError("Offline smoke requires context.embedding=tfidf")
    if not smoke:
        require_models(
            config,
            generation=True,
            judge=True,
            embedding=(
                config.mode == "docs"
                or (config.context.strategy == "chunks" and config.context.embedding == "openai")
                or config.quality.semantic_dedup
            ),
        )
    remote = None if smoke else Remote(config.synthesis)
    current_docs = ingest(Path(config.input), config.language)
    signature = fingerprint(config, current_docs, smoke, limit)
    root = Path(config.output)
    requested_file = root if root.suffix.lower() in {".jsonl", ".json", ".csv"} else None
    parent = root.parent if requested_file else root
    directory = resume or parent / (
        "run_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]
    )
    if resume:
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        if manifest["fingerprint"] != signature:
            raise ValueError(
                "Resume refused: input, configuration, code, dependencies, or smoke/limit changed"
            )
        docs, chunks, context_lookup = load_bundle(directory)
        contexts = list(context_lookup.values())
    else:
        _, _, _, prep_summary = prepare(config, directory, remote)
        docs, chunks, context_lookup = load_bundle(directory)
        contexts = list(context_lookup.values())
        manifest = {
            "run_id": directory.name,
            "started_at": utc_now(),
            "fingerprint": signature,
            "smoke": smoke,
            "limit_contexts": limit,
            "config": config.model_dump(),
            "versions": version_info(),
            "prompt_version": PROMPT_VERSION,
            "prompt_sha256": prompt_sha256(
                document_rules=config.synthesis.document_rules,
                context_rules=config.synthesis.context_rules,
            ),
            "input_sha256": {d.path: d.file_sha256 for d in docs.values()},
            "preparation": prep_summary,
            "deepeval_cost_usd": 0.0,
            "usage": {},
            "errors": [],
        }
    manifest["status"] = "running"
    manifest["errors"] = [
        e for e in manifest.get("errors", []) if e.get("stage") == "source_alignment"
    ]
    write_json(directory / "manifest.json", manifest)
    checkpoint_dir = directory / "checkpoints"
    checkpoint_dir.mkdir(exist_ok=True)
    records = []
    started = time.monotonic()
    with RunProgress(directory) as progress:
        try:
            if config.mode == "contexts":
                selected = contexts[:limit] if limit is not None else contexts
                for index, context in enumerate(selected):
                    path = checkpoint_dir / (context.context_id + ".jsonl")
                    if path.exists():
                        cached = read_jsonl(path, QA)
                        records.extend(cached)
                        progress.record(
                            "context",
                            "restored",
                            f"Context {index + 1}/{len(selected)}：复用 {len(cached)} 条已保存候选",
                            context_id=context.context_id,
                            counts={"candidates_saved": len(cached)},
                        )
                        continue
                    with context_scope(context.context_id):
                        try:
                            context_rules = rules_for_context(context, docs, config.synthesis)
                            progress.record(
                                "context",
                                "started",
                                f"Context {index + 1}/{len(selected)}：开始合成，最多 {config.synthesis.max_goldens_per_context} 条候选",
                                source_files=[docs[d].path for d in context.document_ids],
                                context_rules=context_rules,
                                counts={
                                    "inputs_returned": None,
                                    "inputs_used": None,
                                    "answers_generated": 0,
                                    "candidates_saved": 0,
                                    "qas_validated": 0,
                                    "accepted_before_dedup": None,
                                    "final_accepted": None,
                                },
                            )
                            if smoke:
                                batch = smoke_candidates(
                                    context, chunks, docs, config, manifest["run_id"]
                                )
                            else:
                                goldens, cost = generate_context(
                                    context, chunks, config, context_rules
                                )
                                if not goldens:
                                    raise ValueError(
                                        "DeepEval returned no goldens for this context"
                                    )
                                batch = [
                                    map_golden(
                                        g,
                                        context,
                                        chunks,
                                        docs,
                                        config,
                                        manifest["run_id"],
                                        i,
                                    )
                                    for i, g in enumerate(goldens)
                                ]
                                accrue_synthesis_cost(manifest, cost)
                            write_jsonl(path, batch)
                            records.extend(batch)
                            progress.record(
                                "context",
                                "completed",
                                f"Context {index + 1}/{len(selected)}：已保存 {len(batch)} 条完整候选 QA，最终审核待进行",
                                counts={"candidates_saved": len(batch)},
                                checkpoint=str(path),
                                output=[qa.model_dump() for qa in batch],
                            )
                            write_json(directory / "manifest.json", manifest)
                        except Exception as exc:
                            manifest["errors"].append(
                                {
                                    "context_id": context.context_id,
                                    "stage": "synthesis",
                                    "error_type": type(exc).__name__,
                                    "error_message": str(exc),
                                }
                            )
                            write_json(directory / "manifest.json", manifest)
                            progress.record(
                                "context",
                                "failed",
                                f"Context 合成失败：{type(exc).__name__}: {exc}",
                                error_type=type(exc).__name__,
                                error_message=str(exc),
                            )
                            log.exception(
                                "Context %s failed (%s); checkpoint remains retryable",
                                context.context_id,
                                type(exc).__name__,
                            )
            else:
                if limit is not None:
                    raise ValueError("--limit-contexts only applies to contexts mode")
                path = checkpoint_dir / "docs.jsonl"
                if path.exists():
                    records = read_jsonl(path, QA)
                else:
                    with progress.operation("docs_synthesis", "文档解析、证据构建与合成"):
                        goldens, cost = generate_docs(list(docs.values()), config)
                    if not goldens:
                        raise ValueError("DeepEval docs mode returned no goldens")
                    accrue_synthesis_cost(manifest, cost)
                    for i, golden in enumerate(goldens):
                        try:
                            context = align_docs_golden(golden, docs, chunks)
                            context_lookup[context.context_id] = context
                            records.append(
                                map_golden(
                                    golden,
                                    context,
                                    chunks,
                                    docs,
                                    config,
                                    manifest["run_id"],
                                    i,
                                )
                            )
                        except ValueError:
                            manifest["errors"].append(
                                {"stage": "source_alignment", "golden_index": i}
                            )
                            write_json(
                                directory / f"unaligned_golden_{i}.json",
                                {
                                    "input": golden.input,
                                    "expected_output": golden.expected_output,
                                    "context": golden.context,
                                    "review_status": "pending_review",
                                    "reason": "unmapped_source",
                                },
                            )
                    write_jsonl(directory / "chunks.jsonl", chunks.values())
                    write_jsonl(directory / "contexts.jsonl", context_lookup.values())
                    write_jsonl(path, records)
            validated = []
            for index, qa in enumerate(records, 1):
                path = checkpoint_dir / (qa.qa_id + ".validated.jsonl")
                with context_scope(qa.context_id):
                    with progress.operation(
                        "final_validation",
                        f"最终质量审核 {index}/{len(records)}",
                        qa_id=qa.qa_id,
                    ) as details:
                        if path.exists():
                            result = read_jsonl(path, QA)[0]
                            details["restored"] = True
                        else:
                            result = validate_qa(
                                qa, context_lookup.get(qa.context_id), chunks, docs, config, remote
                            )
                            # Network failures remain retryable on resume; successful decisions are cached.
                            if not any(r.startswith("judge_error:") for r in result.reasons):
                                write_jsonl(path, [result])
                        details["output"] = result.model_dump()
                    validated.append(result)
                    same_context = [q for q in validated if q.context_id == qa.context_id]
                    counts = Counter(q.review_status for q in same_context)
                    progress.record(
                        "final_validation",
                        "result",
                        f"最终审核结果：{result.review_status}；本 Context 已审核 {len(same_context)} 条，"
                        f"暂通过 {counts['auto_pass']} 条（去重尚未完成）",
                        qa_id=qa.qa_id,
                        review_status=result.review_status,
                        quality_scores=result.quality_scores,
                        reasons=result.reasons,
                        counts={
                            "qas_validated": len(same_context),
                            "accepted_before_dedup": counts["auto_pass"],
                        },
                    )
            with progress.operation("deduplication", f"精确及语义去重：{len(validated)} 条候选"):
                records = deduplicate(validated, config, remote)
            with progress.operation("export", "导出数据及统计"):
                summary = export(records, directory)
            for context_id in {q.context_id for q in records}:
                results = [q for q in records if q.context_id == context_id]
                counts = Counter(q.review_status for q in results)
                progress.record(
                    "context_summary",
                    "completed",
                    f"最终结果：候选 {len(results)}，通过 {counts['auto_pass']}，"
                    f"待审 {counts['pending_review']}，拒绝 {counts['rejected']}，重复 {counts['duplicate']}",
                    context_id=context_id,
                    counts={
                        "final_candidates": len(results),
                        "final_accepted": counts["auto_pass"],
                        "final_pending_review": counts["pending_review"],
                        "final_rejected": counts["rejected"],
                        "final_duplicates": counts["duplicate"],
                    },
                )
            if requested_file:
                source = directory / ("dataset" + requested_file.suffix)
                from .storage import atomic_text

                atomic_text(requested_file, source.read_text(encoding="utf-8-sig"))
            manifest["summary"] = summary
            manifest["status"] = "partial" if manifest["errors"] else "completed"
            progress.snapshot["result_status"] = manifest["status"]
            progress.record("summary", "completed", f"批次统计：{summary}", output=summary)
        except KeyboardInterrupt:
            manifest["status"] = "interrupted"
            raise
        except Exception as exc:
            manifest["status"] = "failed"
            manifest["errors"].append(
                {"stage": "pipeline", "error_type": type(exc).__name__, "error_message": str(exc)}
            )
            raise
        finally:
            previous_usage = manifest.get("usage", {})
            if remote:
                manifest["usage"] = {
                    k: previous_usage.get(k, 0) + v for k, v in remote.usage.items()
                }
            manifest["judge_embedding_cost_usd"] = None  # Provider-specific; never invent pricing.
            manifest["elapsed_seconds_last_attempt"] = round(time.monotonic() - started, 3)
            manifest["updated_at"] = utc_now()
            write_json(directory / "manifest.json", manifest)
    return directory, manifest


def rededuplicate(
    input_path: Path | None, output: Path, bundle: Path, config: Config, *, exact_only: bool = False
):
    """Reuse pre-deduplication scores, without generation or quality judging."""
    if output.resolve() == bundle.resolve():
        raise ValueError("Choose a separate deduplication output directory")
    if input_path:
        sources = [input_path]
    elif (bundle / "scored_candidates.jsonl").exists():
        sources = [bundle / "scored_candidates.jsonl"]
    else:
        sources = sorted((bundle / "checkpoints").glob("*.validated.jsonl"))
    records = [qa for path in sources for qa in read_jsonl(path, QA)]
    if not input_path and sources and sources[0].parent == bundle / "checkpoints":
        original = bundle / "candidates.jsonl"
        if original.exists():
            order = {qa.qa_id: index for index, qa in enumerate(read_jsonl(original, QA))}
            records.sort(key=lambda qa: order.get(qa.qa_id, len(order)))
    if not records:
        raise ValueError(
            "No scored candidates found; use scored_candidates.jsonl or validated checkpoints"
        )
    if len({qa.qa_id for qa in records}) != len(records):
        raise ValueError("Scored input contains duplicate qa_id values")
    if any(
        qa.duplicate_of
        or qa.dedup_group_id
        or any(reason.startswith("semantic_dedup_error:") for reason in qa.reasons)
        for qa in records
    ):
        raise ValueError(
            "Use pre-deduplication scores, not candidates.jsonl after a deduplication attempt"
        )
    config = config.model_copy(deep=True)
    if exact_only:
        config.quality.semantic_dedup = False
    if config.quality.semantic_dedup:
        require_models(config, judge=True, embedding=True)
    remote = Remote(config.synthesis) if config.quality.semantic_dedup else None
    output.mkdir(parents=True, exist_ok=True)
    with RunProgress(output) as progress:
        write_jsonl(output / "scored_candidates.jsonl", records)
        with progress.operation("deduplication", f"仅重新去重：{len(records)} 条已评分候选"):
            records = deduplicate(records, config, remote)
        with progress.operation("export", "导出去重结果"):
            summary = export(records, output)
            write_json(output / "config.json", config.model_dump())
            for name in ("documents", "chunks", "contexts"):
                source = bundle / f"{name}.jsonl"
                if source.exists():
                    write_jsonl(output / source.name, read_jsonl(source))
            write_json(
                output / "deduplication_manifest.json",
                {
                    "sources": [str(path.resolve()) for path in sources],
                    "bundle": str(bundle.resolve()),
                    "timestamp": utc_now(),
                    "config": config.model_dump(),
                    "exact_only": exact_only,
                    "usage": remote.usage if remote else {},
                    "prompt_sha256": prompt_sha256(),
                },
            )
        progress.record("summary", "completed", f"去重统计：{summary}", output=summary)
    return summary


def revalidate(input_path: Path, output: Path, bundle: Path, config: Config, offline: bool):
    docs, chunks, contexts = load_bundle(bundle)
    if not offline:
        require_models(config, judge=True, embedding=config.quality.semantic_dedup)
    remote = None if offline else Remote(config.synthesis)
    output.mkdir(parents=True, exist_ok=True)
    with RunProgress(output) as progress:
        candidates = read_jsonl(input_path, QA)
        records = []
        for index, qa in enumerate(candidates, 1):
            with context_scope(qa.context_id):
                with progress.operation(
                    "final_validation", f"重新审核 {index}/{len(candidates)}", qa_id=qa.qa_id
                ) as details:
                    result = validate_qa(
                        qa, contexts.get(qa.context_id), chunks, docs, config, remote
                    )
                    details["output"] = result.model_dump()
                records.append(result)
                progress.record(
                    "final_validation",
                    "result",
                    f"重新审核 {index}/{len(candidates)}：{result.review_status}",
                    qa_id=qa.qa_id,
                    review_status=result.review_status,
                    quality_scores=result.quality_scores,
                    reasons=result.reasons,
                )
        # Preserve completed scoring independently of downstream embedding access.
        write_jsonl(output / "scored_candidates.jsonl", records)
        with progress.operation("deduplication", f"精确及语义去重：{len(records)} 条候选"):
            records = deduplicate(records, config, remote)
        with progress.operation("export", "导出重新审核结果"):
            summary = export(records, output)
            write_jsonl(output / "documents.jsonl", docs.values())
            write_jsonl(output / "chunks.jsonl", chunks.values())
            write_jsonl(output / "contexts.jsonl", contexts.values())
            write_json(output / "config.json", config.model_dump())
            write_json(
                output / "validation_manifest.json",
                {
                    "source": str(input_path.resolve()),
                    "bundle": str(bundle.resolve()),
                    "config": config.model_dump(),
                    "timestamp": utc_now(),
                    "usage": remote.usage if remote else {},
                    "offline": offline,
                },
            )
        progress.record("summary", "completed", f"重新审核统计：{summary}", output=summary)
    return summary
