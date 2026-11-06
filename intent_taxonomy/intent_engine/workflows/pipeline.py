"""Batch classification against an explicitly supplied workflow taxonomy."""
from __future__ import annotations
import json
import hashlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List
import pandas as pd
from tqdm import tqdm
from intent_engine.infrastructure.checkpoint import JsonlCheckpoint
from intent_engine.classification.classifier import IntentClassifier
from intent_engine.infrastructure.llm_client import LocalLLM
from intent_engine.models import SemanticResult
from intent_engine.infrastructure.utils import ensure_dir
from intent_engine.infrastructure.progress import ProgressReporter


class IterativePipeline:
    def __init__(self, cfg: dict, progress: ProgressReporter | None = None):
        self.cfg = cfg
        self.progress = progress or ProgressReporter()
        self.llm = LocalLLM(cfg["llm"])
        self.report_dir = ensure_dir(cfg["paths"]["reports_dir"])
        self.checkpoint_dir = ensure_dir(cfg["paths"]["checkpoint_dir"])

    def _start_stage(self, round_no: int, name: str):
        if getattr(self, "progress", None):
            self.progress.start(f"round_{round_no}:{name}")

    def classify_dataframe(self, df: pd.DataFrame, taxonomy, round_no: int) -> List[SemanticResult]:
        pcfg = self.cfg["pipeline"]
        text_col = pcfg["text_column"]
        retriever = None
        recall_method = self.cfg.get("task_matching", {}).get("recall_method", "lexical")
        if recall_method not in {"vector", "lexical"}:
            raise ValueError("task_matching.recall_method 必须是 vector 或 lexical")
        if recall_method == "vector" and taxonomy.tasks:
            from intent_engine.classification.vector_recall import VectorTaskRecall
            retriever = VectorTaskRecall(
                self.cfg.get("embedding", {}), self.checkpoint_dir / "task_embeddings",
                examples=int(self.cfg.get("task_matching", {}).get("embedding_examples", 4)),
            )
        self._discovery_encoder = retriever
        classifier = IntentClassifier(
            self.llm,
            taxonomy,
            known_threshold=pcfg.get("known_confidence_threshold", pcfg.get("unknown_confidence_threshold", 0.72)),
            matching_cfg=self.cfg.get("task_matching", {}),
            task_retriever=retriever,
        )

        # Cache identity covers ordered input, actual taxonomy contents and all
        # classification settings, including the model. Version alone is not
        # sufficient when the taxonomy contents change or another input is selected.
        texts = ["" if value is None else str(value).strip() for value in df[text_col]]
        identity = {
            "texts": texts,
            "atomic_semantics": df["_atomic_semantics"].tolist() if pcfg.get("atomic_input") and "_atomic_semantics" in df else [],
            "atomic_slots": df["_atomic_slots"].tolist() if pcfg.get("atomic_input") and "_atomic_slots" in df else [],
            "taxonomy": taxonomy.model_dump(),
            "settings": {key: self.cfg.get(key, {}) for key in (
                "pipeline", "llm", "embedding", "task_matching",
            )},
        }
        digest = hashlib.sha256(json.dumps(
            identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")).hexdigest()
        checkpoint = JsonlCheckpoint(str(self.checkpoint_dir / f"round_{round_no:02d}_taxv{taxonomy.version:03d}_semanticv16_{digest}.jsonl"))
        cached = checkpoint.load()
        results: List[SemanticResult | None] = [None] * len(df)
        for idx, result in cached.items():
            if (0 <= idx < len(results) and result.raw_text == texts[idx]
                    and not any(it.match_error for it in result.intents)):
                results[idx] = result

        pending = [i for i, r in enumerate(results) if r is None]
        progress = getattr(self, "progress", None)
        cached_count = len(results) - len(pending)
        completed, failed = cached_count, 0
        if progress:
            progress.update(completed, len(results), cached=cached_count)
        if pending:
            if retriever is not None:
                retriever.prepare(taxonomy, [texts[i] for i in pending] if pcfg.get("atomic_input") else [])
            with ThreadPoolExecutor(max_workers=self.cfg["llm"]["max_workers"]) as pool:
                futures = {
                    (pool.submit(classifier.classify_atomic, df.iloc[i][text_col], df.iloc[i].get("_atomic_slots", {}), df.iloc[i].get("_atomic_semantics"))
                     if pcfg.get("atomic_input") else pool.submit(classifier.classify, df.iloc[i][text_col])): i
                    for i in pending
                }
                for future in tqdm(as_completed(futures), total=len(futures), desc=f"classify round {round_no}"):
                    idx = futures[future]
                    try:
                        result = future.result()
                    except Exception as e:
                        text = str(df.iloc[idx][text_col])
                        result = SemanticResult(raw_text=text, intents=[])
                        print(f"[ERROR] row={idx}: {e}")
                        # Do not checkpoint transient request/validation failures.
                        # A rerun should retry them instead of permanently reusing
                        # an empty SemanticResult.
                        results[idx] = result
                        completed += 1
                        failed += 1
                        if progress:
                            progress.update(completed, len(results), cached=cached_count, failed=failed)
                        continue
                    results[idx] = result
                    if not any(it.match_error for it in result.intents):
                        checkpoint.append(idx, result)
                    else:
                        failed += 1
                    completed += 1
                    if progress:
                        progress.update(completed, len(results), cached=cached_count, failed=failed)

        return [r if r is not None else SemanticResult() for r in results]
