"""Task 3: Create a conversation + session and chat with the model.

ChatSession is the composition entrypoint: it wraps ONE llama-index FunctionAgent,
adds memory (summary buffer + vector + facts), exposes retrieval tools filtered by
conversation type, and routes every tool result through the Reconstructor so the
agent reasons over full file cards with highlighted sections.

This example:
- creates a local conversation bound to a knowledge
- opens a ChatSession with all four retrieval pipelines + the Reconstructor wired
- runs a constrained-mode turn (model forced to use local_embedded_retrieval) and
  prints the live chat.* events, the reply, and the reconstructed file cards
"""

import asyncio
import json
from uuid import uuid4

from raven.chat_session import ChatSession
from raven.conversation_manager import ConversationManager
from raven.events import EventBus, EventType
from raven.knowledge import KnowledgeBase
from raven.model import DEFAULT_BASE_MODEL, DEFAULT_EMBED_MODEL, ModelManager
from raven.pipeline import (
    AgreementBasedRetrievalPipeline,
    EmbeddedRetrievalPipeline,
    HierarchicalRetrievalPipeline,
    VectorConditionedRetrievalPipeline,
    LOCAL_EMBEDDED_RETRIEVAL,
)
from raven.reconstructor import Reconstructor

KNOWLEDGE_NAME = "engineering"


async def main() -> None:
    bus = EventBus()
    mm = ModelManager(bus)
    await mm.start()

    embed = await mm.load_embed_model(DEFAULT_EMBED_MODEL)
    llm = await mm.load_base_model(DEFAULT_BASE_MODEL)

    kb = KnowledgeBase(bus, embed_model=embed)
    await kb.start()

    try:
        kb.get(KNOWLEDGE_NAME)
    except KeyError as exc:
        print(f"knowledge missing — run create_knowledge.py first: {exc}")
        await kb.close()
        await mm.stop()
        return

    # ---- pipelines + reconstructor (shared by the session) -----------
    embedded = EmbeddedRetrievalPipeline(kb, embed, bus)
    hierarchical = HierarchicalRetrievalPipeline(kb, llm, bus)
    agreement = AgreementBasedRetrievalPipeline(kb, embedded, hierarchical, bus)
    vector_conditioned = VectorConditionedRetrievalPipeline(kb, embedded, hierarchical, bus)
    reconstructor = Reconstructor(kb, bus)

    # ---- conversation + session --------------------------------------
    mgr = ConversationManager(bus, knowledge_base=kb)
    await mgr.start()
    conv = await mgr.create(knowledge_name=KNOWLEDGE_NAME)
    print(f"conversation {conv.conversation_id} ({conv.type})")

    sess = ChatSession(
        knowledge_base=kb,
        bus=bus,
        conversation=conv,
        llm=llm,
        embed_model=embed,
        embedded_pipeline=embedded,
        hierarchical_pipeline=hierarchical,
        agreement_pipeline=agreement,
        vector_conditioned_pipeline=vector_conditioned,
        reconstructor=reconstructor,
        token_limit=None,  # None -> llama-index derives it from the model's context window
    )

    # ---- constrained-mode turn (deterministic: model MUST call the tool)
    op_id = uuid4().hex
    task = asyncio.create_task(
        sess.stream(
            "What is a reaction wheel?",
            retrieval_mode=LOCAL_EMBEDDED_RETRIEVAL,
            op_id=op_id,
        )
    )

    # Live renderer: deltas arrive token-by-token, so buffer them and print one
    # block per kind (thinking / tool call / response) instead of one line per
    # token. Non-delta events (tool calls, retrievals, errors) flush the current
    # buffer and print on their own line.
    #
    # NOTE: do NOT re-read bus.history() after the turn to collect chat.retrieved
    # events — the history deque is small and the per-token chat.delta flood evicts
    # the early events. Collect everything we need live, as it streams.
    kind: str | None = None
    buffer: list[str] = []
    retrieved_sections: list[str] = []

    def flush_block() -> None:
        nonlocal kind, buffer
        if kind is not None:
            text = "".join(buffer).strip()
            if text:
                print(f"\n[{kind}]\n{text}")
            kind = None
            buffer = []

    async def on_event(ev) -> None:
        nonlocal kind, buffer
        if ev.type is EventType.CHAT_DELTA:
            if ev.data["kind"] == "thinking_chunk":
                if kind != "thinking":
                    flush_block()
                    kind = "thinking"
                buffer.append(ev.data["delta"])
            elif ev.data["kind"] == "response_chunk":
                if kind != "response":
                    flush_block()
                    kind = "response"
                buffer.append(ev.data["delta"])
        elif ev.type is EventType.CHAT_TOOL_CALL:
            flush_block()
            print(
                f"\n[tool_call] {ev.data['name']} "
                f"{json.dumps(ev.data['args'], ensure_ascii=False)}"
            )
        elif ev.type is EventType.CHAT_TOOL_RESULT:
            flush_block()
            print(
                f"[tool_result] {ev.data['name']} ok={ev.data['ok']}"
                + (f" error={ev.data['error']}" if ev.data.get("error") else "")
            )
        elif ev.type is EventType.CHAT_RETRIEVED:
            flush_block()
            retrieved_sections.append(ev.data["section_id"])
            print(
                f"[retrieved] {ev.data['knowledge']}/{ev.data['file']} "
                f"#{ev.data['section_id']}"
            )
        elif ev.type is EventType.ERROR:
            flush_block()
            print(f"\n[error] {ev.data}")
        elif ev.type is EventType.CHAT_COMPLETE:
            flush_block()
        else:
            flush_block()
            print(f"[{ev.type.value}]")

    try:
        try:
            async for ev in bus.subscribe(op_id=op_id):
                await on_event(ev)
                if ev.type in (EventType.CHAT_COMPLETE, EventType.ERROR):
                    break
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        reply = await task
        print(f"\nreply:\n{reply}\n")

        # ---- reconstructed cards were built inside tool_result; show one ----
        print(f"highlighted sections from this turn: {retrieved_sections}")
    finally:
        # close/delete must ALWAYS run, even if the turn raised above, otherwise
        # the local Qdrant stores keep their file locks until process exit.
        sess.close()  # releases the local Qdrant lock so the dir can be reused/deleted
        await mgr.delete(conv.conversation_id)
        await kb.close()
        await mm.stop()


if __name__ == "__main__":
    asyncio.run(main())