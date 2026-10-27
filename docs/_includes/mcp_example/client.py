"""Small asynchronous client for the RAVEN endpoints exposed by this example."""

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import UUID

import anyio
import httpx2 as httpx
from mcp.server.mcpserver.exceptions import ToolError



class RavenClient:
    OPERATION_TIMEOUT_SECONDS = 600.0
    OPERATION_POLL_SECONDS = 1.0
    MAX_UPLOAD_BYTES = 64 * 1024 * 1024

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http


    async def list_knowledges(
        self,
        limit: int = 10,
        after_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if after_name is not None:
            params["after_name"] = after_name
        return await self._request("GET", "/api/v1/knowledges", params=params)


    async def list_conversations(
        self,
        limit: int = 10,
        after_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if after_conversation_id is not None:
            params["after_conversation_id"] = after_conversation_id
        return await self._request("GET", "/api/v1/conversations", params=params)


    async def create_conversation(
        self,
        knowledge_name: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/conversations",
            json={"knowledge_name": knowledge_name},
        )


    async def create_knowledge(self, name: str, user_summary: str = "") -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/knowledges",
            json={"name": name, "user_summary": user_summary},
        )


    async def configure_models(self, llm_model: str, embedding_model: str) -> dict[str, Any]:
        models = {
            "llm": {"provider": "ollama", "model": llm_model, "role": "llm"},
            "embedding": {"provider": "ollama", "model": embedding_model, "role": "embedding"},
        }
        reference = await self._request(
            "POST", "/api/v1/provider/models/configure", json=models,
        )
        operation_id, task_id = self._operation_ids(reference)
        await self._wait_for_operation(operation_id)
        return {
            "operation_id": operation_id,
            "task_id": task_id,
            "status": "completed",
            "models": models,
        }


    async def ingest_file(self, knowledge_name: str, file_path: str) -> dict[str, Any]:
        file_name, content, content_type = await asyncio.to_thread(self._read_upload, file_path)
        reference = await self._request(
            "POST",
            f"/api/v1/knowledges/{quote(knowledge_name, safe='')}/files",
            files={"file": (file_name, content, content_type)},
        )
        operation_id, task_id = self._operation_ids(reference)
        await self._wait_for_operation(operation_id)
        return {
            "operation_id": operation_id,
            "task_id": task_id,
            "status": "completed",
            "knowledge_name": knowledge_name,
            "file_name": file_name,
        }


    async def ask_raven(self, conversation_id: str, user_query: str) -> dict[str, Any]:
        path = f"/api/v1/conversations/{quote(conversation_id, safe='')}/turns"
        reference = await self._request(
            "POST", path, json={"user_query": user_query, "retrieval_mode": "auto"},
        )
        operation_id, task_id = self._operation_ids(reference)
        try:
            turn_id = str(UUID(reference["turn_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ToolError(f"RAVEN operation {operation_id} returned an invalid turn reference.") from None

        try:
            await self._wait_for_operation(operation_id)
            turn = await self._request("GET", f"{path}/{turn_id}")
        except ToolError as error:
            raise ToolError(f"Conversation {conversation_id}, turn {turn_id}: {error}") from None
        result = turn.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("response"), str):
            raise ToolError(
                f"RAVEN operation {operation_id} finished, but turn {turn_id} "
                "has no usable saved answer. Check the turn before submitting again."
            )
        return {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "operation_id": operation_id,
            "task_id": task_id,
            "status": "completed",
            "response": result["response"],
            "evidence": result.get("evidence", []),
            "iteration_limit_reached": result.get("iteration_limit_reached", False),
        }


    def _read_upload(self, file_path: str) -> tuple[str, bytes, str]:
        path = Path(file_path).expanduser()
        if not path.is_absolute():
            raise ToolError("file_path must be an absolute path on the MCP bridge's machine.")
        content_types = {
            ".txt": "text/plain", ".md": "text/markdown",
            ".pdf": "application/pdf",
            ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        }
        content_type = content_types.get(path.suffix.lower())
        if content_type is None:
            raise ToolError("This example accepts only .txt, .md, .pdf, and .docx documents.")
        try:
            with path.open("rb") as source:
                content = source.read(self.MAX_UPLOAD_BYTES + 1)
        except OSError:
            raise ToolError("Cannot read the selected file. Check that it exists and is readable.") from None
        if not content:
            raise ToolError("The selected file is empty.")
        if len(content) > self.MAX_UPLOAD_BYTES:
            raise ToolError("The selected file exceeds this example's 64 MiB upload limit.")
        return path.name, content, content_type


    @staticmethod
    def _operation_ids(reference: dict[str, Any]) -> tuple[str, str]:
        try:
            return str(UUID(reference["operation_id"])), str(UUID(reference["task_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ToolError("RAVEN returned an invalid operation reference.") from None


    async def _wait_for_operation(self, operation_id: str) -> None:
        try:
            async with asyncio.timeout(self.OPERATION_TIMEOUT_SECONDS):
                while True:
                    record = await self._request("GET", f"/api/v1/operations/{operation_id}")
                    status = record.get("status")
                    if status == "completed":
                        return
                    if status in ("failed", "cancelled"):
                        raise ToolError(
                            f"RAVEN operation {operation_id} {status}. Check its status "
                            "and server logs. Work may have committed changes before "
                            "failing or stopping; do not assume rollback or resubmit blindly."
                        )
                    if status not in ("queued", "running"):
                        raise ToolError(f"RAVEN operation {operation_id} returned an invalid status.")
                    await asyncio.sleep(self.OPERATION_POLL_SECONDS)
        except TimeoutError:
            cancelled = await self._cancel_operation(operation_id)
            action = "Cancellation was requested" if cancelled else "Cancellation could not be confirmed"
            raise ToolError(
                f"Stopped waiting for RAVEN operation {operation_id}. {action}; "
                "check the operation before submitting again."
            ) from None
        except asyncio.CancelledError:
            await self._cancel_operation(operation_id)
            raise
        except ToolError as error:
            # Keep the accepted operation recoverable even if observing it fails.
            raise ToolError(f"Operation {operation_id}: {error}") from None


    async def _cancel_operation(self, operation_id: str) -> bool:
        # MCP uses AnyIO cancellation scopes. Shield this short cleanup request
        # so cancelling the tool still gives RAVEN a chance to stop its work.
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(5):
                try:
                    await self._request("POST", f"/api/v1/operations/{operation_id}/cancel")
                    return True
                except ToolError:
                    pass
        return False


    async def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        try:
            response = await self._http.request(method, path, **kwargs)
        except httpx.TimeoutException:
            raise ToolError(
                "RAVEN did not respond before the HTTP timeout. A submitted write "
                "may already have succeeded; check RAVEN before retrying."
            ) from None
        except httpx.RequestError:
            raise ToolError(
                "Cannot communicate with RAVEN. Check RAVEN_URL and whether the "
                "server is running. A submitted write may already have succeeded."
            ) from None

        if response.is_error:
            # Do not forward arbitrary response bodies or HTTP exception strings:
            # they may include deployment details or sensitive request information.
            hints = {
                400: "Check the supplied names, arguments, and model configuration.",
                401: "Check RAVEN_TOKEN and the configured user identity.",
                403: "The configured identity is not permitted to make this request.",
                404: "The resource was not found; check the knowledge, conversation, or turn ID.",
                409: "The resource may already exist, or a turn may already be running in this conversation.",
                413: "The upload exceeds RAVEN's configured size limit.",
                422: "RAVEN rejected the arguments; check their values.",
                429: "RAVEN is busy; try again later.",
                503: "RAVEN is unavailable; check the server status.",
            }
            hint = hints.get(response.status_code, "Check the RAVEN server logs.")
            raise ToolError(f"RAVEN returned HTTP {response.status_code}. {hint}")
        if response.status_code not in (200, 201, 202):
            raise ToolError("RAVEN returned an unexpected HTTP status for this tool.")

        try:
            data = response.json()
        except ValueError:
            raise ToolError("RAVEN returned an invalid JSON response.") from None
        if not isinstance(data, dict):
            raise ToolError("RAVEN returned an unexpected response format.")
        return data
