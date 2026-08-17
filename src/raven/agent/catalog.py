from __future__ import annotations

from ..pipeline import (
    GLOBAL_AGREEMENT_RETRIEVAL,
    GLOBAL_EMBEDDED_RETRIEVAL,
    GLOBAL_HIERARCHICAL_RETRIEVAL,
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL,
    LOCAL_AGREEMENT_RETRIEVAL,
    LOCAL_EMBEDDED_RETRIEVAL,
    LOCAL_HIERARCHICAL_RETRIEVAL,
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL,
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    LOCAL_EMBEDDED_RETRIEVAL: "Retrieves relevant context from a specific knowledge using vector similarity search.",
    LOCAL_HIERARCHICAL_RETRIEVAL: "Retrieves relevant context from a specific knowledge using section metadata and reasoning.",
    LOCAL_AGREEMENT_RETRIEVAL: "Retrieves context from a specific knowledge using vector and hierarchical retrieval.",
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL: "Narrows a specific knowledge to relevant files before hierarchical retrieval.",
    GLOBAL_EMBEDDED_RETRIEVAL: "Retrieves relevant context across all knowledges using vector similarity search.",
    GLOBAL_HIERARCHICAL_RETRIEVAL: "Retrieves relevant context across knowledges using section metadata and reasoning.",
    GLOBAL_AGREEMENT_RETRIEVAL: "Retrieves context across knowledges using vector and hierarchical retrieval.",
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL: "Narrows the knowledge base to relevant files before hierarchical retrieval.",
}

RETRIEVAL_TOOLS = set(TOOL_DESCRIPTIONS)
MEMORY_TOOL_NAMES = {"get_memory", "save_preference"}
NAVIGATION_TOOL_NAMES = {
    "list_knowledges", "list_files", "list_sections", "get_section_metadata",
}
NON_RETRIEVAL_TOOL_NAMES = MEMORY_TOOL_NAMES | NAVIGATION_TOOL_NAMES
LOCAL_TOOL_NAMES = {
    LOCAL_EMBEDDED_RETRIEVAL, LOCAL_HIERARCHICAL_RETRIEVAL,
    LOCAL_AGREEMENT_RETRIEVAL, LOCAL_VECTOR_CONDITIONED_RETRIEVAL,
}
GLOBAL_TOOL_NAMES = {
    GLOBAL_EMBEDDED_RETRIEVAL, GLOBAL_HIERARCHICAL_RETRIEVAL,
    GLOBAL_AGREEMENT_RETRIEVAL, GLOBAL_VECTOR_CONDITIONED_RETRIEVAL,
}


def summarize_reconstructed_evidence(reconstructed: list[dict] | None) -> dict:
    """Build the bounded UI evidence summary from reconstructed file cards.

    The reconstructed cards are the transparency representation of a retrieval
    result: each card represents one file and its highlighted sections identify
    the evidence used by the model.
    """
    files: list[dict] = []
    section_count = 0
    for card in reconstructed or []:
        section_ids = [
            section["section_id"]
            for section in card.get("sections", [])
            if section.get("highlighted") and section.get("section_id")
        ]
        if not section_ids:
            continue
        section_count += len(section_ids)
        files.append({
            "knowledge": card.get("knowledge_name"),
            "file": card.get("file_name"),
            "section_ids": section_ids,
        })
    return {
        "kind": "retrieval",
        "file_count": len(files),
        "section_count": section_count,
        "files": files,
    }
