import asyncio
import json
from types import SimpleNamespace

import pytest

from raven.events import Event, EventBus, EventType
from raven.operations import OperationManager, OperationStatus
from raven.agent.catalog import summarize_reconstructed_evidence
from raven.chat_session import ChatSession


def test_summarize_reconstructed_evidence_contains_sources_without_raw_sections():
    summary = summarize_reconstructed_evidence(
        [
            {
                "knowledge_name": "engineering",
                "file_name": "manual.pdf",
                "sections": [
                    {"section_id": "file-1", "highlighted": True, "raw_content": "secret"},
                    {"section_id": "file-2", "highlighted": False, "raw_content": "other"},
                ],
            }
        ]
    )

    assert summary == {
        "kind": "retrieval",
        "file_count": 1,
        "section_count": 1,
        "files": [
            {
                "knowledge": "engineering",
                "file": "manual.pdf",
                "section_ids": ["file-1"],
            }
        ],
    }
    assert "raw_content" not in str(summary)


def test_chat_session_exposes_memory_and_navigation_tools():
    session = ChatSession.__new__(ChatSession)
    session.conversation = SimpleNamespace(type="local", knowledge_name="engineering")

    names = {tool.metadata.name for tool in session._build_tools()}

    assert {"get_memory", "save_preference"}.issubset(names)
    assert {
        "list_knowledges",
        "list_files",
        "list_sections",
        "get_section_metadata",
    }.issubset(names)
    assert "global_embedded_retrieval" not in names
    assert "local_embedded_retrieval" in names


@pytest.mark.asyncio
async def test_section_navigation_returns_metadata_and_raw_content():
    section = {
        "section_id": "file-1",
        "file_id": "file",
        "file_name": "manual.pdf",
        "section_index": 1,
        "summary": "A summary",
        "keywords": ["thermal"],
        "conditions": [],
        "definitions": [],
        "raw_content": "The complete section text.",
    }

    class FakeKnowledge:
        name = "engineering"

        def get_section(self, section_id):
            return section if section_id == "file-1" else None

    session = ChatSession.__new__(ChatSession)
    session.conversation = SimpleNamespace(type="global", knowledge_name=None)
    session._kb = SimpleNamespace(get=lambda _name: FakeKnowledge())
    session._pending_tool_results = {}

    result = json.loads(await session._get_section_metadata("file-1", "engineering"))

    assert result["section"]["summary"] == "A summary"
    assert result["section"]["raw_content"] == "The complete section text."


@pytest.mark.asyncio
async def test_operation_completion_has_replayable_events():
    bus = EventBus(history_size=20)
    manager = OperationManager(bus)

    async def work(op_id: str):
        bus.publish(Event(
            type=EventType.RETRIEVAL_MODE_SELECTED,
            data={"mode": "test"},
            op_id=op_id,
        ))
        await asyncio.sleep(0)
        return {"ok": True}

    record = await manager.submit(work)
    await manager.wait(record.operation_id)

    assert record.status is OperationStatus.COMPLETED
    events = manager.history(record.operation_id)
    assert [event.event_id for event in events] == list(range(1, len(events) + 1))
    assert events[-1].type is EventType.OPERATION_COMPLETED
    assert manager.history(record.operation_id, after_event_id=1)[0].event_id == 2
    await manager.close()


@pytest.mark.asyncio
async def test_operation_cancellation_is_terminal():
    bus = EventBus()
    manager = OperationManager(bus)

    async def work(_op_id: str):
        await asyncio.Event().wait()

    record = await manager.submit(work)
    await asyncio.sleep(0)
    await manager.cancel(record.operation_id)

    assert record.status is OperationStatus.CANCELLED
    assert any(event.type is EventType.OPERATION_CANCELLED for event in manager.history(record.operation_id))
    await manager.close()
