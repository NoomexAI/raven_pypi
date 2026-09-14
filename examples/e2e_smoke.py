"""End-to-end smoke test for the local RAVEN runtime and HTTP service.

This script deliberately points LocalOllama at the repository root. It never
uses a system Ollama installation. The project-local binary and models must be
available under ``<repo>/ollama`` or the normal RAVEN downloader will populate
that directory.

Run from the repository root:

    . .\test_venv\Scripts\Activate.ps1
    $env:PYTHONPATH = "$PWD\src"
    python examples/e2e_smoke.py
"""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator

import httpx

from nraven.events import EventType
from nraven.high_level_api import Raven
from nraven.local_ollama import LocalOllama
from nraven.pipeline import GLOBAL_EMBEDDED_RETRIEVAL, LOCAL_EMBEDDED_RETRIEVAL
from nraven.server import ServerSettings, create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]
BASE_MODEL = "gemma4:e2b"
EMBED_MODEL = "bge-m3"


def check_project_ollama(root: Path) -> None:
    binary = root / "ollama" / "bin" / ("ollama.exe" if __import__("os").name == "nt" else "ollama")
    if not binary.is_file():
        raise RuntimeError(
            f"project-local Ollama binary is missing: {binary}. "
            "The RAVEN downloader will install it when startup is allowed to download."
        )


def fixture_text() -> str:
    return """RAVEN smoke-test manual.

The reaction wheel controller enters thermal protection when the bearing
temperature exceeds 80 degrees Celsius. The controller then reduces the
maximum wheel speed to prevent further damage.

The preferred maintenance interval is 500 operating hours.
"""


def decode_json(raw: str) -> Any:
    return json.loads(raw)


async def invoke_tools(session, expected: dict[str, tuple[Any, ...]]) -> None:
    tools = {tool.metadata.name: tool for tool in session._tools}
    for name, args in expected.items():
        if name not in tools:
            raise AssertionError(f"tool {name!r} is not exposed")
        output = await tools[name].acall(*args)
        if output.is_error:
            raise AssertionError(f"tool {name!r} failed: {output.raw_output}")
        print(f"    tool {name}: ok")


async def run_core_api(root: Path) -> None:
    print("[core] starting in-process Raven")
    with tempfile.TemporaryDirectory(prefix="raven-core-e2e-") as temp_name:
        temp = Path(temp_name)
        fixture = temp / "manual.txt"
        fixture.write_text(fixture_text(), encoding="utf-8")

        raven = Raven(
            base_model=BASE_MODEL,
            embed_model=EMBED_MODEL,
            server=LocalOllama(root=root),
            knowledge_base_path=temp / "knowledge_base",
            conversation_dir=temp / "conversations",
        )
        await raven.start()
        try:
            engineering = await raven.create_knowledge("engineering")
            safety = await raven.create_knowledge("safety")
            assert engineering.name == "engineering"
            assert safety.name == "safety"

            count = await raven.ingest("engineering", str(fixture))
            assert count > 0
            files = raven.list_files("engineering")
            assert files and files[0]["file_name"] == "manual.txt"
            sections = raven.list_sections("engineering", "manual.txt")
            assert sections and sections[0]["raw_content"]
            section = raven.get_section("engineering", sections[0]["section_id"])
            assert section and section["raw_content"]

            local = await raven.create_conversation("engineering")
            global_ = await raven.create_conversation()
            local_session = await raven.session(local.conversation_id)
            global_session = await raven.session(global_.conversation_id)

            local_names = {tool.metadata.name for tool in local_session._tools}
            global_names = {tool.metadata.name for tool in global_session._tools}
            assert "global_embedded_retrieval" not in local_names
            assert "local_embedded_retrieval" in local_names
            assert "global_embedded_retrieval" in global_names
            for name in {
                "get_memory",
                "save_preference",
                "list_knowledges",
                "list_files",
                "list_sections",
                "get_section_metadata",
            }:
                assert name in local_names and name in global_names

            await invoke_tools(
                local_session,
                {
                    "list_knowledges": (),
                    "list_files": (),
                    "list_sections": ("manual.txt",),
                    "get_section_metadata": (sections[0]["section_id"],),
                    "get_memory": ("thermal protection",),
                    "save_preference": ("Prefer concise maintenance summaries.",),
                },
            )

            local_events = []
            async for event in raven.stream(
                "What temperature triggers thermal protection?",
                conversation_id=local.conversation_id,
                retrieval_mode=LOCAL_EMBEDDED_RETRIEVAL,
            ):
                local_events.append(event)
            assert any(event.type is EventType.CHAT_TOOL_CALL for event in local_events)
            assert any(event.type is EventType.CHAT_TOOL_RESULT for event in local_events)
            assert any(event.type is EventType.CHAT_COMPLETE for event in local_events)
            print("    local retrieval chat: ok")

            global_events = []
            async for event in raven.stream(
                "Which knowledge contains information about thermal protection?",
                conversation_id=global_.conversation_id,
                retrieval_mode=GLOBAL_EMBEDDED_RETRIEVAL,
            ):
                global_events.append(event)
            assert any(event.type is EventType.CHAT_TOOL_CALL for event in global_events)
            assert any(event.type is EventType.CHAT_COMPLETE for event in global_events)
            print("    global retrieval chat: ok")
        finally:
            await raven.close()
    print("[core] passed")


