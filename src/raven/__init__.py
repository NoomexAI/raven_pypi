"""RAVEN — Retrieval Augmented Adaptive Epistemic Navigation.

Privacy-first, fully offline RAG backend (v2): llama-index FunctionAgent +
Ollama + Qdrant local. Everything here is the domain core: async, event-emitting,
transport-agnostic. See `raven/chat_session.py` for the main entrypoint.
"""

from .conversation_manager import Conversation, ConversationManager, DEFAULT_TITLE
from .events import Event, EventBus, EventType
from .knowledge import Knowledge, KnowledgeBase
from .model import (
    DEFAULT_BASE_MODEL,
    DEFAULT_EMBED_MODEL,
    ModelManager,
)
from .pipeline import (
    AgreementBasedRetrievalPipeline,
    EmbeddedRetrievalPipeline,
    GLOBAL_AGREEMENT_RETRIEVAL,
    GLOBAL_EMBEDDED_RETRIEVAL,
    GLOBAL_HIERARCHICAL_RETRIEVAL,
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL,
    HIERARCHICAL_BY_FILE,
    HIERARCHICAL_BY_KNOWLEDGE,
    HierarchicalRetrievalPipeline,
    IngestionPipeline,
    LOCAL_AGREEMENT_RETRIEVAL,
    LOCAL_EMBEDDED_RETRIEVAL,
    LOCAL_HIERARCHICAL_RETRIEVAL,
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL,
    RetrievalPipeline,
    VectorConditionedRetrievalPipeline,
)
from .reconstructor import Reconstructor
from .chat_session import ChatSession
from .local_ollama import LocalOllama

__all__ = [
    "ChatSession",
    "Conversation",
    "ConversationManager",
    "DEFAULT_BASE_MODEL",
    "DEFAULT_EMBED_MODEL",
    "DEFAULT_TITLE",
    "Event",
    "EventBus",
    "EventType",
    "Knowledge",
    "KnowledgeBase",
    "ModelManager",
    "LocalOllama",
    "IngestionPipeline",
    "RetrievalPipeline",
    "EmbeddedRetrievalPipeline",
    "HierarchicalRetrievalPipeline",
    "AgreementBasedRetrievalPipeline",
    "VectorConditionedRetrievalPipeline",
    "Reconstructor",
    "LOCAL_EMBEDDED_RETRIEVAL",
    "LOCAL_HIERARCHICAL_RETRIEVAL",
    "LOCAL_AGREEMENT_RETRIEVAL",
    "LOCAL_VECTOR_CONDITIONED_RETRIEVAL",
    "GLOBAL_EMBEDDED_RETRIEVAL",
    "GLOBAL_HIERARCHICAL_RETRIEVAL",
    "GLOBAL_AGREEMENT_RETRIEVAL",
    "GLOBAL_VECTOR_CONDITIONED_RETRIEVAL",
    "HIERARCHICAL_BY_FILE",
    "HIERARCHICAL_BY_KNOWLEDGE",
]
