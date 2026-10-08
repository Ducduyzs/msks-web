"""Neural-first adaptive hierarchical retrieval for scientific QA."""

from .config import Settings
from .context import assemble_context
from .expansion import expand_selection
from .hierarchy import HierarchyBuilder
from .pipeline import AdaptiveHierarchicalPipeline, classify_query
from .policy import AdaptiveMergePolicy, decide_merges
from .schemas import DocumentSection, ScientificDocument
from .verification import verify_generation

__all__ = [
    "AdaptiveHierarchicalPipeline", "AdaptiveMergePolicy", "DocumentSection",
    "HierarchyBuilder", "ScientificDocument", "Settings",
    "assemble_context", "classify_query", "decide_merges",
    "expand_selection", "verify_generation",
]
