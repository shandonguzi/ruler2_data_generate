from __future__ import annotations

import os
import re
from pathlib import Path

from .config import Config, SynthesisConfig, require_models
from .context import context_text
from .prompts import PROMPT_VERSION
from .schema import Chunk, Context, Document, QA, digest, evidence_for, stable_id
from .synthesis_context import ContextAwareGenerationMixin
from .synthesis_progress import SynthesisProgressMixin


def install_local_templates():
    """Route DeepEval 4.3.2's module-level template lookups to project prompts.

    DeepEval has no template injection setting. Register before starting synthesis;
    this affects synthesizers in this process, without editing site-packages.
    Preserve upstream methods for unrelated SQL/conversational generation.
    """
    import deepeval.synthesizer.synthesizer as backend
    from deepeval.synthesizer import templates as upstream

    from .synthesis_templates import EvolutionTemplate, SynthesizerTemplate

    class ProjectSynthesizerTemplate(SynthesizerTemplate, upstream.SynthesizerTemplate):
        pass

    backend.SynthesizerTemplate = ProjectSynthesizerTemplate
    # DeepEval captures these function references at import time; changing the
    # template class alone would leave evolution using the package prompts.
    backend.evolution_map = {
        "Reasoning": EvolutionTemplate.reasoning_evolution,
        "Multi-context": EvolutionTemplate.multi_context_evolution,
        "Concretizing": EvolutionTemplate.concretizing_evolution,
        "Constrained": EvolutionTemplate.constrained_evolution,
        "Comparative": EvolutionTemplate.comparative_question_evolution,
        "Hypothetical": EvolutionTemplate.hypothetical_scenario_evolution,
        "In-Breadth": EvolutionTemplate.in_breadth_evolution,
    }


def rules_for_context(
    context: Context, documents: dict[str, Document], config: SynthesisConfig
) -> str:
    """Select trusted config rules, keeping their source scope in cross-file contexts."""
    sections = []
    for document_id in context.document_ids:
        filename = Path(documents[document_id].path).name
        rules = config.document_rules.get(filename, [])
        if rules:
            sections.append(
                f"### Document: {filename} [document_id={document_id}]\n"
                + "\n".join(f"- {rule}" for rule in rules)
            )
    rules = config.context_rules.get(context.context_id, [])
    if rules:
        sections.append(
            f"#### Context: {context.context_id}\n" + "\n".join(f"- {rule}" for rule in rules)
        )
    return "\n\n".join(sections)


def require_contexts_for_rules(config: Config):
    if any(config.synthesis.document_rules.values()) or any(
        config.synthesis.context_rules.values()
    ):
        raise ValueError(
            "document_rules/context_rules 需要 mode=contexts；"
            "DeepEval docs 模式不支持按本项目的 Context 选择规则。"
        )


