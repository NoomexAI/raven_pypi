# RAVEN

**Retrieval Augmented Adaptive Epistemic Navigation**

RAVEN is an asynchronous RAG and agent backend for building applications over
persistent knowledge and conversations. It combines semantic document
ingestion, multiple retrieval strategies, agent-directed tool use, durable
operations, replayable event streams, conversation memory, and source
reconstruction behind one Python API and an optional FastAPI server.

RAVEN is local-first and privacy-conscious, but it is not limited to offline
models. Applications can use a local Ollama server, cloud models through
LiteLLM, or a mixture of the two.

RAVEN supports two primary integration paths:

```text
Python application  ───────────────────────>  Raven facade

Browser / desktop UI  ─>  FastAPI server  ─>  Raven facade
```

The high-level `Raven` class is the recommended entry point for library use.
The underlying components remain public for applications that need more direct
control.


## Key features

- **Agent-driven retrieval** — the model selects and sequences the tools
  permitted by the conversation scope and the requested retrieval mode.
- **Local and cloud models** — use Ollama locally, LiteLLM-compatible cloud
  providers, or different providers for generation and embeddings.
- **Durable asynchronous operations** — long-running work has observable
  status, task results, cancellation, persisted events, and explicit retry.
- **Replayable streaming** — operation events can be consumed directly in
  Python or transported to a UI through replayable Server-Sent Events (SSE).
- **Persistent knowledge** — parse, semantically split, embed, retrieve, and
  navigate documents stored in isolated knowledge databases.
- **Multiple retrieval strategies** — embedded, hierarchical,
  agreement-based, and vector-conditioned retrieval, each with local and
  global variants.
- **Persistent conversations** — retain messages, tool calls, tool results,
  compacted context, vector memory, preferences, titles, and turn identity.
- **Transparent evidence** — reconstruct the source sections used during a
  response so users can inspect and verify the supporting material.
- **Crash-aware storage** — interrupted operations are detected after restart,
  retryable work remains discoverable, and ingestion state is reconciled.
- **Per-user isolation** — each user UUID receives a separate data,
  operations, settings, and temporary-storage tree.
- **Application-ready server** — the optional FastAPI layer provides bearer
  authentication, OpenAPI schemas, upload controls, operation endpoints, and
  SSE delivery.


## How RAVEN fits together

```text
Python caller                         Browser / desktop UI
      |                                       |
      |                               Authenticated FastAPI
      +-------------------+-------------------+
                          |
                      Raven facade
                          |
                 Operations and events
                          |
       +------------------+-------------------+
       |                  |                   |
 Knowledge pipeline   Conversations      Agent harness
       |              and sessions            |
       +------------------+-------------------+
                          |
               SQLite and local Qdrant
                          |
                 Ollama or LiteLLM
```

The main ownership boundaries are:

| Component | Responsibility |
| --- | --- |
| `Raven` | Composes the backend and exposes its high-level, operation-first API. |
| `KnowledgeBase` | Discovers and manages persistent `Knowledge` resources. |
| `Knowledge` | Owns one knowledge database, its source records, and its vectors. |
| `ConversationManager` | Discovers and manages persistent `Conversation` resources. |
| `Conversation` | Owns messages, turns, memory, preferences, and conversation metadata. |
| `Session` | Temporarily connects one conversation to the agent; it is not persisted. |
| `AgentHarness` | Defines model behavior, available tools, streaming, and evidence handling. |
| `Provider` | Resolves provider-neutral model specifications to Ollama or LiteLLM adapters. |
| `Operation` | Owns the lifecycle and event stream of one user-visible action. |
| `OperationTask` | Represents one invocation, result, cancellation, and retry boundary inside an operation. |

Knowledge and conversations are durable resources. A session is only an
interaction medium: create one when you need to run a turn, then discard it.
Different conversations may run concurrently, while RAVEN prevents overlapping
turns from mutating the same conversation.


## Requirements

