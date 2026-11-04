from pathlib import Path

import pytest

from doc2qa.chunking import chunk_document
from doc2qa.config import load_config
from doc2qa.context import build_contexts
from doc2qa.document import ingest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv("DEEPEVAL_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("DEEPEVAL_JUDGE_MODEL", "gpt-4.1-mini")
    monkeypatch.setenv("DEEPEVAL_EMBEDDING_MODEL", "text-embedding-3-small")
    cfg = load_config(ROOT / "configs/transport_qa.yaml")
    cfg.input = str(ROOT / "cairo_transportation")
    # Keep the existing regression suite exercising explicit cross-document rules.
    # Production YAML uses whole-document contexts with no domain-specific keywords.
    cfg.context.strategy = "chunks"
    cfg.context.embedding = "tfidf"
    cfg.context.cross_document = True
    cfg.context.entities = ["Adly Mansour", "Cairo University", "Wadi El Nile"]
    cfg.context.relations = ["interchange", "connects", "connections", "terminus", "transfer"]
    return cfg


@pytest.fixture
def bundle(config):
    documents = ingest(Path(config.input), config.language)
    chunks = [c for d in documents for c in chunk_document(d, config.chunking)]
    contexts, _ = build_contexts(chunks, config.context)
    return {d.document_id: d for d in documents}, {c.chunk_id: c for c in chunks}, contexts
