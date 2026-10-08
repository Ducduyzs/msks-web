from __future__ import annotations

from pathlib import Path

from .config import Settings
from .contextual import apply_chunk_context
from .hierarchy import HierarchyBuilder
from .index import MultiRepresentationIndex
from .ingestion import DoclingScientificLoader
from .models import (
    AntigravityStructuredGenerator,
    BGEM3Encoder,
    BGEReranker,
    GeminiStructuredGenerator,
    NliVerifier,
    OpenAIStructuredGenerator,
)
from .pipeline import AdaptiveHierarchicalPipeline
from .policy import policies_from_settings
from .schemas import ScientificDocument


def build_pipeline(
    pdf_paths: list[str | Path], settings: Settings | None = None
) -> AdaptiveHierarchicalPipeline:
    settings = settings or Settings()
    documents = DoclingScientificLoader().load(pdf_paths)
    return build_pipeline_from_documents(documents, settings)


def build_pipeline_from_documents(
    documents: list[ScientificDocument], settings: Settings | None = None
) -> AdaptiveHierarchicalPipeline:
    """Build the same runtime from already structured scientific documents."""
    settings = settings or Settings()
    hierarchy = apply_chunk_context(
        HierarchyBuilder(settings).build(documents), settings.chunk_context,
        settings.chunk_context_path, settings.chunk_context_model,
    )
    encoder = BGEM3Encoder(settings.embedding_model, settings.device, settings.use_fp16)
    index = MultiRepresentationIndex(hierarchy, encoder, settings)
    reranker = BGEReranker(settings.reranker_model, settings.device, settings.use_fp16)
    if settings.llm_provider == "antigravity":
        generator = AntigravityStructuredGenerator(
            settings.llm_model, settings.gemini_api_key,
            agent_name=settings.antigravity_agent,
            max_total_tokens=settings.antigravity_max_total_tokens,
        )
    elif settings.llm_provider == "gemini":
        generator = GeminiStructuredGenerator(settings.llm_model, settings.gemini_api_key)
    else:
        generator = OpenAIStructuredGenerator(settings.llm_model, settings.openai_api_key)
    verifier = NliVerifier(settings.nli_model, settings.device)
    parent_policy, section_policy = policies_from_settings(settings)
    return AdaptiveHierarchicalPipeline(
        hierarchy=hierarchy,
        retriever=index,
        reranker=reranker,
        generator=generator,
        verifier=verifier,
        settings=settings,
        parent_policy=parent_policy,
        section_policy=section_policy,
    )
