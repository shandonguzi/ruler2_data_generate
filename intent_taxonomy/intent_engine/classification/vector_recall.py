"""Version-aware task retrieval with persistent, per-text semantic embeddings."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from threading import RLock

import numpy as np


class VectorTaskRecall:
    def __init__(self, cfg: dict, cache_dir: Path, examples: int = 4):
        # Lazy import keeps the classifier independent of BERTopic installation.
        from intent_engine.atomic.atomic_discovery import SemanticEmbeddings
        self.encoder = SemanticEmbeddings(cfg, cache_dir / "batches")
        self.cache_dir = cache_dir / "texts"
        self.identity = {k: cfg.get(k) for k in
                         ("base_url", "model", "dimensions", "cache_version", "extra_body")}
        self.examples = max(0, examples)
        self.lock = RLock()
        self.memory = {}
        self.index_key = None
        self.index = None

    def _path(self, text):
        digest = hashlib.sha256(json.dumps([self.identity, text], ensure_ascii=False,
                                          sort_keys=True).encode()).hexdigest()
        return self.cache_dir / (digest + ".json")

    def encode(self, texts):
        from intent_engine.atomic.atomic_discovery import _write_json
        from intent_engine.infrastructure.progress import ProgressReporter
        # A separate reporter must not overwrite the classification phase counters.
        with self.lock:
            missing = []
            for text in dict.fromkeys(texts):
                if text in self.memory:
                    continue
                path = self._path(text)
                try:
                    self.memory[text] = self.encoder._validate(
                        [json.loads(path.read_text(encoding="utf-8"))], 1)[0]
                except (OSError, ValueError, TypeError):
                    missing.append(text)
            if missing:
                vectors = self.encoder.encode(missing, ProgressReporter())
                for text, vector in zip(missing, vectors):
                    self.memory[text] = vector
                    _write_json(self._path(text), vector.tolist())
            vectors = np.stack([self.memory[text] for text in texts])
            return vectors / np.linalg.norm(vectors, axis=1, keepdims=True)

    def task_text(self, taxonomy, task):
        domain = next((d for d in taxonomy.domains if d.name == task.domain), None)
        parent = getattr(domain, "parent_domain", "") or ""
        return json.dumps({
            "path": [x for x in (parent, task.domain, task.object, task.action) if x],
            "ancestor_definition": getattr(domain, "description", ""),
            "definition": task.description,
            "object_aliases": getattr(task, "object_aliases", []),
            "action_aliases": getattr(task, "action_aliases", []),
            "subtasks": getattr(task, "subtasks", []),
            "examples": task.positive_examples[:self.examples],
        }, ensure_ascii=False, sort_keys=True)

    def prepare(self, taxonomy, queries=()):
        if not taxonomy.tasks:
            return
        with self.lock:
            texts = [self.task_text(taxonomy, task) for task in taxonomy.tasks]
            key = tuple((task.task_id, text) for task, text in zip(taxonomy.tasks, texts))
            if key != self.index_key:
                self.index = self.encode(texts)
                self.index_key = key
            if queries:
                self.encode(list(queries))



    def recall(self, taxonomy, text, top_k):
        if not taxonomy.tasks or not text.strip():
            return []
        with self.lock:
            self.prepare(taxonomy)
            query = self.encode([text])[0]
            if query.shape[0] != self.index.shape[1]:
                raise ValueError("主题与意图的 Embedding 维度不一致，请更新 embedding.cache_version 后重试")
            scores = self.index @ query
            order = sorted(range(len(scores)), key=lambda i: (-float(scores[i]), taxonomy.tasks[i].task_id))
            return [taxonomy.tasks[i] for i in order[:top_k]]
