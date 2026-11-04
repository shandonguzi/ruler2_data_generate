import pytest

from doc2qa.context import build_contexts, has_entity


def test_cross_document_relations_are_explicit(bundle, config):
    docs, chunks, contexts = bundle
    cross = [c for c in contexts if len(c.document_ids) > 1]
    assert cross
    assert any("Adly Mansour" in c.shared_entities for c in cross)
    for context in cross:
        assert context.shared_entities
        for entity in context.shared_entities:
            assert all(has_entity(chunks[c].text, entity) for c in context.chunk_ids)
        assert all(chunks[c].document_id in context.document_ids for c in context.support_chunk_ids)
        assert context.token_count <= config.context.max_tokens
    assert len({d for c in contexts for d in c.document_ids}) == len(docs)


def test_cross_document_switch_and_determinism(bundle, config):
    _, chunks, _ = bundle
    config.context.cross_document = False
    one, _ = build_contexts(list(chunks.values()), config.context)
    two, _ = build_contexts(list(chunks.values()), config.context)
    assert [c.model_dump() for c in one] == [c.model_dump() for c in two]
    assert all(len(c.document_ids) == 1 for c in one)


def test_no_relation_no_cross_context(bundle, config):
    _, chunks, _ = bundle
    config.context.relations = ["nonexistentrelationship"]
    contexts, _ = build_contexts(list(chunks.values()), config.context)
    assert all(len(c.document_ids) == 1 for c in contexts)


def test_refuse_to_truncate_caveats(bundle, config):
    _, chunks, _ = bundle
    config.context.max_tokens = 128
    contexts, _ = build_contexts(list(chunks.values()), config.context)
    assert all(c.token_count <= 128 for c in contexts)


def test_whole_document_covers_every_source_span_without_retrieval(bundle, config):
    docs, chunks, _ = bundle
    config.context.strategy = "whole_document"
    config.context.entities = []
    config.context.relations = []
    config.context.max_chunks = 1
    config.context.max_contexts_per_document = 1
    config.context.embedding = "openai"

    class NoRetrieval:
        def embed(self, *args):
            pytest.fail("Whole-document contexts must not call retrieval embeddings")

    contexts, index = build_contexts(list(chunks.values()), config.context, NoRetrieval())
    assert len(contexts) == len(docs) == 5
    assert index["backend"] == "none" and index["vectors"] == []
    for context in contexts:
        assert context.relation_type == "whole_document"
        assert len(context.document_ids) == 1
        doc = docs[context.document_ids[0]]
        primary = [chunks[c] for c in context.chunk_ids]
        assert [c.char_start for c in primary] == sorted(c.char_start for c in primary)
        covered = set()
        for chunk_id in context.chunk_ids + context.support_chunk_ids:
            chunk = chunks[chunk_id]
            assert chunk.document_id == doc.document_id
            assert chunk.text == doc.text[chunk.char_start : chunk.char_end]
            covered.update(range(chunk.char_start, chunk.char_end))
        # Every non-whitespace source character must reach generation, including tables
        # and limitations. Merely having one context per document is not sufficient.
        assert all(i in covered for i, char in enumerate(doc.text) if not char.isspace())
        assert context.token_count <= config.context.max_tokens
        assert not any(
            chunks[s].char_start >= c.char_start and chunks[s].char_end <= c.char_end
            for s in context.support_chunk_ids
            for c in primary
        )


def test_whole_document_overflow_fails_instead_of_dropping_a_document(bundle, config):
    _, chunks, _ = bundle
    config.context.strategy = "whole_document"
    config.context.max_tokens = 128
    with pytest.raises(ValueError, match=r"exceeding context.max_tokens=128"):
        build_contexts(list(chunks.values()), config.context)


def test_empty_entities_and_relations_still_generate_single_document_contexts(bundle, config):
    _, chunks, _ = bundle
    config.context.entities = []
    config.context.relations = []
    contexts, _ = build_contexts(list(chunks.values()), config.context)
    assert contexts
    assert all(len(c.document_ids) == 1 for c in contexts)
