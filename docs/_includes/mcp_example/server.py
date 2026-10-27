"""Expose a focused subset of RAVEN's HTTP API as MCP tools over stdio."""

import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Annotated, Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx2 as httpx
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.types import ToolAnnotations
from pydantic import Field

from client import RavenClient


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncGenerator[RavenClient, None]:
    token = os.environ.get("RAVEN_TOKEN", "").strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("Set RAVEN_TOKEN to the current RAVEN service bearer token.")

    base_url = os.environ.get("RAVEN_URL", "http://127.0.0.1:8765").strip()
    try:
        url = urlsplit(base_url)
        valid_url = (
            url.scheme in ("http", "https")
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
        )
        _ = url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("RAVEN_URL must be an HTTP(S) origin without credentials or a path.")

    headers = {"Authorization": f"Bearer {token}"}
    user_id = os.environ.get("RAVEN_USER_ID", "").strip()
    if user_id:
        try:
            headers["X-Raven-User-ID"] = str(UUID(user_id))
        except ValueError:
            raise ValueError("RAVEN_USER_ID must be a valid UUID when supplied.") from None

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    ) as http:
        yield RavenClient(http)


mcp = MCPServer(
    "RavenMCP",
    instructions=(
        "Configure installed Ollama models, create knowledge, and discover it with list_knowledges. "
        "Upload documents with ingest_file, then create a conversation and ask_raven. "
        "A knowledge_name creates a local conversation; omitting it creates a global one. "
        "Find existing conversations with list_conversations and reuse their IDs for follow-ups."
    ),
    lifespan=lifespan,
    log_level="WARNING",
)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def list_conversations(
    ctx: Context[RavenClient, Any],
    limit: Annotated[int, Field(
        ge=1, le=100, strict=True,
        description="Optional. Maximum conversations per page, from 1 to 100; defaults to 10. The server may impose a lower limit.",
    )] = 10,
    after_conversation_id: Annotated[str | None, Field(
        min_length=1, max_length=255,
        description="Optional. Use the previous page's next_cursor to continue; omit or pass null for the first page.",
    )] = None,
) -> dict[str, Any]:
    """Find an existing conversation by title and reuse its ID with ask_raven.

    Args:
        limit (optional, default 10): Maximum conversations per page, from 1
            to 100, subject to the server's page-size limit.
        after_conversation_id (optional, default null): The exact next_cursor
            from the preceding page. Omit or pass null to start at the first page.

    Returns one newest-first page as items and next_cursor. Each item includes
    conversation_id, title, type, knowledge_name, pinned, and creation metadata,
    not its messages. Pass next_cursor as after_conversation_id for the next
    page. An empty items list means no conversations were found. Models are
    not required; a server may impose a lower page-size limit.
    """
    return await ctx.request_context.lifespan_context.list_conversations(limit, after_conversation_id)


@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
))
async def configure_models(
    ctx: Context[RavenClient, Any],
    llm_model: Annotated[str, Field(
        min_length=1, max_length=255, pattern=r"^\S+$",
        description="Required. Installed Ollama model name for generation and agent tool calls, including its tag; no default.",
    )],
    embedding_model: Annotated[str, Field(
        min_length=1, max_length=255, pattern=r"^\S+$",
        description="Required. Installed Ollama model name for document/query embeddings; no default. Keep it fixed for existing knowledge.",
    )],
) -> dict[str, Any]:
    """Configure an installed Ollama LLM and embedding model before ingestion or chat.

    Args:
        llm_model (required): Installed Ollama LLM name, including its tag,
            used for generation and agent tool calls. There is no default.
        embedding_model (required): Installed Ollama embedding model name,
            used to embed documents and queries. There is no default.

    Ollama must already be running with both models installed. Supply model
    names only; this demo always uses the Ollama provider. Waits for RAVEN's
    configuration operation, which prepares adapters and preloads models.
    Reconfiguration can unload replaced Ollama models. Do not change an
    embedding model already used by ingested knowledge. The wait limit is
    ten minutes; cancellation or expiry requests backend cancellation, which
    is cooperative and does not roll back an installed configuration.
    """
    return await ctx.request_context.lifespan_context.configure_models(llm_model, embedding_model)


@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
))
async def create_knowledge(
    ctx: Context[RavenClient, Any],
    name: Annotated[str, Field(
        min_length=1, max_length=255,
        description="Required. Unique name for the new knowledge, reused when ingesting files or creating local conversations; no default.",
    )],
    user_summary: Annotated[str, Field(
        max_length=32768,
        description="Optional. Description of the knowledge's intended contents; defaults to an empty string.",
    )] = "",
) -> dict[str, Any]:
    """Create an empty knowledge before adding documents to it.

    Args:
        name (required): A unique knowledge name to use in later ingestion and
            local-conversation calls. There is no default.
        user_summary (optional, default empty string): A description of the
            knowledge's subject or intended contents.

    Supply a new knowledge name and an optional summary of its contents.
    Models are not required for creation. Returns the saved metadata. An
    existing name produces a conflict; use list_knowledges to discover and
    reuse existing knowledge rather than creating it again.
    """
    return await ctx.request_context.lifespan_context.create_knowledge(name, user_summary)


