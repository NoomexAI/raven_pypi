"""Ingest a document and stream progress through the operation task.

Run create_knowledge.py first and set FILE_PATH below. Start Ollama and pull
gemma4:e2b and bge-m3, then run:
    python examples/ingest_knowledge.py
"""

import argparse
import asyncio
from pathlib import Path

from nraven import EventType, ModelRole, ModelSpec, Raven


# Supported formats: .txt, .md, .markdown, .pdf, and .docx.
FILE_PATH = "<Your-file-path.txt>"


async def main(home: Path, name: str, source: Path, llm: str, embedding: str) -> None:
    raven = Raven(home)
    await raven.start()
    try:
        # Look up the target before loading the models.
        raven.get_knowledge(name)
        configured = await raven.configure_models(
            ModelSpec(provider="ollama", model=llm, role=ModelRole.LLM),
            ModelSpec(provider="ollama", model=embedding, role=ModelRole.EMBEDDING),
        )
        await configured.result()

        task = await raven.ingest(name, source)
        async for event in task.events():
            if event.type == EventType.INGESTION_PROGRESS:
                print(f"{event.data['stage']}: {event.data['status']}", flush=True)
        record = await task.result()
        print(f"Ingested {source.name} with file ID {record['file_id']}.")
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path, default=FILE_PATH)
    parser.add_argument("--home", type=Path, default=Path("raven-home/examples"))
    parser.add_argument("--knowledge", default="engineering")
    parser.add_argument("--llm", default="gemma4:e2b")
    parser.add_argument("--embedding", default="bge-m3:latest")
    args = parser.parse_args()
    asyncio.run(main(args.home, args.knowledge, args.source, args.llm, args.embedding))