async def read_sse(response: httpx.Response) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    current: dict[str, Any] = {}
    async for line in response.aiter_lines():
        if line.startswith("id: "):
            current["event_id"] = int(line[4:])
        elif line.startswith("event: "):
            current["type"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
        elif not line and current:
            events.append(current)
            current = {}
    if current:
        events.append(current)
    return events


async def get_operation_events(
    client: httpx.AsyncClient,
    token: str,
    events_url: str,
    last_event_id: int = 0,
) -> list[dict[str, Any]]:
    headers = {"Authorization": f"Bearer {token}"}
    if last_event_id:
        headers["Last-Event-ID"] = str(last_event_id)
    async with client.stream("GET", events_url, headers=headers) as response:
        assert response.status_code == 200, await response.aread()
        return await read_sse(response)


async def run_server_api(root: Path) -> None:
    print("[server] starting in-process ASGI application")
    with tempfile.TemporaryDirectory(prefix="raven-server-e2e-") as temp_name:
        temp = Path(temp_name)
        fixture = temp / "manual.txt"
        fixture.write_text(fixture_text(), encoding="utf-8")
        token = "e2e-test-token"
        settings = ServerSettings(
            token=token,
            allowed_roots=(temp,),
            upload_dir=temp / "uploads",
            raven_kwargs={
                "base_model": BASE_MODEL,
                "embed_model": EMBED_MODEL,
                "server": LocalOllama(root=root),
                "knowledge_base_path": temp / "knowledge_base",
                "conversation_dir": temp / "conversations",
            },
        )
        app = create_app(settings)

        @asynccontextmanager
        async def client_context() -> AsyncIterator[httpx.AsyncClient]:
            async with app.router.lifespan_context(app):
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(
                    transport=transport, base_url="http://testserver", timeout=None
                ) as client:
                    yield client

        async with client_context() as client:
            headers = {"Authorization": f"Bearer {token}"}
            assert (await client.get("/api/v1/health/live")).status_code == 200
            assert (await client.get("/api/v1/runtime")).status_code == 401

            initialize = await client.post("/api/v1/runtime/initialize", headers=headers)
            assert initialize.status_code == 202, initialize.text
            init_ref = initialize.json()
            init_events = await get_operation_events(client, token, init_ref["events_url"])
            assert any(event["type"] == "operation.completed" for event in init_events)
            assert (await client.get("/api/v1/health/ready", headers=headers)).status_code == 200
            print("    initialization and readiness: ok")

            created = await client.post(
                "/api/v1/knowledges",
                headers=headers,
                json={"name": "engineering"},
            )
            assert created.status_code == 201, created.text

            with fixture.open("rb") as file_handle:
                upload = await client.post(
                    "/api/v1/knowledges/engineering/files",
                    headers=headers,
                    files={"upload": ("manual.txt", file_handle, "text/plain")},
                )
            assert upload.status_code == 202, upload.text
            upload_ref = upload.json()
            upload_events = await get_operation_events(
                client, token, upload_ref["events_url"]
            )
            assert any(event["type"] == "operation.completed" for event in upload_events), upload_events
            print("    multipart ingestion: ok")

            files = await client.get(
                "/api/v1/knowledges/engineering/files", headers=headers
            )
            assert files.status_code == 200
            file_name = files.json()[0]["file_name"]
            sections = await client.get(
                f"/api/v1/knowledges/engineering/files/{file_name}/sections",
                headers=headers,
            )
            assert sections.status_code == 200
            section = sections.json()[0]
            detail = await client.get(
                f"/api/v1/knowledges/engineering/files/{file_name}/sections/{section['section_id']}",
                headers=headers,
            )
            assert detail.status_code == 200
            assert detail.json()["raw_content"]
            print("    knowledge/file/section navigation: ok")

            global_response = await client.post(
                "/api/v1/conversations", headers=headers, json={}
            )
            local_response = await client.post(
                "/api/v1/conversations",
                headers=headers,
                json={"knowledge_name": "engineering"},
            )
            assert global_response.json()["type"] == "global"
            assert local_response.json()["type"] == "local"
            print("    global/local conversations: ok")

            turn = await client.post(
                f"/api/v1/conversations/{local_response.json()['conversation_id']}/turns",
                headers=headers,
                json={
                    "user_text": "What temperature triggers thermal protection?",
                    "retrieval_mode": LOCAL_EMBEDDED_RETRIEVAL,
                },
            )
            assert turn.status_code == 202, turn.text
            turn_ref = turn.json()
            turn_events = await get_operation_events(
                client, token, turn_ref["events_url"]
            )
            event_types = {event["type"] for event in turn_events}
            assert "chat.tool_call" in event_types
            assert "chat.tool_result" in event_types
            assert "chat.complete" in event_types
            assert "operation.completed" in event_types
            print("    streamed chat events: ok")

            runtime = app.state.runtime
            oldest, _latest = runtime.operations.history_bounds(
                turn_ref["operation_id"]
            )
            if oldest is not None and oldest > 2:
                expired = await client.get(
                    turn_ref["events_url"],
                    headers={**headers, "Last-Event-ID": "1"},
                )
                assert expired.status_code == 410

            replay_after = max(0, (oldest or 1) - 1)
            replay = await get_operation_events(
                client,
                token,
                turn_ref["events_url"],
                last_event_id=replay_after,
            )
            assert replay and all(event["event_id"] > replay_after for event in replay)
            print("    SSE replay: ok")

            async def blocked(_operation_id: str):
                await asyncio.Event().wait()

            record = await runtime.operations.submit(blocked)
            cancelled = await client.post(
                f"/api/v1/operations/{record.operation_id}/cancel",
                headers=headers,
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled", cancelled.json()
            print("    cancellation: ok")
    print("[server] passed")


async def main(args: argparse.Namespace) -> None:
    root = args.ollama_root.resolve()
    check_project_ollama(root)
    if not args.server_only:
        await run_core_api(root)
    if not args.core_only:
        await run_server_api(root)
    print("E2E smoke test passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ollama-root",
        type=Path,
        default=PROJECT_ROOT,
        help="RAVEN root containing the project-local ollama/ directory",
    )
    parser.add_argument("--core-only", action="store_true")
    parser.add_argument("--server-only", action="store_true")
    options = parser.parse_args()
    if options.core_only and options.server_only:
        parser.error("--core-only and --server-only cannot be combined")
    asyncio.run(main(options))
