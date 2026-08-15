from uuid import uuid4

from .events import Event, EventBus, EventType
from .knowledge import KnowledgeBase


class Reconstructor:
    """Reconstructs full file context from retrieval results.

    Given a retrieval result (section list or agreement dict), produces
    one "file card" per affected file containing ALL sections of that file
    with a `highlighted` flag marking which sections were retrieved.

    Async + event-emitting like every other component: publishes
    `reconstruction.started` / `reconstruction.completed` (and `error` on
    failure) on the bus with the op_id contract.
    """

    def __init__(self, knowledge_base: KnowledgeBase, bus: EventBus) -> None:
        self._kb = knowledge_base
        self._bus = bus

    def _emit(self, etype: EventType, data: dict, op_id: str) -> None:
        self._bus.publish(Event(type=etype, data=data, op_id=op_id))

    def _get_section_list(self, retrieval_result, tool_name: str | None) -> list[dict] | None:
        if not retrieval_result:
            return None
        if isinstance(retrieval_result, str):
            return None
        if tool_name in {"get_memory", "save_preference", None}:
            return None

        if isinstance(retrieval_result, dict):
            agreement_type = retrieval_result.get("agreement_type")
            if agreement_type == "Strong Agreement":
                return retrieval_result.get("retrieved_content") or []
            retrieved = retrieval_result.get("retrieved_content") or {}
            embedded = retrieved.get("embedded_retrieval") or []
            hierarchical = retrieved.get("hierarchical_retrieval") or []
            return embedded + hierarchical

        return retrieval_result

    async def reconstruct(
        self,
        retrieval_result,
        tool_name: str | None,
        op_id: str | None = None,
    ) -> list[dict] | None:
        op_id = op_id or uuid4().hex
        self._emit(
            EventType.RECONSTRUCTION_STARTED,
            {"tool_name": tool_name, "count": len(retrieval_result) if isinstance(retrieval_result, list) else 0},
            op_id,
        )
        try:
            sections = self._get_section_list(retrieval_result, tool_name)
            if not sections:
                self._emit(EventType.RECONSTRUCTION_COMPLETED, {"files": []}, op_id)
                return None

            file_map: dict[tuple[str, str], set[str]] = {}
            for s in sections:
                key = (s["knowledge_name"], s["file_name"])
                file_map.setdefault(key, set()).add(s["section_id"])

            reconstructed = []
            for (knowledge_name, file_name), highlighted_ids in file_map.items():
                knowledge = self._kb.get(knowledge_name)
                if knowledge is None:
                    continue
                all_sections = knowledge.list_sections(file_name)
                sections_with_highlights = [
                    {
                        "section_id": sec["section_id"],
                        "highlighted": sec["section_id"] in highlighted_ids,
                        "raw_content": sec["raw_content"],
                    }
                    for sec in all_sections
                ]
                reconstructed.append({
                    "knowledge_name": knowledge_name,
                    "file_name": file_name,
                    "sections": sections_with_highlights,
                })

            self._emit(EventType.RECONSTRUCTION_COMPLETED, {"files": reconstructed}, op_id)
            return reconstructed
        except Exception as exc:
            self._emit(EventType.ERROR, {"op": "reconstruction", "error": str(exc)}, op_id)
            raise