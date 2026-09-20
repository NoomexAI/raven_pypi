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
