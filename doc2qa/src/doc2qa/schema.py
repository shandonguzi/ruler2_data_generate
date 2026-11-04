from __future__ import annotations

import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


def digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def stable_id(prefix: str, *parts: Any) -> str:
    return prefix + "_" + digest(json.dumps(parts, ensure_ascii=False, sort_keys=True))[:20]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Document(Record):
    document_id: str
    path: str
    aliases: list[str] = Field(default_factory=list)
    file_sha256: str
    size_bytes: int
    format: str
    text: str
    language: str
    source_urls: list[str] = Field(default_factory=list)
    compiled_date: str | None = None
    page_spans: list[dict] = Field(default_factory=list)
    parser_version: str = "parser_v1"


class Chunk(Record):
    chunk_id: str
    document_id: str
    text: str
    section_path: list[str]
    char_start: int
    char_end: int
    page_start: int | None = None
    page_end: int | None = None
    content_hash: str
    language: str
    kind: Literal["text", "table", "caveat"] = "text"
    table_header: str | None = None


class Context(Record):
    context_id: str
    chunk_ids: list[str]
    support_chunk_ids: list[str] = Field(default_factory=list)
    document_ids: list[str]
    relation_type: str
    topic: str
    shared_entities: list[str] = Field(default_factory=list)
    token_count: int = 0


class Evidence(Record):
    chunk_id: str
    document_id: str
    source_file: str
    section_path: list[str]
    page_start: int | None = None
    page_end: int | None = None
    char_start: int
    char_end: int
    quote: str


class QA(Record):
    qa_id: str
    context_id: str
    question: str
    answer: str
    language: str
    qa_type: str = "factual"
    difficulty: str = "easy"
    source_document_ids: list[str]
    evidence: list[Evidence]
    quality_scores: dict[str, float] = Field(default_factory=dict)
    generation_metadata: dict[str, Any] = Field(default_factory=dict)
    review_status: Literal["pending_review", "auto_pass", "rejected", "duplicate"] = (
        "pending_review"
    )
    reasons: list[str] = Field(default_factory=list)
    dedup_group_id: str | None = None
    duplicate_of: str | None = None
    judge_result: dict[str, Any] | None = None


def evidence_for(chunk: Chunk, document: Document, quote: str | None = None) -> Evidence:
    quote = chunk.text if quote is None else quote
    offset = chunk.text.find(quote)
    if not quote.strip() or offset < 0:
        raise ValueError("Evidence quote is not an exact substring of the chunk")
    start = chunk.char_start + offset
    return Evidence(
        chunk_id=chunk.chunk_id,
        document_id=chunk.document_id,
        source_file=document.path,
        section_path=chunk.section_path,
        page_start=chunk.page_start,
        page_end=chunk.page_end,
        char_start=start,
        char_end=start + len(quote),
        quote=quote,
    )
