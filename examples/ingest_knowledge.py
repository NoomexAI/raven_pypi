"""Task 2: Ingest a file into an existing knowledge.

The IngestionPipeline sections the file semantically (embedding-driven), extracts
summary/keywords/conditions/definitions per section (schema-enforced JSON), then
hands the sections to Knowledge.ingest which chunks, embeds, and stores in Qdrant.

Streams ingestion.* events. Uses the history-then-subscribe idiom so we never miss
events an ingest task already published before we subscribed.
"""

import asyncio
from pathlib import Path

from nraven.events import EventBus, EventType
from nraven.knowledge import KnowledgeBase
from nraven.model import DEFAULT_BASE_MODEL, DEFAULT_EMBED_MODEL, ModelManager
from nraven.pipeline import IngestionPipeline

KNOWLEDGE_NAME = "story"
FILE_PATH = Path(__file__).resolve().parent.parent / "test_docs" / "short_story.txt"


async def drain(bus: EventBus, op_id: str) -> None:
    for ev in bus.history(op_id=op_id):
        print(f"  {ev.type.value}: {ev.data}")
        if ev.type in (EventType.INGESTION_COMPLETE, EventType.ERROR):
            return
    async for ev in bus.subscribe(op_id=op_id):
        print(f"  {ev.type.value}: {ev.data}")
        if ev.type in (EventType.INGESTION_COMPLETE, EventType.ERROR):
            break


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

    pipe = IngestionPipeline(bus, kb, llm=llm, embed_model=embed)
    op_id = await pipe.ingest(KNOWLEDGE_NAME, str(FILE_PATH))

    print(f"ingesting {FILE_PATH.name} into '{KNOWLEDGE_NAME}' (op {op_id})")
    await drain(bus, op_id)

    knowledge = kb.get(KNOWLEDGE_NAME)
    print(f"files now: {knowledge.list_files()}")

    await kb.close()
    await mm.stop()


if __name__ == "__main__":
    asyncio.run(main())