"""Task 1: Create a knowledge.

Boots Ollama (ModelManager is the sole model owner), opens the KnowledgeBase,
creates a knowledge, and lists what exists. Idempotent: creating a name that
already exists raises ValueError.
"""

import asyncio

from nraven.events import EventBus
from nraven.knowledge import KnowledgeBase
from nraven.model import DEFAULT_EMBED_MODEL, ModelManager

KNOWLEDGE_NAME = "clinical"


async def main() -> None:
    bus = EventBus()
    mm = ModelManager(bus)
    await mm.start()

    embed = await mm.load_embed_model(DEFAULT_EMBED_MODEL)
    kb = KnowledgeBase(bus, embed_model=embed)
    await kb.start()

    try:
        knowledge = await kb.create(
            KNOWLEDGE_NAME, user_summary="Contains clinical files"
        )
        print(f"created knowledge: {knowledge.name} @ {knowledge.dir_path}")
    except ValueError as exc:
        print(f"already exists: {exc}")

    for entry in kb.list():
        print(f"  {entry['name']}: {entry['count']} chunks, summary={entry['user_summary']!r}")

    await kb.close()
    await mm.stop()


if __name__ == "__main__":
    asyncio.run(main())