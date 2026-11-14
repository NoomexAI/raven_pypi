"""Exercise v0.2.1 retrieval, reconstruction, and activity history over HTTP.

Install noomexai-raven[server]==0.2.1. Start Ollama with gemma4:e2b and bge-m3
available, then run:
    python examples/e2e_smoke.py
"""

import asyncio
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from nraven.server import create_app


SAMPLE = Path(__file__).resolve().parents[1] / "docs/assets/downloads/test_files/Reaction_wheel.txt"
QUERY = "What is the safe-hold deadline after the angular momentum anomaly is detected?"


async def read_events(client, events_url, *, after=None):
    # RAVEN sends each event envelope on a single SSE data line.
    headers = {"Last-Event-ID": str(after)} if after is not None else {}
    async with client.stream("GET", events_url, headers=headers) as response:
        response.raise_for_status()
        return [
            json.loads(line.removeprefix("data: "))
            async for line in response.aiter_lines()
            if line.startswith("data: ")
        ]


async def complete(client, response):
    response.raise_for_status()
    assert response.status_code == 202, response.text
    events = await read_events(client, response.json()["events_url"])
    assert events and events[-1]["type"] == "operation.completed", events[-1:]
    return events


async def main(home: Path) -> None:
    app = create_app(home)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://127.0.0.1",
            headers={"Authorization": f"Bearer {app.state.bearer_token}"},
        ) as client:
            await complete(client, await client.post("/api/v1/provider/models/configure", json={
                "llm": {"provider": "ollama", "model": "gemma4:e2b", "role": "llm"},
                "embedding": {"provider": "ollama", "model": "bge-m3:latest", "role": "embedding"},
            }))
            created = await client.post("/api/v1/knowledges", json={"name": "engineering"})
            created.raise_for_status()
            files_url = "/api/v1/knowledges/engineering/files"
            with SAMPLE.open("rb") as source:
                await complete(client, await client.post(
                    files_url, files={"file": (SAMPLE.name, source, "text/plain")},
                ))
            files = await client.get(files_url)
            files.raise_for_status()
            file, = files.json()["items"]
            scope = {"selections": [{"knowledge_name": "engineering", "file_id": file["file_id"]}]}

            # Standalone retrieval returns its result on the operation stream.
            search = await client.post("/api/v1/retrieval/embedded", json={"query": QUERY, "scope": scope})
            events = await complete(client, search)
            result, = (event for event in events if event["type"] == "retrieval.result")
            sections = result["data"]["result"]
            assert sections and all(section["file_name"] == SAMPLE.name for section in sections)
            replay = await read_events(client, search.json()["events_url"], after=result["event_id"] - 1)
            assert replay[0] == result

            references = [
                {key: section[key] for key in ("knowledge_name", "file_name", "section_id")}
                for section in sections
            ]
            reconstructed = await complete(client, await client.post(
                "/api/v1/reconstruction", json={"sections": references},
            ))
            file_event, = (event for event in reconstructed if event["type"] == "reconstruction.file")
            highlighted = {section["section_id"] for section in file_event["data"]["sections"] if section["highlighted"]}
            assert highlighted == {section["section_id"] for section in references}
            print(f"Retrieved and reconstructed {SAMPLE.name}, then verified SSE replay.")

            # The activity route records the action. GET reads the log for the UI.
            conversation = await client.post("/api/v1/conversations", json={"scope": scope})
            conversation.raise_for_status()
            url = f"/api/v1/conversations/{conversation.json()['conversation_id']}/activities"
            actions = await complete(client, await client.post(f"{url}/knowledges/engineering/files"))
            recorded, = (event for event in actions if event["type"] == "conversation.activity.recorded")
            history = await client.get(url, params={"limit": 1})
            history.raise_for_status()
            entry, = history.json()["items"]
            assert entry["activity_id"] == recorded["data"]["activity_id"]
            assert entry["kind"] == "list_files" and entry["turn_id"] is None
            assert history.json()["next_cursor"] is None
            print(f"Read pending activity {entry['activity_id']} from the conversation log.")


if __name__ == "__main__":
    with TemporaryDirectory(prefix="raven-http-") as directory:
        asyncio.run(main(Path(directory)))
