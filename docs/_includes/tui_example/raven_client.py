"""Small RAVEN adapter: consume operation results, leave storage to RAVEN."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from importlib import import_module
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nraven import Raven
    from nraven.agent.contracts import AgentRunResult
    from nraven.core.events import Event
    from nraven.data_management.conversation_manager import Conversation



class RavenClient:

    def __init__(self, home: Path) -> None:
        self.home = home
        self.raven: Raven | None = None


    async def start(self) -> None:
        # Import the model/document dependencies without freezing the UI.
        module = await asyncio.to_thread(import_module, "nraven")
        self.raven = module.Raven(self.home)
        await self.raven.start()


    async def close(self) -> None:
        if self.raven is not None:
            await self.raven.close()


    async def list_knowledges(self) -> list[dict[str, Any]]:
        assert self.raven is not None
        knowledges = await self.raven.list_knowledges()
        return [
            {
                **knowledge,
                "file_count": len(await self.raven.list_knowledge_files(knowledge["name"])),
            }
            for knowledge in knowledges
        ]


    async def create_knowledge(self, name: str, summary: str) -> dict[str, Any]:
        assert self.raven is not None
        task = await self.raven.create_knowledge(name, summary)
        knowledge = await task.result()
        return knowledge.meta


    async def knowledge_details(self, name: str) -> dict[str, Any]:
        assert self.raven is not None
        return {
            "metadata": self.raven.get_knowledge_details(name),
            "files": await self.raven.list_knowledge_files(name),
        }


    def list_conversations(self) -> list[dict[str, Any]]:
        assert self.raven is not None
        return sorted(
            self.raven.list_conversations(),
            key=lambda item: (item["created_at"], item["conversation_id"]),
            reverse=True,
        )


    async def create_conversation(self, knowledge_name: str | None = None) -> dict[str, Any]:
        assert self.raven is not None
        task = await self.raven.create_conversation(knowledge_name)
        try:
            conversation = await task.result()
            return conversation.to_dict()
        except BaseException:
            if not task.is_finished:
                await task.cancel()
            raise


    async def open_conversation(self, conversation_id: str) -> dict[str, Any]:
        assert self.raven is not None
        conversation = self.raven.get_conversation(conversation_id)
        records = []
        cursor = 0
        while True:
            page = await self.raven.get_conversation_messages_page(
                conversation_id, limit=100, after_message_id=cursor,
            )
            records.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1]["message_id"]
        turns: dict[str, dict[str, Any]] = {}
        for record in records:
            turn = turns.setdefault(record["turn_id"], {
                "turn_id": record["turn_id"], "messages": [], "has_sources": False,
            })
            turn["messages"].append(record["message"])
            turn["has_sources"] |= message_has_sources(record["message"])
        return {
            "conversation": conversation,
            "metadata": conversation.to_dict(),
            "messages": [record["message"] for record in records],
            "turns": list(turns.values()),
        }


    async def turn_sources(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        rebuild: bool,
        on_progress: Callable[[str], Awaitable[None]],
    ) -> dict[str, Any]:
        assert self.raven is not None
        if not rebuild:
            turn = await self.raven.get_conversation_turn(conversation_id, turn_id)
            result = turn.get("result") or {}
            files = result.get("reconstructed_sources")
            if isinstance(files, list) and files:
                return {"files": files, "snapshot": True}
        await on_progress("Reconstructing from this turn's saved evidence…")
        task = await self.raven.reconstruct_from_turn(conversation_id, turn_id)
        try:
            async for event in task.events():
                if event.type.value == "reconstruction.file":
                    await on_progress(f"Reconstructed {event.data.get('file_name', 'file')}")
            return {"files": await task.result(), "snapshot": False}
        except BaseException:
            if not task.is_finished:
                await task.cancel()
            raise


    async def generate_response(
        self,
        conversation: Conversation,
        query: str,
        on_event: Callable[[Event], Awaitable[None]],
    ) -> AgentRunResult:
        """Use a temporary session; RAVEN owns generation and turn persistence."""
        if not self.models_ready:
            raise RuntimeError("Configure both models with /models/configure before chatting.")
        assert self.raven is not None
        session = self.raven.session(conversation)
        run = None
        try:
            await session.start()
            run = await session.generate_response(query, retrieval_mode="auto")
            async for event in run.stream:
                await on_event(event)
            # chat.completed is not enough: the parent session still saves the turn.
            return await run.collect()
        except BaseException:
            if run is not None and not run.task.is_finished:
                await run.cancel()
            raise
        finally:
            await session.close()


    def configured_models(self) -> dict[str, Any]:
        assert self.raven is not None
        return self.raven.runtime_status()["models"]


    @property
    def models_ready(self) -> bool:
        return self.raven is not None and self.raven.models_loaded


    async def list_ollama_models(self) -> list[dict[str, Any]]:
        assert self.raven is not None
        task = await self.raven.list_ollama_models()
        return await task.result()


    async def configure_models(
        self,
        llm: dict[str, Any],
        embedding: dict[str, Any],
        on_progress: Callable[[str, str], Awaitable[None]],
    ) -> dict[str, Any]:
        from nraven.providers import ModelSpec

        assert self.raven is not None
        task = await self.raven.configure_models(
            ModelSpec.model_validate(llm),
            ModelSpec.model_validate(embedding),
        )
        try:
            async for event in task.events():
                message = model_progress_text(event.type.value, event.data)
                if message is not None:
                    await on_progress(f"{event.type.value}:{event.data.get('model', '')}", message)
            return await task.result()
        except BaseException:
            # A closing UI must not leave its configuration operation running.
            if not task.is_finished:
                await task.cancel()
            raise


    async def ingest(
        self,
        knowledge_name: str,
        source_path: str,
        on_progress: Callable[[str, str], Awaitable[None]],
    ) -> dict[str, Any]:
        from nraven.core.events import EventType

        if not self.models_ready:
            raise RuntimeError("Configure both models with /models/configure before ingestion.")
        assert self.raven is not None
        text = source_path.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1]

        def validate_path() -> Path:
            path = Path(text).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Source file '{path}' was not found.")
            if path.suffix.lower() not in {".txt", ".md", ".pdf", ".docx"}:
                raise ValueError("This demo accepts .txt, .md, .pdf, and .docx files.")
            return path

        path = await asyncio.to_thread(validate_path)
        task = await self.raven.ingest(knowledge_name, path)
        try:
            async for event in task.events():
                if event.type == EventType.INGESTION_PROGRESS:
                    await on_progress(str(event.data.get("stage", "ingestion")), ingestion_progress_text(event.data))
            return await task.result()
        except BaseException:
            if not task.is_finished:
                await task.cancel()
            raise



def message_has_sources(message: Any) -> bool:
    """A successful tool must supply actual section references, not just a name."""
    if message.role.value != "tool" or not isinstance(message.content, str):
        return False
    try:
        payload = json.loads(message.content)
    except ValueError:
        return False
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    evidence = payload.get("evidence")
    return isinstance(evidence, list) and any(
        isinstance(item, dict) and all(
            isinstance(item.get(key), str) and item[key]
            for key in ("knowledge_name", "file_name", "section_id")
        ) for item in evidence
    )



def model_progress_text(event_type: str, data: dict[str, Any]) -> str | None:
    """Render useful milestones, never raw provider payloads or credentials."""
    model = data.get("model") or "model"
    actions = {
        "model.load_llm": "Preparing LLM adapter",
        "model.load_embedding": "Preparing embedding adapter",
        "model.preload_llm": "Loading LLM into Ollama memory",
        "model.preload_embedding": "Loading embedding model into Ollama memory",
        "model.unload_llm": "Unloading previous LLM",
        "model.unload_embedding": "Unloading previous embedding model",
        "model.pull": "Downloading model",
    }
    if event_type == "model.pull.progress":
        completed, total = data.get("completed"), data.get("total")
        if isinstance(completed, (int, float)) and isinstance(total, (int, float)) and total > 0:
            return f"Downloading {model}: {min(100, max(0, completed / total * 100)):.0f}%"
        return f"Downloading {model}: {data.get('status') or 'in progress'}"
    action, _, stage = event_type.rpartition(".")
    if action in actions and stage in {"started", "completed", "failed"}:
        return f"{actions[action]} · {model} · {stage}"
    return None



def ingestion_progress_text(data: dict[str, Any]) -> str:
    labels = {
        "source_validation": "Checking source file",
        "source_snapshot": "Preparing source snapshot",
        "parsing": "Parsing document",
        "sectioning": "Finding semantic sections",
        "metadata_extraction": "Extracting section metadata",
        "chunking": "Creating chunks",
        "embedding": "Embedding chunks",
        "vector_storage": "Writing vectors",
        "metadata_commit": "Saving file metadata",
    }
    stage = str(data.get("stage", "ingestion"))
    text = f"{labels.get(stage, stage.replace('_', ' ').capitalize())} · {data.get('status', 'running')}"
    completed, total = data.get("completed"), data.get("total")
    if isinstance(completed, int) and isinstance(total, int) and total > 0:
        text += f" · {completed}/{total}"
    return text
