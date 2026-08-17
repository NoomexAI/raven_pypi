import asyncio

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from raven.events import Event, EventType
from raven.server import ServerSettings, create_app


def test_server_has_auth_health_and_openapi_contract():
    token = "unit-test-token"
    app = create_app(ServerSettings(token=token))
    with TestClient(app) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/runtime").status_code == 401
        response = client.get(
            "/api/v1/runtime",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["ready"] is False

        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        paths = schema.json()["paths"]
        assert "/api/v1/conversations/{conversation_id}/turns" in paths
        assert "/api/v1/operations/{operation_id}/events" in paths
        assert "/api/v1/knowledges/{name}/files/{file_name}/sections/{section_id}" in paths


def test_sse_replays_after_last_event_id():
    token = "unit-test-token"
    app = create_app(ServerSettings(token=token, event_history_size=20))
    runtime = app.state.runtime

    async def submit_finished_operation() -> str:
        async def work(op_id: str):
            runtime.bus.publish(
                Event(
                    type=EventType.CHAT_DELTA,
                    data={"text": "hello"},
                    op_id=op_id,
                )
            )
            return {"reply": "hello"}

        record = await runtime.operations.submit(work)
        await runtime.operations.wait(record.operation_id)
        return record.operation_id

    operation_id = asyncio.run(submit_finished_operation())
    with TestClient(app) as client:
        response = client.get(
            f"/api/v1/operations/{operation_id}/events",
            headers={"Authorization": f"Bearer {token}", "Last-Event-ID": "1"},
        )

    assert response.status_code == 200
    assert "event: chat.delta" in response.text
    assert "event: operation.completed" in response.text
    assert "id: 1\n" not in response.text
