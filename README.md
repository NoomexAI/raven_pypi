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


## Contents

- [Choose how to use RAVEN](#choose-how-to-use-raven)
- [Key features](#key-features)
- [How RAVEN fits together](#how-raven-fits-together)
- [Glossary](#glossary)
- [Requirements](#requirements)
- [Installation](#installation)
- [Library quick start](#library-quick-start)
- [Operation-first API](#operation-first-api)
- [Events and streaming](#events-and-streaming)
- [Errors](#errors)
- [Model providers](#model-providers)
- [Knowledge and ingestion](#knowledge-and-ingestion)
- [Retrieval](#retrieval)
- [Conversations, sessions, and memory](#conversations-sessions-and-memory)
- [Agent harness](#agent-harness)
- [Source reconstruction](#source-reconstruction)
- [FastAPI server quick start](#fastapi-server-quick-start)
- [Using RAVEN from a web or desktop UI](#using-raven-from-a-web-or-desktop-ui)
- [HTTP API overview](#http-api-overview)
- [HTTP request reference](#http-request-reference)
- [Configuration](#configuration)
- [Persistence and recovery](#persistence-and-recovery)
- [Backup, restore, and cleanup](#backup-restore-and-cleanup)
- [Security and deployment responsibilities](#security-and-deployment-responsibilities)
- [Server troubleshooting](#server-troubleshooting)
- [Advanced component API](#advanced-component-api)
- [Development and testing](#development-and-testing)
- [Current limitations](#current-limitations)
- [License and third-party notices](#license-and-third-party-notices)


## Choose how to use RAVEN

RAVEN supports three deployment paths. They share the same backend behavior,
but differ in who creates the `Raven` instance and who supplies user identity.

| You are building | Start here | Who owns `Raven` | User identity |
| --- | --- | --- | --- |
| A Python application | [Library quick start](#library-quick-start) | Your Python process | Pass `user_id` to `Raven`; the stable default is suitable for one local user. |
| A desktop application with a web UI | [FastAPI server quick start](#fastapi-server-quick-start) | The bundled RAVEN server | Normally uses the stable default user; the desktop bridge keeps the launch bearer token private. |
| A hosted multi-user service | [Using RAVEN from a web or desktop UI](#hosted-applications) | RAVEN's runtime registry | Your host authenticates the account and forwards its internal UUID on every request. |

Use the library when Python code is the application boundary. Use the server
when another process, browser UI, or network service needs an HTTP and SSE
contract. The server is not a different backend: it validates HTTP input,
selects the correct user-scoped `Raven` runtime, and serializes the same
operations and events exposed by the library.

If this is your first time using RAVEN, follow this order:

1. Install Ollama 0.34.2 and start its server. RAVEN does not install or
   manage Ollama for you.
2. Install the package and verify the import.
3. Start RAVEN and configure one LLM plus one embedding model.
4. Create a knowledge and ingest a document.
5. Create a conversation and a temporary session.
6. Start a turn, consume its events, and collect its final result.
7. Close the session and RAVEN cleanly.

The detailed sections explain each step, including cancellation, retry,
reconnection, persistence, and failures.


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


## Glossary

| Term | Meaning |
| --- | --- |
| RAVEN home | The application-controlled root directory. User-specific data is stored below it. |
| User runtime | One user-scoped `Raven` instance with isolated paths, settings, operations, knowledges, and conversations. |
| Knowledge | A persistent collection of source files, semantic sections, and vectors. |
| Knowledge base | The registry and lifecycle manager for all knowledges belonging to one user. |
| Conversation | Persistent metadata, canonical messages, turns, compacted context, vector memory, and preferences. |
| Session | A temporary interaction object connecting one conversation to the agent. It is never persisted. |
| Agent harness | The component that builds model behavior, exposes permitted tools, runs the agent loop, and translates output into events. |
| Operation | The status, event stream, and durable history of one user-visible action. |
| Operation task | One invocation inside an operation, with its own result, status, parent relationship, and retry metadata. |
| Event | An ordered, persisted observation emitted while an operation runs. |
| Local retrieval | Retrieval constrained to one explicitly named knowledge. |
| Global retrieval | Retrieval that can select across all knowledges belonging to the current user. |
| Reconstruction | Reloading complete source-section sequences and marking which sections supplied the model's evidence. |


### Runtime lifecycle

The normal library lifecycle is:

```text
construct Raven
      |
      v
await raven.start()
      |
      +--> recover interrupted operation records
      +--> discover persistent knowledge and conversations
      +--> load per-user runtime settings
      |
      v
configure model pair
      |
      v
run knowledge, retrieval, or conversation work
      |
      v
close temporary sessions
      |
      v
await raven.close()
```

`Raven.start()` does not configure models. Model configuration is explicit
because the LLM and embedding model are application choices. `Raven.close()`
cancels active work, flushes operation state, closes open Qdrant and SQLite
resources, and closes provider adapters. It does not stop an external Ollama
server.

`start()` and `close()` are lifecycle methods, not operation-producing methods.
Do not create new work after `close()`.

Runtime inspection is immediate:

```python
print(raven.is_started)
print(raven.models_loaded)
print(raven.runtime_status())
```

`runtime_status()` returns the user ID, lifecycle flags, model-loaded flag,
operation-store health, runtime-settings revision, and the configured LLM and
embedding identities. It never returns API-key values or provider clients.


## Requirements

- Python 3.11 or newer.
- A writable RAVEN home directory supplied by the application.
- For local models, a separately installed and running
  [Ollama 0.34.2](https://ollama.com/download) server.
- For cloud models, the relevant provider API key available through an
  environment variable.
- Enough system memory, accelerator memory, and storage for the models and
  document collections selected by the application.

Installing `noomexai-raven` does not install the Ollama application or binary.
RAVEN connects to Ollama but does not start, stop, update, or supervise the
Ollama process. The desktop application or deployment host is responsible for
installing Ollama 0.34.2 and managing its lifecycle and host-level resources.

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

Verify the installation before configuring external models:

```bash
python -c "import nraven; print(nraven.Raven)"
```

For an Ollama-backed setup, also verify that the external service is reachable
and that the required model names exist:

```bash
ollama list
```

RAVEN can pull a missing Ollama model later through an observable operation,
but the Ollama server itself must already be running. Cloud usage requires the
provider API-key environment variable to be present in the process that runs
RAVEN.


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
            user_summary = "Engineering reference documents",
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

This quick start assumes an empty home. On a later process start, persistent
knowledges and conversations are discovered automatically. Use
`raven.get_knowledge("engineering")`, `raven.list_conversations()`, and
`raven.get_conversation(conversation_id)` instead of recreating resources that
already exist.

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

The states mean:

| Status | Meaning | Can more work be added? | `task.result()` behavior |
| --- | --- | --- | --- |
| `queued` | The operation or task has been durably registered but its worker has not started. | The root task is being established. | Waits. |
| `running` | The root worker is active and may create nested tasks. | Yes, while the operation remains running. | Waits. |
| `completed` | The worker and all child tasks reached a successful terminal state. | No. | Returns the task's native result. |
| `failed` | A worker raised an error and the failure was persisted. | No. | Raises the original `RavenError` or a safe reconstructed error. |
| `cancelled` | Cancellation was requested and terminal cancellation was persisted. | No. | Raises `RavenError` with `code="operation_cancelled"`. |

`Operation.status` describes the complete root action. Each `OperationTask`
also has a status because nested work can finish before the root operation.
The operation cannot complete successfully until its active child tasks have
joined.

### Root, child, and sibling tasks

The first task run in a new operation is its root task. If that worker calls
another operation-aware component and passes the same `Operation`, the new
invocation becomes a child task. Two tasks started by the same parent are
siblings. RAVEN records `parent_task_id` for this relationship.

```text
operation: session.generate_response
|
+-- root task: session.generate_response
    |
    +-- child: conversation.get_context
    +-- child: retrieval.embedded.global
    +-- child: reconstruction.reconstruct
    +-- child: conversation.append_turn
```

This is why a session turn does not create disconnected retrieval and
reconstruction operations. The UI sees one ordered stream for the turn while
the library can still inspect or await each task independently.

The IDs serve different purposes:

| Identifier | Stability and scope | Use it for |
| --- | --- | --- |
| `operation_id` | One UUID for the complete user-visible action. | Status, event replay, cancellation, and grouping all nested work. |
| `task_id` | One UUID for one invocation inside that operation. | Obtaining that invocation's result, inspecting its error, and retrying eligible work. |
| `parent_task_id` | The task that directly initiated a child; `null` for the root. | Reconstructing task hierarchy for diagnostics or UI detail views. |

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

History is returned newest first. Use the last ID from the previous page as
the next cursor:

```python
page = await raven.list_operations(limit=25, status="failed")

if page:
    next_page = await raven.list_operations(
        limit=25,
        status="failed",
        after_operation_id=page[-1].operation_id,
    )
```

For task history, call `list_operation_tasks()` with the owning operation ID
and use `after_task_id` in the same way. A cursor must identify an existing
record in the requested scope; it is not an arbitrary offset.

### Creating custom operations

Applications integrating their own long-running workers can use the same
contract:

```python
from nraven import Event, EventType


async def custom_worker(operation):
    await operation.publish(
        Event(
            type=EventType.WORK_PROGRESS,
            data={"stage": "indexing", "completed": 4, "total": 10},
        )
    )
    operation.raise_if_cancelled()
    return {"indexed": 10}


task = await raven.run_operation("my_app.index", custom_worker)
result = await task.result()
```

The example above uses `run_operation()` for a single worker: it creates the
operation, starts that worker, and gives you an `OperationTask` whose
`result()` waits for its return value.

For a workflow that calls several RAVEN components, create the operation
first and pass it to each call. This example assumes `raven` is started, its
models are configured, the source file exists, and `engineering` is a new
knowledge name:

```python
async def prepare_knowledge(operation):
    create_task = await raven.create_knowledge(
        "engineering",
        operation=operation,
    )
    knowledge = await create_task.result()

    ingest_task = await raven.ingest(
        knowledge.name,
        "./documents/system-design.pdf",
        operation=operation,
    )
    return await ingest_task.result()


operation = await raven.create_operation("my_app.prepare_knowledge")
root_task = await operation.run("my_app.prepare_knowledge", prepare_knowledge)
ingested_file = await root_task.result()
print(ingested_file["file_id"])
```

`operation.run()` starts the root task; its name must match the name passed to
`create_operation()`. Inside the worker, both component calls run as child
tasks under the same operation. Await each child's `result()` before depending
on its output. All three tasks share one operation ID and replayable event
stream, while `root_task.result()` waits for the whole workflow and returns
the ingestion result. Operation names must be non-empty stable strings;
application names should use a namespace such as `my_app.prepare_knowledge`
to avoid collisions with built-in names.

`submit_operation(name, worker)` is the event-oriented convenience form: it
creates and starts the root task but returns the owning `Operation`. Use it
when the caller primarily needs operation-level status/events. Use
`run_operation()` when the caller needs the returned `OperationTask` and its
native result.

### Built-in operation names

These stable names appear in task records and event correlation metadata:

| Area | Operation names |
| --- | --- |
| Runtime | `nraven.configure_models`, `runtime.settings.update`, `runtime.settings.reset` |
| Model provider | `model.check_connection`, `model.list`, `model.inspect`, `model.pull`, `model.delete`, `model.load_llm`, `model.load_embedding`, `model.preload_llm`, `model.preload_embedding`, `model.unload_llm`, `model.unload_embedding` |
| Knowledge | `knowledge.set_summary`, `knowledge.ingest`, `knowledge.delete_file`, `knowledge.create`, `knowledge.delete` |
| Ingestion | `ingestion.run`, `ingestion.cleanup` |
| Embedded retrieval | `retrieval.embedded.local`, `retrieval.embedded.global` |
| Hierarchical retrieval | `retrieval.hierarchical.local`, `retrieval.hierarchical.global`, `retrieval.hierarchical.by_knowledge`, `retrieval.hierarchical.by_file` |
| Agreement retrieval | `retrieval.agreement.local`, `retrieval.agreement.global` |
| Vector-conditioned retrieval | `retrieval.vector_conditioned.local`, `retrieval.vector_conditioned.global` |
| Reconstruction | `reconstruction.reconstruct`, `reconstruction.from_turn` |
| Conversation | `conversation.save_preference`, `conversation.remove_preference`, `conversation.update`, `conversation.generate_title`, `conversation.get_context`, `conversation.append_turn`, `conversation.reconcile_turn`, `conversation.create`, `conversation.update_metadata`, `conversation.delete` |
| Agent/session | `chat.generate_response`, `session.generate_response` |

Applications may display these names for diagnostics, but normal UI labels
should be friendlier and should not infer retryability from the name alone.

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

Cancelling a root task cancels the complete operation and its active children.
Cancelling a non-root task cancels only that task. Component workers check
cancellation between expensive stages, but a blocking provider request may not
stop until the provider returns control. Therefore cancellation means "stop as
soon as a safe boundary is reached," not "undo every committed side effect."

After cancellation, wait for `task.result()`, `run.wait()`, or an operation
terminal event before assuming cleanup is complete.

### User-confirmed retry

Only task types with an explicit retry policy are retryable. RAVEN currently
uses user-confirmed retry for operations such as ingestion, reconstruction,
and session generation rather than automatically repeating arbitrary model or
storage work.
The persisted `retry_policy` field is exactly `"never"` or
`"user_confirmed"`; it is not an instruction to auto-retry on startup.

| Retryable root task | Maximum attempts | What is reused |
| --- | --- | --- |
| `ingestion.run` | 3 | Knowledge name, retained source/snapshot information, and ingestion parameters. |
| `reconstruction.reconstruct` | 3 | The normalized evidence/retrieval input. |
| `session.generate_response` | 3 | Conversation ID, stable turn ID, query, retrieval mode, and run settings. |

An attempt is retryable only when it failed with an approved recoverable error,
has JSON-serializable retry input, has attempts remaining, and has not already
been retried. Completed, cancelled, non-retryable, and already-retried tasks
are rejected.

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
the original operation's history. The new task exposes
`retry_of_operation_id`, `retry_of_task_id`, and an incremented `attempt`.
Retries are idempotency-aware: ingestion does not duplicate a committed file,
and a session retry reuses an already committed turn with the same `turn_id`
instead of writing a second response.


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

The remaining envelope fields have fixed semantics:

| Field | Type | Meaning |
| --- | --- | --- |
| `type` | `EventType` / string | Stable machine-readable event name. In JSON and SSE it is serialized as its string value. |
| `data` | object | Event-specific payload. Treat unknown additive fields as forward-compatible. |
| `operation_id` | UUID | Owning operation. RAVEN assigns it when the event is published. |
| `task_id` | UUID or `null` | Emitting task. Domain events published inside a task inherit the active task ID. |
| `task_name` | string or `null` | Stable built-in or application task name. |
| `event_id` | integer | One-based sequence within this operation; use it as the replay cursor. |
| `timestamp` | UTC datetime | Time RAVEN created the event. |
| `is_final` | boolean | `true` only for the operation's terminal event. No later event can be published. |

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

```python
events = await raven.read_operation_events(
    operation_id,
    after_event_id=0,
    limit=100,
)
```

`after_event_id` is exclusive: a cursor of `12` asks for event 13 onward. A
cursor of `0` starts at the beginning. A negative cursor is invalid. A cursor
ahead of the retained operation history raises `event_history_gap` rather than
silently pretending that no events exist.

`task.events()` filters the operation stream to that task and its descendants.
For a root task this is the complete operation stream; for a child task it is
the corresponding subtree. `operation.events()` always exposes the entire
operation.

### Event reference

Event names are grouped by what a consumer normally does with them:

| Family | Event types | Typical consumer behavior |
| --- | --- | --- |
| Operation lifecycle | `operation.queued`, `operation.started`, `operation.completed`, `operation.failed`, `operation.cancelled` | Update the overall job state. The last three are terminal; the terminal operation event has `is_final=true`. |
| Task lifecycle | `operation.task.queued`, `operation.task.started`, `operation.task.completed`, `operation.task.failed`, `operation.task.cancelled` | Update one task row or nested-stage indicator. The payload contains the task `name`; failures include `error`. |
| Generic progress | `work.progress` | Render application-defined progress from `data`. |
| Chat stream | `chat.thinking_delta`, `chat.response_delta` | Append `data.delta` to separate thinking and answer buffers. `chat.delta` remains a generic/compatibility event type; the current harness emits the two explicit variants instead. |
| Chat tools | `chat.tool_call`, `chat.tool_result` | Correlate with `data.call_id`; render the step, tool name, bounded `ui_summary`, evidence, and recoverable error. |
| Chat outcome | `chat.max_iterations`, `chat.result_reused`, `chat.completed`, `chat.failed` | Mark iteration fallback, an idempotently reused turn, final response/evidence, or failure. `chat.completed` is a domain event; wait for the operation terminal event before releasing all operation state. |
| Ingestion pipeline | `ingestion.started`, `ingestion.progress`, `ingestion.vectors_written`, `ingestion.metadata_committed`, `ingestion.completed`, `ingestion.failed` | Render stages and counters, then show the committed file result or failure. |
| Ingestion cleanup | `ingestion.cleanup.started`, `ingestion.cleanup.completed`, `ingestion.cleanup.failed` | Explain rollback or crash-recovery cleanup. |
| Knowledge lifecycle | `knowledge.create.started`, `knowledge.create.completed`, `knowledge.create.failed`, `knowledge.updated`, `knowledge.delete.started`, `knowledge.delete.completed`, `knowledge.delete.failed` | Refresh knowledge metadata or remove a deleted item. |
| Knowledge file work | `knowledge.ingest.started`, `knowledge.ingest.progress`, `knowledge.ingest.completed`, `knowledge.ingest.failed`, `knowledge.file_delete.started`, `knowledge.file_delete.completed`, `knowledge.file_delete.failed` | Track storage-level ingestion and file deletion nested inside a higher-level operation. |
| Conversation lifecycle | `conversation.create.started`, `conversation.create.completed`, `conversation.create.failed`, `conversation.update.started`, `conversation.update.completed`, `conversation.update.failed`, `conversation.delete.started`, `conversation.delete.completed`, `conversation.delete.failed` | Refresh conversation metadata or remove a deleted conversation. |
| Context compaction | `conversation.memory_compaction.started`, `conversation.memory_compaction.completed`, `conversation.memory_compaction.failed` | Show that model context is being summarized; canonical UI history remains intact. |
| Turn persistence | `conversation.turn_commit.started`, `conversation.turn_commit.completed`, `conversation.turn_commit.reused`, `conversation.turn_commit.failed` | Mark durable turn commit or idempotent reuse. |
| Vector memory | `conversation.memory_index.started`, `conversation.memory_index.completed`, `conversation.memory_index.failed` | Track semantic indexing of committed user/assistant messages. |
| Embedded retrieval | `retrieval.embedded.started`, `retrieval.embedded.completed`, `retrieval.embedded.failed` | Show direct vector retrieval progress and result count/failure. |
| Hierarchical retrieval | `retrieval.hierarchical.started`, `retrieval.hierarchical.read`, `retrieval.hierarchical.completed`, `retrieval.hierarchical.failed` | Show knowledge/file reads and LLM-assisted scoring progress. |
| Agreement retrieval | `retrieval.agreement.started`, `retrieval.agreement.completed`, `retrieval.agreement.failed` | Show the combined embedded/hierarchical comparison. |
| Vector-conditioned retrieval | `retrieval.vector_conditioned.started`, `retrieval.vector_conditioned.completed`, `retrieval.vector_conditioned.failed` | Show vector narrowing followed by hierarchical selection. |
| Reconstruction | `reconstruction.started`, `reconstruction.file`, `reconstruction.completed`, `reconstruction.failed` | Render each reconstructed file from `reconstruction.file`, then mark the source set complete. |
| Ollama connection | `model.connection.started`, `model.connection.completed`, `model.connection.failed` | Display connection-check state. |
| Ollama inventory | `model.list.started`, `model.list.completed`, `model.list.failed`, `model.inspect.started`, `model.inspect.completed`, `model.inspect.failed` | Refresh or inspect the external Ollama model inventory. |
| Ollama mutation | `model.pull.started`, `model.pull.progress`, `model.pull.completed`, `model.pull.failed`, `model.delete.started`, `model.delete.completed`, `model.delete.failed` | Display pull progress or refresh inventory after deletion. |
| LLM adapter | `model.load_llm.started`, `model.load_llm.completed`, `model.load_llm.failed` | Show LLM adapter validation/construction for local or cloud models. |
| Embedding adapter | `model.load_embedding.started`, `model.load_embedding.completed`, `model.load_embedding.failed` | Show embedding-adapter validation/construction for local or cloud models. |
| LLM residency | `model.preload_llm.started`, `model.preload_llm.completed`, `model.preload_llm.failed`, `model.unload_llm.started`, `model.unload_llm.completed`, `model.unload_llm.failed` | Show local LLM residency changes. Cloud models report a no-local-residency result instead. |
| Embedding residency | `model.preload_embedding.started`, `model.preload_embedding.completed`, `model.preload_embedding.failed`, `model.unload_embedding.started`, `model.unload_embedding.completed`, `model.unload_embedding.failed` | Show local embedding residency changes. Cloud models report a no-local-residency result instead. |

Failure payloads use the same safe error shape as the library and HTTP API:

```json
{
  "error": {
    "code": "knowledge_not_found",
    "message": "Knowledge 'engineering' does not exist.",
    "details": {}
  }
}
```

Clients should branch on `code`, display `message`, and treat `details` as
structured context. Do not parse human-readable messages to determine logic.


## Errors

Expected failures use `RavenError`. Its stable fields are:

```python
from nraven import RavenError


try:
    await task.result()
except RavenError as error:
    print(error.code.value)
    print(error.message)
    print(error.details)
```

| Field | Meaning |
| --- | --- |
| `code` | Stable `ErrorCode` enum used for program logic. |
| `message` | Safe human-readable explanation. |
| `details` | Optional structured context such as IDs, limits, or revision values. |

Unexpected exceptions are normalized at transport boundaries to
`internal_error`; raw exception text is not exposed to clients. Important
error groups are:

| Group | Common codes | What the caller should do |
| --- | --- | --- |
| Resource lookup | `knowledge_not_found`, `conversation_not_found`, `file_not_found`, `section_not_found`, `conversation_turn_not_found` | Refresh the relevant list and correct the identifier. |
| Resource conflict | `knowledge_already_exists`, `file_already_exists`, `conversation_turn_active`, `conversation_turn_conflict` | Reuse the existing resource, choose another name, or wait for the active turn. |
| Model configuration | `llm_model_required`, `embedding_model_required`, `invalid_model_spec`, `model_api_key_not_found`, `model_capability_missing`, `model_reload_in_progress` | Configure both roles, supply the referenced environment variable, or wait for the current reconfiguration. |
| Provider/runtime | `model_provider_failed`, `ollama_unavailable`, `ollama_operation_failed` | Check provider credentials/connectivity or the external Ollama service, then retry eligible work. |
| Ingestion input | `source_file_not_found`, `source_file_changed`, `source_file_unreadable`, `source_file_too_large`, `unsupported_source_file`, `document_parse_failed` | Correct or re-upload the source; do not retry unchanged invalid input indefinitely. |
| Embeddings | `invalid_embedding_result`, `embedding_dimension_mismatch`, `embedding_identity_mismatch` | Use a working embedding adapter; use the same embedding identity for an existing knowledge or rebuild that knowledge deliberately. |
| Retrieval/agent | `invalid_retrieval_mode`, `retrieval_mode_not_allowed`, `retrieval_scoring_invalid`, `agent_max_iterations` | Correct the mode/scope, inspect provider output, or adjust the bounded runtime setting. |
| Operation | `operation_not_found`, `operation_task_not_found`, `operation_cancelled`, `operation_interrupted`, `operation_finished` | Refresh history; retry only if the task record reports `can_retry=true`. |
| Retry | `operation_task_not_retryable`, `operation_task_already_retried`, `invalid_retry_input`, `upload_retry_expired` | Follow the existing linked retry or resubmit the original request/source. |
| Persistence | `persistence_failed`, `operation_sync_failed`, `operation_database_failed`, `operation_database_in_use`, `operation_database_corrupted` | Stop new writes, inspect filesystem ownership/health, and restore or repair before resuming service. |
| Configuration | `invalid_system_config`, `invalid_runtime_config`, `runtime_config_conflict`, unsupported schema-version codes | Correct the field; for a revision conflict, read current settings and reapply the intended change. |
| Server boundary | `authentication_required`, `invalid_user_id`, `request_body_too_large`, `request_query_too_large`, capacity-exceeded codes | Correct credentials/input or wait for capacity. |
| Event replay | `invalid_event_cursor`, `invalid_event_page_size`, `event_history_gap`, `event_stream_closed`, `event_stream_finished` | Correct the cursor/page size, refresh operation status, or treat an expired/deleted history as unavailable. |

Whether an error is retryable is determined by the persisted task's retry
policy—not merely by this table. Always inspect `can_retry` or use
`list_retryable_tasks()` before presenting a Retry action.

### Complete error-code catalogue

The following codes are the stable public vocabulary. Several related codes
can lead to the same caller action, but keeping them distinct makes logs,
events, and UI messages precise.

| Resource and conversation code | Condition |
| --- | --- |
| `invalid_knowledge_name` | A knowledge name normalizes to no valid characters. |
| `knowledge_already_exists` | The normalized knowledge identity is already present. |
| `knowledge_not_found` | The requested knowledge is not registered. |
| `knowledge_closed` | Work was attempted on a permanently closed knowledge handle. |
| `knowledge_not_started` | Storage-dependent work was attempted before the knowledge opened. |
| `invalid_resource_cache_size` | A knowledge/conversation resource-cache bound is invalid. |
| `conversation_already_exists` | A conversation identity conflicts with an existing resource. |
| `conversation_not_found` | The requested conversation is not registered. |
| `conversation_closed` | Work was attempted through a closed conversation or session. |
| `conversation_not_started` | Session/conversation work began before startup. |
| `conversation_memory_not_initialized` | Context or vector memory was used before model-backed initialization. |
| `context_token_limit_too_small` | The configured memory budget cannot support the memory implementation. |
| `conversation_turn_conflict` | An existing turn ID is associated with different input. |
| `conversation_turn_active` | Another session currently owns the conversation's turn lock. |
| `conversation_turn_not_found` | The requested committed turn does not exist. |
| `conversation_turn_result_missing` | A committed/reused turn lacks a valid stored agent result. |
| `reconstruction_evidence_not_found` | Persisted turn data contains no usable reconstruction evidence where evidence was required. |
| `foreign_conversation` | A `Conversation` from another `Raven` instance was supplied to `session()`. |
| `invalid_preference_id` | A preference ID is not a valid UUID. |
| `preference_not_found` | No preference has the supplied ID. |
| `invalid_conversation_id` | The conversation identifier is malformed. |
| `invalid_conversation_title` | The supplied title violates title requirements. |
| `invalid_message_cursor` | A message pagination cursor is invalid. |
| `file_already_exists` | The knowledge already contains or is ingesting that file name. |
| `file_not_found` | The requested stored file or file cursor does not exist. |
| `section_not_found` | The section does not exist or does not belong to the asserted file. |

| Model, retrieval, and ingestion code | Condition |
| --- | --- |
| `invalid_retrieval_mode` | The supplied retrieval mode string is unknown. |
| `retrieval_mode_not_allowed` | The mode exists but is outside the current conversation/run policy. |
| `retrieval_scoring_invalid` | Hierarchical model scoring remained invalid after bounded retries. |
| `llm_model_required` | An operation requires a configured LLM. |
| `embedding_model_required` | An operation requires a configured embedding model. |
| `invalid_model_spec` | A `ModelSpec`, role pairing, option, or reserved field is invalid. |
| `model_api_key_not_found` | The environment variable named by `api_key_ref` is absent or empty. |
| `model_provider_failed` | A non-Ollama provider or adapter operation failed. |
| `model_capability_missing` | The loaded adapter lacks a required async model capability. |
| `model_reload_in_progress` | Model-dependent work was requested during pair reconfiguration. |
| `agent_max_iterations` | The agent exhausted its allowed iterations and could not finish safely. |
| `invalid_chunking` | Chunk size, overlap, or related splitting values are invalid. |
| `no_chunks_produced` | Valid document sections produced no embedding chunks. |
| `invalid_embedding_result` | The embedding adapter returned missing, malformed, or non-finite vectors. |
| `embedding_dimension_mismatch` | A vector dimension differs from the established collection dimension. |
| `embedding_identity_mismatch` | The configured embedding identity differs from the one recorded for a knowledge. |
| `source_file_not_found` | The ingestion source path does not identify a file. |
| `source_file_changed` | A retry/snapshot hash no longer matches the submitted source. |
| `source_file_unreadable` | RAVEN cannot read the source. |
| `source_file_too_large` | The source exceeds the effective size limit. |
| `trusted_ingestion_disabled` | The server's trusted local-path endpoint is disabled. |
| `ingestion_path_not_allowed` | A trusted path is outside configured allowed roots. |
| `upload_retry_expired` | A retained upload required for retry no longer exists. |
| `invalid_upload_filename` | A browser upload name is empty, unsafe, or otherwise invalid. |
| `unsupported_source_file` | The file extension is not one of the supported document types. |
| `document_parse_failed` | Native or Docling parsing failed. |
| `no_sections_produced` | Parsing/splitting produced no semantic sections. |
| `section_metadata_extraction_failed` | LLM metadata extraction failed after bounded retries. |
| `ingestion_reconciliation_failed` | Pending vectors/metadata could not be reconciled safely. |

| Operation and event code | Condition |
| --- | --- |
| `invalid_operation_name` | An operation name is empty/invalid or a root task name does not match it. |
| `operation_not_found` | The operation UUID has no retained record. |
| `operation_cancelled` | A result was requested from cancelled work. |
| `operation_interrupted` | Startup recovery found non-terminal work whose process worker no longer exists. |
| `operation_finished` | New work/publication was attempted after terminal state. |
| `invalid_operation_id` | An operation ID is malformed. |
| `invalid_operation_status` | A status filter/value is unknown. |
| `invalid_operation_page_size` | An operation/task history page size is invalid. |
| `invalid_list_page_size` | A resource-list page size is invalid. |
| `invalid_operation_cache_size` | The finished-operation in-memory cache bound is invalid. |
| `operation_manager_closed` | Work was requested after the manager closed. |
| `operation_task_not_found` | The task UUID is absent or belongs to another operation. |
| `operation_task_not_retryable` | Status, type, error, retry input, or attempts disallow retry. |
| `operation_task_already_retried` | A linked retry already exists for that task. |
| `invalid_retry_input` | Durable retry input is missing, malformed, or not JSON serializable. |
| `event_stream_closed` | The stream is closed or reserved for cleanup. |
| `event_stream_finished` | Publication was attempted after its final event. |
| `invalid_event_cursor` | The cursor is negative or otherwise invalid. |
| `invalid_event_page_size` | The requested event page size is invalid. |
| `event_history_gap` | The requested cursor is ahead of the recovered retained history. |
| `operation_sync_failed` | SQLite operation/event state could not be checkpointed durably. |
| `operation_database_failed` | General operation-store access failed. |
| `operation_database_in_use` | Another owner holds the operation database. |
| `operation_database_corrupted` | Integrity validation found a corrupt operation database. |
| `unsupported_operation_database_version` | The database schema is newer/unsupported. |
| `invalid_operation_sync_interval` | The periodic durability interval is invalid. |
| `invalid_retention` | An operation/upload retention value is invalid. |
| `invalid_cleanup_batch_size` | A bounded cleanup batch size is invalid. |

| Configuration, server, and general code | Condition |
| --- | --- |
| `invalid_metadata` | Persisted or caller-supplied structured data violates its contract. |
| `unsupported_metadata_version` | Knowledge/conversation metadata uses an unsupported schema version. |
| `persistence_failed` | A durable resource update could not be committed. |
| `invalid_system_config` | A process-wide configuration field is invalid. |
| `unsupported_system_config_version` | The system-settings schema version is unsupported. |
| `invalid_runtime_config` | A per-user runtime setting/update is invalid. |
| `unsupported_runtime_config_version` | The runtime-settings schema version is unsupported. |
| `runtime_config_conflict` | `expected_revision` does not match the current revision. |
| `invalid_user_id` | User context is missing where required or is not a UUID. |
| `authentication_required` | The bearer token is missing or invalid. |
| `server_already_running` | Another RAVEN server already owns the selected home. |
| `runtime_registry_closed` | A server request reached a closed runtime registry. |
| `runtime_capacity_exceeded` | The server cannot create/lease another user runtime under its configured bound. |
| `operation_capacity_exceeded` | Per-user or global active-operation capacity is exhausted. |
| `request_body_too_large` | The HTTP request body exceeds the configured maximum. |
| `request_query_too_large` | The URL query string exceeds the configured maximum. |
| `ollama_unavailable` | The configured Ollama server cannot be reached. |
| `ollama_operation_failed` | Ollama failed a requested model action. |
| `internal_error` | An unexpected exception was hidden behind a safe boundary error. |


## Model providers

RAVEN separates model configuration from model implementation through three
public types:

- `ModelSpec` describes a model without constructing its adapter.
- `ModelRole` distinguishes the generation model from the embedding model.
- `Provider` routes each specification to Ollama or LiteLLM.

`ModelSpec` contains:

| Field | Type | Required | Meaning and constraints |
| --- | --- | --- | --- |
| `provider` | string | Yes | `"ollama"` for local Ollama, or the LiteLLM provider identifier required by the selected service. It is trimmed, normalized to lowercase, and cannot be empty. |
| `model` | string | Yes | Provider-specific model name. It is trimmed but otherwise preserved and cannot be empty. For Ollama, use the name shown by `ollama list`, including its tag where applicable. |
| `role` | `ModelRole` | Yes | Exactly `ModelRole.LLM` (`"llm"`) or `ModelRole.EMBEDDING` (`"embedding"`). The two arguments to `configure_models()` must have the corresponding roles. |
| `api_key_ref` | string or `None` | No | Name of an environment variable containing the API key. It is resolved when a non-Ollama adapter is loaded; the value is not stored in the spec. Omit it when the provider uses ambient credentials or no key. |
| `options` | object | No | Non-secret LiteLLM adapter options. Defaults to `{}`. Ollama specs currently reject non-empty options. Reserved model/provider/key fields and credential-like keys are rejected. |

`ModelSpec` is immutable and rejects unknown fields. It describes one role,
not a complete model pair. RAVEN requires one LLM spec and one embedding spec:

```python
llm_spec = ModelSpec(
    provider="ollama",
    model="qwen3:8b",
    role=ModelRole.LLM,
)

embedding_spec = ModelSpec(
    provider="ollama",
    model="bge-m3",
    role=ModelRole.EMBEDDING,
)
```

> **WARNING — Choose your embedding model before ingesting data.** Knowledge
> vectors and conversation vector memory are generated in that model's vector
> space. A different embedding model cannot reliably search those existing
> vectors, even if it produces vectors with the same dimensions. RAVEN records
> the embedding identity and rejects mismatches; configuring another model
> does **not** convert stored embeddings. To change models after data exists,
> plan an explicit rebuild/re-embedding of the affected knowledge and memory
> stores from their source data. Changing the LLM does not have this particular
> vector-compatibility constraint.

Secrets must not be placed in `options`. RAVEN rejects common credential fields
there and resolves `api_key_ref` from the process environment when the model is
loaded.

### What model configuration does

`Raven.configure_models(llm_spec, embedding_spec)` is deliberately more than
assigning two names. It performs this serialized sequence:

1. Validate that the first spec has role `llm` and the second has role
   `embedding`.
2. Build or reuse both LlamaIndex adapters through `Provider`.
3. Verify that the LLM exposes asynchronous chat and that the embedding model
   exposes asynchronous query embedding.
4. If the pair changed, validate the embedding model against existing
   knowledge stores. Existing vector collections cannot silently switch to an
   incompatible embedding identity or dimension.
5. Install the validated pair into RAVEN and rebuild model-dependent pipelines
   and the harness.
6. Unload replaced Ollama models that are no longer part of the active pair.
7. Preload the configured Ollama embedding model and LLM. Cloud adapters skip
   local residency actions.

The call returns an `OperationTask`; model-dependent work must wait for its
result:

```python
configure_task = await raven.configure_models(llm_spec, embedding_spec)
configured = await configure_task.result()
```

While configuration is in progress, new sessions are rejected with
`model_reload_in_progress`. If adapter construction, capability validation, or
embedding compatibility fails before installation, the previously configured
pair remains active. Configuration itself is runtime state: applications
should configure the required pair again after restarting RAVEN.

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

By default RAVEN connects to `http://127.0.0.1:11434`. Set `OLLAMA_HOST` in the
RAVEN process environment before constructing `Raven` to use another Ollama
endpoint. RAVEN does not choose Ollama's CPU/GPU backend; that is a property of
the externally managed Ollama process.

Configuration verifies both adapters and checks the embedding model against
existing knowledge stores before installing the new pair. When no models are
configured, RAVEN installs the pair and preloads its Ollama models. When a pair
is already configured, RAVEN unloads replaced Ollama models, installs the new
pair, and preloads its Ollama models. If pre-installation validation fails, the
previous configured pair remains active.

RAVEN also exposes operation-based Ollama administration for connection checks,
listing, inspection, pulling, and deletion. These methods connect to the
Ollama server; they do not own its process.

```python
# Returns None on success; raises ollama_unavailable on failure.
connection = await raven.check_ollama_connection()
await connection.result()

# Returns the model records supplied by Ollama.
listing = await raven.list_ollama_models()
models = await listing.result()

# Returns Ollama's detailed model record.
inspection = await raven.inspect_ollama_model("qwen3:8b")
details = await inspection.result()

# Returns None. Progress is available as model.pull.progress events and through
# the optional callback.
pull = await raven.pull_ollama_model("qwen3:8b")
async for event in pull.events():
    if event.type == EventType.MODEL_PULL_PROGRESS:
        print(event.data)
await pull.result()

# Permanently deletes the model from Ollama and clears its cached adapters.
deletion = await raven.delete_ollama_model("unused-model:latest")
await deletion.result()
```

Deleting an Ollama model is different from unloading it. Deletion removes the
downloaded model from Ollama storage. Unloading only releases runtime residency
and keeps the model installed.

### Cloud models through LiteLLM

Set the provider's API key in the environment managed by your application or
deployment platform. `api_key_ref` contains only the variable's name:

```bash
# Shell syntax varies by platform. The important part is that the variable is
# present in the process environment before Python or the RAVEN server starts.
GEMINI_API_KEY=replace-with-your-secret
```

RAVEN reads `os.environ`; it does not automatically parse a `.env` file. If an
application uses `python-dotenv`, call `load_dotenv()` before creating/configuring
RAVEN. In production, prefer the host platform's secret/environment mechanism.

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

For LiteLLM-backed models, `provider` is forwarded as LiteLLM's custom provider
identifier and `model` is forwarded as the provider's model name. The exact
identifier is provider-dependent; consult LiteLLM's provider documentation and
the model vendor's current model catalogue. For embeddings, RAVEN constructs
the provider-qualified model name expected by the LiteLLM embedding adapter.

Examples of non-secret options include generation temperature, timeout, API
base URL where supported, and embedding batch size. Options are adapter
specific: an option valid for an LLM may not be valid for an embedding model.
RAVEN reserves fields that it controls itself, including model name, API key,
and custom-provider selection.

### Switching configured models

Call `Raven.configure_models()` with a new valid pair to switch models. The
replacement is serialized and validated before it becomes active:

**Keep the same embedding model for existing knowledge and vector memory.**
The warning above applies to model switches too: RAVEN does not re-embed old
data during configuration, and an incompatible replacement is rejected.

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

The response intentionally describes configured identity, not credentials or
provider client internals. API-key values are never returned.

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

The residency result has this shape:

```json
{
  "provider": "ollama",
  "model": "qwen3:8b",
  "role": "llm",
  "action": "preload",
  "performed": true
}
```

When `performed` is `false`, the result also includes a `reason`, such as
`"provider_has_no_local_residency"`.

### Provider troubleshooting

| Symptom/code | Meaning | Corrective action |
| --- | --- | --- |
| `model_api_key_not_found` | The environment variable named by `api_key_ref` is absent or empty. | Set it in the process environment and configure again. Do not place the value in `options`. |
| `invalid_model_spec` | Roles are reversed, required strings are empty, Ollama options were supplied, or options contain reserved/secret fields. | Correct the spec using the field table above. |
| `model_capability_missing` | The selected adapter does not provide the async LLM or embedding capability RAVEN requires. | Select a compatible chat or embedding model/adapter. |
| `embedding_identity_mismatch` | Existing knowledge was created with another embedding identity. | Restore the original embedding model or deliberately rebuild/re-ingest that knowledge. |
| `embedding_dimension_mismatch` | Produced vectors do not match the stored Qdrant collection dimension. | Use the original compatible embedding model or rebuild the knowledge. |
| `ollama_unavailable` | RAVEN cannot reach the configured Ollama host. | Start/check the external Ollama server and `OLLAMA_HOST`, then run the connection check. |
| `ollama_operation_failed` | Ollama rejected or failed an inspect, pull, delete, load, preload, or unload action. | Inspect Ollama logs and the model name, then retry if appropriate. |
| `model_provider_failed` | A LiteLLM/provider adapter failed to load or execute. | Verify provider name, model name, credentials, network access, options, quota, and rate limits. |


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

Names are trimmed, every character outside letters, numbers, `_`, and `-` is
replaced with `_`, and leading/trailing underscores are removed. The normalized
name must contain at least one valid character. Because the normalized name is
the persistent identity, names such as `Engineering Docs` and
`Engineering_Docs` resolve to the same resource and cannot both be created.

Creation returns an opened `Knowledge` object. Its persisted metadata is:

```json
{
  "schema_version": 1,
  "created_at": "2026-09-20T10:30:00+00:00",
  "name": "engineering",
  "user_summary": "Engineering specifications and design documents."
}
```

### Knowledge API reference

Inspection methods return their final value directly; mutations return an
`OperationTask` so their events and terminal state remain observable.

| Method | Inputs | Return/result | Side effect |
| --- | --- | --- | --- |
| `create_knowledge(name, user_summary="")` | Name and optional summary | Task result: `Knowledge` | Creates and registers a persistent knowledge directory. |
| `get_knowledge(name)` | Knowledge name | Open `Knowledge` handle | Opens or reuses the resource; no content mutation. |
| `get_knowledge_details(name)` | Knowledge name | Metadata object shown above | None. |
| `list_knowledges()` | None | All knowledge metadata records | None. |
| `list_knowledges_page(limit, after_name=None)` | Positive page size and optional prior-page name | One metadata page | None. |
| `update_knowledge(name, user_summary=...)` | Knowledge name and replacement summary | Task result: `None` | Atomically replaces `user_summary`; read it with `get_knowledge_details()`. |
| `list_knowledge_files(name)` | Knowledge name | File records | None. |
| `list_knowledge_files_page(name, limit, after_file_id=None)` | Knowledge, page size, optional file cursor | One file page in insertion order | None. |
| `list_file_sections(name, file_name)` | Knowledge and exact stored file name | Sections in document order | None. |
| `list_file_sections_page(name, file_name, limit, after_section_index=0)` | Knowledge, file, page size, last section index | One section page | None. |
| `get_knowledge_section(name, section_id, file_name=None)` | Knowledge, section ID, optional file assertion | Complete section record | None. |
| `count_knowledge_files(name)` | Knowledge name | Integer file count | None. |
| `count_knowledge_vectors(name)` | Knowledge name | Integer vector-point count | None. |
| `delete_knowledge_file(name, file_id)` | Knowledge and file ID—not file name | Task result: deleted file identity | Removes vectors and file/section records. |
| `delete_knowledge(name)` | Knowledge name | Task result on completion | Closes and removes the complete knowledge resource. |

Page cursors are stable resource identities, not numeric offsets. Use the final
item from one page to request the next:

```python
files = await raven.list_knowledge_files_page("engineering", limit=50)

if files:
    next_files = await raven.list_knowledge_files_page(
        "engineering",
        limit=50,
        after_file_id=files[-1]["file_id"],
    )
```

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

The complete stored file record has this shape:

```json
{
  "file_id": "a83fd91c20b4",
  "file_name": "system-design.pdf",
  "section_count": 8,
  "chunk_count": 21,
  "ingested_at": "2026-09-20T10:35:00+00:00",
  "navigation_type": "page"
}
```

A complete section record returned by navigation has this shape:

```json
{
  "file_id": "a83fd91c20b4",
  "file_name": "system-design.pdf",
  "section_index": 3,
  "section_id": "a83fd91c20b4-3",
  "summary": "Thermal protection requirements",
  "keywords": ["thermal", "shutdown"],
  "conditions": ["temperature exceeds the configured limit"],
  "definitions": [],
  "raw_content": "...",
  "source_element_ids": ["a83fd91c20b4-7", "a83fd91c20b4-8"],
  "source_range": [2, 3]
}
```

`navigation_type` belongs to the file because all of its sections share the
same navigation system. `source_range` belongs to each section. The integer
suffix in `section_id` is the section index; it is not an independently
generated ID.

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

Every `Raven.ingest()` argument is:

| Argument | Type | Default when omitted | Constraint/effect |
| --- | --- | --- | --- |
| `knowledge_name` | string | Required | Must identify an existing knowledge. |
| `source_path` | string or `Path` | Required | Must identify a readable supported file. Library paths are trusted application input; server path ingestion has additional policy checks. |
| `breakpoint_percentile_threshold` | integer or `None` | Runtime setting, initially `95` | `1..100`. Lower values generally create more semantic boundaries. |
| `buffer_size` | integer or `None` | Runtime setting, initially `1` | Positive sentence-window buffer used when comparing semantic units. |
| `max_extraction_retries` | integer or `None` | Runtime setting, initially `3` | Positive number of LLM metadata-extraction attempts per section. |
| `chunk_size` | integer or `None` | Runtime setting, initially `512` | Positive embedding chunk size. |
| `chunk_overlap` | integer or `None` | Runtime setting, initially `50` | Non-negative and strictly smaller than `chunk_size`. |
| `max_source_size_bytes` | integer or `None` | System limit, initially `100 MiB` | Positive per-call limit that cannot exceed the process-wide system ceiling. |
| `max_document_pages` | integer or `None` | System limit, initially `1,000` | Positive per-call limit that cannot exceed the process-wide system ceiling. |
| `operation` | `Operation` or `None` | New operation | Pass an existing operation only when composing ingestion into a larger workflow. |

The task result is:

```json
{
  "knowledge": "engineering",
  "file": "system-design.pdf",
  "file_id": "a83fd91c20b4",
  "section_count": 8,
  "chunk_count": 21,
  "source_path": "documents/system-design.pdf"
}
```

An idempotently reused committed retry also includes
`"already_committed": true`.

### Ingestion progress contract

Every `ingestion.progress` payload includes the knowledge, file, generated
file ID, stage, status, and optional counters:

```json
{
  "knowledge": "engineering",
  "file": "system-design.pdf",
  "file_id": "a83fd91c20b4",
  "stage": "metadata_extraction",
  "status": "running",
  "completed": 3,
  "total": 8,
  "section_index": 3
}
```

Stages occur in this order:

| Stage | What is happening | Useful progress fields |
| --- | --- | --- |
| `source_validation` | File existence, readability, extension, and size are checked. | `size_bytes` on completion. |
| `source_snapshot` | An immutable private copy is created for the run/retry boundary. | Stage status. |
| `parsing` | TXT/Markdown native parsing or Docling PDF/DOCX parsing creates normalized elements. | Element count. |
| `sectioning` | Embedding-assisted semantic splitting creates provenance-aware sections. | Section count. |
| `metadata_extraction` | The LLM extracts summary, keywords, conditions, and definitions for each section. | Completed sections, total sections, current `section_index`. |
| `chunking` | Stored sections are divided into embedding-sized chunks. | Completed and total work as supplied by the event. |
| `embedding` | Chunk vectors are generated in batches. | Completed and total chunks. |
| `vector_storage` | Qdrant points are written. | Completed and total points. |
| `metadata_commit` | File and section records are committed to SQLite. | Commit status. |

Counters may be `null` for stages that cannot report meaningful totals. A UI
should display the stage/status text even when a percentage cannot be
calculated.

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

```python
details = raven.get_knowledge_details("engineering")

files = await raven.list_knowledge_files("engineering")
first_file = files[0]

sections = await raven.list_file_sections(
    "engineering",
    first_file["file_name"],
)

full_section = await raven.get_knowledge_section(
    "engineering",
    sections[0]["section_id"],
    file_name=first_file["file_name"],
)
print(full_section["raw_content"])
```

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

Library ingestion snapshots are removed when the run exits because the
original trusted source path remains part of retry input. Server uploads are
different: the browser's original file is unavailable to the backend after the
request, so the server retains its private staged copy for
`upload_retry_retention_seconds`. A retry after that deadline fails with
`upload_retry_expired`; the user must upload the file again.

Duplicate behavior is based on the stored file name within one knowledge:

- a concurrent ingestion claim for that file is rejected;
- an already committed file raises `file_already_exists` during a new
  ingestion;
- retry reconciliation recognizes the same committed `file_id` and returns it
  with `already_committed=true` instead of duplicating vectors;
- deleting a file removes its vectors and records before that name can be
  ingested as a new file again.

Each knowledge stores `metadata.json`, `file.sqlite3`, and its local Qdrant
data in the user-scoped knowledge directory. Do not edit those files while
RAVEN is running. Corrupt or unsupported metadata is reported through
`list_discovery_issues()` instead of preventing unrelated valid resources from
starting.

RAVEN persistently stores parsed semantic sections and vectors, not a permanent
copy of the original uploaded document. The ingestion snapshot is temporary.
Applications that need original-file download or page navigation must retain
the original file in their own managed storage and associate it with the
returned `file_id`/file name. Reconstruction uses RAVEN's stored section text
and provenance; it does not require the original binary.


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

The strategies perform different work:

- **Embedded** embeds the query, searches Qdrant in one or all knowledges,
  deduplicates chunk hits by section, and returns the highest-ranked complete
  sections.
- **Hierarchical** asks the LLM to score knowledge summaries where needed,
  then scores section metadata in bounded batches. Full section content is
  loaded only for selected section IDs.
- **Vector-conditioned** first uses vector retrieval to select likely files or
  knowledges, then applies hierarchical section scoring only inside that
  narrowed scope.
- **Agreement** independently runs embedded and hierarchical retrieval and
  preserves both views unless they strongly agree.

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

`Raven.retrieve()` accepts:

| Argument | Type | Required/default | Meaning |
| --- | --- | --- | --- |
| `mode` | `RetrievalMode` or string | Required | One exact mode listed above. `"auto"` is not accepted by direct retrieval; it belongs to agent sessions. |
| `user_query` | string | Required | Query used for embeddings and/or LLM relevance scoring. |
| `knowledge_name` | string or `None` | Required for local modes; omit for global modes | Restricts retrieval to one existing knowledge. |
| `top_k` | integer or `None` | Runtime setting, initially `3` | Positive requested result count, bounded by `SystemConfig.max_retrieval_top_k` (initially `25`). |
| `operation` | `Operation` or `None` | New operation | Reuse only when composing retrieval into a parent workflow. |

The high-level facade intentionally exposes one `top_k` rather than every
pipeline-internal scoring-batch control. Advanced callers that need controls
such as scoring batch size, anchor count, or full hierarchical traversal can
use the individual retrieval pipeline classes.

Local modes require `knowledge_name`. Global modes select from all available
knowledge collections and do not require a local scope.

Global does not mean cross-user. It searches all knowledges inside the current
user runtime only. An empty knowledge database or no relevant hits can produce
an empty list; that is a successful retrieval with no evidence, not a storage
failure.

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

Agreement is classified as follows:

| Value | Meaning |
| --- | --- |
| `Strong Agreement` | The ordered embedded and hierarchical result lists are equal. `retrieved_content` is that single list. |
| `Weak Agreement` | Corresponding results come from the same source files but the lists differ. Both lists are retained. |
| `Disagreement` | At least one corresponding result comes from a different source file. Both lists are retained. |

Agreement is not a truth score. It reports whether two retrieval mechanisms
selected the same evidence. The model or application must still evaluate the
content.

`Raven.retrieve()` requires an explicit strategy. Automatic selection belongs
to an agent session: pass `retrieval_mode="auto"` or omit the argument and let
the model choose among the tools permitted for that conversation.

### Retrieval events and failures

Every strategy emits `started`, `completed`, and `failed` events for its own
family. Nested work—such as the embedded and hierarchical stages of agreement
or vector-conditioned retrieval—emits its own correlated child-task events in
the same operation stream. Completion payloads include scope, result count or
agreement type, and selected section IDs where applicable.

Common failures are:

- `knowledge_not_found` for an invalid local knowledge;
- `invalid_retrieval_mode` for an unknown direct mode;
- `invalid_metadata` when local retrieval omits `knowledge_name`;
- `embedding_identity_mismatch` or `embedding_dimension_mismatch` when the
  configured embedding model is incompatible with stored vectors;
- `retrieval_scoring_invalid` when hierarchical structured output cannot be
  validated after its bounded retries;
- provider errors when embedding or LLM scoring fails.

Direct retrieval failures terminate its task. Inside the agent, recoverable
tool failures are returned to the model with a corrective `next_action`, as
described in [Tool results and recoverable errors](#tool-results-and-recoverable-errors).


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

```json
{
  "schema_version": 1,
  "conversation_id": "c36c45ac5575",
  "type": "local",
  "knowledge_name": "engineering",
  "title": "New Conversation",
  "is_titled": false,
  "pinned": false,
  "created_at": "2026-09-20T11:00:00+00:00"
}
```

`type` is derived from `knowledge_name`; callers do not set it separately. A
local conversation cannot be created for a missing knowledge. The knowledge
binding is fixed for the conversation's lifetime; title and pinned state are
mutable.

### Conversation API reference

| Method | Inputs | Return/result | Notes |
| --- | --- | --- | --- |
| `create_conversation(knowledge_name=None)` | Optional existing knowledge | Task result: `Conversation` | Omit the name for global scope. |
| `get_conversation(id)` | Conversation ID | `Conversation` handle | Opens or reuses persistent storage lazily. |
| `get_conversation_details(id)` | Conversation ID | Metadata object | No mutation. |
| `list_conversations()` | None | All metadata records | No mutation. |
| `list_conversations_page(limit, after_conversation_id=None)` | Positive size and optional cursor | One metadata page | Use the final ID as the next cursor. |
| `update_conversation(id, title=None, pinned=None)` | One or both mutable fields | Task result: updated `Conversation` | A manual title sets `is_titled=true`; call `.to_dict()` for metadata. Supplying neither field is a no-op. |
| `delete_conversation(id)` | Conversation ID | Task result on completion | Closes resources and removes persistent data. |
| `get_conversation_messages(id)` | Conversation ID | Complete canonical `ChatMessage` list | Never returns compacted-only model context. |
| `get_conversation_messages_page(id, limit, after_message_id=0)` | Conversation, positive size, numeric cursor | Durable message records | Suitable for UI history pagination. |
| `get_conversation_turn(id, turn_id)` | Conversation and turn UUID | Turn record | Includes stored `AgentRunResult` when available. |
| `get_conversation_turn_messages(id, turn_id)` | Conversation and turn UUID | Canonical messages for one turn | Includes user, tool, and assistant messages. |
| `get_conversation_turn_messages_page(id, turn_id, limit, after_message_id=0)` | Conversation, turn, positive size, numeric cursor | Durable message records for that turn | Uses the same global durable message IDs as full-history pagination. |
| `get_conversation_preferences(id)` | Conversation ID | Preference records | No mutation. |
| `save_conversation_preference(id, text)` | Non-empty preference text | Task result: preference record | Exact duplicate text reuses the existing record. |
| `remove_conversation_preference(id, preference_id)` | Stable preference UUID | Task result: removed record | Fails if the ID is invalid or absent. |
| `session(conversation, ...)` | A `Conversation` owned by this `Raven` | Temporary `Session` | Requires configured models. |

`raven.list_discovery_issues()` returns validation problems found while
discovering both knowledge and conversation directories. A malformed resource
is excluded from normal lists, but other valid resources remain available.

```python
update = await raven.update_conversation(
    conversation.conversation_id,
    title="Thermal protection review",
    pinned=True,
)
updated_conversation = await update.result()

page = raven.list_conversations_page(limit=25)
if page:
    next_page = raven.list_conversations_page(
        limit=25,
        after_conversation_id=page[-1]["conversation_id"],
    )
```

A paged canonical message record is:

```json
{
  "message_id": 17,
  "turn_id": "7ad218ba-44c9-4f3d-b6d5-a870cbcb509c",
  "message_order": 2,
  "message": {
    "role": "tool",
    "content": "{\"ok\":true,\"evidence\":[...]}"
  }
}
```

`message_id` is a durable numeric pagination cursor. `message_order` is the
position inside that turn. `message` is the serialized LlamaIndex
`ChatMessage`; assistant tool-call messages may use structured blocks rather
than plain `content`.

A turn record is:

```json
{
  "turn_id": "7ad218ba-44c9-4f3d-b6d5-a870cbcb509c",
  "operation_id": "7e6d796c-6711-4fc7-a967-78738ecac96a",
  "user_query": "What does the design specify for thermal protection?",
  "result": {
    "operation_id": "7e6d796c-6711-4fc7-a967-78738ecac96a",
    "thinking": "...",
    "response": "...",
    "iteration_limit_reached": false,
    "evidence": [],
    "tool_calls": [],
    "reconstructed_sources": []
  },
  "memory_indexed": true,
  "committed_at": "2026-09-20T11:05:00+00:00"
}
```

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

`SessionRun` is the turn handle:

| Member | Meaning |
| --- | --- |
| `operation_id` | UUID used for status, events, cancellation, and server correlation. |
| `turn_id` | Stable UUID used to persist or idempotently reuse the conversation turn. |
| `status` | Current `OperationStatus` of the session task. |
| `stream` / `events(after_event_id=0)` | Fresh replayable iterator over this run and its nested tasks. |
| `collect()` | Waits for completion and builds `AgentRunResult` from events. |
| `wait()` | Waits and returns terminal status without returning/raising the run result. |
| `cancel()` | Cancels the turn, waits for terminal cancellation, and releases the conversation claim. |

`collect()` returns:

```json
{
  "operation_id": "7e6d796c-6711-4fc7-a967-78738ecac96a",
  "thinking": "accumulated thinking deltas",
  "response": "final assistant response",
  "iteration_limit_reached": false,
  "evidence": [
    {
      "knowledge_name": "engineering",
      "file_name": "system-design.pdf",
      "section_id": "a83fd91c20b4-3"
    }
  ],
  "tool_calls": [
    {
      "call_id": "call-1",
      "step": 1,
      "name": "global_embedded_retrieval",
      "ok": true,
      "ui_summary": {}
    }
  ],
  "reconstructed_sources": []
}
```

`collect()` uses persisted events, so consuming `run.stream` first does not
consume or destroy the final result. A failed or cancelled run raises instead
of returning a partial success object. Use the already received events for any
partial UI display and inspect the operation/task error for the terminal cause.

The event stream is authoritative. `collect()` replays those events into an
`AgentRunResult` containing the accumulated thinking text, response, tool
calls, evidence references, reconstructed sources, and iteration-limit state.

`session.generate_response()` accepts a non-empty `user_query`, an optional
`retrieval_mode`, and—only for component composition—an existing `operation`.
Omitting `retrieval_mode` or passing `"auto"` exposes all retrieval tools valid
for the conversation. Passing an exact mode restricts exposure as described in
[Tool exposure and retrieval constraints](#tool-exposure-and-retrieval-constraints).

`Raven.session()` controls per-session defaults:

| Argument | Default | Constraint/effect |
| --- | --- | --- |
| `conversation` | Required | Must be the actual `Conversation` object owned by this `Raven` instance; an object from another runtime raises `foreign_conversation`. |
| `max_iterations` | Runtime setting, initially `10` | Positive and no greater than the system maximum, initially `50`. |
| `top_k` | Runtime setting, initially `3` | Positive and no greater than the retrieval system maximum, initially `25`. |
| `memory_token_limit` | Runtime setting, initially `4,000` | Positive model-context memory budget. Extremely small values can fail memory initialization. |
| `memory_top_k` | Runtime setting, initially `5` | Positive number of semantic memory messages returned per search. |

Starting a session opens and initializes the conversation's compacted and
vector memory using the currently configured model pair. Closing a session
cancels only runs created through that session; it does not delete or close the
shared persistent conversation owned by `ConversationManager`.

Each run also has a stable `turn_id`. A committed turn contains the user
message, assistant tool-call messages, tool-result messages, and final
assistant response. Internal thinking is streamed for the caller but is not
stored as conversation history.

The canonical message sequence for a tool-using turn is:

```text
user message
assistant tool-call message
tool result message
[additional assistant tool-call and tool result messages]
assistant final response
```

Persisting tool calls and results lets later model context remember what was
used and lets `reconstruct_from_turn()` recover evidence. Thinking deltas are
excluded because they are transient model reasoning, not durable conversation
content.

The first completed user turn triggers a separate title completion when the
conversation is still untitled. That title operation does not enter the chat
history, and later turns do not regenerate it. Applications may update the
title or pinned state explicitly.

Title generation is best-effort: a title failure does not discard an otherwise
successful answer or turn commit. Until generation succeeds or the application
sets a title, metadata remains `title="New Conversation"` and
`is_titled=false`. A manual title update marks the conversation titled so
automatic generation will not overwrite it.

### Turn serialization

Multiple temporary sessions may reference the same conversation, which is
useful when the same account has several browser tabs or application windows.
Only one turn may run against a conversation at a time. A competing turn is
rejected instead of racing message, memory, and preference mutations.

Turns in different conversations can run concurrently.

A competing same-conversation turn fails with `conversation_turn_active`. The
caller should wait for, cancel, or observe the active operation before
submitting another turn. RAVEN does not queue an unbounded backlog for one
conversation because the second turn's context would otherwise be ambiguous.

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

Compaction does not replace or delete canonical messages in
`messages.sqlite3`. It updates the derived model-context summary used by the
LlamaIndex memory layer. Vector memory likewise retains semantic entries for
the original committed user and final assistant messages rather than indexing
the compacted summary.

The agent accesses vector memory through `search_memory(query)`. This is for
questions about prior conversation content, not knowledge-base facts. A memory
result can support conversational continuity but is not counted as source
evidence for a knowledge claim.

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

Preferences are conversation-local, not global. They are injected into the
conversation's system policy when that prompt is next built. Saving a
preference does not rewrite prior turns, and removing one does not erase text
that already appears in canonical messages.

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

`"auto"` and `None` both mean automatic mode; there is no `auto` retrieval
tool. Automatic mode changes which concrete tools are exposed and lets the
agent choose among them. A restricted mode physically omits the other
retrieval tools from the `FunctionAgent` configuration.

The exact retrieval tool names and arguments are:

| Tool | Local-conversation arguments | Global-conversation arguments | Use |
| --- | --- | --- | --- |
| `local_embedded_retrieval` | `query` | `knowledge_name`, `query` | Focused semantic facts in one knowledge. |
| `local_hierarchical_retrieval` | `query` | `knowledge_name`, `query` | LLM metadata reasoning inside one knowledge. |
| `local_vector_conditioned_retrieval` | `query` | `knowledge_name`, `query` | Vector-narrowed contextual reasoning inside one knowledge. |
| `local_agreement_retrieval` | `query` | `knowledge_name`, `query` | Expensive embedded/hierarchical cross-check in one knowledge. |
| `global_embedded_retrieval` | Not exposed | `query` | Focused semantic facts across the user's knowledges. |
| `global_hierarchical_retrieval` | Not exposed | `query` | Global LLM metadata reasoning. |
| `global_vector_conditioned_retrieval` | Not exposed | `query` | Vector-narrowed reasoning across knowledges. |
| `global_agreement_retrieval` | Not exposed | `query` | Most expensive global cross-check. |

In a local conversation, the bound `knowledge_name` is injected by the tool
wrapper and the model cannot replace it. In a global conversation, local tools
require the model to provide an explicit knowledge name.

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

Their argument contracts are:

| Tool | Local conversation | Global conversation | Result/effect |
| --- | --- | --- | --- |
| `list_knowledges` | No arguments; returns only the bound knowledge | No arguments; returns all knowledge metadata | Structural discovery only. |
| `list_files` | No arguments | `knowledge_name` | Returns stored file records in the selected knowledge. |
| `list_sections` | `file_name` | `knowledge_name`, `file_name` | Returns section metadata and IDs without requiring semantic retrieval. |
| `get_section` | `section_id` | `knowledge_name`, `section_id` | Returns metadata plus complete `raw_content`; successful lookup creates evidence. |
| `search_memory` | `query` | `query` | Returns semantically relevant messages from this conversation only. |
| `list_preferences` | No arguments | No arguments | Returns this conversation's preference IDs and text. |
| `save_preference` | `text` | `text` | Persists an explicit enduring preference; applies to the next run. |
| `remove_preference` | `preference_id` | `preference_id` | Removes the exact saved preference; applies to the next run. |

Navigation is not retrieval. Listing knowledges/files/sections answers
structural questions, while `get_section` directly inspects a known section.
Retrieval searches by relevance to an open-ended query.

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
  ],
  "next_action": null
}
```

| Field | Model receives it | Published in `chat.tool_result` | Meaning |
| --- | --- | --- | --- |
| `ok` | Yes | Yes | Whether the tool completed its requested action. |
| `result` | Yes | No | Full model-facing data, including raw section content when needed. |
| `ui_summary` | Yes | Yes | Bounded metadata safe for ordinary UI rendering. |
| `evidence` | Yes | Yes | Deduplicable knowledge/file/section references. |
| `error` | On failure | On failure | Stable safe error payload. |
| `next_action` | When useful | When useful | Concrete correction the model should take after a recoverable error. |

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

Reaching the limit emits `chat.max_iterations` and sets
`AgentRunResult.iteration_limit_reached=true` if an early final response is
successfully produced. If early finalization fails, `collect()` raises and the
operation ends as failed. The harness never reports a silently truncated
successful answer.

### Prompt and preference lifecycle

The harness builds a conversation-specific system policy from stable RAVEN
behavior plus that conversation's current preferences. It does not enumerate
the available tools in prompt text because LlamaIndex supplies the actual tool
schemas to the model. Scope and retrieval restrictions are enforced by the
tool catalogue itself.

The prompt is cached per conversation. Saving or removing a preference
invalidates that conversation's cached prompt; the next run rebuilds it. This
avoids rebuilding an unchanged prompt for every turn while ensuring preference
changes take effect at the correct boundary.


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

### Accepted reconstruction input

`Raven.reconstruct()` accepts three forms:

1. `None` or `[]`, which completes successfully with `[]`.
2. The common retrieval section list. `raw_content` may be present but is not
   required because reconstruction reloads canonical stored sections:

   ```json
   [
     {
       "knowledge_name": "engineering",
       "file_name": "system-design.pdf",
       "section_id": "a83fd91c20b4-3"
     }
   ]
   ```

3. An agreement retrieval object whose `retrieved_content` is either one
   section list or an object containing `embedded_retrieval` and
   `hierarchical_retrieval` lists.

Each reference must contain non-empty `knowledge_name`, `file_name`, and
`section_id` strings. Duplicate references are removed. References are grouped
by knowledge and file, then the complete stored section sequence for each file
is returned with matching IDs highlighted.

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

`reconstruction.file` is emitted once per reconstructed file and contains the
same object that appears in the task result. `reconstruction.completed`
contains `file_count` and the total number of sections included across the
reconstructed files.

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

The turn lookup examines successful persisted tool-result messages and
deduplicates their evidence references. It does not repeat retrieval, and it
does not depend on the original session still existing.

### Reconstruction failures and retry

- malformed input raises `invalid_metadata` before source loading;
- a deleted or unknown knowledge raises `knowledge_not_found`;
- a deleted or unknown file raises `file_not_found`;
- a section ID that does not belong to the named file raises
  `section_not_found` and reports the missing IDs;
- an unknown conversation or turn raises the corresponding conversation/turn
  not-found error.

Direct reconstruction stores normalized evidence as retry input and is
eligible for user-confirmed retry after approved recoverable failures. A
previous turn can also be reconstructed again by calling
`reconstruct_from_turn()` because the evidence remains in canonical tool-result
messages.


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
    buffer += decoder.decode(value, {stream: !done});
    const frames = buffer.split(/\r?\n\r?\n/);
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
      onEvent(event);
      cursor = event.event_id;
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

For a desktop UI, the trusted native process obtains the launch token and
forwards the requests in this order: `POST /provider/models/configure`,
`POST /knowledges`, `POST /knowledges/{name}/files`, follow its `events_url`,
`POST /conversations`, `POST /conversations/{id}/turns`, follow that new
`events_url`, then `GET /conversations/{id}/turns/{turn_id}` and `/messages`
for durable display. All paths in this paragraph are under `/api/v1`.
The UI should store the returned `conversation_id` as its chat identity and
the returned `turn_id` as the durable identity of the generated turn; neither
is the SSE `event_id`.

For a hosted UI, the browser calls its **host backend**, which authenticates
the account and authorizes its UUID. The host backend supplies RAVEN's
per-launch bearer token and the authorized `X-Raven-User-ID` on each internal
request, including SSE reconnects. The browser does not choose that UUID or
receive the launch token. The same sequence above then applies per user.

Within one operation, `operation_id` selects the stream, `task_id` identifies
a root or nested task, `event_id` is the replay cursor, and `call_id` matches
an agent's `chat.tool_call` with its `chat.tool_result`. If a connection drops,
reopen the same operation stream with the last successfully handled event ID;
do not submit the turn a second time just to recover deltas. Direct retrieval
and direct reconstruction are currently Python-facade capabilities; the HTTP
server exposes reconstruction of a persisted turn, not a generic direct
retrieval/reconstruction endpoint.


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


## HTTP request reference

The following `curl` examples use POSIX shell syntax (`curl.exe` in
PowerShell). Set `RAVEN_URL=http://127.0.0.1:8765` and supply `RAVEN_TOKEN`
through your trusted desktop or host process. Each protected request explicitly
sends `Authorization: Bearer $RAVEN_TOKEN`. For hosted use, also send
`X-Raven-User-ID: 4b9bd24b-61de-42ed-9176-d2db91b66702` on every protected
request when `require_user_id_header` is enabled. The UUIDs below are example
path values; replace them with IDs returned by your server. Requests without
`-d` or `-F` have no body. JSON bodies use `Content-Type: application/json`.
Do not pass the process-wide launch token directly to an untrusted browser.

Each response example below identifies its HTTP status and useful fields.
`202 Accepted` commands return an operation reference, not the finished task
result. Most `202` commands use this complete body:

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

The initial status may instead be `queued`. A turn submission uses a distinct
`202` body: `conversation_id`, `turn_id`, `operation_id`, `task_id`, `status`,
and `events_url` (without retry-link fields). The `200` and `201` routes below
wait for their work and return the resource. Errors use the common
`{"error":{"code":"...","message":"...","details":{}},"request_id":"..."}`
envelope; see [Errors](#errors). `/openapi.json` supplies the complete typed
response schemas. The examples use valid request formats; response snippets
marked *excerpt* show selected fields rather than the entire object.

### Health and runtime requests

Health and OpenAPI routes are unauthenticated; all runtime routes require the
bearer token. These requests have empty bodies unless `-d` is shown.

| Request | Success | Meaning |
| --- | --- | --- |
| `curl -i "$RAVEN_URL/api/v1/health/live"` | `200 {"status":"ok"}` | HTTP process is live. |
| `curl -i "$RAVEN_URL/api/v1/health/ready"` | `200 {"status":"ready"}` | Registry is ready; otherwise `503`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/runtime"` | `200` excerpt: `{"started":true,"models_loaded":false,"operation_store_healthy":true}` | User-runtime state; full response also has `user_id`, `closed`, `runtime_config_revision`, and `models`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/runtime/settings"` | `200` complete settings document, initially `{"revision":0,...}` | Read current defaults before editing. |
| `curl -i -X PATCH -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"expected_revision":0,"retrieval_top_k":5}' "$RAVEN_URL/api/v1/runtime/settings"` | `200` complete settings document, now `{"revision":1,"retrieval_top_k":5,...}` | Change only specified fields; stale revision gives `409`. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"expected_revision":1}' "$RAVEN_URL/api/v1/runtime/settings/reset"` | `200` complete defaults, now `{"revision":2,"retrieval_top_k":3,...}` | Restore all runtime defaults. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/runtime/discovery-issues"` | `200 {"items":[]}` when no malformed resource was found | Each nonempty item has `resource_type`, `directory_name`, `code`, and `message`. |

The complete mutable field list and request semantics are in
[RuntimeConfig](#runtimeconfig). `operation_store_healthy: false` needs operator
attention before submitting further work. Invalid settings are `422`, not
background tasks.

### Model requests

Configured-model routes affect this user's `Raven` runtime. Ollama
administration affects the externally managed Ollama service and should be
restricted by a hosted application's authorization layer.

| Request | Success | Meaning |
| --- | --- | --- |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/provider/models/configured"` | `200 {"llm":null,"embedding":null}` before configuration | After configuration, each role has `provider`, `model`, and `role`. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"llm":{"provider":"ollama","model":"qwen3:8b","role":"llm","api_key_ref":null,"options":{}},"embedding":{"provider":"ollama","model":"bge-m3","role":"embedding","api_key_ref":null,"options":{}}}' "$RAVEN_URL/api/v1/provider/models/configure"` | `202` operation reference | Validate and install one LLM/embedding pair. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/providers/ollama/status"` | `200 {"provider":"ollama","status":"available"}` | Check Ollama connection; unavailability gives `503`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/providers/ollama/models"` | `200 {"items":[]}` if no models are installed | Items can include `model`, `modified_at`, `digest`, `size`, and `details`. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"model":"qwen3:8b"}' "$RAVEN_URL/api/v1/providers/ollama/models/inspect"` | `200` inspection object containing `model`, `template`, `modelfile`, `license`, `details`, `model_info`, `parameters`, and `capabilities` | Inspect one model. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"model":"qwen3:8b"}' "$RAVEN_URL/api/v1/providers/ollama/models/pull"` | `202` operation reference | Follow `model.pull.progress` events. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"model":"qwen3:8b"}' "$RAVEN_URL/api/v1/providers/ollama/models/delete"` | `202` operation reference | Permanently remove an installed model. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"keep_alive":"10m"}' "$RAVEN_URL/api/v1/provider/models/configured/llm/preload"` | `202` operation reference | Preload the configured role. Omit body to use Ollama's default; `0` is invalid. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/provider/models/configured/embedding/unload"` | `202` operation reference | Unload the configured role; no body. |

For preload/unload, `{role}` is `llm` or `embedding`; substitute either in
both examples. Cloud adapters have no Ollama VRAM residency. Model names for
inspect/pull/delete go in the JSON body, not a path segment. See
[Model providers](#model-providers) for role, API-key, and switching rules.

### Operation and SSE requests

Use the `operation_id` and `task_id` returned by a `202` command. In these
examples they are `cc9dce11-16ca-42be-b418-18588d03a571` and
`8f708231-4a27-40cb-a51e-477339c167ad`. The default list page size is the
`operation_page_size` system setting (50); `limit` must be positive and cannot
exceed it. Pass a non-null `next_cursor` back in the corresponding `after_*`
query parameter. List status filters accept the documented operation states.

| Request | Success | Meaning |
| --- | --- | --- |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/operations?status=failed&limit=10"` | `200 {"items":[],"next_cursor":null}` if none failed | Newest-first operation page; optional `after_operation_id=<uuid>`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/operation-tasks/retryable?limit=10"` | `200 {"items":[],"next_cursor":null}` if none eligible | Retryable task page; optional `after_task_id=<uuid>`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571"` | `200` operation object with `operation_id`, `name`, `status`, `last_event_id`, times, `error`, and `is_finished` | Inspect terminal state independently of SSE. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571/tasks?status=failed&limit=10"` | `200 {"items":[],"next_cursor":null}` if none failed | Tasks within one operation; optional `after_task_id=<uuid>`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571/tasks/8f708231-4a27-40cb-a51e-477339c167ad"` | `200` task object with `status`, `retry_policy`, `attempt`, `max_attempts`, `attempts_remaining`, `can_retry`, retry links, times, and error | Check a task's eligibility before retry. Retry inputs are never exposed. |
| `curl -i -N -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Last-Event-ID: 5' "$RAVEN_URL/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571/events"` | `200 text/event-stream`, frames such as `id: 6`, `event: chat.response_delta`, `data: {...}` | Replay after event 5, then follow live updates. Omit `Last-Event-ID` to start at the beginning. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571/cancel"` | `200` operation object; status may still be transitioning | Request cooperative cancellation; no request body. Recheck operation/task status. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571/tasks/8f708231-4a27-40cb-a51e-477339c167ad/retry"` | `202` new operation reference with `retry_of_operation_id` and `retry_of_task_id` populated | User-confirmed retry; no request body. The original operation is unchanged. |

An SSE frame has `id`, typed `event`, and a JSON `data` envelope with
`event_id`, `operation_id`, `task_id`, `task_name`, `type`, ISO timestamp,
payload `data`, and `is_final`. Heartbeats are SSE comments, not JSON events.
The stream closes after a final event. Invalid cursor gives `400`; an expired
replay gap gives `410`; a cleaned-up operation gives `404`. See
[Replay after disconnection](#replay-after-disconnection) and
[Streaming operation events](#streaming-operation-events) for the client flow.

### Knowledge, ingestion, and navigation requests

The `name` path value below is `engineering`. File deletion uses a `file_id`;
section navigation uses the stored `file_name`, which must be URL-encoded if
it contains spaces or reserved characters. List endpoints return
`{"items":[...],"next_cursor":null}` when the final page is reached.
Non-null cursors belong in the next request's matching `after_*` parameter.
`limit` defaults to `operation_page_size` and may not exceed it.

| Request | Success | Meaning |
| --- | --- | --- |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges?limit=10"` | `200 {"items":[],"next_cursor":null}` if none exist | Knowledge summaries; optional `after_name=engineering`. Items include `name`, `user_summary`, `count`, and `created_at`. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"name":"engineering","user_summary":"Engineering reference documents"}' "$RAVEN_URL/api/v1/knowledges"` | `201` metadata with `schema_version`, `name`, `created_at`, and `user_summary` | Create; duplicate name gives `409`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges/engineering"` | `200` same knowledge metadata object | Inspect an existing knowledge. |
| `curl -i -X PATCH -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"user_summary":"Updated engineering documents"}' "$RAVEN_URL/api/v1/knowledges/engineering"` | `200` updated metadata object | Replace its summary; an empty string is allowed. |
| `curl -i -X DELETE -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges/engineering"` | `202` operation reference | Delete the knowledge and its files/vectors. Irreversible without backup. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges/engineering/files?limit=10"` | `200 {"items":[],"next_cursor":null}` if empty | Files; optional `after_file_id=<file-id>`. Each item has `file_id`, `file_name`, counts, `ingested_at`, and `navigation_type`. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -F 'file=@system-design.pdf;type=application/pdf' "$RAVEN_URL/api/v1/knowledges/engineering/files"` | `202` operation reference | Browser-safe upload. The required multipart field is **`file`**; do not manually set a boundary. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"source_path":"/srv/raven-imports/system-design.pdf"}' "$RAVEN_URL/api/v1/knowledges/engineering/ingest-path"` | `202` operation reference | Trusted server-local path only; disabled by default and limited to allowed roots. No upload bytes in this request. |
| `curl -i -X DELETE -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges/engineering/files/a83fd91c20b4"` | `202` operation reference | Delete by **file ID**, not filename. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges/engineering/files/system-design.pdf/sections?limit=10&after_section_index=0"` | `200 {"items":[],"next_cursor":null}` if none | Sections in increasing index order. Use returned numeric cursor in `after_section_index`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges/engineering/files/system-design.pdf/sections/a83fd91c20b4-3"` | `200` full section with `section_id`, `file_id`, `file_name`, `section_index`, `raw_content`, `summary`, `keywords`, `conditions`, `definitions`, `source_element_ids`, and `source_range` | Inspect exact text and provenance; wrong file/section pairing gives `404`. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/knowledges/engineering/stats"` | `200 {"vector_count":42,"file_count":1}` | Count stored files and vector points. |

Accepted filenames are simple `.txt`, `.md`, `.docx`, or `.pdf` names; paths,
control characters, reserved device names, and unsupported extensions are
rejected. The configured source-file and request-size limits also apply. A
successful upload is removed from staging after ingestion. A failed upload
remains private and retryable only within `upload_retry_retention_seconds`;
after expiry, upload again. Duplicate file ingestion gives a conflict rather
than silently replacing data. A failed/interrupted ingestion may be retried
through its task if `can_retry` is true; RAVEN reconciles pending vector and
metadata state. See [Ingestion safety and retry](#ingestion-safety-and-retry).

### Conversation, turn, and preference requests

The examples use conversation ID `c36c45ac5575`, turn ID
`c4d242e9-f6e9-4a84-8cdc-e7bf5c8a3021`, and preference ID
`562fe22a-8eea-4f24-bb81-eeb0f663fd4d`. Use the IDs returned by your own
server. A conversation's `type` is computed from `knowledge_name`: `null` is
global, a named knowledge is local. The server creates a temporary session for
a turn; there is no session HTTP resource.

| Request | Success | Meaning |
| --- | --- | --- |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations?limit=10"` | `200 {"items":[],"next_cursor":null}` if none exist | Newest-first metadata; optional `after_conversation_id=<id>`. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"knowledge_name":null}' "$RAVEN_URL/api/v1/conversations"` | `201` metadata with `conversation_id`, `type:"global"`, `knowledge_name:null`, `title`, `is_titled`, `pinned`, and `created_at` | Create global. Use `"knowledge_name":"engineering"` for local. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575"` | `200` full conversation metadata | Reload a conversation. |
| `curl -i -X PATCH -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"title":"Thermal design","pinned":true}' "$RAVEN_URL/api/v1/conversations/c36c45ac5575"` | `200` updated metadata | Supply `title`, `pinned`, or both; scope cannot be changed. |
| `curl -i -X DELETE -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575"` | `202` operation reference | Delete conversation data; back up first if needed. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575/messages?limit=10&after_message_id=0"` | `200 {"items":[],"next_cursor":null}` if empty | Canonical, uncompacted transcript. Messages include role, content, turn/order, tool calls, and tool-call identifiers. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575/turns/c4d242e9-f6e9-4a84-8cdc-e7bf5c8a3021"` | `200` turn with `turn_id`, `operation_id`, `user_query`, `result`, `memory_indexed`, `committed_at` | Load a durable turn. Result includes thinking, response, evidence, tool calls, and reconstructed sources. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575/turns/c4d242e9-f6e9-4a84-8cdc-e7bf5c8a3021/messages?limit=10&after_message_id=0"` | `200 {"items":[],"next_cursor":null}` if none | Canonical messages for only this turn, paginated by message ID. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"user_query":"What protects the motor from overheating?","retrieval_mode":"auto"}' "$RAVEN_URL/api/v1/conversations/c36c45ac5575/turns"` | `202` reference plus `conversation_id` and new `turn_id` | Start generation. `retrieval_mode` can be omitted, `auto`, or an allowed exact mode. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575/turns/c4d242e9-f6e9-4a84-8cdc-e7bf5c8a3021/reconstruction"` | `202` operation reference | Reconstruct persisted turn evidence; no body. A casual turn may return an empty result. |
| `curl -i -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575/preferences"` | `200 {"items":[]}` if none | Items contain exact `preference_id` and `text`. |
| `curl -i -X POST -H "Authorization: Bearer $RAVEN_TOKEN" -H 'Content-Type: application/json' -d '{"text":"Prefer concise explanations."}' "$RAVEN_URL/api/v1/conversations/c36c45ac5575/preferences"` | `201` preference with `preference_id` and `text` | Save conversation-scoped preference. |
| `curl -i -X DELETE -H "Authorization: Bearer $RAVEN_TOKEN" "$RAVEN_URL/api/v1/conversations/c36c45ac5575/preferences/562fe22a-8eea-4f24-bb81-eeb0f663fd4d"` | `200` removed preference object | Delete by stable ID, not matching text; no body. |

An accepted turn's `events_url` is the UI's live source of thinking, tool
calls/results, reconstruction, and answer deltas. Fetch the turn and messages
after completion for durable display. Two simultaneous turns on the same
conversation are rejected with `409`; different conversations may run at
once. See [Conversations, sessions, and memory](#conversations-sessions-and-memory).


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


## Backup, restore, and cleanup

The safest consistent backup is an **offline copy of the complete RAVEN home**:
stop accepting new work, wait for or cancel active operations, stop the one
RAVEN server process, and only then copy the home to protected storage. This
keeps each user's knowledge SQLite files, Qdrant directories, conversation
SQLite files, operations/event SQLite store, metadata, and runtime settings
together. Do not copy only `file.sqlite3` without its corresponding Qdrant
directory, or only `messages.sqlite3` without conversation metadata and
memory storage. A live file-by-file copy is not a transactionally consistent
backup across these stores. The externally managed Ollama installation and its
model cache are separate and are **not** included in a RAVEN-home backup.

To restore, stop RAVEN, copy the complete saved home into the intended home
path, ensure the server identity can read/write it, then start one server with
`nraven serve --home <restored-home>`. If you used an explicit
`--system-settings` path outside the home, restore that file separately. Verify
`/api/v1/health/ready`, the user runtime status, knowledge/conversation lists,
and operation discovery issues before opening traffic. The launch bearer token
is per start and must be obtained again; do not reuse a token from a backup.
Provider API keys must be restored through the host's environment/secret
manager. Reconfigure the runtime model pair after restart.

This is a backup/restore procedure for the **same storage schema**. Importing
one user's directory into a different account, combining two homes, or
migrating across incompatible schema versions is not an automatic public API.
Keep a pre-upgrade backup and test new releases on a copy before replacing a
production home. Malformed resources are reported by
`GET /api/v1/runtime/discovery-issues`; do not edit SQLite or Qdrant files
under a live process to repair them.

Operation/event history becomes eligible for automatic removal after
`operation_retention_seconds`; the cleanup service runs every
`operation_cleanup_interval_seconds` and removes at most
`operation_cleanup_batch_size` expired operations per pass. This is a
retention policy, not a promise that rows disappear at the exact deadline.
Private staged browser uploads have their own
`upload_retry_retention_seconds` window; successful uploads are removed after
ingestion, while failed sources expire and are cleaned up. After that, the
original task may still appear in history but its upload can no longer be
retried: the client must upload again. Never delete individual live database
files as a substitute for configured cleanup.


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


## Server troubleshooting

Start with the HTTP status and the response `error.code`, then use
`X-Request-ID` to find the corresponding server log entry. For accepted
background work, inspect the operation and task status and their event stream;
the original `202` response is not proof of completion.

| Symptom | Check | Action |
| --- | --- | --- |
| Server will not start | `--home` was omitted, settings JSON is invalid, port is occupied, or another process owns the home lock | Supply a writable explicit home. Validate/edit `system_settings.json` while stopped. Keep one RAVEN worker per home; do not remove a lock while its owner still runs. |
| `/live` works but `/ready` gives `503` | Runtime registry not ready | Wait for startup or inspect startup logs. Do not begin ingestion or chat until ready. |
| `401 authentication_required` | Missing/wrong per-launch bearer token, or required hosted user ID omitted | Obtain the **current** token from the trusted owner and add the required `X-Raven-User-ID`. A restart rotates the token. |
| `400 invalid_user_id` | Hosted header is not a UUID | Fix the host's account-to-UUID mapping; do not accept a browser-supplied UUID as authorization. |
| `403 host_not_allowed` or `origin_not_allowed` | Request host or browser Origin not in immutable allowlist | Add the intended exact host/origin to system settings and restart; do not open a wildcard merely to silence the error. |
| `503 ollama_unavailable` | Ollama 0.34.2 is not running at configured `OLLAMA_HOST` | Start/check the separately managed Ollama service, its address, and firewall; then retry the provider check. |
| Cloud model fails | Provider key reference absent or key unavailable to the RAVEN process | Ensure the referenced environment variable is present **before** server startup; do not put the secret value in `ModelSpec.options`. Check provider quotas/capabilities separately. |
| `409` during ingestion | A file with the same identity/name is already present or active | Inspect `/files`; delete the existing file deliberately before re-ingesting, or wait for active ingestion to finish. |
| Upload gives `413`, `422`, or trusted path is refused | File/request limit, invalid filename, unsupported format, disabled trusted path, or path outside allowed roots | Check source extension/name and byte limits; for browser files use multipart `file`, not server-local path. Change immutable path policy only after operator review and restart. |
| Turn gives `409 conversation_turn_active` | Another session is already processing that conversation | Wait for its operation to finish/cancel, then submit a new turn. Separate conversations may run concurrently. |
| Operation gives `429` | Per-user or global active-operation limit reached | Wait/cancel old work; only increase limits after capacity planning. |
| SSE reconnect gives `400`, `410`, or `404` | Invalid `Last-Event-ID`, expired replay gap, or fully cleaned operation | Send a decimal cursor; on `410`, fetch operation/turn state and rebuild the UI, because missing deltas cannot be replayed. On `404`, the operation is gone. |
| A server crash left an operation interrupted | No worker survives process termination | Inspect retry eligibility; explicitly retry eligible tasks. Do not assume a started-but-unfinished operation succeeded. |
| `503 operation_database_in_use`, storage failure, or corrupt resource | Another process owns the local database, or storage is unhealthy | Stop conflicting processes, check filesystem permissions/free space, make an offline backup, and inspect discovery issues. Do not manually modify live SQLite/Qdrant files. |

Readiness is not model readiness: `/ready` reports that the runtime registry
can serve requests; `/api/v1/runtime` shows whether **this user's** models
are configured. A hosted deployment should keep RAVEN behind its authenticating
backend and not expose the shared launch token or internal user UUID mapping
to a public browser.


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

### Additional `Raven` facade methods

The main workflows above introduce the commonly used methods. These public
facade methods support custom orchestration, pagination, inspection, and
administration without requiring direct component ownership:

| Method and required inputs | Result and use |
| --- | --- |
| `raven.runtime_config` | Current immutable `RuntimeConfig` snapshot; read-only. Use `update_runtime_settings()` to persist a change. |
| `await raven.create_operation(name)` | A new `Operation`; caller starts its tasks with `operation.run(name, worker)`. |
| `await raven.submit_operation(name, worker)` | Creates an operation and starts a root task; returns the `Operation`. `worker` is an async function taking that operation. Inspect events/status or await its completion. |
| `await raven.list_operation_tasks(operation_id, status=None, limit=None, after_task_id=None)` | A page of `OperationTaskRecord` values for one operation; use the final task ID as the next cursor. Unknown operation raises a not-found error. |
| `await raven.list_knowledges_page(limit=10, after_name=None)` | List of knowledge summary dictionaries, ordered for cursor pagination; the caller chooses a positive page size. |
| `await raven.update_knowledge(name, user_summary="...")` | `OperationTask`; await `.result()` before reading the revised summary. Missing knowledge fails. |
| `await raven.list_file_sections_page(knowledge_name, file_name, limit=10, after_section_index=0)` | Section dictionaries after the given index; an unknown file raises `file_not_found`. |
| `await raven.count_knowledge_vectors(knowledge_name)` | Integer number of vector points in that knowledge. |
| `await raven.count_knowledge_files(knowledge_name)` | Integer number of stored files. |
| `await raven.delete_knowledge(name)` | `OperationTask`; await `.result()` and observe events. Removes the knowledge's storage; back up first. |
| `raven.get_conversation_details(conversation_id)` | Persistent metadata dictionary, including scope and title; missing ID raises `conversation_not_found`. |
| `await raven.get_conversation_messages_page(conversation_id, limit=10, after_message_id=0)` | Canonical message records after the cursor, not compacted model context. |
| `await raven.get_conversation_turn(conversation_id, turn_id)` | Durable turn dictionary; missing turn raises `conversation_turn_not_found`. |
| `await raven.get_conversation_turn_messages(conversation_id, turn_id)` | LlamaIndex `ChatMessage` objects belonging to that turn. |
| `await raven.get_conversation_turn_messages_page(conversation_id, turn_id, limit=10, after_message_id=0)` | Canonical per-turn message records with IDs suitable for paging a UI. |
| `await raven.get_conversation_preferences(conversation_id)` | List of `{preference_id, text}` dictionaries for that conversation. |
| `await raven.delete_conversation(conversation_id)` | `OperationTask`; await `.result()` and observe events. Deletes persisted transcript, memory, and preferences. |

For a long-running method, the returned `OperationTask` is the handle:
`await task.result()` retrieves completion or raises its error, while
`task.operation_id` selects its replayable events. Read-only methods return
their values directly. Pagination methods return a bounded list, not an HTTP
`{items,next_cursor}` envelope; that envelope is added by FastAPI.


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
