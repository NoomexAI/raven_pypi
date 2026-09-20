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
        task = await raven.load(
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

Loading verifies both adapters and checks the embedding model against existing
knowledge stores before installing the new pair. RAVEN then unloads replaced
Ollama models that are no longer selected and preloads newly configured Ollama
models. If pre-installation validation fails, the previous configured pair
remains active.

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
        task = await raven.load(
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

Call `Raven.load()` with a new valid pair to switch models. The replacement is
serialized and validated before it becomes active:

```python
task = await raven.load(new_llm_spec, new_embedding_spec)
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
