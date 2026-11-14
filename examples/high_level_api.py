"""Search selected documents and reconstruct sources directly with the Python API.

Set FILE_PATH below. Start Ollama and pull gemma4:e2b and bge-m3, then run:
    python examples/high_level_api.py
"""

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from nraven import ModelRole, ModelSpec, Raven, RetrievalScope, RetrievalSelection


# Supported formats: .txt, .md, .markdown, .pdf, and .docx.
FILE_PATH = "<Your-file-path.txt>"
QUERY = "What is the main topic of this document?"


async def main(home: Path) -> None:
    raven = Raven(home)
    await raven.start()
    try:
        configured = await raven.configure_models(
            ModelSpec(provider="ollama", model="gemma4:e2b", role=ModelRole.LLM),
            ModelSpec(provider="ollama", model="bge-m3:latest", role=ModelRole.EMBEDDING),
        )
        await configured.result()

        await (await raven.create_knowledge("engineering")).result()
        record = await (await raven.ingest("engineering", Path(FILE_PATH))).result()
        scope = RetrievalScope(selections=(
            RetrievalSelection(knowledge_name="engineering", file_id=record["file_id"]),
        ))

        # The strategy has its own arguments. The result belongs to this task.
        search = await raven.retrieve_embedded(QUERY, scope=scope, top_k=2)
        sections = await search.result()
        if not sections:
            raise RuntimeError("The search returned no sections.")
        for section in sections:
            print(f"Retrieved {section['file_name']} section {section['section_id']}")

        # Reconstruct the file with the returned sections highlighted.
        reconstruction = await raven.reconstruct(sections)
        for file in await reconstruction.result():
            print(f"\n{file['knowledge_name']}/{file['file_name']}")
            for section in file["sections"]:
                if section["highlighted"]:
                    print(section["raw_content"])
    finally:
        await raven.close()


if __name__ == "__main__":
    with TemporaryDirectory(prefix="raven-direct-") as directory:
        asyncio.run(main(Path(directory)))
