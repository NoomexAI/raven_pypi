"""High-level API tour.

Everything the lower-level examples do by hand (create knowledge, ingest,
wire pipelines, manage conversations, stream a chat turn) collapses into a few
async calls on the Raven facade. The event bus and op_id plumbing are hidden:

- ``await api.start()`` / ``await api.close()`` own the whole lifecycle
- ``await api.ingest(...)`` blocks until done and returns the chunk count
- ``await api.stream(...)`` is an async generator of raw ``Event`` objects and
  ends with ``chat.complete`` carrying the reply

Run from the repo root (Ollama + a model must be available):
    python examples/high_level_api.py
"""

import asyncio
import sys
from pathlib import Path

from raven import Raven
from raven.events import EventType

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KNOWLEDGE_NAME = "high-level-demo"
FILE_PATH = Path(__file__).resolve().parent.parent / "test_docs" / "Reaction_wheel.txt"
QUERY = "What is a reaction wheel and how does it work?"


async def run_turn(api: Raven, conversation_id: str) -> str:
    """Stream one turn, rendering events as they arrive. Returns the reply."""
    kind: str | None = None
    buffer: list[str] = []
    reply = ""

    def flush() -> None:
        nonlocal kind, buffer
        if kind is not None:
            text = "".join(buffer).strip()
            if text:
                print(f"  [{kind}]\n  {text}\n")
            kind = None
            buffer = []

    async for ev in api.stream(QUERY, conversation_id=conversation_id):
        if ev.type is EventType.CHAT_DELTA:
            label = "thinking" if ev.data["kind"] == "thinking_chunk" else "response"
            if kind != label:
                flush()
                kind = label
            buffer.append(ev.data["delta"])
        elif ev.type is EventType.CHAT_TOOL_CALL:
            flush()
            print(f"  [tool_call] {ev.data['name']} {ev.data['args']}\n")
        elif ev.type is EventType.CHAT_RETRIEVED:
            flush()
            print(
                f"  [retrieved] {ev.data['knowledge']}/{ev.data['file']} "
                f"#{ev.data['section_id']}\n"
            )
        elif ev.type is EventType.ERROR:
            flush()
            print(f"  [error] {ev.data}\n")
            raise RuntimeError(ev.data.get("error", "chat failed"))
        elif ev.type is EventType.CHAT_COMPLETE:
            flush()
            reply = ev.data["reply"]
    return reply


async def main() -> None:
    api = Raven()
    await api.start()
    conv = None
    try:
        print("Raven started\n")

        print("== knowledge ==")
        print("existing:", [k["name"] for k in api.list_knowledges()], "\n")

        try:
            await api.create_knowledge(KNOWLEDGE_NAME, "reaction wheel explainer demo")
            print(f"created '{KNOWLEDGE_NAME}'\n")
        except ValueError as exc:
            print(f"'{KNOWLEDGE_NAME}' already exists — reusing it ({exc})\n")

        files = api.list_files(KNOWLEDGE_NAME)
        if any(f["file_name"] == FILE_PATH.name for f in files):
            print(f"{FILE_PATH.name} already ingested — skipping\n")
        else:
            print(f"ingesting {FILE_PATH.name} into '{KNOWLEDGE_NAME}' ...")
            count = await api.ingest(KNOWLEDGE_NAME, str(FILE_PATH))
            print(f"ingested {count} chunks\n")
            print("files:", api.list_files(KNOWLEDGE_NAME), "\n")

        print("== conversation ==")
        conv = await api.create_conversation(knowledge_name=KNOWLEDGE_NAME)
        print(f"created {conv.conversation_id} (type={conv.type})\n")

        print("== streaming chat (no op_id to manage) ==")
        reply = await run_turn(api, conv.conversation_id)
        print(f"reply: {reply}\n")

        print("== conversation management ==")
        await api.rename_conversation(conv.conversation_id, "Reaction Wheel QA")
        await api.toggle_pin(conv.conversation_id)
        print(
            "all conversations:",
            [(c["title"], c["type"], c["pinned"]) for c in api.list_conversations()],
            "\n",
        )
    finally:
        print("== cleanup ==")
        if conv is not None:
            await api.delete_conversation(conv.conversation_id)
        try:
            await api.delete_knowledge(KNOWLEDGE_NAME)
        except KeyError:
            pass
        await api.close()
        print("done")


if __name__ == "__main__":
    asyncio.run(main())
