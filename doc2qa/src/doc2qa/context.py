from __future__ import annotations

import math
import re
from collections import Counter

from .chunking import tokens
from .config import ContextConfig
from .schema import Chunk, Context, stable_id

STOP = set(
    "the a an and or of in to for from with at by is are as its it this that on source page portal cairo governorate transportation services english reference compiled october names figures follow".split()
)


def words(text: str) -> list[str]:
    return [w for w in re.findall(r"\w+", text.lower()) if w not in STOP and len(w) > 1]


def cosine(a: list[float], b: list[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a) * sum(x * x for x in b))
    return sum(x * y for x, y in zip(a, b)) / norm if norm else 0.0


def tfidf(texts: list[str]) -> list[list[float]]:
    counts = [Counter(words(t)) for t in texts]
    vocab = sorted(set().union(*(set(c) for c in counts)))
    idf = {w: math.log((1 + len(texts)) / (1 + sum(w in c for c in counts))) + 1 for w in vocab}
    return [[(1 + math.log(c[w])) * idf[w] if c[w] else 0.0 for w in vocab] for c in counts]


def entity_names(chunks: list[Chunk], configured: list[str]) -> list[str]:
    if configured:
        return configured
    counts = Counter()
    for chunk in chunks:
        phrases = set(re.findall(r"\b[A-Z][a-z]+(?:[ -][A-Z][a-z]+){1,3}\b", chunk.text))
        counts.update(phrases)
    return sorted(p for p, count in counts.items() if count >= 2 and len(words(p)) >= 2)


def has_entity(text: str, entity: str) -> bool:
    return bool(re.search(r"(?<!\w)" + re.escape(entity) + r"(?!\w)", text, re.I))


def render_chunk(chunk: Chunk) -> str:
    header = f"\nTable columns: {chunk.table_header}" if chunk.table_header else ""
    return f"[chunk_id={chunk.chunk_id}; document_id={chunk.document_id}; section={' / '.join(chunk.section_path)}]{header}\n{chunk.text}"


def context_text(context: Context, chunks: dict[str, Chunk]) -> list[str]:
    return [render_chunk(chunks[c]) for c in context.chunk_ids + context.support_chunk_ids]


def build_contexts(
    chunks: list[Chunk], config: ContextConfig, remote=None
) -> tuple[list[Context], dict]:
    main = [c for c in chunks if c.kind != "caveat"]
    lookup = {c.chunk_id: c for c in chunks}
    result, used = [], set()
    caveats = [c for c in chunks if c.kind == "caveat"]

    def add(selected: list[Chunk], relation: str, shared=None):
        ids = [c.chunk_id for c in selected]
        key = tuple(sorted(ids))
        if key in used:
            return False
        docs = sorted({c.document_id for c in selected})
        support = [
            c.chunk_id
            for c in caveats
            if c.document_id in docs
            and not any(
                c.char_start >= s.char_start and c.char_end <= s.char_end
                for s in selected
                if c.document_id == s.document_id
            )
        ]
        # A later table segment still needs an independently traceable column header.
        for selected_chunk in selected:
            if selected_chunk.table_header:
                header_chunk = next(
                    (
                        c
                        for c in chunks
                        if c.document_id == selected_chunk.document_id
                        and c.kind == "table"
                        and selected_chunk.table_header in c.text
                    ),
                    None,
                )
                if header_chunk and header_chunk.chunk_id not in ids + support:
                    support.append(header_chunk.chunk_id)
        ctx = Context(
            context_id=stable_id("ctx", ids, support),
            chunk_ids=ids,
            support_chunk_ids=support,
            document_ids=docs,
            relation_type=relation,
            topic=" / ".join(shared or selected[0].section_path[-1:]),
            shared_entities=shared or [],
        )
        ctx.token_count = sum(tokens(t) for t in context_text(ctx, lookup))
        if ctx.token_count > config.max_tokens:
            if relation == "whole_document":
                raise ValueError(
                    f"Whole-document context for {selected[0].section_path[0]} requires "
                    f"{ctx.token_count} tokens, exceeding context.max_tokens={config.max_tokens}. "
                    "Increase max_tokens or use context.strategy=chunks; no text was truncated."
                )
            return False  # Never silently truncate caveats to fit a context.
        result.append(ctx)
        used.add(key)
        return True

    if config.strategy == "whole_document":
        # Include every primary span in source order. Keep chunk IDs for precise citations.
        # Retrieval and cross-document settings do not alter a whole-document context.
        for doc_id in sorted({c.document_id for c in main}):
            group = sorted(
                (c for c in main if c.document_id == doc_id),
                key=lambda c: (c.char_start, c.char_end),
            )
            add(group, "whole_document")
        return result, {
            "backend": "none",
            "model": None,
            "chunk_ids": [c.chunk_id for c in main],
            "vectors": [],
        }

    texts = [c.text for c in main]
    if config.embedding == "openai" and remote is None:
        raise ValueError("context.embedding=openai requires a configured remote embedding client")
    vectors = (
        remote.embed(texts, config.embedding_model)
        if config.embedding == "openai"
        else tfidf(texts)
    )
    entities = entity_names(main, config.entities)

    # Round-robin over a document's ordered chunks to cover its full range under the cap.
    for doc_id in sorted({c.document_id for c in main}):
        group = [c for c in main if c.document_id == doc_id]
        budget = config.max_contexts_per_document
        pairs = [(a, b) for a, b in zip(group, group[1:]) if a.section_path == b.section_path]
        reserved = (
            min(len(pairs), max(1, budget // 4)) if config.max_chunks >= 2 and budget > 1 else 0
        )
        cap = min(len(group), budget - reserved)
        selected_indices = sorted(
            {round(i * (len(group) - 1) / max(cap - 1, 1)) for i in range(cap)}
        )
        for index in selected_indices:
            add([group[index]], "single_chunk")
        if config.max_chunks >= 2:
            for a, b in pairs[: max(0, budget - cap)]:
                add([a, b], "same_document")
    if config.cross_document and config.max_chunks >= 2:
        candidates = []
        for i, a in enumerate(main):
            for j in range(i + 1, len(main)):
                b = main[j]
                if a.document_id == b.document_id:
                    continue
                shared = [e for e in entities if has_entity(a.text, e) and has_entity(b.text, e)]
                # The relation must be stated near the shared entity, not elsewhere in a long chunk.
                related = any(
                    any(
                        has_entity(sentence, e)
                        and any(
                            re.search(r"\b" + re.escape(r) + r"\b", sentence, re.I)
                            for r in config.relations
                        )
                        for sentence in re.split(r"[.!?\n]", a.text + "\n" + b.text)
                    )
                    for e in shared
                )
                sim = cosine(vectors[i], vectors[j])
                if shared and related and sim >= config.similarity_threshold:
                    candidates.append((sim, a.chunk_id, b.chunk_id, shared))
        count = 0
        for _, a, b, shared in sorted(candidates, key=lambda x: (-x[0], x[1], x[2])):
            if count >= config.max_cross_contexts:
                break
            count += add([lookup[a], lookup[b]], "cross_document_relation", shared)
    index = {
        "backend": config.embedding,
        "model": config.embedding_model if config.embedding == "openai" else "tfidf_v1",
        "chunk_ids": [c.chunk_id for c in main],
        "vectors": vectors,
    }
    return result, index