- Python 3.11 or newer.
- A writable RAVEN home directory supplied by the application.
- For local models, a separately installed and running
  [Ollama](https://ollama.com/download) server.
- For cloud models, the relevant provider API key available through an
  environment variable.
- Enough system memory, accelerator memory, and storage for the models and
  document collections selected by the application.

RAVEN connects to Ollama but does not start, stop, update, or supervise the
Ollama process. Process lifecycle and host-level resource management belong to
the desktop application or deployment host.

RAVEN does not require a particular GPU API or a fixed amount of VRAM. Those
requirements depend on the selected local model and the way Ollama is deployed.


## Installation

Install the Python library with both Ollama and LiteLLM provider support:

```bash
pip install noomexai-raven
```

Install the library and FastAPI server dependencies:

```bash
pip install "noomexai-raven[server]"
```

RAVEN uses different names at different integration boundaries:

| Context | Name |
| --- | --- |
| Product | RAVEN |
| PyPI distribution | `noomexai-raven` |
| Python package | `nraven` |
| Command-line program | `nraven` |
| High-level Python class | `Raven` |

For example:

```python
from nraven import Raven
```


## Library quick start

The `Raven` facade is the recommended entry point for Python applications. The
example below configures local Ollama models, ingests one document, creates a
global conversation, and streams an agent response:

```python
import asyncio

from nraven import EventType, ModelRole, ModelSpec, Raven


async def main() -> None:
    raven = Raven("./raven-home")
    session = None
    await raven.start()

    try:
        configure_task = await raven.configure_models(
            ModelSpec(
                provider="ollama",
                model="qwen3:8b",
                role=ModelRole.LLM,
            ),
            ModelSpec(
                provider="ollama",
                model="bge-m3",
                role=ModelRole.EMBEDDING,
            ),
        )
        await configure_task.result()

        create_knowledge_task = await raven.create_knowledge(
            "engineering",
            "Engineering reference documents",
        )
        await create_knowledge_task.result()

        ingestion_task = await raven.ingest(
            "engineering",
            "./documents/system-design.pdf",
        )
        await ingestion_task.result()

        create_conversation_task = await raven.create_conversation()
        conversation = await create_conversation_task.result()

        session = raven.session(conversation)
        await session.start()
        run = await session.generate_response(
            "What does the design say about thermal protection?",
            retrieval_mode="auto",
        )

        async for event in run.stream:
            if event.type == EventType.CHAT_THINKING_DELTA:
                print(event.data.get("delta", ""), end="")
            elif event.type == EventType.CHAT_TOOL_CALL:
                print(f"\nTool: {event.data.get('name')}")
            elif event.type == EventType.CHAT_RESPONSE_DELTA:
                print(event.data.get("delta", ""), end="")

        result = await run.collect()
        print(f"\n\nFinal response: {result.response}")
    finally:
        if session is not None:
            await session.close()
        await raven.close()


asyncio.run(main())
```

Ollama must already be running and the selected models must be installed. Use
`Raven.pull_ollama_model()` when the application should pull a missing Ollama
model as an observable operation.

Operation-producing calls return an `OperationTask`, not the final domain
value. Await `task.result()` when the next step depends on that value, or
consume the operation's events when progress is the primary concern.


## Operation-first API

RAVEN represents work with two related objects:

- An `Operation` is the root lifecycle of one user-visible action. It owns the
  status and the ordered event stream.
- An `OperationTask` is one unit of work within that operation. It owns that
  invocation's result and retry metadata.

Nested component calls reuse the parent operation. This preserves one coherent
event stream while assigning each task its own UUID, name, status, and result.

Most high-level methods start their work immediately and return an
`OperationTask`:

```python
task = await raven.list_ollama_models()
models = await task.result()
```

Awaiting the method creates and schedules the task; it does not wait for the
operation to finish. `await task.result()` waits for completion and returns the
method's domain result. If the task failed or was cancelled, `result()` raises
the corresponding error instead of returning an ambiguous value.

An operation moves through these states:

```text
queued -> running -> completed
                  -> failed
                  -> cancelled
```

Applications can inspect and control work through the facade:

```python
operation = await raven.get_operation(task.operation_id)
record = await raven.get_operation_record(task.operation_id)
task_record = await raven.get_operation_task(task.operation_id, task.task_id)

await raven.wait_operation(task.operation_id)
await raven.cancel_operation(task.operation_id)
```

Operations and tasks are also pageable through `list_operations()` and
`list_operation_tasks()`. This lets a library caller or server recover status
without holding the original Python object.

### Cancellation

Cancel the task directly when its handle is available:

```python
await task.cancel()
```

Or cancel its complete operation by ID:

```python
await raven.cancel_operation(task.operation_id)
```

Cancellation is cooperative. RAVEN records a terminal cancellation state and
allows cleanup or durable state transitions already in progress to finish
safely where required.

### User-confirmed retry

Only task types with an explicit retry policy are retryable. RAVEN currently
uses user-confirmed retry for operations such as ingestion, reconstruction,
and session generation rather than automatically repeating arbitrary model or
storage work.

```python
retryable = await raven.list_retryable_tasks()

if retryable:
    failed = retryable[0]
    retry_task = await raven.retry_task(
        failed.operation_id,
        failed.task_id,
    )
    result = await retry_task.result()
```

A retry creates a new operation linked to the failed task. It does not rewrite
the original operation's history.


## Events and streaming

Events are RAVEN's primary observability and streaming contract. Every event is
associated with an operation and may also identify the task that emitted it.

Conceptually, an event contains:

```json
{
  "type": "chat.tool_call",
  "data": {
    "name": "global_embedded_retrieval",
    "step": 1
  },
  "operation_id": "7e6d796c-6711-4fc7-a967-78738ecac96a",
  "task_id": "b3e7752e-64db-4924-a171-4c761f1594c0",
  "task_name": "session.generate_response",
  "event_id": 8,
  "timestamp": "2026-09-20T10:30:00+00:00",
  "is_final": false
}
```

`event_id` increases monotonically within an operation. `task_id` and
`task_name` correlate events from nested work without splitting the operation
into disconnected streams.

Consume events from a task while it runs:

```python
task = await raven.pull_ollama_model("qwen3:8b")

async for event in task.events():
    print(event.event_id, event.type.value, event.data)

await task.result()
```

Important event families include:

- `chat.thinking_delta`, `chat.tool_call`, `chat.tool_result`,
  `chat.response_delta`, and `chat.completed`;
- ingestion start, progress, commit, cleanup, and completion events;
- retrieval strategy events;
- `reconstruction.file` and reconstruction completion events;
- context compaction and conversation turn-commit events;
- model connection, pull, load, preload, unload, and failure events;
- operation and task terminal events.

### Replay after disconnection

Events are persisted, not limited to the lifetime of a subscriber. Store the
last successfully processed event ID and resume after it:

```python
last_event_id = 12

async for event in raven.operation_events(
    operation_id,
    after_event_id=last_event_id,
):
    last_event_id = event.event_id or last_event_id
    print(event)
```

The same cursor model powers the FastAPI SSE endpoint and its
`Last-Event-ID` reconnection behavior. A consumer receives retained events
after its cursor, then waits asynchronously for new events until the operation
reaches a terminal state.

Use `read_operation_events()` when a finite snapshot is more appropriate than
a live iterator.


## Model providers

RAVEN separates model configuration from model implementation through three
public types:

- `ModelSpec` describes a model without constructing its adapter.
- `ModelRole` distinguishes the generation model from the embedding model.
- `Provider` routes each specification to Ollama or LiteLLM.

`ModelSpec` contains:

| Field | Meaning |
| --- | --- |
| `provider` | `"ollama"` for local Ollama, or a LiteLLM provider name for cloud models. |
| `model` | The provider-specific model name. |
| `role` | `ModelRole.LLM` or `ModelRole.EMBEDDING`. |
| `api_key_ref` | Optional environment-variable name containing the provider key. |
| `options` | Non-secret adapter options for LiteLLM-backed models. |

Secrets must not be placed in `options`. RAVEN rejects common credential fields
there and resolves `api_key_ref` from the process environment when the model is
loaded.

### Local models with Ollama

The following configures both roles from a running Ollama server:

```python
import asyncio

from nraven import ModelRole, ModelSpec, Raven


async def main() -> None:
    raven = Raven("./raven-home")
    await raven.start()

    try:
        task = await raven.configure_models(
            ModelSpec(
                provider="ollama",
                model="qwen3:8b",
                role=ModelRole.LLM,
            ),
            ModelSpec(
                provider="ollama",
                model="bge-m3",
                role=ModelRole.EMBEDDING,
            ),
        )
        configured = await task.result()
        print(configured)
    finally:
        await raven.close()


asyncio.run(main())
```

Configuration verifies both adapters and checks the embedding model against
existing knowledge stores before installing the new pair. When no models are
configured, RAVEN installs the pair and preloads its Ollama models. When a pair
is already configured, RAVEN unloads replaced Ollama models, installs the new
pair, and preloads its Ollama models. If pre-installation validation fails, the
previous configured pair remains active.

RAVEN also exposes operation-based Ollama administration for connection checks,
listing, inspection, pulling, and deletion. These methods connect to the
Ollama server; they do not own its process.

### Cloud models through LiteLLM

Set the provider's API key in the environment managed by your application or
deployment platform. `api_key_ref` contains only the variable's name:

```python
import asyncio

from nraven import ModelRole, ModelSpec, Raven


async def main() -> None:
    raven = Raven("./raven-home")
    await raven.start()

    try:
        task = await raven.configure_models(
            ModelSpec(
                provider="gemini",
                model="gemini-2.5-flash",
                role=ModelRole.LLM,
                api_key_ref="GEMINI_API_KEY",
                options={"temperature": 0.2},
            ),
            ModelSpec(
                provider="ollama",
                model="bge-m3",
                role=ModelRole.EMBEDDING,
            ),
        )
        configured = await task.result()
        print(configured)
    finally:
        await raven.close()


asyncio.run(main())
```

The LLM and embedding model do not need to use the same provider. Provider
availability, capabilities, prices, and rate limits remain properties of the
selected external service.

### Switching configured models

Call `Raven.configure_models()` with a new valid pair to switch models. The
replacement is serialized and validated before it becomes active:

```python
task = await raven.configure_models(new_llm_spec, new_embedding_spec)
configured = await task.result()
```

The result is JSON-compatible configuration metadata:

```json
{
  "llm": {
    "provider": "gemini",
    "model": "gemini-2.5-flash",
    "role": "llm"
  },
  "embedding": {
    "provider": "ollama",
    "model": "bge-m3",
    "role": "embedding"
  }
}
```

### Ollama model residency

Configured Ollama models can be explicitly preloaded into or released from
Ollama's runtime memory without changing the configured adapter:

```python
preload = await raven.preload_configured_model(
    ModelRole.LLM,
    keep_alive="10m",
)
print(await preload.result())

unload = await raven.unload_configured_model(ModelRole.LLM)
print(await unload.result())
```

`keep_alive` accepts an Ollama duration string or a finite number of seconds.
A negative value requests indefinite residency. Zero is rejected for preload;
call `unload_configured_model()` when the intended action is unloading.

For a cloud model, these methods complete without attempting local residency
and report that the provider has no local residency to manage.


## Knowledge and ingestion

A knowledge is one persistent collection of related documents and vectors.
`KnowledgeBase` manages the collection of knowledge resources; each `Knowledge`
owns its own metadata, source-section records, and local Qdrant storage.

Create a knowledge before ingesting documents into it:

```python
create_task = await raven.create_knowledge(
    "engineering",
    user_summary="Engineering specifications and design documents.",
)
knowledge = await create_task.result()
```

Knowledge names identify their storage directories and must be unique within
the current user's RAVEN home. The user summary helps global, reasoning-based
retrieval decide which knowledge collections are relevant.

### Supported document formats

RAVEN currently accepts:

| Format | Extensions | Parsing behavior |
| --- | --- | --- |
| Plain text | `.txt` | Read natively as UTF-8 text. |
| Markdown | `.md`, `.markdown` | Read natively while preserving the textual source. |
| PDF | `.pdf` | Parsed through Docling into ordered document elements and page provenance. |
| Word | `.docx` | Parsed through Docling into ordered document elements and available page provenance. |

The parser normalizes text, headings, list items, tables, captions, formulas,
and picture elements. Tables are projected to Markdown where possible. The
current ingestion path indexes the document's textual projection; it does not
send raw image data to a vision model.

PDF and DOCX elements may carry page ranges. Plain-text and Markdown files use
`navigation_type="none"` because they do not have an intrinsic page system.
These provenance fields later allow reconstructed sections to link back to the
appropriate part of the source.

### Ingestion flow

```text
Source validation and snapshot
              |
              v
     Provenance-aware parsing
              |
              v
       Semantic sectioning
              |
              v
   LLM section-metadata extraction
              |
              v
       Chunking and embedding
              |
              v
  Qdrant vectors + SQLite file records
```

The parser first produces ordered elements. The semantic splitter then creates
sections while retaining the contributing element IDs and combined source
range. Each stored section receives an ID shaped as:

```text
<file-id>-<section-index>
```

For example, `a83fd91c20b4-3` identifies the third semantic section of that
file. Section metadata includes a summary, keywords, conditions, definitions,
raw content, source element IDs, and the source range used for navigation.

Start ingestion through the high-level API after configuring the models:

```python
from nraven import EventType


ingestion = await raven.ingest(
    "engineering",
    "./documents/system-design.pdf",
)

async for event in ingestion.events():
    if event.type == EventType.INGESTION_PROGRESS:
        print(event.data)

ingested_file = await ingestion.result()
print(ingested_file)
```

The result contains the knowledge name, file name, generated file ID, section
count, chunk count, and original source path. Progress events identify stages
such as source validation, snapshotting, parsing, sectioning, metadata
extraction, chunking, embedding, vector storage, and metadata commit.

Chunking and semantic-splitting values use the current per-user runtime
settings unless explicitly overridden for the call:

```python
ingestion = await raven.ingest(
    "engineering",
    "./documents/system-design.pdf",
    breakpoint_percentile_threshold=92,
    chunk_size=768,
    chunk_overlap=64,
)
```

### Navigation and management

RAVEN exposes the same source hierarchy to applications that it exposes to the
agent's navigation tools:

```python
knowledges = await raven.list_knowledges()
files = await raven.list_knowledge_files("engineering")
sections = await raven.list_file_sections(
    "engineering",
    "system-design.pdf",
)
section = await raven.get_knowledge_section(
    "engineering",
    sections[0]["section_id"],
)
```

The section lookup returns metadata together with `raw_content`, so an
application can inspect a source directly without performing semantic
retrieval. Pageable variants are available for knowledge, file, and section
listings.

File deletion and knowledge deletion are operation-based:

```python
delete_file = await raven.delete_knowledge_file(
    "engineering",
    ingested_file["file_id"],
)
await delete_file.result()
```

### Ingestion safety and retry

RAVEN snapshots the submitted source before parsing so the file cannot silently
change underneath a running ingestion. Concurrent ingestion of the same file
is rejected, and an already committed file is not duplicated during a retry.

Vector writes happen before the SQLite metadata commit. If the metadata commit
fails or cancellation occurs at that boundary, RAVEN removes the uncommitted
vectors. Startup and graceful-shutdown reconciliation also remove orphaned
vectors left by a hard process termination.

Ingestion tasks persist the information required for user-confirmed retry. A
server upload remains in its private retry area only for the configured
retention window; after that window the source must be uploaded again.


## Retrieval

RAVEN provides four retrieval strategies. Each strategy has a local variant
for one named knowledge and a global variant for searching across the current
user's knowledge base.

| Strategy | Best suited for | Relative cost |
| --- | --- | --- |
| Embedded | Focused scientific, factual, or numeric questions where semantic precision matters most. | Lowest |
| Hierarchical | Broader context, narrative structure, and relationships that benefit from LLM metadata scoring. | High |
| Vector-conditioned | Vector narrowing followed by hierarchical scoring; a practical compromise for contextual questions. | Medium to high |
| Agreement | Comparing embedded and hierarchical results when an expensive cross-check is justified. | Highest |

The available mode strings are:

```text
local_embedded
local_hierarchical
local_vector_conditioned
local_agreement

global_embedded
global_hierarchical
global_vector_conditioned
global_agreement
```

Run a strategy directly through `Raven.retrieve()`:

```python
retrieval = await raven.retrieve(
    "local_embedded",
    "What operating temperature does the controller require?",
    knowledge_name="engineering",
    top_k=3,
)
sections = await retrieval.result()
```

Local modes require `knowledge_name`. Global modes select from all available
knowledge collections and do not require a local scope.

Except for agreement retrieval, every strategy returns the same bounded list:

```json
[
  {
    "knowledge_name": "engineering",
    "file_name": "system-design.pdf",
    "section_id": "a83fd91c20b4-3",
    "raw_content": "..."
  }
]
```

This is deliberately the complete model-facing retrieval contract. Internal
scores, chunks, and vector identifiers do not leak into the result.

### Agreement retrieval

Agreement retrieval preserves the relationship between its two source
strategies:

```json
{
  "agreement_type": "Weak Agreement",
  "retrieved_content": {
    "embedded_retrieval": [
      {
        "knowledge_name": "engineering",
        "file_name": "system-design.pdf",
        "section_id": "a83fd91c20b4-3",
        "raw_content": "..."
      }
    ],
    "hierarchical_retrieval": [
      {
        "knowledge_name": "engineering",
        "file_name": "requirements.pdf",
        "section_id": "fd05a621bd91-2",
        "raw_content": "..."
      }
    ]
  }
}
```

When both strategies return identical results, `agreement_type` is
`"Strong Agreement"` and `retrieved_content` is a single section list. Weak
agreement and disagreement retain the two lists so the model can reason about
their differences. Reconstruction accepts both shapes.

`Raven.retrieve()` requires an explicit strategy. Automatic selection belongs
to an agent session: pass `retrieval_mode="auto"` or omit the argument and let
the model choose among the tools permitted for that conversation.


## Conversations, sessions, and memory

A conversation and a session are intentionally different resources:

- A `Conversation` is persistent. It owns metadata, canonical messages,
  committed turns, compacted model context, vector memory, and preferences.
- A `Session` is temporary. It connects one conversation to the agent harness
  so the caller can generate responses.

A session is never restored from disk. To continue an existing conversation,
load the conversation and create a new session around it.

### Global and local conversations

Create a global conversation by omitting `knowledge_name`:

```python
create_task = await raven.create_conversation()
conversation = await create_task.result()
```

Create a local conversation by binding it to an existing knowledge:

```python
create_task = await raven.create_conversation("engineering")
conversation = await create_task.result()
```

The conversation derives its type from that binding:

```text
knowledge_name is None   -> global conversation
knowledge_name is set    -> local conversation
```

The stable metadata representation includes `conversation_id`, `type`,
`knowledge_name`, `title`, `is_titled`, `pinned`, and `created_at`.

### Running a turn

```python
from nraven import EventType


session = raven.session(conversation)
await session.start()

try:
    run = await session.generate_response(
        "What does the design specify for thermal protection?",
        retrieval_mode="auto",
    )

    async for event in run.stream:
        if event.type == EventType.CHAT_THINKING_DELTA:
            print(event.data.get("delta", ""), end="")
        elif event.type == EventType.CHAT_TOOL_CALL:
            print("Tool:", event.data.get("name"))
        elif event.type == EventType.CHAT_TOOL_RESULT:
            print("Tool result:", event.data.get("ui_summary"))
        elif event.type == EventType.RECONSTRUCTION_FILE:
            print("Source:", event.data.get("file_name"))
        elif event.type == EventType.CHAT_RESPONSE_DELTA:
            print(event.data.get("delta", ""), end="")

    result = await run.collect()
    print(result.response)
finally:
    await session.close()
```

The event stream is authoritative. `collect()` replays those events into an
`AgentRunResult` containing the accumulated thinking text, response, tool
calls, evidence references, reconstructed sources, and iteration-limit state.

Each run also has a stable `turn_id`. A committed turn contains the user
message, assistant tool-call messages, tool-result messages, and final
assistant response. Internal thinking is streamed for the caller but is not
stored as conversation history.

The first completed user turn triggers a separate title completion when the
conversation is still untitled. That title operation does not enter the chat
history, and later turns do not regenerate it. Applications may update the
title or pinned state explicitly.

### Turn serialization

Multiple temporary sessions may reference the same conversation, which is
useful when the same account has several browser tabs or application windows.
Only one turn may run against a conversation at a time. A competing turn is
rejected instead of racing message, memory, and preference mutations.

Turns in different conversations can run concurrently.

Turn commits are atomic and idempotent. If retry encounters an already
committed `turn_id`, RAVEN verifies the query, reconciles memory indexing, and
reuses the stored result rather than generating and storing a duplicate turn.

### Context and vector memory

RAVEN maintains two complementary memory views:

- **Compacted context** uses LlamaIndex's summary memory to keep recent
  messages and a summary of older history within the configured token limit.
  This is the context passed to the model.
- **Vector memory** indexes canonical user messages and final assistant
  responses for semantic recall when the agent needs details from earlier
  discussion. Internal tool traces are not added to this semantic index.

The complete canonical history remains available to the application even when
the model-facing context has been compacted:

```python
messages = await raven.get_conversation_messages(
    conversation.conversation_id,
)
```

Compaction emits started, completed, and failed events so a UI can explain a
delay instead of appearing stalled.

### Conversation preferences

Preferences are local to one conversation. Each preference has a stable UUID
and its text:

```json
{
  "preference_id": "90297ef3-5336-4d09-a155-edf21408e242",
  "text": "Use concise answers unless I ask for detail."
}
```

The agent can list, save, and remove preferences when the user explicitly asks
it to do so. Removal uses `preference_id`, avoiding fragile exact-text
matching. Preference changes invalidate the conversation's cached prompt and
apply on the next agent run.

Applications can manage the same records directly:

```python
save_task = await raven.save_conversation_preference(
    conversation.conversation_id,
    "Use concise answers unless I ask for detail.",
)
preference = await save_task.result()

remove_task = await raven.remove_conversation_preference(
    conversation.conversation_id,
    preference["preference_id"],
)
await remove_task.result()
```


## Agent harness

The agent harness owns model behavior; it does not own conversation storage.
For each run it creates a fresh LlamaIndex `FunctionAgent`, builds the current
conversation policy and prompt, exposes only permitted tools, translates
workflow output into RAVEN events, tracks evidence, reconstructs sources, and
normalizes failures.

RAVEN does not classify the user's query with a separate brittle intent
classifier. The model decides whether it should:

- answer directly;
- search conversation memory;
- navigate knowledge names, files, and sections;
- retrieve relevant sections;
- inspect a specific section;
- modify an explicit conversation preference;
- or use several tools sequentially.

Tool calls are sequential by default. The system prompt tells the model to use
the smallest sufficient tool sequence and not to call additional retrieval or
navigation tools merely to reconfirm evidence it already has.

### Tool exposure and retrieval constraints

Tool availability is enforced by construction, not merely requested in the
system prompt:

| Conversation/run | Retrieval tools exposed |
| --- | --- |
| Local conversation, automatic mode | All four local retrieval tools. |
| Global conversation, automatic mode | All local and global retrieval tools. Local tools require a `knowledge_name`. |
| Specific retrieval mode | Only that retrieval tool, if valid for the conversation scope. |

Memory, preference, and permitted navigation tools remain available when a
specific retrieval strategy is selected. A local conversation's navigation is
automatically bound to its knowledge. Global navigation requires explicit
knowledge names where appropriate.

The non-retrieval tools are:

- `list_knowledges`
- `list_files`
- `list_sections`
- `get_section`
- `search_memory`
- `list_preferences`
- `save_preference`
- `remove_preference`

An invalid retrieval mode or a global-only mode requested for a local
conversation is rejected before model execution.

### Tool results and recoverable errors

Inside the agent, every tool result separates model-facing content from bounded
UI metadata:

```json
{
  "ok": true,
  "result": [
    {
      "knowledge_name": "engineering",
      "file_name": "system-design.pdf",
      "section_id": "a83fd91c20b4-3",
      "raw_content": "..."
    }
  ],
  "ui_summary": {
    "kind": "retrieval",
    "section_count": 2,
    "sections": [
      {
        "knowledge_name": "engineering",
        "file_name": "system-design.pdf",
        "section_id": "a83fd91c20b4-3"
      }
    ]
  },
  "evidence": [
    {
      "knowledge_name": "engineering",
      "file_name": "system-design.pdf",
      "section_id": "a83fd91c20b4-3"
    }
  ]
}
```

The model-facing result can contain the complete context needed to answer. The
bounded `ui_summary` gives the frontend safe metadata for rendering. The
published `chat.tool_result` event contains `ok`, `ui_summary`, evidence, and
error information when applicable; it deliberately omits the full model-facing
`result` so raw sections are not duplicated in the event stream.

Recoverable mistakes—such as an invalid knowledge name, file name, section ID,
or preference ID—are returned to the model as `ok=false` with an error and a
concrete `next_action`. This lets the model correct its arguments and retry.
Cancellation, infrastructure failures, and unrecoverable persistence failures
terminate the operation.

### Evidence and completion behavior

Successful retrieval and direct section inspection produce evidence references
containing only `knowledge_name`, `file_name`, and `section_id`. The harness
deduplicates these references and uses them for source reconstruction.

When the agent has attempted to use knowledge tools but obtains no valid
evidence, RAVEN does not accept an unsupported knowledge answer. It emits a safe
insufficient-evidence response instead. Conversation memory is not treated as
evidence for a knowledge-base fact.

The harness enforces a maximum number of agent iterations. If the limit is
reached, it records that state and asks the agent engine for an early final
response. Failure to produce that response becomes an observable failed
operation rather than an incomplete success.


## Source reconstruction

Source reconstruction is RAVEN's transparency layer. It takes retrieval
evidence, groups section IDs by knowledge and file, reloads the complete stored
section sequence, and marks the sections used by the model.

A reconstructed file has this shape:

```json
{
  "knowledge_name": "engineering",
  "file_name": "system-design.pdf",
  "navigation_type": "page",
  "sections": [
    {
      "section_id": "a83fd91c20b4-1",
      "highlighted": false,
      "raw_content": "...",
      "source_range": [1, 2]
    },
    {
      "section_id": "a83fd91c20b4-3",
      "highlighted": true,
      "raw_content": "...",
      "source_range": [2, 2]
    }
  ]
}
```

For page-based documents, `[12, 12]` means the section belongs to page 12 and
`[12, 14]` means it spans pages 12 through 14. Text and Markdown sources use
`navigation_type="none"` and may have `source_range=null`.

Reconstruction preserves semantic sections and source navigation provenance;
it is not intended to reproduce the original file's pixel-perfect layout.

### Automatic reconstruction

During an agent turn, the harness automatically reconstructs files whenever a
successful tool result provides knowledge evidence. Each reconstructed file is
published as `reconstruction.file`, and `run.collect()` includes all files in
`reconstructed_sources`.

### Direct reconstruction

Any compatible retrieval result can be reconstructed independently of the
agent:

```python
retrieval = await raven.retrieve(
    "global_embedded",
    "What does the documentation say about thermal protection?",
)
retrieved_sections = await retrieval.result()

reconstruction = await raven.reconstruct(retrieved_sections)
files = await reconstruction.result()
```

The method accepts the common section-list contract and the agreement-retrieval
contract.

### Reconstruction from a persisted turn

Tool results are stored with their conversation turn, so reconstruction can be
requested again later without repeating retrieval:

```python
reconstruction = await raven.reconstruct_from_turn(
    conversation.conversation_id,
    run.turn_id,
)
files = await reconstruction.result()
```

If the selected turn contains no successful knowledge evidence—for example, a
casual conversation with no tool calls—the method returns an empty list.


## FastAPI server quick start

Install the server dependencies and start RAVEN with an explicit storage home:

```bash
pip install "noomexai-raven[server]"
nraven serve --home /path/to/raven-home
```

`--home` is mandatory. On its first start, the server creates the default
system configuration at:

```text
<raven-home>/system_settings/system_settings.json
```

Pass `--system-settings /path/to/system_settings.json` to use another settings
file instead. Host, port, and log level come from that immutable
`SystemConfig`; the defaults are `127.0.0.1`, `8765`, and `info`.

The CLI starts one Uvicorn worker. RAVEN's embedded Qdrant databases and local
SQLite stores are intentionally owned by one server process, so increasing the
Uvicorn worker count is not supported.

At launch, the server:

- obtains an exclusive lock for the supplied RAVEN home;
- creates a random bearer token;
- writes the token to `<raven-home>/temp/server_auth/bearer_token` for the
  owning application or deployment host;
- starts the user-runtime registry and recovery services;
- removes the token and releases the home lock on normal shutdown.

A second server cannot own the same home concurrently. Treat the bearer-token
file like a password and do not expose it through a public web root.

The generated API contract is available at:

- `http://127.0.0.1:8765/docs`
- `http://127.0.0.1:8765/openapi.json`

Those routes, along with liveness and readiness probes, are intentionally
available without the bearer token. Application routes require it.


## Using RAVEN from a web or desktop UI

The HTTP interface follows the same operation-first design as the Python API.
A normal UI flow is:

1. Start or connect to the RAVEN server.
2. Obtain the launch bearer token through the trusted application or host
   layer.
3. Configure the LLM and embedding-model pair.
4. Create a knowledge database and upload documents.
5. Consume the returned operation's SSE stream until ingestion finishes.
6. Create a global or local conversation.
7. Submit a turn and render its typed events as they arrive.
8. Reconnect with `Last-Event-ID` if the connection drops.
9. Cancel an active operation or explicitly retry an eligible failed task when
   needed.

### Desktop applications

The default server configuration uses RAVEN's persisted default user UUID, so
a single-user desktop application can omit `X-Raven-User-ID`. The native
application process reads the launch token locally and attaches it to requests
made by, or proxied for, its UI. A browser view should not independently read
credentials from the filesystem.

A desktop request therefore normally contains:

```http
POST /api/v1/conversations HTTP/1.1
Host: 127.0.0.1:8765
Authorization: Bearer <launch-token>
Content-Type: application/json

{"knowledge_name": null}
```

### Hosted applications

In a hosted deployment, RAVEN is an internal backend behind the host's own
application server. The host authenticates the end user, authorizes access to
an internal UUID, and forwards that UUID in `X-Raven-User-ID`. Set
`require_user_id_header` to `true` in `SystemConfig` so requests without that
context are rejected.

The host-to-RAVEN request contains both credentials:

```http
POST /api/v1/conversations HTTP/1.1
Host: raven.internal:8765
Authorization: Bearer <launch-token>
X-Raven-User-ID: 4b9bd24b-61de-42ed-9176-d2db91b66702
Content-Type: application/json

{"knowledge_name": "engineering"}
```

This example assumes `raven.internal` has been added to `allowed_hosts` in the
server's `SystemConfig`.

RAVEN validates the UUID and isolates that user's files, databases,
operations, and settings beneath a separate directory. It does not authenticate
the public account or decide which UUID that account may use; those are host
application responsibilities.

### Streaming operation events

Native browser `EventSource` cannot attach the required `Authorization`
header. Use `fetch()` with an SSE parser, or proxy the stream through the host
application. This compact TypeScript example preserves the last successfully
received event ID for reconnection:

```typescript
type RavenEvent = {
  event_id: number;
  operation_id: string;
  task_id: string | null;
  task_name: string | null;
  type: string;
  timestamp: string;
  data: Record<string, unknown>;
  is_final: boolean;
};

async function streamOperation(
  eventsUrl: string,
  token: string,
  onEvent: (event: RavenEvent) => void,
  options: {
    userId?: string;
    lastEventId?: number;
    signal?: AbortSignal;
  } = {},
): Promise<number | undefined> {
  const headers: Record<string, string> = {
    Authorization: `Bearer ${token}`,
  };
  if (options.userId) headers["X-Raven-User-ID"] = options.userId;
  if (options.lastEventId !== undefined) {
    headers["Last-Event-ID"] = String(options.lastEventId);
  }

  const response = await fetch(eventsUrl, {
    headers,
    signal: options.signal,
  });
  if (!response.ok || !response.body) {
    throw new Error(`SSE request failed: ${response.status}`);
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let cursor = options.lastEventId;

  while (true) {
    const {value, done} = await reader.read();
    buffer += decoder.decode(value, {stream: !done}).replaceAll("\r\n", "\n");
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";

    for (const frame of frames) {
      if (!frame || frame.startsWith(":")) continue;
      const data = frame
        .split("\n")
        .filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart())
        .join("\n");
      if (!data) continue;

      const event = JSON.parse(data) as RavenEvent;
      cursor = event.event_id;
      onEvent(event);
    }

    if (done) return cursor;
  }
}
```

Persist `cursor` only after the UI has successfully handled the event. On
reconnect, send that value as `Last-Event-ID`; RAVEN replays retained events
after it and then waits for new events. SSE heartbeat comments keep idle
connections alive. The stream closes after a final event. If retention cleanup
has created a gap before the supplied cursor, the API returns `410 Gone`
rather than silently skipping events. A fully expired operation is no longer a
resource and returns `404 Not Found`.

Render event types independently: thinking is not response text, tool calls
and bounded tool results describe agent work, `reconstruction.file` supplies
verifiable sources, and response deltas form the user-visible answer.


## HTTP API overview

Every application route uses versioned paths and typed Pydantic contracts.
This table summarizes the implemented surface; use `/docs` or
`/openapi.json` for exact request fields, response schemas, query parameters,
and error bodies.

| Area | Representative routes |
| --- | --- |
| Health | `GET /api/v1/health/live`, `GET /api/v1/health/ready` |
| Runtime | Status, revision-safe settings read/update/reset, and discovery issues under `/api/v1/runtime` |
| Configured models | `GET /api/v1/provider/models/configured`, `POST /api/v1/provider/models/configure`, configured-role preload and unload routes |
| Ollama administration | Status, list, inspect, pull, and delete routes under `/api/v1/providers/ollama` |
| Operations | List and inspect operations/tasks, stream events, cancel operations, discover retryable tasks, and retry a task |
| Knowledge | CRUD, file listing/deletion, section navigation, statistics, multipart upload, and trusted-path ingestion under `/api/v1/knowledges` |
| Conversations | CRUD, canonical messages, turns, turn messages, preferences, turn submission, and reconstruction under `/api/v1/conversations` |

### Authentication and errors

Send the launch token on protected routes:

```http
Authorization: Bearer <launch-token>
```

Include `X-Raven-User-ID: <uuid>` when hosted user context is enabled. Errors
share one machine-readable envelope and request ID:

```json
{
  "error": {
    "code": "knowledge_not_found",
    "message": "Knowledge 'engineering' does not exist.",
    "details": {}
  },
  "request_id": "632d4f96-1e5f-4a4b-b1fd-f6167a742f01"
}
```

The same request ID is returned in `X-Request-ID` for correlation with server
logs.

### Configuring models

Configure one validated LLM and embedding pair:

```http
POST /api/v1/provider/models/configure HTTP/1.1
Authorization: Bearer <launch-token>
Content-Type: application/json

{
  "llm": {
    "provider": "ollama",
    "model": "qwen3:8b",
    "role": "llm",
    "api_key_ref": null,
    "options": {}
  },
  "embedding": {
    "provider": "ollama",
    "model": "bge-m3",
    "role": "embedding",
    "api_key_ref": null,
    "options": {}
  }
}
```

Long-running commands return `202 Accepted` with an operation reference:

```json
{
  "operation_id": "cc9dce11-16ca-42be-b418-18588d03a571",
  "task_id": "8f708231-4a27-40cb-a51e-477339c167ad",
  "status": "running",
  "events_url": "/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571/events",
  "retry_of_operation_id": null,
  "retry_of_task_id": null
}
```

The initial status may be `queued` or `running`. Follow `events_url` for
progress and terminal state, or inspect the task directly.

### Uploading and ingesting a document

Browser and desktop clients should use multipart upload:

```http
POST /api/v1/knowledges/engineering/files HTTP/1.1
Authorization: Bearer <launch-token>
Content-Type: multipart/form-data; boundary=...

--...
Content-Disposition: form-data; name="file"; filename="system-design.pdf"
Content-Type: application/pdf

<file bytes>
--...--
```

The returned operation reference points to ingestion progress and completion
events. Uploaded sources remain in a private user-scoped retry area for the
configured retry window, then cleanup removes them. Trusted local paths use
`POST /api/v1/knowledges/{name}/ingest-path` and are disabled unless the server
explicitly enables them and allowlists their roots.

### Submitting a turn

Create a global conversation with `{"knowledge_name": null}` or a local one
with a knowledge name, then submit a turn:

```http
POST /api/v1/conversations/<conversation-id>/turns HTTP/1.1
Authorization: Bearer <launch-token>
Content-Type: application/json

{
  "user_query": "What does the design say about thermal protection?",
  "retrieval_mode": "auto"
}
```

The `202 Accepted` response adds `conversation_id` and `turn_id` to the
operation reference. Use the operation's SSE endpoint while the turn is
running. The persisted turn and canonical messages can be fetched later from
their conversation routes.


## Configuration

RAVEN separates deployment policy, user-scoped paths, and mutable operation
defaults into three configuration types.

### `SystemConfig`

`SystemConfig` is immutable for the lifetime of a running server. It controls
host, port, logging, allowed origins and hosts, request and upload limits,
operation retention, SSE heartbeat timing, concurrency limits, cache bounds,
trusted-path ingestion, and deployment-wide safety ceilings.

The server uses one shared `SystemConfig` for every user runtime. It is not a
per-user preference object and cannot be changed through the HTTP API.

#### Server loading and changes

Starting the server with only a home directory:

```bash
nraven serve --home /path/to/raven-home
```

loads this file:

```text
<raven-home>/system_settings/system_settings.json
```

If the file does not exist, RAVEN creates it with all default values. To change
system settings, stop the server, edit the file, and restart it. The file is
validated at startup and is not hot-reloaded.

An existing settings file elsewhere can be selected explicitly:

```bash
nraven serve \
  --home /path/to/raven-home \
  --system-settings /path/to/production-system-settings.json
```

An explicit file must already exist. RAVEN reads it in place and does not copy
it into the home directory.

The default generated file has this complete shape:

```json
{
  "schema_version": 1,
  "host": "127.0.0.1",
  "port": 8765,
  "log_level": "info",
  "operation_sync_interval_seconds": 1.0,
  "operation_retention_seconds": 86400.0,
  "upload_retry_retention_seconds": 86400.0,
  "operation_cleanup_interval_seconds": 300.0,
  "operation_cleanup_batch_size": 100,
  "event_replay_page_size": 256,
  "operation_page_size": 50,
  "finished_operation_cache_size": 256,
  "open_knowledge_limit": 16,
  "open_conversation_limit": 64,
  "llm_adapter_cache_size": 8,
  "embedding_adapter_cache_size": 8,
  "prompt_cache_size": 32,
  "sse_heartbeat_interval_seconds": 30.0,
  "max_source_file_bytes": 104857600,
  "max_upload_request_overhead_bytes": 65536,
  "max_request_body_bytes": 1048576,
  "max_query_string_bytes": 8192,
  "max_active_operations_per_user": 8,
  "max_active_operations": 64,
  "max_document_pages": 1000,
  "max_retrieval_top_k": 25,
  "max_agent_iterations": 50,
  "runtime_idle_seconds": 1800.0,
  "max_user_runtimes": 100,
  "cors_origins": ["http://localhost:3000"],
  "allowed_hosts": ["localhost", "127.0.0.1", "[::1]"],
  "require_user_id_header": false,
  "trusted_ingestion_enabled": false,
  "allowed_ingestion_roots": []
}
```

Unknown fields, invalid types, non-finite numbers, and unsupported
`schema_version` values cause startup to fail with a configuration error.

#### Server and request boundary fields

| Field | Default | Accepted value | Purpose |
| --- | ---: | --- | --- |
| `schema_version` | `1` | Exactly the supported schema version | Identifies the system-settings file format. It should not be changed manually. |
| `host` | `"127.0.0.1"` | Non-empty string | Address passed to Uvicorn. Keep the loopback default for desktop use; hosted deployments may bind another interface deliberately. |
| `port` | `8765` | Integer from `1` through `65535` | TCP port used by the FastAPI server. |
| `log_level` | `"info"` | `"critical"`, `"error"`, `"warning"`, `"info"`, or `"debug"` | Uvicorn logging level. |
| `sse_heartbeat_interval_seconds` | `30.0` | Finite number greater than zero | Maximum idle interval before the operation SSE endpoint sends a heartbeat comment. |
| `max_source_file_bytes` | `104857600` (100 MiB) | Positive integer | Maximum accepted source-document size. |
| `max_upload_request_overhead_bytes` | `65536` (64 KiB) | Positive integer | Maximum multipart framing and metadata overhead allowed beyond the uploaded file bytes. |
| `max_request_body_bytes` | `1048576` (1 MiB) | Positive integer | Maximum ordinary non-file request-body size. |
| `max_query_string_bytes` | `8192` (8 KiB) | Positive integer | Maximum encoded query-string size. |
| `cors_origins` | `["http://localhost:3000"]` | List of complete HTTP or HTTPS origins | Browser origins allowed by CORS. Entries may contain a scheme, host, and optional port, but no path, query, or fragment. |
| `allowed_hosts` | `["localhost", "127.0.0.1", "[::1]"]` | Non-empty list of hostnames or IP literals without ports | Host-header allowlist checked before request processing. Add an internal hostname explicitly before using it in hosted deployment. |
| `require_user_id_header` | `false` | Boolean | When true, protected requests must include a valid `X-Raven-User-ID`; when false, a missing header selects the default desktop user. |

#### Operations and event fields

| Field | Default | Accepted value | Purpose |
| --- | ---: | --- | --- |
| `operation_sync_interval_seconds` | `1.0` | Finite number greater than zero | Interval used by the operation manager's periodic durable-store synchronization service. Lifecycle checkpoints are still written immediately where required. |
| `operation_retention_seconds` | `86400.0` (24 hours) | Finite number greater than or equal to zero | Time terminal operations, tasks, and events remain eligible for replay before cleanup. Zero makes completed history immediately eligible. |
| `operation_cleanup_interval_seconds` | `300.0` (5 minutes) | Finite number greater than zero | Interval between background cleanup passes. |
| `operation_cleanup_batch_size` | `100` | Positive integer | Maximum expired operations removed by one cleanup pass. |
| `event_replay_page_size` | `256` | Positive integer | Number of persisted events loaded per page while replaying an operation stream. |
| `operation_page_size` | `50` | Positive integer | Default and maximum page size exposed by operation and resource-list HTTP endpoints. |
| `finished_operation_cache_size` | `256` | Non-negative integer | Maximum completed `Operation` objects retained in the in-memory LRU cache. Zero disables that completed-operation cache. Durable records remain in SQLite until retention cleanup. |
| `max_active_operations_per_user` | `8` | Positive integer no greater than `max_active_operations` | Maximum accepted non-terminal operation tasks for one user. |
| `max_active_operations` | `64` | Positive integer | Maximum accepted non-terminal operation tasks across the server. |

#### Resource and cache fields

| Field | Default | Accepted value | Purpose |
| --- | ---: | --- | --- |
| `open_knowledge_limit` | `16` | Positive integer | Maximum idle/open knowledge resources retained in one user's LRU registry before eligible resources are closed and evicted. |
| `open_conversation_limit` | `64` | Positive integer | Maximum idle/open conversation resources retained in one user's LRU registry. Active resources remain protected. |
| `llm_adapter_cache_size` | `8` | Positive integer | Maximum cached LLM adapters in the provider layer. |
| `embedding_adapter_cache_size` | `8` | Positive integer | Maximum cached embedding adapters in the provider layer. |
| `prompt_cache_size` | `32` | Positive integer | Maximum cached conversation-specific agent prompts. Preference changes invalidate the affected prompt. |
| `runtime_idle_seconds` | `1800.0` (30 minutes) | Finite number greater than zero | Time an unleased user runtime may remain idle before server eviction. Active tasks retain their runtime. |
| `max_user_runtimes` | `100` | Positive integer | Maximum user runtimes that may be resident or initializing simultaneously. Idle runtimes are evicted when possible before capacity is rejected. |

#### Ingestion and runtime ceiling fields

| Field | Default | Accepted value | Purpose |
| --- | ---: | --- | --- |
| `upload_retry_retention_seconds` | `86400.0` (24 hours) | Finite number greater than zero | Time a failed private staged browser upload remains available for user-confirmed ingestion retry. Successful uploads are removed after ingestion. |
| `max_document_pages` | `1000` | Positive integer | Maximum number of pages accepted from a page-based document parser. |
| `max_retrieval_top_k` | `25` | Positive integer | Server-wide ceiling for `RuntimeConfig.retrieval_top_k` and per-call retrieval overrides. |
| `max_agent_iterations` | `50` | Positive integer | Server-wide ceiling for `RuntimeConfig.agent_max_iterations` and per-session overrides. |
| `trusted_ingestion_enabled` | `false` | Boolean | Enables the trusted local-path ingestion route for controlled host environments. Browser clients should normally use multipart upload. |
| `allowed_ingestion_roots` | `[]` | List of filesystem paths | Roots beneath which trusted local-path ingestion is permitted. Paths are expanded and resolved. At least one root is required when trusted ingestion is enabled. |

`max_active_operations_per_user` cannot exceed `max_active_operations`.
Trusted-path ingestion cannot be enabled with an empty
`allowed_ingestion_roots` list.

#### Library usage

Library applications can construct and pass the immutable configuration
directly:

```python
from nraven import Raven, SystemConfig


config = SystemConfig(
    max_active_operations_per_user=4,
    max_active_operations=32,
    max_source_file_bytes=50 * 1024 * 1024,
)
raven = Raven("./raven-home", system_config=config)
```

When `system_config` is omitted, `Raven` uses an in-memory `SystemConfig()` with
the defaults above. Unlike the server CLI, the `Raven` constructor does not
automatically load or create `system_settings.json`.

Library applications that want file-backed system settings can manage them
explicitly:

```python
from nraven import Raven, SystemConfig


config = SystemConfig(
    max_active_operations_per_user=4,
    max_active_operations=32,
)
config.save("./system_settings.json")

loaded_config = SystemConfig.load("./system_settings.json")
raven = Raven("./raven-home", system_config=loaded_config)
```

`SystemConfig.save()` uses an atomic temporary-file replacement and requests a
filesystem synchronization before returning. Because the dataclass is frozen,
changing a setting means creating or loading a new `SystemConfig`, then
constructing a new `Raven` or restarting the server with it.

### `PathConfig`

`PathConfig` is immutable for one RAVEN/user runtime. It validates the user ID
as a UUID and derives all user-specific storage paths from `raven_home` and
`user_id`:

```text
<raven-home>/
├── system_settings/
│   └── system_settings.json
├── temp/
│   └── server_auth/
│       ├── server.lock
│       └── bearer_token
└── <user-uuid>/
    ├── data/
    │   ├── knowledge_base/
    │   └── conversations/
    ├── operations/
    │   └── operations.sqlite3
    ├── runtime_settings/
    │   └── runtime_settings.json
    └── temp/
        └── uploads/
```

The server lazily creates one `Raven` runtime per validated UUID and evicts
idle runtimes according to `SystemConfig`. Active operation tasks retain their
runtime until they reach a terminal state.

### `RuntimeConfig`

`RuntimeConfig` is a per-user immutable snapshot of mutable defaults. It stores
semantic-splitting parameters, extraction retries, chunk settings, retrieval
`top_k`, agent iteration limits, and conversation-memory limits in
`runtime_settings.json`.

The complete settings document contains these fields:

| Field | Default | Accepted value | Purpose |
| --- | ---: | --- | --- |
| `schema_version` | `1` | Managed by RAVEN | Identifies the persisted runtime-settings schema. It is returned to callers but cannot be updated directly. |
| `revision` | `0` initially | Managed by RAVEN | Increases after every successful update or reset and provides optimistic concurrency control. It cannot be updated directly. |
| `breakpoint_percentile_threshold` | `95` | Integer from `1` through `100` | Percentile used to identify semantic distances that become section boundaries. Lower values generally produce more boundaries. |
| `buffer_size` | `1` | Positive integer | Number of neighboring semantic units included on each side when constructing embedding windows for boundary detection. |
| `max_extraction_retries` | `3` | Positive integer | Maximum attempts for LLM-based structured section-metadata extraction during ingestion. |
| `chunk_size` | `512` | Positive integer | Target chunk size supplied to LlamaIndex's sentence-aware splitter before embedding and vector storage. |
| `chunk_overlap` | `50` | Non-negative integer smaller than `chunk_size` | Overlap between adjacent embedding chunks. |
| `retrieval_top_k` | `3` | Positive integer no greater than `SystemConfig.max_retrieval_top_k` (`25` by default) | Default number of results requested by retrieval pipelines and agent retrieval tools. |
| `agent_max_iterations` | `10` | Positive integer no greater than `SystemConfig.max_agent_iterations` (`50` by default) | Maximum number of agent workflow iterations allowed for one turn. |
| `memory_token_limit` | `4000` | Positive integer | Token budget for the model-facing compacted conversation context. |
| `memory_top_k` | `5` | Positive integer | Number of semantically similar canonical messages returned by conversation-memory search. |

Runtime settings are defaults, not global mutable variables inside running
work. Each ingestion, retrieval, or session run captures its effective values
when it starts. Updating settings affects subsequent work; it does not alter an
operation that is already running. Explicit per-call arguments can override
the persisted defaults without changing them.

#### Python API

Update it atomically through the Python facade:

```python
current = raven.get_runtime_settings()

task = await raven.update_runtime_settings(
    {
        "retrieval_top_k": 5,
        "agent_max_iterations": 12,
    },
    expected_revision=current["revision"],
)
updated = await task.result()
```

Each successful change creates a new revision. Supplying
`expected_revision` prevents one caller from silently overwriting a concurrent
update. `Raven.reset_runtime_settings()` restores built-in defaults as another
revision.

#### HTTP API

The same revision-safe contract is available over HTTP:

```http
GET   /api/v1/runtime/settings
PATCH /api/v1/runtime/settings
POST  /api/v1/runtime/settings/reset
```

All three endpoints require the normal bearer authorization header. In hosted
mode they also use `X-Raven-User-ID`, so each user reads and changes only their
own runtime settings.

##### Read settings

```http
GET /api/v1/runtime/settings HTTP/1.1
Authorization: Bearer <launch-token>
X-Raven-User-ID: <user-uuid>
```

The GET endpoint has no request body. `X-Raven-User-ID` is required only when
the server has `require_user_id_header=true`.

Example response:

```json
{
  "schema_version": 1,
  "revision": 0,
  "breakpoint_percentile_threshold": 95,
  "buffer_size": 1,
  "max_extraction_retries": 3,
  "chunk_size": 512,
  "chunk_overlap": 50,
  "retrieval_top_k": 3,
  "agent_max_iterations": 10,
  "memory_token_limit": 4000,
  "memory_top_k": 5
}
```

##### Update settings

```http
PATCH /api/v1/runtime/settings HTTP/1.1
Authorization: Bearer <launch-token>
Content-Type: application/json
```

The PATCH body requires `expected_revision` and at least one configurable
field. Include only the fields that should change:

```json
{
  "expected_revision": 0,
  "retrieval_top_k": 5,
  "agent_max_iterations": 12
}
```

The complete request-body shape is shown below. Apart from
`expected_revision`, every field is optional:

```json
{
  "expected_revision": 0,
  "breakpoint_percentile_threshold": 90,
  "buffer_size": 2,
  "max_extraction_retries": 4,
  "chunk_size": 768,
  "chunk_overlap": 75,
  "retrieval_top_k": 5,
  "agent_max_iterations": 12,
  "memory_token_limit": 8000,
  "memory_top_k": 8
}
```

Every configurable field is optional independently, so these are also valid
partial updates:

```json
{
  "expected_revision": 4,
  "chunk_size": 768,
  "chunk_overlap": 75
}
```

```json
{
  "expected_revision": 5,
  "memory_token_limit": 8000
}
```

Do not include `schema_version` or `revision` in the body, and omit fields that
should remain unchanged. Unknown fields and an update containing no setting
are rejected with `422 Unprocessable Entity`. Setting values must be JSON
integers rather than strings or booleans. RAVEN validates the merged result, so
`chunk_overlap` must remain smaller than `chunk_size`, and retrieval or agent
limits must remain below their `SystemConfig` ceilings.

On success, PATCH returns the complete settings document with `revision`
incremented by one. If the current revision differs from
`expected_revision`, RAVEN returns `409 Conflict`; read the current settings
again before deciding whether to submit a new update.

##### Reset settings

```http
POST /api/v1/runtime/settings/reset HTTP/1.1
Authorization: Bearer <launch-token>
Content-Type: application/json

{
  "expected_revision": 6
}
```

Reset accepts no setting fields. It restores every configurable field to the
defaults listed above, increments the current revision, persists the result,
and returns the complete updated settings document. It never changes the
revision back to zero. A stale `expected_revision` returns `409 Conflict` just
as it does for PATCH.


## Persistence and recovery

RAVEN stores each user's state under:

```text
<raven-home>/<user-uuid>/
├── data/
│   ├── knowledge_base/
│   │   └── <knowledge-name>/
│   │       ├── metadata.json
│   │       ├── file.sqlite3
│   │       └── qdrant/
│   └── conversations/
│       └── <conversation-id>/
│           ├── metadata.json
│           ├── messages.sqlite3
│           └── qdrant/
├── operations/
│   └── operations.sqlite3
├── runtime_settings/
│   └── runtime_settings.json
└── temp/
    └── uploads/
```

This persists:

- knowledge metadata, parsed source sections, and Qdrant vectors;
- conversation metadata, original messages, tool messages, turns, compacted
  context, vector memory, and preferences;
- operation records, task records, statuses, retry links, and events;
- per-user runtime settings.

### Recovery after interruption

On startup, RAVEN inspects persisted operations. If a previous process stopped
while an operation was non-terminal, the worker no longer exists, so RAVEN
marks the operation as interrupted instead of pretending that it completed or
leaving it permanently running.

Eligible failed tasks remain discoverable for explicit retry. A retry starts a
new linked operation and uses the task's persisted retry input; RAVEN does not
blindly resume arbitrary Python workers or automatically repeat external model
calls.

Ingestion uses pending records and reconciliation to remove or complete
partially committed storage changes. Conversation turn IDs and idempotent turn
commits prevent the same completed turn from being written twice during retry
or recovery.

Model adapters, active network clients, and Ollama VRAM residency are runtime
state. They are rebuilt or reconfigured after process restart rather than being
treated as durable application state.


## Security and deployment responsibilities

RAVEN provides the security boundary needed between its API and a trusted
desktop application or deployment host. It does not attempt to replace a
public application's identity system.

### What RAVEN enforces

- A random per-launch bearer token protects application routes.
- Bearer-token comparison is constant-time, and credential values are not
  returned in API responses or events.
- Optional `X-Raven-User-ID` context must be a valid UUID.
- Each UUID receives a separate storage root and independently owned runtime.
- Request hosts and browser origins must match explicit `SystemConfig`
  allowlists.
- Request bodies, query strings, source files, multipart overhead, active
  operations, and list pages are bounded.
- Uploaded filenames are validated and staged inside controlled user-specific
  directories.
- Trusted-path ingestion is disabled by default and, when enabled, is limited
  to configured roots.
- Model specifications refer to API keys by environment-variable name;
  applications should never place secret values in `options`.
- Errors use stable codes and sanitized messages, while an `X-Request-ID`
  allows operators to correlate failures with private server logs.

Health and OpenAPI routes do not require the bearer token, but they remain
subject to host validation. All other routes require the launch token. The
default host and CORS configuration is loopback-only and should remain narrow
unless a deployment has a deliberate network boundary.

### What the host application owns

For a desktop product, the native application owns the server process, reads
the local launch token, and decides how its UI reaches RAVEN.

For a hosted product, the host application must:

- authenticate end users;
- authorize each account to one internal user UUID;
- inject that UUID and the private RAVEN bearer token into proxied requests;
- decide which model-administration and destructive routes each user may
  invoke;
- keep provider API keys in its environment or secret manager;
- terminate TLS and configure its reverse proxy safely;
- start, stop, monitor, and resource-limit Ollama when local models are used;
- back up and protect the RAVEN home at the filesystem level.

Do not expose the bearer-token file, RAVEN home, or an unrestricted internal
RAVEN port to untrusted clients. In a hosted deployment, browser requests
should normally go through the host backend rather than carrying RAVEN's
process-wide launch token directly.

Ollama model residency is process-global. Changing or unloading a configured
Ollama model can affect shared host resources, so a multi-user host should
restrict model-administration routes and choose an explicit residency policy.


## Advanced component API

`Raven` is the recommended composition root, but the package exports its major
components for applications that need lower-level integration or focused
testing:

| Area | Public components |
| --- | --- |
| Operations and events | `OperationManager`, `Operation`, `OperationTask`, records, statuses, retry policies, cleanup services, `EventStream`, `Event`, and `EventType` |
| Providers | `Provider`, `ModelSpec`, `ModelRole`, `OllamaManager`, and `LiteLLMManager` |
| Knowledge and conversations | `KnowledgeBase`, `Knowledge`, `ConversationManager`, `Conversation`, and `DiscoveryIssue` |
| Document processing | `DocumentParser`, parsed element/document contracts, `ProvenanceAwareSemanticSplitter`, and semantic unit/section contracts |
| Pipelines | `IngestionPipeline`, all four retrieval pipeline classes, the common `RetrievalPipeline`, and `Reconstructor` |
| Agent execution | `AgentPolicy`, `AgentHarness`, `Session`, and `SessionRun` |
| Configuration | `SystemConfig`, `PathConfig`, and `RuntimeConfig` |

These types preserve the same operation and event contracts used by the
facade. When composing them manually, the application becomes responsible for
their dependency order, lifecycle, shared operation context, and cleanup. Most
applications should begin with `Raven` and move to individual components only
when they need a boundary the facade intentionally does not expose.


## Development and testing

Clone the repository and install the package in editable mode with development
and server dependencies:

```bash
python -m venv .venv
python -m pip install -e ".[dev,server]"
```

Run the fast suite without external model services:

```bash
python -m pytest -m "not integration"
```

Run the complete pytest suite when the required external services and model
assets are available:

```bash
python -m pytest
```

Tests marked `integration` may require a running Ollama server, installed test
models, cloud-provider credentials, or real document assets. Use a dedicated
temporary RAVEN home for destructive, crash-recovery, and subprocess tests; do
not point them at application data.

The repository also contains focused end-to-end scripts for provider,
ingestion, retrieval, reconstruction, session, memory, retry, and hard-crash
behavior. Review a script's model and filesystem requirements before running
it.

Build distributions with:

```bash
python -m pip install build
python -m build
```

Before release, test the generated wheel in fresh environments in both
supported installation forms:

```bash
python -m pip install ./dist/noomexai_raven-0.2.0-py3-none-any.whl
python -m pip install "./dist/noomexai_raven-0.2.0-py3-none-any.whl[server]"
```

Replace `0.2.0` with the version being tested.

The core wheel must cover both Ollama and LiteLLM/cloud provider paths. The
server smoke test must additionally verify CLI startup, bearer authentication,
OpenAPI generation, SSE replay, cancellation, and shutdown.


## Current limitations

- The server uses one Uvicorn worker. Embedded Qdrant and per-user SQLite
  stores are not a distributed multi-process backend.
- Durable operations and events are local to one RAVEN home. Running several
  replicas requires an external coordination and storage design that this
  release does not provide.
- RAVEN does not own the Ollama process. A desktop application or deployment
  host must supervise it and clean up its host-level resources.
- Model specifications and adapter/VRAM state are runtime configuration, not
  durable state. Configure models again after a server restart.
- Supported ingestion formats in this release are `.txt`, `.md`, `.pdf`, and
  `.docx`.
- Parsing and reconstruction preserve semantic content and navigation ranges,
  but do not reproduce the source's exact visual layout or bounding boxes.
- Crash recovery marks abandoned operations as interrupted and supports
  explicit retry for eligible tasks. It does not serialize arbitrary Python
  execution state or resume a model call at the exact interrupted instruction.
- Uploaded sources can be retried only within the configured private retention
  window. After expiry, the user must upload the file again.
- RAVEN validates user UUIDs and isolates their storage, but public account
  authentication, account-to-UUID authorization, TLS, and internet-facing
  policy belong to the host application.
- Cloud model capabilities, rate limits, availability, and pricing are defined
  by the selected provider and LiteLLM adapter.


## License and third-party notices

RAVEN is released under the [MIT License](LICENSE.txt).

The distribution depends on third-party open-source packages under their own
licenses. See [ThirdPartyNotices.txt](ThirdPartyNotices.txt) for the packages,
license identifiers, copyright notices, and license texts included with this
project.