def build_synthesizer(config: Config, context_rules: str = ""):
    require_models(config, generation=True)
    os.environ.setdefault("DEEPEVAL_TELEMETRY_OPT_OUT", "YES")
    from deepeval.models import OpenAIModel
    from deepeval.config.settings import get_settings
    from deepeval.synthesizer import Evolution, Synthesizer
    from deepeval.synthesizer.config import EvolutionConfig, StylingConfig

    from .synthesis_generation import EvolutionStylingMixin

    install_local_templates()
    settings = get_settings()
    settings.DEEPEVAL_RETRY_MAX_ATTEMPTS = config.synthesis.max_retries + 1
    settings.DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE = config.synthesis.timeout
    options = {"base_url": os.environ["OPENAI_BASE_URL"]} if os.getenv("OPENAI_BASE_URL") else {}
    options.update(
        api_key=os.getenv("OPENAI_API_KEY"), timeout=config.synthesis.timeout, max_retries=0
    )
    llm = OpenAIModel(model=config.synthesis.model, **options)
    costs_known = llm.calculate_cost(0, 0) is not None
    task = (
        f"- Write questions in language '{config.language}'.\n"
        "- Consider every allowed question type for this Context: "
        + ", ".join(config.synthesis.question_types)
        + ". Choose the evidence-supported mix without equal-count quotas; return one qa_type per query."
    )
    if config.synthesis.type_weights:
        task += (
            "\n- Relative type preferences (larger means more emphasis, not a fixed quota): "
            + ", ".join(
                f"{name}={config.synthesis.type_weights.get(name, 1)}"
                for name in config.synthesis.question_types
            )
        )
    if context_rules.strip():
        task += (
            "\n\nApply document-specific rules only to evidence from the labelled document.\n"
            f"{context_rules.strip()}"
        )

    class CostAwareSynthesizer(
        SynthesisProgressMixin,
        ContextAwareGenerationMixin,
        EvolutionStylingMixin,
        Synthesizer,
    ):
        # DeepEval 4.3.2 resets this to 0 for native models, then adds costs
        # without checking for None. Its cost_tracking flag only controls printing.
        # Keep the accumulator disabled across resets if the generation price is unknown.
        @property
        def synthesis_cost(self):
            return self._synthesis_cost

        @synthesis_cost.setter
        def synthesis_cost(self, value):
            self._synthesis_cost = value if costs_known else None

    synthesizer = CostAwareSynthesizer(
        model=llm,
        async_mode=True,
        max_concurrent=config.synthesis.max_concurrent,
        cost_tracking=costs_known,
        evolution_config=EvolutionConfig(
            evolutions={
                Evolution.MULTICONTEXT: 0.35,
                Evolution.CONCRETIZING: 0.25,
                Evolution.CONSTRAINED: 0.2,
                Evolution.COMPARATIVE: 0.2,
            },
            num_evolutions=config.synthesis.num_evolutions,
        ),
        styling_config=StylingConfig(
            task=task,
            expected_output_format=f"One concise, complete factual sentence in language '{config.language}'. Preserve source scope, dates and caveats.",
        ),
    )

    synthesizer.allowed_question_types = tuple(config.synthesis.question_types)
    return synthesizer


def map_golden(
    golden,
    context: Context,
    chunks: dict[str, Chunk],
    documents: dict[str, Document],
    config: Config,
    run_id: str,
    index: int,
) -> QA:
    metadata = getattr(golden, "additional_metadata", None) or {}
    qa_type = metadata.get("qa_type")
    if qa_type not in config.synthesis.question_types:
        raise ValueError("Generated QA must carry an allowed per-query qa_type")
    return QA(
        qa_id=stable_id(
            "qa", run_id, context.context_id, index, golden.input, golden.expected_output
        ),
        context_id=context.context_id,
        question=golden.input or "",
        answer=golden.expected_output or "",
        language=config.language,
        qa_type=qa_type,
        difficulty="medium" if len(context.document_ids) > 1 else "easy",
        source_document_ids=context.document_ids,
        evidence=[
            evidence_for(chunks[c], documents[chunks[c].document_id])
            for c in context.chunk_ids + context.support_chunk_ids
        ],
        generation_metadata={
            "engine": "deepeval",
            "method": f"generate_goldens_from_{config.mode}",
            "model": config.synthesis.model,
            "judge_model": config.synthesis.judge_model,
            "prompt_version": PROMPT_VERSION,
            "run_id": run_id,
            "qa_type_source": "generation",
            "initial_qa_type": metadata.get("initial_qa_type", qa_type),
        },
    )


def generate_context(
    context: Context,
    chunks: dict[str, Chunk],
    config: Config,
    context_rules: str = "",
):
    synthesizer = build_synthesizer(config, context_rules=context_rules)
    goldens = synthesizer.generate_goldens_from_contexts(
        contexts=[context_text(context, chunks)],
        source_files=[context.context_id],
        include_expected_output=True,
        max_goldens_per_context=config.synthesis.max_goldens_per_context,
    )
    return goldens, synthesizer.synthesis_cost