@mcp.tool(annotations=ToolAnnotations(read_only_hint=True))
async def list_knowledges(
    ctx: Context[RavenClient, Any],
    limit: Annotated[int, Field(
        ge=1, le=100, strict=True,
        description="Optional. Maximum knowledges per page, from 1 to 100; defaults to 10. The server may impose a lower limit.",
    )] = 10,
    after_name: Annotated[str | None, Field(
        max_length=255,
        description="Optional. Use the previous page's next_cursor to continue; omit or pass null for the first page.",
    )] = None,
) -> dict[str, Any]:
    """Discover knowledge names and summaries before choosing a conversation scope.

    Args:
        limit (optional, default 10): Maximum knowledges per page, from 1 to
            100, subject to the server's page-size limit.
        after_name (optional, default null): The exact next_cursor from the
            preceding page. Omit or pass null to start at the first page.

    Returns one page as items and next_cursor. If next_cursor is not null,
    pass it as after_name to read the next page. A server may impose a lower
    page-size limit. An empty items list means no knowledge is available.
    """
    return await ctx.request_context.lifespan_context.list_knowledges(limit, after_name)


@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
))
async def ingest_file(
    ctx: Context[RavenClient, Any],
    knowledge_name: Annotated[str, Field(
        min_length=1, max_length=255,
        description="Required. Existing knowledge to receive this document; use a name from create_knowledge or list_knowledges. No default.",
    )],
    file_path: Annotated[str, Field(
        min_length=1, max_length=32768,
        description="Required. Absolute path to a readable .txt, .md, .pdf, or .docx file on the MCP bridge's machine, not the RAVEN server. No default.",
    )],
) -> dict[str, Any]:
    """Add a document to an existing knowledge after configuring models.

    Args:
        knowledge_name (required): The existing knowledge that will store the
            document. Obtain its name from create_knowledge or list_knowledges.
        file_path (required): Absolute path to the document on the MCP bridge's
            machine. No file picker or default path is supplied.

    Supply an absolute path on the MCP bridge's machine, not the RAVEN server's
    machine. The bridge reads and uploads .txt, .md, .pdf, or .docx files, up to
    64 MiB; RAVEN may impose a lower limit. Waits for ingestion to complete.
    Use only with trusted local callers: this tool reads the chosen local file
    and sends its contents to RAVEN. Do not re-upload a file already ingested.
    Cancellation or the ten-minute wait limit requests backend cancellation.
    """
    return await ctx.request_context.lifespan_context.ingest_file(knowledge_name, file_path)


@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
))
async def ask_raven(
    ctx: Context[RavenClient, Any],
    conversation_id: Annotated[str, Field(
        min_length=1, max_length=255, pattern=r"^[A-Za-z0-9_-]+$",
        description="Required. ID returned by create_conversation or list_conversations; reuse it for follow-ups. No default.",
    )],
    user_query: Annotated[str, Field(
        min_length=1, max_length=32768, pattern=r"\S",
        description="Required. Nonblank question or message for RAVEN, up to 32768 characters; no default.",
    )],
) -> dict[str, Any]:
    """Ask RAVEN a question in an existing conversation and wait for its answer.

    Args:
        conversation_id (required): The ID from create_conversation or
            list_conversations. Reuse it to continue the same discussion.
        user_query (required): A nonblank question or message, up to 32768
            characters. There is no default message or conversation.

    Configure models first and ingest relevant documents for knowledge questions.
    RAVEN's agent chooses its tools automatically within the conversation scope.
    Reuse conversation_id for follow-ups. Returns the saved response, evidence
    references, turn ID, and operation IDs, not internal thinking or full sources.
    This delegates to RAVEN's agent; it is not a direct retrieval call. Only one
    turn can run in a conversation at a time. Cancellation or the ten-minute
    wait limit requests backend cancellation; a turn may already be committed.
    """
    return await ctx.request_context.lifespan_context.ask_raven(conversation_id, user_query)


@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
))
async def create_conversation(
    ctx: Context[RavenClient, Any],
    knowledge_name: Annotated[str | None, Field(
        min_length=1, max_length=255,
        description="Optional. Existing knowledge name for a local conversation. Omit or pass null (the default) for a global conversation.",
    )] = None,
) -> dict[str, Any]:
    """Create a conversation when a new document discussion is needed.

    Args:
        knowledge_name (optional, default null): An existing knowledge name to
            restrict the conversation to that knowledge. Omit or pass null for
            a global conversation spanning the knowledge base. Do not use an
            empty string to request global scope.

    Supply an existing knowledge_name for a local conversation restricted to
    that knowledge, or omit it for a global conversation. Returns metadata
    including conversation_id and type. Each call creates a new conversation;
    reuse the returned ID rather than creating one for each question.
    """
    return await ctx.request_context.lifespan_context.create_conversation(knowledge_name)


if __name__ == "__main__":
    mcp.run(transport="stdio")
