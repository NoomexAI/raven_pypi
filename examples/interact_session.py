"""Record manual document work, then ask the agent to continue in the same session.

Run create_knowledge.py and ingest_knowledge.py first:
    python examples/interact_session.py
"""

import argparse
import asyncio
from pathlib import Path

from nraven import EventType, ModelRole, ModelSpec, Raven, RetrievalScope, RetrievalSelection


DEFAULT_QUERY = (
    "According to Reaction_wheel.txt, what is the maximum time in milliseconds for the spacecraft "
    "to enter magnetic-torquer-only safe-hold after detecting an angular momentum anomaly?"
)


async def main(home: Path, name: str, filename: str, query: str, llm: str, embedding: str) -> None:
    raven = Raven(home)
    await raven.start()
    try:
        configured = await raven.configure_models(
            ModelSpec(provider="ollama", model=llm, role=ModelRole.LLM),
            ModelSpec(provider="ollama", model=embedding, role=ModelRole.EMBEDDING),
        )
        await configured.result()

        scope = RetrievalScope(selections=(RetrievalSelection(knowledge_name=name, file_name=filename),))
        conversation = await (await raven.create_conversation(scope=scope)).result()
        session = raven.session(conversation)
        await session.start()
        try:
            # Session operations record the user's work in the conversation activity log.
            manual_search = await session.retrieve(query, retrieval_mode="embedded")
            sections = await manual_search.result()
            if not sections:
                raise RuntimeError("The manual search returned no sections.")
            source = sections[0]
            await (await session.open_section(source["knowledge_name"], source["section_id"])).result()

            # The next turn receives the pending manual actions and uses the same scope.
            run = await session.generate_response(f"Use embedded retrieval to answer. {query}", retrieval_mode="embedded")
            async for event in run.stream:
                if event.type == EventType.CHAT_TOOL_CALL:
                    print(f"[tool] {event.data['name']}", flush=True)
                elif event.type == EventType.CHAT_RESPONSE_DELTA:
                    print(event.data["delta"], end="", flush=True)
            result = await run.collect()
            print(f"\nConversation {conversation.conversation_id}, turn {run.turn_id}")
            for file in result.reconstructed_sources:
                print(f"Reconstructed {file['file_name']}")

            for activity in await raven.list_activities(conversation.conversation_id, run.turn_id):
                print(f"Linked activity {activity.sequence}: {activity.kind}")
        finally:
            await session.close()
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("query", nargs="?", default=DEFAULT_QUERY)
    parser.add_argument("--home", type=Path, default=Path("raven-home/examples"))
    parser.add_argument("--knowledge", default="engineering")
    parser.add_argument("--file", default="Reaction_wheel.txt")
    parser.add_argument("--llm", default="gemma4:e2b")
    parser.add_argument("--embedding", default="bge-m3:latest")
    args = parser.parse_args()
    asyncio.run(main(args.home, args.knowledge, args.file, args.query, args.llm, args.embedding))