def generate_docs(documents: list[Document], config: Config):
    require_contexts_for_rules(config)
    require_models(config, generation=True, judge=True, embedding=True)
    from deepeval.models import OpenAIModel, OpenAIEmbeddingModel
    from deepeval.synthesizer.config import ContextConstructionConfig

    # Small files cannot yield eight distinct 650-token contexts. Start conservatively.
    synthesizer = build_synthesizer(config)
    embedding_options = (
        {"base_url": os.environ["OPENAI_BASE_URL"]} if os.getenv("OPENAI_BASE_URL") else {}
    )
    embedder = OpenAIEmbeddingModel(
        model=config.context.embedding_model,
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=config.synthesis.timeout,
        **embedding_options,
    )
    critic = OpenAIModel(
        model=config.synthesis.judge_model,
        api_key=os.getenv("OPENAI_API_KEY"),
        timeout=config.synthesis.timeout,
        **embedding_options,
    )
    goldens = synthesizer.generate_goldens_from_docs(
        document_paths=[d.path for d in documents],
        include_expected_output=True,
        max_goldens_per_context=config.synthesis.max_goldens_per_context,
        context_construction_config=ContextConstructionConfig(
            chunk_size=config.chunking.size,
            chunk_overlap=config.chunking.overlap,
            min_contexts_per_document=1,
            max_contexts_per_document=1,
            min_context_length=1,
            max_context_length=config.context.max_chunks,
            allow_cross_file_contexts=config.context.cross_document,
            max_files_per_context=max(2, config.context.max_chunks),
            embedder=embedder,
            critic_model=critic,
            max_retries=config.synthesis.max_retries,
        ),
    )
    return goldens, synthesizer.synthesis_cost


def align_docs_golden(golden, documents: dict[str, Document], chunks: dict[str, Chunk]) -> Context:
    """Only exact/whitespace-only spans are eligible; never assign a fuzzy source."""
    selected = []
    for piece in golden.context or []:
        found = []
        if not piece.strip():
            continue
        for doc in documents.values():
            match = re.search(r"\s+".join(re.escape(w) for w in piece.split()), doc.text)
            if match:
                found.append((doc, match))
        if len(found) != 1:
            raise ValueError("DeepEval docs context has missing or ambiguous source alignment")
        doc, match = found[0]
        start, end = match.span()
        text = doc.text[start:end]
        spans = [p["page"] for p in doc.page_spans if p["end"] > start and p["start"] < end]
        chunk = Chunk(
            chunk_id=stable_id("chunk", doc.document_id, start, end, "docs"),
            document_id=doc.document_id,
            text=text,
            section_path=[Path(doc.path).name, "DeepEval context"],
            char_start=start,
            char_end=end,
            page_start=min(spans) if spans else None,
            page_end=max(spans) if spans else None,
            content_hash=digest(text),
            language=doc.language,
        )
        chunks[chunk.chunk_id] = chunk
        selected.append(chunk.chunk_id)
    if not selected:
        raise ValueError("DeepEval docs golden has no evidence context")
    doc_ids = sorted({chunks[c].document_id for c in selected})
    support = [
        c.chunk_id for c in chunks.values() if c.kind == "caveat" and c.document_id in doc_ids
    ]
    return Context(
        context_id=stable_id("ctx", selected, support),
        chunk_ids=selected,
        support_chunk_ids=support,
        document_ids=doc_ids,
        relation_type="cross_document_relation" if len(doc_ids) > 1 else "docs",
        topic="DeepEval selected context",
    )


def smoke_candidates(
    context: Context,
    chunks: dict[str, Chunk],
    documents: dict[str, Document],
    config: Config,
    run_id: str,
) -> list[QA]:
    # Explicit extractive fixtures: useful for exercising storage, never auto-pass.
    first = chunks[context.chunk_ids[0]]
    question = f"According to the supplied reference, what information is recorded in {Path(documents[first.document_id].path).name}, section {first.section_path[-1]}?"
    answer = first.text
    candidate = QA(
        qa_id=stable_id("qa", run_id, context.context_id, "smoke"),
        context_id=context.context_id,
        question=question,
        answer=answer,
        language=config.language,
        source_document_ids=context.document_ids,
        evidence=[
            evidence_for(chunks[c], documents[chunks[c].document_id])
            for c in context.chunk_ids + context.support_chunk_ids
        ],
        generation_metadata={
            "engine": "offline_smoke",
            "run_id": run_id,
            "prompt_version": PROMPT_VERSION,
            "model": None,
            "not_model_generated": True,
        },
        reasons=["offline_smoke_requires_review"],
    )
    return [candidate]
