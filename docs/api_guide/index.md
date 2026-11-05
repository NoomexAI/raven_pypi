---
layout: default
title: API guide
description: "Understand RAVEN's Python API: prepare documents, manage conversations, inspect sources, and observe the work behind an answer."
scripts:
  - /assets/js/code-disclosure.js
---

# Using RAVEN's Python API

RAVEN provides the document and conversation backend for applications that
answer questions using their own source material. An application supplies
the interface; RAVEN prepares documents for search, connects that search to
a model, saves the discussion, and makes the supporting sources available
for inspection.

The Python API is how an application requests that work. This guide begins
with what the API offers and how its methods relate to one another. The
following sections explain those methods and their use in detail. The guide
covers RAVEN v0.2.0, installed as `noomexai-raven` and imported as `nraven`.

<nav class="page-links" aria-label="On this page" markdown="1">

## On this page

The high-level API connects the backend's components for the application.
The component API gives the application direct control over that connection.
These are two ways of using RAVEN, not successive setup stages.

<details class="page-links-group" markdown="1">
<summary>High-level API</summary>

- [Overview of the Raven API](#raven-api-overview) — The capabilities available
  through `Raven`, the methods serving them, and where they fit in an application.
  Section 1.
- [Creating, starting, and closing Raven](#raven-lifecycle) — Choose the home
  and user, prepare the workspace, read its state, and release its resources.
  Section 2.
- [System settings, paths, and runtime defaults](#raven-configuration) — Supply
  fixed policy, locate saved data, and apply persistent defaults with revision
  checks. Section 3.
- [Models and provider utilities](#raven-models) — Configure the language and
  embedding models, supply cloud credentials, and manage Ollama models.
  Section 4.
- [Knowledge, ingestion, and navigation](#raven-documents) — Create document
  stores, ingest a file with progress events, inspect its sections, and manage
  saved files. Section 5.
- [Retrieval and source reconstruction](#raven-retrieval) — Choose a direct
  search, interpret agreement, and build source views. Section 6.
- [Conversations, history, and preferences](#raven-conversations) — Manage saved
  discussions, page through actual messages, and use preference IDs. Section 7.
- [Sessions and streamed responses](#raven-sessions) — Run a turn, separate its
  events, inspect saved sources, and handle completion or cancellation. Section 8.
- [Operations, tasks, and event history](#raven-operations) — Share one
  operation across calls, inspect its status, and replay events. Section 9.
- [Errors, retry, and cleanup](#raven-failures) — Handle failures, request
  eligible retries, and explicitly delete expired operation history. Section 10.

</details>

<details class="page-links-group" markdown="1">
<summary>Component API</summary>

- [Overview of the component API](#component-api-overview) — What direct
  composition adds, when it is useful, and which responsibilities move to
  the application. Section 11.
- [Constructing and closing a workspace](#component-workspace) — Share
  managers, recover interrupted work, and close owned resources. Section 12.
- [Model adapters for components](#component-adapters) — Obtain adapters,
  configure provider clients, and supply models to individual stages. Section 13.
- [Working directly with knowledge](#component-knowledge) — Read stored
  records, inspect chunk matches, and manage open storage. Section 14.

</details>

</nav>

<div class="page-jump-target" id="raven-api-overview" aria-hidden="true"></div>

## 1. Overview of the Raven API

The `Raven` class is the high-level entry point for an application. It
connects the backend's parts and exposes their work through one object.
This section introduces the capabilities available through that interface;
the following sections explain how to use them.

The Python blocks in this overview are **signature references**, like the
declarations shown by an editor. The top-right label identifies the class
and method. `self` is the instance supplied automatically by Python, `*`
marks arguments passed by name, and the arrow shows the declared return
type. These declarations describe the interface; they are not standalone
programs. The method-focused sections provide runnable usage examples.

### 1.1 One entry point into a persistent workspace

A document assistant needs more than a model connection. Documents must be
stored and prepared for search, conversations must survive application
restarts, and the application must be able to tell whether requested work
has finished. The `Raven` class connects the backend components responsible
for these tasks and exposes methods through one object.

That object works with a **home**: a directory chosen by the application for
RAVEN's saved data. Constructing `Raven` identifies the home and the user whose
data will be used.

**`await raven.start()` prepares the workspace:**

- Creates the required storage directories when they are missing.
- Loads the user's runtime settings, or creates the defaults file, and
  validates those values against system policy.
- Opens operation history and marks unfinished operations and tasks from a
  previous process termination as failed. Eligible tasks can be retried
  explicitly; their workers are not restarted automatically.
- Discovers existing knowledges and conversations for the selected user.

Startup does not configure models, launch a model server, or open an HTTP port.

**`await raven.close()` releases the runtime's resources:**

- Cancels active operations.
- Checks unfinished ingestion writes for vectors that need removal and
  attempts to finish recorded file deletions.
- Attempts to close the instance's database handles and provider connections.
  If cleanup steps fail, it attempts the remaining steps before reporting
  the failures.
- Leaves stored document content and conversations on disk. An external
  Ollama server continues running.

This separates the application's lifetime from its data. A later launch can
start a new `Raven` instance using the same home and user ID to access what
was saved there.

<figure class="code-snippet api-signature" id="raven-init" markdown="1">
<figcaption class="code-snippet-filename">Raven.__init__()</figcaption>

```python
def __init__(
    self,
    raven_home: str | Path,
    user_id: UUID | str = DEFAULT_USER_ID,
    *,
    system_config: SystemConfig | None = None,
) -> None
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.start()</figcaption>

```python
async def start(self) -> None
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.close()</figcaption>

```python
async def close(self) -> None
```

</figure>

### 1.2 Choosing the models that perform the work

RAVEN uses models for two different purposes. A **language model** generates
answers, reasons about source material, and can choose tools. An **embedding
model** represents text as numerical vectors so passages can be compared by
similarity. The two roles can use different providers.

`Raven.configure_models()` sets up the language and embedding model pair used
by the backend. Each choice is described by a `ModelSpec`, which identifies
its provider, model, role, and any supported options or credential reference.
The method prepares and validates the model adapters, the Python objects
through which RAVEN calls the models, and connects them to its pipelines.
For Ollama, it also requests unloading of previously configured models that
are no longer in the pair, then preloads the configured embedding and language
models. Cloud providers have no corresponding local unload or preload action.

A local Ollama model and a supported cloud model are alternative ways to
supply a role; the document and conversation APIs remain the same. Existing
knowledge vectors must remain compatible with their original embedding model;
configuring another pair does not regenerate those vectors.

<figure class="code-snippet api-signature" id="model-spec" markdown="1">
<figcaption class="code-snippet-filename">ModelSpec</figcaption>

```python
class ModelSpec(
    *,
    provider: str,
    model: str,
    role: ModelRole,
    api_key_ref: str | None = None,
    options: dict[str, Any] = dict,
)
```

</figure>

This is the class's editor-style construction signature. `provider` selects
the provider and `model` identifies the model. `role` is `ModelRole.LLM`
(`"llm"`) or `ModelRole.EMBEDDING` (`"embedding"`). `api_key_ref` names an
environment variable containing the API key, rather than containing the key
itself. `options` supplies supported settings to the LiteLLM provider path.
For Ollama, it must currently remain empty: the provider interface rejects
nonempty options. `dict` in this signature denotes a default factory that
creates a fresh empty dictionary for each specification, not a value that
needs to be passed explicitly.

<figure class="code-snippet api-signature" id="raven-configure-models" markdown="1">
<figcaption class="code-snippet-filename">Raven.configure_models()</figcaption>

```python
async def configure_models(
    self,
    llm_spec: ModelSpec,
    embedding_spec: ModelSpec,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

Supporting methods handle provider-specific needs. For example,
`Raven.list_ollama_models()` discovers locally available models and
`Raven.pull_ollama_model()` requests a download when one is missing. These
utilities do not make RAVEN the owner of the Ollama server process.
The preload requests ask Ollama to load the configured models, but do not
guarantee that both models remain in GPU memory. Placement and eviction are
controlled by Ollama and the machine's available resources. An adapter held
by RAVEN can remain configured even when Ollama has evicted its model.

<figure class="code-snippet api-signature" id="raven-list-ollama-models" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_ollama_models()</figcaption>

```python
async def list_ollama_models(
    self,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" id="raven-pull-ollama-model" markdown="1">
<figcaption class="code-snippet-filename">Raven.pull_ollama_model()</figcaption>

```python
async def pull_ollama_model(
    self,
    model: str,
    on_progress: ProgressCallback | None = None,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

### 1.3 Bringing documents into a knowledge

Documents need an identifiable place in the workspace. RAVEN calls that
place a **knowledge**: a named store of related document content, metadata,
and search vectors. An application might keep engineering references in one
knowledge and narrative material in another. Together, the knowledges managed
by the runtime form its **knowledge base**.

`Raven.create_knowledge()` creates one of these stores.
After `Raven.start()`, `Raven.get_knowledge()` returns the managed `Knowledge`
object for an existing knowledge, while
`Raven.list_knowledges()` returns summaries that include their names.
Creating a knowledge does not add documents to it. That is the role of
`Raven.ingest()`, which prepares a source file and stores its searchable
representation in the chosen knowledge.

<figure class="code-snippet api-signature" id="raven-create-knowledge" markdown="1">
<figcaption class="code-snippet-filename">Raven.create_knowledge()</figcaption>

```python
async def create_knowledge(
    self,
    name: str,
    user_summary: str = "",
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_knowledge()</figcaption>

```python
def get_knowledge(self, name: str) -> Knowledge
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_knowledges()</figcaption>

```python
async def list_knowledges(self) -> list[dict[str, Any]]
```

</figure>

Ingestion reads the supported source format, divides its content into
sections, extracts section metadata, and creates the embedding chunks used
for vector search. It also retains the relationships needed to trace a
retrieved section back to its file. The application does not need to perform
those stages itself before calling the ingestion API.

<figure class="code-snippet api-signature" id="raven-ingest" markdown="1">
<figcaption class="code-snippet-filename">Raven.ingest()</figcaption>

```python
async def ingest(
    self,
    knowledge_name: str,
    source_path: str | Path,
    *,
    breakpoint_percentile_threshold: int | None = None,
    buffer_size: int | None = None,
    max_extraction_retries: int | None = None,
    chunk_size: int | None = None,
    chunk_overlap: int | None = None,
    max_source_size_bytes: int | None = None,
    max_document_pages: int | None = None,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

Saved material can also be browsed without asking a model a question.
`Raven.list_knowledge_files()` shows the files in a knowledge;
`Raven.list_file_sections()` and `Raven.get_knowledge_section()` expose their
stored sections. Related management methods update descriptions or remove
files and knowledges. These calls support document browsers and maintenance
interfaces as well as chat applications.

<figure class="code-snippet api-signature" id="raven-list-knowledge-files" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_knowledge_files()</figcaption>

```python
async def list_knowledge_files(
    self,
    knowledge_name: str,
) -> list[dict[str, Any]]
```

</figure>

<figure class="code-snippet api-signature" id="raven-list-file-sections" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_file_sections()</figcaption>

```python
async def list_file_sections(
    self,
    knowledge_name: str,
    file_name: str,
) -> list[dict[str, Any]]
```

</figure>

<figure class="code-snippet api-signature" id="raven-get-knowledge-section" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_knowledge_section()</figcaption>

```python
async def get_knowledge_section(
    self,
    knowledge_name: str,
    section_id: str,
    *,
    file_name: str | None = None,
) -> dict[str, Any]
```

</figure>

### 1.4 Turning a question into a continuing discussion

A **conversation** is the saved record of a discussion, including its
messages and tool interactions. `Raven.create_conversation()` creates that
record. It can be associated with one knowledge or left global so that the
agent can investigate across the user's knowledge base.

<figure class="code-snippet api-signature" id="raven-create-conversation" markdown="1">
<figcaption class="code-snippet-filename">Raven.create_conversation()</figcaption>

```python
async def create_conversation(
    self,
    knowledge_name: str | None = None,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

The conversation itself is not the active connection through which a question
is submitted. `Raven.session(conversation)` creates a temporary **Session**
for that interaction. After the session is started,
`Session.generate_response()` accepts a question and starts a turn: one
user message and the work leading to its answer. This method belongs to the
session returned by `Raven`, rather than being another method on `Raven`.

<figure class="code-snippet api-signature" id="raven-session" markdown="1">
<figcaption class="code-snippet-filename">Raven.session()</figcaption>

```python
def session(
    self,
    conversation: Conversation,
    *,
    max_iterations: int | None = None,
    top_k: int | None = None,
    memory_token_limit: int | None = None,
    memory_top_k: int | None = None,
) -> Session
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Session.start()</figcaption>

```python
async def start(self) -> None
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Session.generate_response()</figcaption>

```python
async def generate_response(
    self,
    user_query: str,
    *,
    retrieval_mode: RetrievalMode | str | None = None,
    operation: Operation | None = None,
    retry_of: OperationTaskRecord | None = None,
) -> SessionRun
```

</figure>

During a turn, the agent can choose permitted retrieval tools, obtain source
material, and generate a response. The application can show the work as it
happens rather than waiting for the entire answer. RAVEN manages the
conversation context. For a successful turn, the session saves the user
message, tool interactions, and assistant response after the agent finishes
and before the session task completes. Seeing response text stream to the
interface does not mean that transcript has already been saved. The interface
does not need to recreate the agent loop or maintain a separate model transcript.

Closing a session leaves its conversation intact. An application can later
use `Raven.list_conversations()` to find a saved discussion,
`Raven.get_conversation()` to access it, and
`Raven.get_conversation_messages()` to display its actual history. Preferences
saved through the conversation APIs guide that particular discussion, while
conversation memory provides a compacted model context and a way to search
earlier turns as the saved history grows.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_conversations()</figcaption>

```python
def list_conversations(self) -> list[dict[str, Any]]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation()</figcaption>

```python
def get_conversation(self, conversation_id: str) -> Conversation
```

</figure>

<figure class="code-snippet api-signature" id="raven-get-conversation-messages" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation_messages()</figcaption>

```python
async def get_conversation_messages(
    self,
    conversation_id: str,
) -> list[ChatMessage]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Session.close()</figcaption>

```python
async def close(self) -> None
```

</figure>

### 1.5 Finding sources directly and making them inspectable

Chat is one use of the prepared documents, not the only one.
`Raven.retrieve()` lets an application request relevant sections directly,
without generating an assistant response. It can search one knowledge or the
whole knowledge base using embedded, hierarchical, vector-conditioned, or
agreement retrieval. These mechanisms choose source material; they do not
answer the question themselves.

<figure class="code-snippet api-signature" id="raven-retrieve" markdown="1">
<figcaption class="code-snippet-filename">Raven.retrieve()</figcaption>

```python
async def retrieve(
    self,
    mode: RetrievalMode | str,
    user_query: str,
    *,
    knowledge_name: str | None = None,
    top_k: int | None = None,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

A retrieved passage still needs context if a reader is to check it.
`Raven.reconstruct()` takes source references and builds readable views of
their files with the selected sections highlighted. For an answer that has
already been saved, `Raven.reconstruct_from_turn()` identifies the sources
through evidence references in that turn's saved tool-result messages, using
its conversation and turn IDs. The referenced knowledge, file, and sections
must still be available; reconstruction reports an error if they have been
removed. A turn without usable evidence references, such as a casual exchange,
returns an empty list of reconstructed files. This lets an interface offer
source inspection for an earlier answer, not only the latest response on screen.

<figure class="code-snippet api-signature" id="raven-reconstruct" markdown="1">
<figcaption class="code-snippet-filename">Raven.reconstruct()</figcaption>

```python
async def reconstruct(
    self,
    retrieval_result: list[dict[str, Any]] | dict[str, Any] | None,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" id="raven-reconstruct-from-turn" markdown="1">
<figcaption class="code-snippet-filename">Raven.reconstruct_from_turn()</figcaption>

```python
async def reconstruct_from_turn(
    self,
    conversation_id: str,
    turn_id: UUID | str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

Reconstruction is intended to show what material supported the investigation,
what surrounded it, and where the reader can navigate in the original source
when navigation information is available. It does not promise an exact visual
copy of the original document or certify every claim in the model's answer.

### 1.6 Following work instead of treating a request as a result

Document ingestion and response generation can take time. An application
needs to distinguish a submitted request from completed work, show progress,
and report a failure without losing track of the action involved.

RAVEN represents a complete action as an **operation**, with an identity,
status, and history of events. An **operation task** represents a particular
method's work within that action and provides access to its result. An
**event** is a structured update about what happened. For ingestion, events
can describe the current processing stage; for chat, they distinguish tool
activity, response text, and other outputs.

Awaiting an operation-producing method, such as `Raven.ingest()`, returns an
`OperationTask` handle; it does not by itself wait for that task's work to
finish. `await task.result()` waits for completion and returns the method's
result, or raises the task's error. Ordinary inspection methods can return
values directly instead, so the detailed sections explain each contract.

A session turn returns a `SessionRun`. Its `.stream` exposes the events as
work happens, while `await run.collect()` waits for an `AgentRunResult`.
That result contains response text, thinking text, tool calls, evidence,
reconstructed sources, and an iteration-limit flag. Reading events or
collecting the result observes the same execution; neither starts another
inference.

The operation methods support interfaces that must keep working after the
initial request. `Raven.get_operation()` finds an operation,
`Raven.operation_events()` exposes its retained events, and
`Raven.cancel_operation()` requests that its work stop. If an action fails,
`Raven.list_retryable_tasks()` lists failed tasks whose saved retry policy
allows another attempt. `Raven.retry_task()` checks the remaining
prerequisites when retry is requested, including required models, retained
uploads where applicable, and whether that task already has a retry. Appearing
in the list does not guarantee that a later retry can start or succeed.
Interrupted work is marked as failed at startup; eligible work can then be
retried explicitly rather than being resumed automatically.

<figure class="code-snippet api-signature" id="raven-get-operation" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_operation()</figcaption>

```python
async def get_operation(self, operation_id: UUID | str) -> Operation
```

</figure>

<figure class="code-snippet api-signature" id="raven-operation-events" markdown="1">
<figcaption class="code-snippet-filename">Raven.operation_events()</figcaption>

```python
async def operation_events(
    self,
    operation_id: UUID | str,
    *,
    after_event_id: int = 0,
) -> AsyncIterator[Event]
```

</figure>

<figure class="code-snippet api-signature" id="raven-cancel-operation" markdown="1">
<figcaption class="code-snippet-filename">Raven.cancel_operation()</figcaption>

```python
async def cancel_operation(self, operation_id: UUID | str) -> Operation
```

</figure>

<figure class="code-snippet api-signature" id="raven-list-retryable-tasks" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_retryable_tasks()</figcaption>

```python
async def list_retryable_tasks(
    self,
    *,
    limit: int | None = None,
    after_task_id: UUID | str | None = None,
) -> list[OperationTaskRecord]
```

</figure>

<figure class="code-snippet api-signature" id="raven-retry-task" markdown="1">
<figcaption class="code-snippet-filename">Raven.retry_task()</figcaption>

```python
async def retry_task(
    self,
    operation_id: UUID | str,
    task_id: UUID | str,
) -> OperationTask
```

</figure>

### 1.7 Controlling behavior without rebuilding the backend

Applications need both policy and adjustable choices. A host may limit the
resources a runtime can use, while an individual workflow may need a different
retrieval result count or conversation-context budget. RAVEN separates these
controls rather than treating every setting as a constructor argument.

#### SystemConfig: fixed policy and limits

`SystemConfig` describes the limits and resource policy supplied through
`Raven(..., system_config=config)`. Omitting that argument uses
`SystemConfig()` with the defaults below. Its fields are fixed for the
lifetime of that `Raven` instance; changing them requires a new instance.
The HTTP server shares one system configuration across its user runtimes.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">SystemConfig</figcaption>

```python
class SystemConfig(
    schema_version: int = 1,
    host: str = "127.0.0.1",
    port: int = 8765,
    log_level: str = "info",
    operation_sync_interval_seconds: float = 1.0,
    operation_retention_seconds: float = 86_400.0,
    upload_retry_retention_seconds: float = 86_400.0,
    operation_cleanup_interval_seconds: float = 300.0,
    operation_cleanup_batch_size: int = 100,
    event_replay_page_size: int = 256,
    operation_page_size: int = 50,
    finished_operation_cache_size: int = 256,
    open_knowledge_limit: int = 16,
    open_conversation_limit: int = 64,
    llm_adapter_cache_size: int = 8,
    embedding_adapter_cache_size: int = 8,
    prompt_cache_size: int = 32,
    sse_heartbeat_interval_seconds: float = 30.0,
    max_source_file_bytes: int = 104_857_600,
    max_upload_request_overhead_bytes: int = 65_536,
    max_request_body_bytes: int = 1_048_576,
    max_query_string_bytes: int = 8_192,
    max_active_operations_per_user: int = 8,
    max_active_operations: int = 64,
    max_document_pages: int = 1_000,
    max_retrieval_top_k: int = 25,
    max_agent_iterations: int = 50,
    runtime_idle_seconds: float = 1_800.0,
    max_user_runtimes: int = 100,
    cors_origins: tuple[str, ...] = ("http://localhost:3000",),
    allowed_hosts: tuple[str, ...] = ("localhost", "127.0.0.1", "[::1]"),
    require_user_id_header: bool = False,
    trusted_ingestion_enabled: bool = False,
    allowed_ingestion_roots: tuple[Path, ...] = (),
)
```

</figure>

All arguments are optional. The reference expands the backend's named
constants to their current values. They fall into three groups:

**Storage, resources, and workflow limits** affect the library runtime:

| Argument | Meaning |
| --- | --- |
| `schema_version` | Configuration format version. Keep the supported value, `1`. |
| `operation_sync_interval_seconds` | How often pending operation-history writes are synchronized to durable storage. Operation queued, started, completed, failed, and cancelled events trigger immediate synchronization instead of waiting for that interval. |
| `event_replay_page_size` | Maximum number of saved events loaded in each replay page. |
| `finished_operation_cache_size` | Maximum finished operation objects kept in memory for reuse. Zero disables this cache; saved operation records remain on disk. |
| `open_knowledge_limit` | Limit for keeping knowledge database connections open. Connections not in use can be closed to make room; this does not delete the knowledges. |
| `open_conversation_limit` | Corresponding limit for open conversation database connections. Connections in use are kept open; closing an unused connection does not delete its conversation. |
| `llm_adapter_cache_size`, `embedding_adapter_cache_size` | Numbers of language-model and embedding-model connection objects cached by the provider layer. These are not GPU-memory limits. |
| `prompt_cache_size` | Number of conversation-specific system prompts cached by an agent harness. |
| `max_source_file_bytes` | Maximum source-document size accepted for ingestion, in bytes; the default is 100 MiB. |
| `max_document_pages` | Maximum number of pages accepted from page-based document parsing. |
| `max_retrieval_top_k` | Upper bound for the default and overridden retrieval result count. |
| `max_agent_iterations` | Upper bound for the default and overridden agent iteration count per turn. |

**Retention and maintenance policy** describes work that a service must carry out:

| Argument | Meaning |
| --- | --- |
| `operation_retention_seconds` | How long to retain history after an operation completes, fails, or is cancelled, when the application configures operation cleanup. The default is one day. Zero allows removal on the next cleanup pass. |
| `operation_cleanup_interval_seconds` | Suggested operation-cleanup scheduling interval. Also bounds the HTTP server's maintenance interval for runtime eviction and upload cleanup. |
| `operation_cleanup_batch_size` | Number of expired operations to consider per pass when configuring operation cleanup. |
| `upload_retry_retention_seconds` | HTTP server retention window for failed staged uploads so ingestion can be retried; successful uploads are removed after ingestion. |

The operation-retention values do **not** start automatic operation-history
cleanup. Scheduling that cleanup remains the application or host's job.

**HTTP server settings** control the optional server layer. Supplying them
to a library-only `Raven` instance does not start a server or enforce HTTP
request limits on direct Python calls:

| Argument | Meaning |
| --- | --- |
| `host`, `port` | Server bind address and TCP port. The port must be from 1 through 65535. |
| `log_level` | Server logging verbosity: `"critical"`, `"error"`, `"warning"`, `"info"`, or `"debug"`. |
| `sse_heartbeat_interval_seconds` | Idle interval before an operation's Server-Sent Events connection sends a heartbeat comment. |
| `operation_page_size` | Default and maximum page size exposed by operation and resource-list endpoints. |
| `max_upload_request_overhead_bytes` | Allowed multipart framing and metadata overhead beyond uploaded file bytes. |
| `max_request_body_bytes` | Maximum ordinary, non-file request-body size. |
| `max_query_string_bytes` | Maximum encoded URL query-string size. |
| `max_active_operations_per_user`, `max_active_operations` | Admission limits for non-terminal operation tasks per user and across the server. The per-user limit cannot exceed the total limit. |
| `runtime_idle_seconds` | How long a user runtime can remain unused before the server can close it and remove it from memory. Requests and active tasks retain their runtime while using it; saved user data remains on disk. |
| `max_user_runtimes` | Maximum number of resident or initializing user runtimes. |
| `cors_origins` | Browser origins allowed by CORS, expressed as complete HTTP or HTTPS origins without paths, queries, or fragments. |
| `allowed_hosts` | Accepted request hostnames or IP literals, without ports. |
| `require_user_id_header` | Whether protected requests must include `X-Raven-User-ID`. When false, an omitted header selects the default user. This selects a storage scope; the host still authenticates its users. |
| `trusted_ingestion_enabled` | Enables the trusted local-path ingestion endpoint. |
| `allowed_ingestion_roots` | Filesystem roots allowed for that endpoint. At least one is required when trusted ingestion is enabled. |

Durations must be finite and positive, except that operation retention may
be zero. Counts and byte limits must be positive, except that the finished
operation cache may be zero. `Raven` uses the supplied object directly; it
does not automatically load a system-settings file. By default, the server CLI
reads `<raven_home>/system_settings/system_settings.json` when starting the
server, creating it with default values if it is absent. The
`--system-settings` argument selects another file, which must already exist.
Changes made while the server is running apply the next time it starts.

#### PathConfig: the home and the user's storage scope

`PathConfig` determines where data belongs, rather than how work is performed.
`Raven` constructs it from its own `raven_home` and `user_id` arguments and
exposes it as `raven.paths`; no separate path configuration needs to be
passed to `Raven`.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">PathConfig</figcaption>

```python
class PathConfig(
    raven_home: Path,
    user_id: UUID | str = DEFAULT_USER_ID,
)
```

</figure>

| Argument | Meaning |
| --- | --- |
| `raven_home` | Required root directory, supplied by the application. The path is expanded and resolved to an absolute path; there is no default home. |
| `user_id` | UUID identifying the user's storage scope. Accepts a UUID object or UUID string. `DEFAULT_USER_ID` is the stable value `00000000-0000-0000-0000-000000000001`. An invalid UUID is rejected. |

The user root is `<raven_home>/<user_id>/`. Below it, `PathConfig` derives
`data/knowledge_base/`, `data/conversations/`, `operations/operations.sqlite3`,
`runtime_settings/runtime_settings.json`, and `temp/uploads/`. These are
derived paths, not additional constructor arguments. The object is immutable;
a different home or user means a different `Raven` instance. Constructing this
path object derives names; it does not itself create the storage databases
or authenticate a user.

#### RuntimeConfig: adjustable defaults for later work

`RuntimeConfig` holds one user's defaults for ingestion, retrieval, agent
turns, and memory. `Raven.start()` loads or creates the user's
`runtime_settings/runtime_settings.json`; `raven.runtime_config` exposes the
current settings object. Its fields cannot be reassigned. A successful change
saves the new values and replaces that object; a reference to the previous
object still contains the previous values.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">RuntimeConfig</figcaption>

```python
class RuntimeConfig(
    schema_version: int = 1,
    revision: int = 0,
    breakpoint_percentile_threshold: int = 95,
    buffer_size: int = 1,
    max_extraction_retries: int = 3,
    chunk_size: int = 512,
    chunk_overlap: int = 50,
    retrieval_top_k: int = 3,
    agent_max_iterations: int = 10,
    memory_token_limit: int = 4_000,
    memory_top_k: int = 5,
)
```

</figure>

All constructor arguments have defaults. When settings are managed through
`Raven`, `schema_version` and `revision` are maintained by the backend and
cannot be supplied as update fields.

| Argument | Meaning |
| --- | --- |
| `schema_version` | Runtime-settings file format; currently `1`. |
| `revision` | Version of the saved settings, initially `0`. Pass it as `expected_revision` when updating or resetting settings to reject the request if another caller has changed them. Omitting `expected_revision` skips this conflict check. |
| `breakpoint_percentile_threshold` | Percentile of semantic distances used to choose section boundaries, from 1 through 100. Lower values generally produce more sections. |
| `buffer_size` | Number of neighboring semantic units included on each side of the unit being embedded for boundary detection. |
| `max_extraction_retries` | Maximum attempts at structured section-metadata extraction during ingestion. |
| `chunk_size` | Target size, in tokens, of sentence-aware chunks prepared for embedding. These chunks are smaller units within semantic sections. |
| `chunk_overlap` | Token overlap between adjacent embedding chunks. Must be non-negative and smaller than `chunk_size`. |
| `retrieval_top_k` | Default result-count parameter supplied to retrieval pipelines and agent retrieval tools. Must not exceed `SystemConfig.max_retrieval_top_k`. |
| `agent_max_iterations` | Default maximum agent workflow iterations per turn. Must not exceed `SystemConfig.max_agent_iterations`. |
| `memory_token_limit` | Token budget for the model-facing conversation context managed through compaction. It is not the model's entire context-window size. |
| `memory_top_k` | Maximum matched vector-memory nodes returned by memory search. Nodes represent turns, so this is not a strict message-count limit. |

Apart from the metadata and overlap fields described above, counts and token
budgets must be positive integers. Constructing a separate `RuntimeConfig`
does not change a running `Raven` instance; the facade's update methods are
the way to apply and persist its adjustable defaults.

`Raven.get_runtime_settings()` reads those defaults.
`Raven.update_runtime_settings()` starts a task to validate and save changes, and
`Raven.reset_runtime_settings()` validates the built-in defaults against the
system's limits before saving them as a new revision. It does not reduce
default values to fit a lower limit: incompatible defaults cause reset to
fail without changing the saved settings. Methods can also accept per-call
choices, allowing one request to differ without changing the user's saved
defaults. [Section 3](#raven-configuration) explains saving, loading, and
revision checks. Ingestion and retrieval calls already started keep the
settings selected for those calls. New calls can use the updated defaults,
even if they share an operation with earlier calls. An existing session keeps
the defaults selected when it was constructed.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_runtime_settings()</figcaption>

```python
def get_runtime_settings(self) -> dict[str, Any]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.update_runtime_settings()</figcaption>

```python
async def update_runtime_settings(
    self,
    changes: Mapping[str, Any],
    *,
    expected_revision: int | None = None,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.reset_runtime_settings()</figcaption>

```python
async def reset_runtime_settings(
    self,
    *,
    expected_revision: int | None = None,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

### 1.8 Building with the Raven API

The high-level API connects these capabilities so that an application can
request work without assembling the backend first. It is suitable for a chat
interface, a document browser, a direct retrieval service, or another Python
application that needs only part of the complete workflow. The methods above
are related capabilities, not a requirement to run every step for every request.

The following Raven API sections explain lifecycle and configuration, model
selection, document workflows, conversations and sessions, and operation
management. Each method is explained in terms of what the application supplies,
what happens when it is called, and how its result is obtained.

For a complete application built around the high-level API, the
[In action walkthrough]({{ '/demo/' | relative_url }}) follows a terminal
assistant from documents to inspectable answers. The
[Architecture page]({{ '/architecture/' | relative_url }}) explains why the
backend separates these responsibilities, and the
[Integration guides]({{ '/integration_guides/' | relative_url }}) demonstrate
connecting another tool to RAVEN through its HTTP interface.

<div class="page-jump-target" id="raven-lifecycle" aria-hidden="true"></div>

## 2. Creating, starting, and closing Raven

We begin with a workspace that can be opened and closed correctly. No model,
API key, or running Ollama server is needed for this section. Document
ingestion and chat will need models later; the saved workspace has a lifetime
independent of those connections.

### 2.1 Choosing the home and user

The [`Raven`](#raven-init) constructor takes three arguments that determine
the instance's storage scope and fixed policy:

| Argument | How it is supplied | What it determines |
| --- | --- | --- |
| `raven_home` | Required string or `Path`. | The root directory chosen by the application. There is no automatically selected home. |
| `user_id` | Optional UUID object or UUID string. | The user directory beneath the home. Omission selects the stable default `00000000-0000-0000-0000-000000000001`. |
| `system_config` | Optional `SystemConfig`, passed by name. | The policy and limits used by this instance. Omission uses an in-memory `SystemConfig()`; it does not load a JSON file. |

A relative `raven_home` is resolved from the process's current working directory.
Running a script from two different directories can therefore resolve the
same relative `raven_home` to two different absolute locations. An application
that must reopen a particular workspace should retain its absolute home and user ID.
Creating a new random user ID on every launch would select a different user
directory on each launch.

User IDs separate stored data. They do not authenticate the caller. In a
hosted application, the host determines the authenticated user's identity
before selecting the corresponding RAVEN runtime. An invalid UUID is rejected
with `ValueError` during construction.

### 2.2 Preparing and releasing the workspace

Constructing `Raven` establishes its Python objects and derives their paths.
`await raven.start()` then prepares the workspace for use:

1. It loads the user's runtime-settings file, or creates one with defaults
   if it is absent, and checks those values against system policy. The file
   is named `runtime_settings.json` and lives at
   `<raven_home>/<user_id>/runtime_settings/runtime_settings.json`.
2. It opens operation history and marks unfinished tasks and operations from
   an earlier process termination as failed with an interruption error.
3. It reads metadata from the user's existing knowledge and conversation
   directories and registers the resources available to this instance.

Await `raven.start()` before creating, listing, or accessing knowledges and
conversations through the instance. Startup does not choose models, launch
Ollama, or open an HTTP port.
A started `Raven` instance and its live resources are called a **runtime**
here; this does not mean that the HTTP server is running.

`await raven.close()` cancels active operations, checks unfinished ingestion
writes for vectors that need removal, and attempts to finish recorded file
deletions. It then attempts to close the conversation and knowledge database
handles, provider connections, and operation storage used by the instance.
Saved data remains on disk, and an external Ollama server keeps running.
If one cleanup step fails, shutdown still attempts the remaining steps and
then raises a `RavenError` describing the failures.

Both lifecycle methods return `None` after finishing. They do not return an
`OperationTask`. The `await` keyword lets the caller wait for that preparation
or cleanup while Python's event loop can run other asynchronous work.

### 2.3 A complete lifecycle program

Install the documented version in the Python environment used for these
programs. Python 3.11 or later is required:

```bash
python -m pip install "noomexai-raven==0.2.0"
```

The distribution name is `noomexai-raven`; the Python import is `nraven`.
Save the following program as `runtime_lifecycle.py` and run
`python runtime_lifecycle.py`. It uses an example home named `raven-lifecycle-home`
under the current working directory and does not clear existing data there.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">runtime_lifecycle.py</figcaption>

```python
import asyncio
from pathlib import Path

from nraven import Raven


async def main() -> None:
    home = Path.cwd() / "raven-lifecycle-home"
    raven = Raven(home)

    try:
        await raven.start()

        knowledges = await raven.list_knowledges()
        print("Available knowledges:", [item["name"] for item in knowledges])
        print("User root:", raven.paths.user_root)
        print("Runtime settings:", raven.paths.runtime_settings_path)
        print("Started:", raven.is_started)
        print("Models configured:", raven.models_loaded)
    finally:
        await raven.close()


if __name__ == "__main__":
    asyncio.run(main())
```

</figure>

On a new home, the knowledge list is empty, `is_started` is `True`, and
`models_loaded` is `False`. This is a prepared workspace with no model pair
configured. A previously used home can list existing knowledges instead.

`asyncio.run(main())` supplies the event loop for this standalone program.
The `try/finally` ensures that shutdown is attempted if startup or a later
statement raises an exception. In an application that already has an event
loop, call and await the asynchronous function from that loop rather than
starting a nested `asyncio.run()`.

A closed object cannot be started again. To reopen its data, construct a new
`Raven` with the same home and user ID, then start that new instance. Repeating
`start()` on an already started object returns without repeating startup.
If `start()` fails while preparing the workspace, RAVEN attempts to release
its resources. That instance cannot be started again; after resolving the
cause, construct a new `Raven` instance using the same home and user ID.

### 2.4 Reading readiness and status

After startup, an interface may need to decide whether document management
is available or whether it should ask for model configuration. Two properties
and one method provide that information without starting background work:

| Read | Meaning |
| --- | --- |
| `raven.is_started` | `True` after successful startup and before closure. |
| `raven.models_loaded` | Whether both model adapters are assigned. It does not report GPU residency or guarantee that a remote provider is reachable. |
| `raven.runtime_status()` | A dictionary containing the fields explained below. Reading it does not start the runtime, configure models, or run a new storage check. Call it without `await`. |

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.runtime_status()</figcaption>

```python
def runtime_status(self) -> dict[str, Any]
```

</figure>

The dictionary has these fields:

| Field | Meaning |
| --- | --- |
| `user_id` | The normalized UUID string selecting this runtime's user directory. |
| `started` | Whether startup completed successfully and the instance has not begun closing. |
| `closed` | Whether the instance has been closed, has begun shutdown, or failed during startup preparation. `True` means it cannot be started again; it does not confirm that every cleanup step succeeded. |
| `models_loaded` | The same adapter-assignment check as the property above. |
| `operation_store_healthy` | `True` when the operation manager has not recorded a synchronization failure. It does not confirm that all databases are intact or that storage is currently open. |
| `runtime_config_revision` | Revision of the runtime's current settings snapshot. |
| `models.llm`, `models.embedding` | Each configured role's `provider`, `model`, and `role`, or `None` when no specification has been configured for it. |

No API-key values or provider clients are included. Reading this dictionary
does not change the instance. Similarly, `await raven.list_knowledges()`
returns its list directly after the read; it does not produce a task handle
requiring another `.result()` call.

<div class="page-jump-target" id="raven-configuration" aria-hidden="true"></div>

## 3. System settings, paths, and runtime defaults

With lifecycle established, we can choose the policy under which a workspace
runs and the defaults used for its later work. The three configuration
classes introduced in [Section 1.7](#17-controlling-behavior-without-rebuilding-the-backend)
answer different questions. `SystemConfig` sets fixed policy, `PathConfig`
locates the user's saved data, and `RuntimeConfig` supplies adjustable defaults.
That overview contains the complete constructor fields and their meanings;
here we apply them through the Raven API.

### 3.1 Supplying system policy

Suppose an application accepts source files up to 50 MiB and allows retrieval
counts up to 12. Those are system-policy choices, not defaults that one chat
turn should change. We construct a `SystemConfig` and pass it to `Raven`.
Unspecified fields retain their built-in values. The configuration can be
passed directly; saving it to a file is optional. Because its fields cannot
be reassigned, custom values are supplied when constructing `SystemConfig`.

Save this independent program as `system_policy.py` and run
`python system_policy.py`. It supplies its policy directly to `Raven`, using
`raven-policy-home` as a separate example workspace from Section 2. It does
not create or read a `system_settings.json` file. Startup still loads or
creates the user's runtime-settings file as explained in Section 2.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">system_policy.py</figcaption>

```python
import asyncio
from pathlib import Path

from nraven import Raven, SystemConfig


async def use_workspace(home: Path, config: SystemConfig) -> None:
    raven = Raven(home, system_config=config)

    try:
        await raven.start()
        print("Maximum source bytes:", raven.system_config.max_source_file_bytes)
        print("Retrieval ceiling:", raven.system_config.max_retrieval_top_k)
        print("Runtime retrieval default:", raven.runtime_config.retrieval_top_k)
    finally:
        await raven.close()


def main() -> None:
    home = Path.cwd() / "raven-policy-home"
    config = SystemConfig(
        max_source_file_bytes=50 * 1024 * 1024,
        max_retrieval_top_k=12,
        max_agent_iterations=24,
    )
    asyncio.run(use_workspace(home, config))


if __name__ == "__main__":
    main()
```

</figure>

The program supplies a retrieval **ceiling** of 12 on every run. With a new
example home, the runtime retrieval **default** is 3; later runs load that
user's saved runtime defaults. A ceiling is the largest permitted choice;
it does not select the value for every request.

Choose system ceilings that allow RAVEN's initial runtime defaults, including
retrieval count 3 and agent iteration count 10. `Raven` checks its initial
defaults during construction, before startup loads the saved runtime settings.
A system configuration can therefore be valid on its own but incompatible
with constructing `Raven` if those ceilings are too low.

#### Optional: saving and loading policy

File persistence is useful when policy should be edited or retained outside
the program. An application can choose a JSON file path and call
`config.save(path)`. On a later launch, `SystemConfig.load(path)` returns the
configuration to pass as `system_config` when constructing `Raven`. Neither
step is required for the direct configuration program above.

Both file methods below are synchronous. They can be called before starting
the event loop or delegated to a worker thread when used inside an
asynchronous application.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">SystemConfig.save()</figcaption>

```python
def save(self, path: str | Path) -> None
```

</figure>

`save(path)` creates missing parent directories, writes the configuration as
JSON to a temporary file, and synchronizes that file before replacing the
destination. The replacement is atomic: readers see the previous file or the
replacement, rather than a partially rewritten JSON document. Calling
`save()` replaces an existing file. An application that should preserve an
existing policy file can check whether it exists and load it instead of
saving a new configuration over it.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">SystemConfig.load()</figcaption>

```python
@classmethod
def load(cls, path: str | Path) -> "SystemConfig"
```

</figure>

`load(path)` reads and validates an existing JSON file and returns a new
`SystemConfig`. It does not create a missing file or update an already running
`Raven`. Unknown fields, unsupported schema versions, and invalid values are
rejected. Its fields cannot be reassigned. To change system policy, create
or load another configuration and use it when constructing a new `Raven`
instance.

In library use, the application decides whether to read a policy file.
`Raven(home, system_config=config)` uses the supplied object; `Raven(home)`
uses in-memory system defaults even if a system-settings file happens to
exist beneath the home.

The server CLI makes that file decision automatically: it reads
`<raven_home>/system_settings/system_settings.json`, creating defaults there
when absent. `--system-settings` selects another existing file. Changes made
to the selected file while the server runs apply the next time the server
starts. Server fields such as `host`, `port`, and request admission limits are
enforced by the server layer, not by starting a library runtime.

Operation-retention fields are policy inputs for an application-scheduled
cleanup service. Setting them does not start automatic operation-history
cleanup, in either library or bundled-server use.

### 3.2 Using the paths owned by Raven

The lifecycle program already prints `raven.paths.user_root` and
`raven.paths.runtime_settings_path`. These come from the `PathConfig` owned by
`Raven`. The application supplies the home and user ID once; the remaining
paths follow from those inputs.

| Property on `raven.paths` | Value or location |
| --- | --- |
| `raven_home` | The application's expanded, absolute home path. |
| `user_id` | The normalized UUID object selecting the user's scope. |
| `user_root` | `<raven_home>/<user_id>/` |
| `knowledge_base_dir` | `<user_root>/data/knowledge_base/` |
| `conversations_dir` | `<user_root>/data/conversations/` |
| `operation_storage_dir` | `<user_root>/operations/` |
| `operation_database_path` | `<user_root>/operations/operations.sqlite3` |
| `runtime_settings_dir` | `<user_root>/runtime_settings/` |
| `runtime_settings_path` | `<user_root>/runtime_settings/runtime_settings.json` |
| `uploads_dir` | `<user_root>/temp/uploads/` |

These are derived properties, not independently configurable directories.
Reading one does not itself create its directory or database. Storage is
created when the corresponding runtime or resource needs it. Using these
properties keeps the application's file references consistent with the home
and user ID already selected for `Raven`.

The system-settings file discussed above is outside the per-user directory:
it belongs to the home-level server policy. Runtime settings belong to one
user. Reopening a saved workspace therefore requires both the same home and
the same user ID, even when several users share a home.

### 3.3 Applying and retaining runtime defaults

A runtime default answers a workflow question such as how many retrieval
results to request when a call does not specify `top_k`. Changing that default
is different from increasing the maximum allowed by system policy.

`raven.get_runtime_settings()` returns a new dictionary of current values.
Editing that dictionary does not change the runtime. `raven.runtime_config`
returns the current `RuntimeConfig` object, whose fields cannot be reassigned.
After an update, reading the property returns the new object; any reference
retained to the old object still shows the old values.

To apply a change, `await raven.update_runtime_settings(changes, ...)` returns
an `OperationTask`. The `changes` mapping contains only fields to change;
the others retain their current values. `await task.result()` waits for the
validated, persisted change and returns the **complete settings dictionary**,
including its revision. Validation errors from the worker are raised by
`.result()`. Receiving the task handle confirms that the task was submitted;
awaiting its result confirms whether the settings change succeeded.

Save this independent program as `runtime_defaults.py` and run
`python runtime_defaults.py`. It uses `raven-defaults-home`, sets two
defaults there, and reopens the same user's workspace to read them back.
It does not ingest documents or call a model.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">runtime_defaults.py</figcaption>

```python
import asyncio
from pathlib import Path

from nraven import Raven


async def main() -> None:
    home = Path.cwd() / "raven-defaults-home"
    raven = Raven(home)

    try:
        await raven.start()
        current = raven.get_runtime_settings()
        task = await raven.update_runtime_settings(
            {"retrieval_top_k": 5, "agent_max_iterations": 12},
            expected_revision=current["revision"],
        )
        updated = await task.result()
        print("Retrieval default:", updated["retrieval_top_k"])
        print("Agent iteration default:", updated["agent_max_iterations"])
        print("Saved revision:", updated["revision"])
    finally:
        await raven.close()

    reopened = Raven(home)
    try:
        await reopened.start()
        saved = reopened.get_runtime_settings()
        print("Reopened retrieval default:", saved["retrieval_top_k"])
        print("Reopened revision:", saved["revision"])
    finally:
        await reopened.close()


if __name__ == "__main__":
    asyncio.run(main())
```

</figure>

The values read after reopening should match the confirmed update. Model
configuration is not restored by this settings file; the new runtime still
needs an explicit model pair before model-dependent work.

`changes` accepts the nine workflow fields listed under `RuntimeConfig` in
[Section 1.7](#17-controlling-behavior-without-rebuilding-the-backend).
`schema_version` and `revision` cannot be updated directly. Values must satisfy
their field rules and the system ceilings. For example, `chunk_overlap` must
be smaller than `chunk_size`, and `retrieval_top_k` must not exceed
`SystemConfig.max_retrieval_top_k`. Invalid updates leave the current and saved
settings unchanged.

The optional `operation` argument attaches the settings task to a caller's
existing operation. Omitting it creates an operation for this change. It is
not required to use the settings API; shared operations are covered later.

### 3.4 Detecting conflicting changes and resetting defaults

`expected_revision` compares the revision read by the caller with the revision
current when the worker applies its change. If they differ, the task fails
with `runtime_config_conflict` and does not apply the mapping. Omitting the
argument skips that comparison, while validation still applies. Conflict
checking is useful when two settings forms or requests may edit the same
user's defaults.

The next program demonstrates a stale settings form: one update changes the
revision, then another tries to use the old revision. It also demonstrates
reset. Save it as `settings_revisions.py` and run
`python settings_revisions.py`.

**This program resets runtime defaults in its separate
`raven-revisions-home` example workspace.** It does not delete knowledges
or conversations. Keep its example home separate from a workspace whose
custom defaults need to be retained.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">settings_revisions.py</figcaption>

```python
import asyncio
from pathlib import Path

from nraven import Raven
from nraven.core.errors import ErrorCode, RavenError


async def main() -> None:
    raven = Raven(Path.cwd() / "raven-revisions-home")

    try:
        await raven.start()
        original = raven.get_runtime_settings()
        first = await raven.update_runtime_settings(
            {"memory_top_k": original["memory_top_k"] + 1},
            expected_revision=original["revision"],
        )
        updated = await first.result()

        try:
            stale = await raven.update_runtime_settings(
                {"memory_top_k": original["memory_top_k"] + 2},
                expected_revision=original["revision"],
            )
            await stale.result()
        except RavenError as error:
            if error.code != ErrorCode.RUNTIME_CONFIG_CONFLICT:
                raise
            print("Stale update rejected:", error.code.value)

        reset = await raven.reset_runtime_settings(
            expected_revision=updated["revision"],
        )
        restored = await reset.result()
        print("Restored memory_top_k:", restored["memory_top_k"])
        print("Reset revision:", restored["revision"])
    finally:
        await raven.close()


if __name__ == "__main__":
    asyncio.run(main())
```

</figure>

On a fresh example home, the first update changes `memory_top_k` from 5 to 6
at revision 1. The stale update is rejected. Reset restores 5 at revision 2.
On later runs, revision numbers continue increasing rather than starting over.

A valid update that makes no actual change leaves the revision unchanged.
This includes submitting the same values again, provided the optional expected
revision is still current. Reset behaves differently: it saves built-in
defaults as a new revision even if the values already match them. It does not
rewind revision to zero. Defaults are validated against system policy before
being saved, rather than being automatically reduced to fit that policy.

The default `expected_revision=None` is therefore a choice to omit conflict
checking, not a request to use revision zero. When a conflict occurs, read the
new settings and decide how to reconcile the change before submitting again.

### 3.5 When a setting takes effect

`Raven.ingest()` and `Raven.retrieve()` read their defaults when called.
`Raven.session()` reads its defaults when constructing the session. Explicit
arguments take priority without changing the saved defaults:

| Work | Saved defaults | Explicit choices |
| --- | --- | --- |
| `Raven.ingest()` | Semantic threshold, buffer size, extraction attempts, chunk size, and overlap. | The corresponding named ingestion arguments. |
| `Raven.retrieve()` | `retrieval_top_k` | The call's `top_k` argument. |
| `Raven.session()` | Agent iteration limit, retrieval count, conversation-context token budget, and memory-search result count. | `max_iterations`, `top_k`, `memory_token_limit`, and `memory_top_k` when creating the session. |

Using `None` for one of these optional facade overrides selects the current
default. An explicit value still has to pass validation; it does not bypass
system ceilings. For example, requesting `top_k=5` for one retrieval leaves
the saved `retrieval_top_k` unchanged.

An ingestion or retrieval already started keeps its captured values. A session
captures its defaults when `Raven.session()` constructs it, so updating settings
does not change subsequent turns on that existing session. A newly created
session uses the updated defaults. These boundaries let settings be changed
without rewriting the configuration of work already in progress.

Editing `runtime_settings.json` directly while the runtime is running is not
a live update mechanism. The file is loaded at startup; use the Raven API's
update and reset methods to validate, persist, and install changes in the
running instance.

<div class="page-jump-target" id="raven-models" aria-hidden="true"></div>

## 4. Models and provider utilities

Starting a workspace does not configure its models. Before ingestion,
retrieval, or chat, the application supplies two model specifications to
`Raven.configure_models()`: one for the language model and one for embeddings.
Knowledge creation and browsing stored content can be used without that pair.

### 4.1 Choosing the two model roles

The language model extracts section metadata during ingestion and generates
responses during chat. Chat also requires a model that supports tool calling.
The embedding model converts text into vectors for semantic sectioning,
document search, and conversation memory. A provider is the service through
which RAVEN accesses a model: `ollama` selects the local Ollama integration;
other supported provider identifiers select LiteLLM.

Each role is described by [`ModelSpec`](#model-spec). The fields have these
calling rules:

| Field | Required? | What to supply |
| --- | --- | --- |
| `provider` | Yes | `"ollama"` for Ollama, or the LiteLLM provider identifier for the selected service. Provider names are trimmed and lowercased. |
| `model` | Yes | The model name recognized by that provider. For Ollama, use the installed name and tag, such as `"bge-m3:latest"`. |
| `role` | Yes | `ModelRole.LLM` for the language model or `ModelRole.EMBEDDING` for the embedding model. |
| `api_key_ref` | No for Ollama; depends on the cloud provider's authentication requirements | The name of an environment variable containing the credential. It is not the credential itself. |
| `options` | No | Settings supported by the LiteLLM adapter for that role. The default is an empty dictionary. The current Ollama provider interface rejects nonempty options. |

`ModelRole` defines the two accepted role values:

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">ModelRole</figcaption>

```python
class ModelRole(StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"
```

</figure>

The first argument of [`Raven.configure_models()`](#raven-configure-models)
must have role `LLM`, and the second must have role `EMBEDDING`. The provider
and model name can differ between them. Both are required by this facade,
even when the next action needs only one role internally.

> **Choose the embedding model before ingesting documents. Existing vectors
> cannot be searched correctly with an unrelated model that did not generate
> them, even if its vector dimensions match. Configuration does not regenerate
> document or conversation-memory vectors. Keep the original embedding model
> for existing data; using another requires rebuilding the affected vectors.**

RAVEN checks the embedding identity against existing knowledge stores when a
configured pair changes. That check is not an automatic conversion between
embedding models. It also does not remove the application's responsibility
to keep conversation vector memory paired with its original embedding model.

### 4.2 Configuring a local pair

For the local example, Ollama must already be installed and running. RAVEN
connects to it; it does not launch or terminate the Ollama process. The default
address is `http://127.0.0.1:11434`. Set `OLLAMA_HOST` in the process environment
before constructing `Raven` if another Ollama address is needed.

Save the following independent program as `configure_models.py` and run
`python configure_models.py`. It uses `raven-models-home`, configures one LLM
and one embedding model, waits for configuration to finish, and prints their
configured identities. The default names below are example choices; the
`--llm` and `--embedding` arguments select other installed models.

**If a specified Ollama model is missing, configuration can download it.**
Model files are managed by the connected Ollama server, outside RAVEN's home.
Use names already available there to avoid an unexpected large download.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">configure_models.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import ModelRole, ModelSpec, Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(Path.cwd() / "raven-models-home")

    try:
        await raven.start()
        task = await raven.configure_models(
            ModelSpec(
                provider=args.provider,
                model=args.llm,
                role=ModelRole.LLM,
                api_key_ref=args.api_key_ref,
            ),
            ModelSpec(
                provider="ollama",
                model=args.embedding,
                role=ModelRole.EMBEDDING,
            ),
        )
        configured = await task.result()
        print(json.dumps(configured, indent=2))
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", type=str.lower, default="ollama")
    parser.add_argument("--llm")
    parser.add_argument("--embedding", default="bge-m3:latest")
    parser.add_argument("--api-key-ref")
    args = parser.parse_args()
    if args.provider != "ollama" and (not args.llm or not args.api_key_ref):
        parser.error("Cloud use requires --llm and --api-key-ref.")
    args.llm = args.llm or "gemma4:e2b"
    asyncio.run(main(args))
```

</figure>

The task result is a dictionary with `llm` and `embedding` entries. Each entry
contains only `provider`, `model`, and `role`; it does not contain an API key
or a provider client. `await task.result()` confirms successful configuration
or raises its error. Do not treat receiving the task handle, or reading
`models_loaded`, as confirmation that configuration finished successfully.

Configuration performs these actions:

1. Builds or reuses the model adapters, which are the Python objects used to
   call the selected models.
2. Checks their required asynchronous interfaces and, when the pair changes,
   embedding compatibility with existing knowledges.
3. Assigns the pair and rebuilds model-dependent retrieval pipelines.
4. For Ollama, unloads previously configured models no longer present in the
   new pair, then requests preloading of the configured embedding model and LLM.

Ollama controls CPU/GPU placement and eviction. RAVEN keeping an adapter does
not guarantee that the corresponding weights remain in GPU memory.
`Raven.close()` releases provider connections; it does not unload Ollama's
resident models or stop that server. To switch pairs, call `configure_models()`
again and await its task result before submitting dependent work. Close
sessions using the previous pair and create new sessions for the replacement.
After restarting RAVEN, configure the pair again; model configuration is not
restored from the user's runtime-settings file.

### 4.3 Using a cloud LLM with local embeddings

The same program can use a cloud LLM while keeping its embedding role on
Ollama. Supply `--provider`, `--llm`, and `--api-key-ref`. The provider identifier
and model name must be supported by the installed LiteLLM integration and the
selected service; RAVEN does not translate arbitrary vendor names into provider
identifiers. The chosen model must support the work being requested, including
structured metadata extraction for ingestion and tool calling for chat.

Before starting Python, make the API key available in an environment variable
using the application's or host's credential mechanism. Pass that variable's
**name** through `--api-key-ref`. RAVEN reads it from the process environment;
it does not automatically read a `.env` file. If a reference is supplied but
the variable is absent or empty, configuration fails with
`model_api_key_not_found`.

For example, this PowerShell command sequence asks for the three non-secret
values rather than assuming a particular cloud model:

```powershell
$provider = Read-Host "LiteLLM provider identifier"
$model = Read-Host "Cloud model name"
$keyRef = Read-Host "Environment variable containing the API key"
python configure_models.py --provider $provider --llm $model --api-key-ref $keyRef
```

The key must already be set; that sequence asks only for its variable name.
Cloud requests send the submitted content to the selected provider and are
subject to its access, usage limits, and charges. Cloud model configuration
creates the adapter but does not make a test inference request, so successful
configuration alone does not verify the credential or the provider's response.
In this mixed example, Ollama is still required for the embedding role.

For LiteLLM models, `options` can supply adapter-supported settings such as
generation `temperature`. The accepted options depend on the role and adapter.
Do not put API keys in `options`: credential fields and RAVEN-controlled fields
such as the model name and custom provider selection are rejected there.

### 4.4 Inspecting, downloading, and deleting Ollama models

These methods operate on the Ollama server selected when the `Raven` instance
was constructed. They do not configure RAVEN's active model pair. Each returns
an `OperationTask`; await its `.result()` for the value shown below. The
optional `operation` argument reuses an existing operation rather than creating
a new one.

| Method | Input | Task result and effect |
| --- | --- | --- |
| `check_ollama_connection()` | No model name. | `None` if Ollama's model-list request succeeds; raises on failure. |
| `list_ollama_models()` | No model name. | A list of Ollama model dictionaries describing installed models. It is not the configured pair or a list of only resident models. |
| `inspect_ollama_model(model)` | Exact model name. | Ollama's model-description dictionary. |
| `pull_ollama_model(model, on_progress=None)` | Model name and optional callback. | Downloads the model; result is `None`. Progress is also published as `model.pull.progress` events. |
| `delete_ollama_model(model)` | Model name. | Deletes the model from Ollama's storage; result is `None`. It affects the shared Ollama installation, not only the current RAVEN home. |

The [`list`](#raven-list-ollama-models) and
[`pull`](#raven-pull-ollama-model) signatures are in the overview. The other
signatures are:

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.check_ollama_connection()</figcaption>

```python
async def check_ollama_connection(
    self,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.inspect_ollama_model()</figcaption>

```python
async def inspect_ollama_model(
    self,
    model: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.delete_ollama_model()</figcaption>

```python
async def delete_ollama_model(
    self,
    model: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

`on_progress` receives each progress dictionary and may be synchronous or
asynchronous. It is an additional callback, not a replacement for the event
stream. Downloading or deleting a model does not update the configured
specifications. Avoid deleting a model still required by this application or
another client of the same Ollama server.

### 4.5 Preloading and unloading the configured role

`preload_configured_model()` and `unload_configured_model()` select the model
from the currently configured role. Pass `ModelRole.LLM` or
`ModelRole.EMBEDDING`, rather than repeating a model name.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.preload_configured_model()</figcaption>

```python
async def preload_configured_model(
    self,
    role: ModelRole,
    keep_alive: str | float | None = None,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.unload_configured_model()</figcaption>

```python
async def unload_configured_model(
    self,
    role: ModelRole,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

For preload, `keep_alive` is an optional Ollama duration string, such as
`"10m"`, or a finite number of seconds. `None` leaves the choice to Ollama;
a negative duration requests indefinite residency. Numeric zero and the string
`"0"` are rejected for preload. Use unload when removal from memory is intended.
Both methods also accept the optional shared `operation` argument.

The task result includes `action`, `performed`, `provider`, `model`, and `role`.
For an Ollama role, a successful result reports `performed=True`. For a cloud
role, no residency request is made: the result has `performed=False` and
`reason="provider_has_no_local_residency"`. A role must have been configured
before either method can select its model.

Unloading removes the model from Ollama's runtime memory; it does not delete
the installed model or remove RAVEN's adapter. A later inference request can
load it again. Use deletion only when removing the installed model files is
the intended action.

<div class="page-jump-target" id="raven-documents" aria-hidden="true"></div>

## 5. Knowledge, ingestion, and navigation

A knowledge is the named store into which documents are ingested. Creating
the knowledge, preparing a source file, and reading its saved sections are
separate calls. We first manage a knowledge without models, then configure
models for ingestion and inspect what was stored.

### 5.1 Creating and updating a knowledge

[`Raven.create_knowledge()`](#raven-create-knowledge) accepts a `name` and an
optional `user_summary`. The name determines the knowledge directory and must
be unique for the current user. RAVEN trims it, replaces characters outside
ASCII letters, digits, `_`, and `-` with `_`, then removes leading and trailing
underscores. The result must not be empty. For example, `Engineering Docs`
and `Engineering_Docs` select the same normalized name. Case is not folded;
use one consistent spelling for a knowledge.

The summary describes the material stored there. Reasoning-based global
retrieval can use it when deciding which knowledges to search. Creating a
knowledge does not ingest a file or generate document vectors.

The create method returns a task. Its result is the managed `Knowledge`
object. After startup, `get_knowledge(name)` returns an existing object
synchronously; it does not create one when the name is missing.
`get_knowledge_details(name)` returns the metadata dictionary containing
`schema_version`, `created_at`, `name`, and `user_summary`.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_knowledge_details()</figcaption>

```python
def get_knowledge_details(self, name: str) -> dict[str, Any]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.update_knowledge()</figcaption>

```python
async def update_knowledge(
    self,
    name: str,
    *,
    user_summary: str,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

`update_knowledge(name, user_summary=...)` replaces the summary. The keyword
is required, and an empty string clears it. Its task result is `None`; read
`get_knowledge_details()` afterward to obtain the updated metadata. These
methods do not rename the knowledge or rewrite its documents.

Save this independent program as `manage_knowledge.py` and run
`python manage_knowledge.py`. It uses `raven-knowledge-home` and needs no models.
It creates `engineering` if absent, reuses it otherwise, updates its summary,
and prints its metadata and file count.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">manage_knowledge.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import Raven


async def main(delete: bool) -> None:
    raven = Raven(Path.cwd() / "raven-knowledge-home")

    try:
        await raven.start()
        names = {item["name"] for item in await raven.list_knowledges()}
        if "engineering" not in names:
            task = await raven.create_knowledge("engineering")
            knowledge = await task.result()
        else:
            knowledge = raven.get_knowledge("engineering")

        task = await raven.update_knowledge(
            knowledge.name,
            user_summary="Engineering specifications and design references.",
        )
        await task.result()
        print(json.dumps(raven.get_knowledge_details(knowledge.name), indent=2))
        print("Files:", await raven.count_knowledge_files(knowledge.name))

        if delete:
            task = await raven.delete_knowledge(knowledge.name)
            await task.result()
            print("Deleted knowledge:", knowledge.name)
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.delete))
```

</figure>

The existence check makes repeated runs reuse the knowledge instead of
requesting a duplicate creation. It is not a concurrency lock: simultaneous
creators can still receive `knowledge_already_exists` from one create task.

**Running `python manage_knowledge.py --delete` deletes the `engineering`
knowledge in that example home, including any files and vectors stored there.**
Without that flag, the program leaves the knowledge available for inspection.
The ingestion example below has its own home and is unaffected by this flag.

### 5.2 Ingesting a supported source file

[`Raven.ingest()`](#raven-ingest) takes an existing knowledge name and a source
path readable by the Python process. Relative paths are resolved from that
process's working directory. This is library filesystem access; the HTTP
server's multipart-upload and trusted-path rules are separate interfaces.

| Source format | Extensions | What is retained for search |
| --- | --- | --- |
| Plain text | `.txt` | UTF-8 text parsed natively. |
| Markdown | `.md`, `.markdown` | Textual Markdown source parsed natively. |
| PDF | `.pdf` | Docling's ordered textual projection and available page provenance. |
| Word | `.docx` | Docling's ordered textual projection and available page provenance. |

Tables can become Markdown text. Pictures may contribute textual parser
output, but the ingestion pipeline does not send original image data to a
vision model. Parsing and reconstruction do not preserve the source's exact
visual layout.

The pipeline validates and snapshots the source, parses it, splits it into
semantic sections, extracts section metadata with the LLM, and divides those
sections into embedding chunks. It writes the vectors to the knowledge's
Qdrant store and commits the file and section records to `file.sqlite3`.

For a reproducible input, download [Reaction_wheel.txt]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }})
and place it in the working directory. Save the following program as
`ingest_document.py`, then run `python ingest_document.py Reaction_wheel.txt`.
Ollama must be running, with the same model prerequisites as Section 4. The
program uses its own `raven-ingestion-home` and creates or reuses the knowledge
`engineering` there.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">ingest_document.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import EventType, ModelRole, ModelSpec, Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(Path.cwd() / "raven-ingestion-home")
    source = args.source.expanduser().resolve()

    try:
        await raven.start()
        names = {item["name"] for item in await raven.list_knowledges()}
        if "engineering" not in names:
            task = await raven.create_knowledge(
                "engineering", "Reaction-wheel engineering references."
            )
            await task.result()

        files = await raven.list_knowledge_files("engineering")
        existing = next((item for item in files if item["file_name"] == source.name), None)
        if existing is None:
            task = await raven.configure_models(
                ModelSpec(provider="ollama", model=args.llm, role=ModelRole.LLM),
                ModelSpec(
                    provider="ollama", model=args.embedding, role=ModelRole.EMBEDDING
                ),
            )
            await task.result()
            ingestion = await raven.ingest("engineering", source)
            async for event in ingestion.events():
                if event.type == EventType.INGESTION_PROGRESS:
                    data = event.data
                    print(
                        data["stage"], data["status"],
                        data.get("completed"), "/", data.get("total"),
                        flush=True,
                    )
            result = await ingestion.result()
            print("Ingested:", json.dumps(result, indent=2))
        else:
            print("Already stored; reading existing file:", existing["file_name"])

        print("Files:", await raven.count_knowledge_files("engineering"))
        print("Vector points:", await raven.count_knowledge_vectors("engineering"))
        cursor = 0
        while True:
            page = await raven.list_file_sections_page(
                "engineering", source.name, limit=2, after_section_index=cursor
            )
            if not page:
                break
            for section in page:
                print("Section:", section["section_id"], section["summary"])
            cursor = page[-1]["section_index"]

        sections = await raven.list_file_sections_page(
            "engineering", source.name, limit=1
        )
        if sections:
            section = await raven.get_knowledge_section(
                "engineering", sections[0]["section_id"], file_name=source.name
            )
            print("First section text (first 240 characters):")
            print(section["raw_content"][:240])
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--llm", default="gemma4:e2b")
    parser.add_argument("--embedding", default="bge-m3:latest")
    asyncio.run(main(parser.parse_args()))
```

</figure>

For a new file, the program prints progress, waits for the ingestion result,
then reads the saved sections. On a repeat run with the same filename, its
own existence check skips model configuration and ingestion and reads the
previously stored content. It does not replace that content if the source
file on disk has changed. To ingest changed content under the same filename,
delete the stored file explicitly, then ingest it again.

### 5.3 Progress, completion, and ingestion arguments

Each `ingestion.progress` event identifies `knowledge`, `file`, `file_id`,
`stage`, and `status`. `completed` and `total` may be integers or `None`.
Display the stage and status even when a percentage cannot be calculated.
Counters describe the current stage, not a single percentage across the whole
pipeline.

| Stage | Work represented by that stage |
| --- | --- |
| `source_validation` | Checks source existence, readability, extension, and size. |
| `source_snapshot` | Creates a private copy for this ingestion run. |
| `parsing` | Produces ordered document elements. |
| `sectioning` | Uses embeddings to choose semantic section boundaries. |
| `metadata_extraction` | Extracts summary, keywords, conditions, and definitions for each section. |
| `chunking` | Splits sections into embedding chunks. |
| `embedding` | Generates chunk vectors in batches. |
| `vector_storage` | Writes the vector points to Qdrant. |
| `metadata_commit` | Commits the file and section records to SQLite. |

The task's event iterator also contains child-task and operation events.
Filtering for `EventType.INGESTION_PROGRESS` selects the progress updates;
`await ingestion.result()` still determines whether the whole task succeeded.
It raises the error if ingestion failed.

The successful result is a dictionary with these fields:

| Field | Meaning |
| --- | --- |
| `knowledge` | Knowledge receiving the file. |
| `file` | Stored filename, derived from the source's basename. |
| `file_id` | Generated identity of this stored file. Use it for file deletion. |
| `section_count` | Number of semantic sections stored. |
| `chunk_count` | Number of embedding chunks/vector points stored. |
| `source_path` | The source path used by ingestion, not a permanent RAVEN-managed original-file download. |

The required arguments and optional controls are:

| Argument | Required or default | Effect |
| --- | --- | --- |
| `knowledge_name` | Required. | Existing knowledge name for the current user. |
| `source_path` | Required string or `Path`. | Readable supported source file. |
| `breakpoint_percentile_threshold` | Current runtime default, initially 95. | Percentile for semantic boundaries, from 1 through 100. |
| `buffer_size` | Current runtime default, initially 1. | Positive number of neighboring semantic units included on each side during boundary comparison. |
| `max_extraction_retries` | Current runtime default, initially 3. | Positive maximum metadata-extraction attempts per section. |
| `chunk_size` | Current runtime default, initially 512. | Positive target size in tokens for embedding chunks. |
| `chunk_overlap` | Current runtime default, initially 50. | Non-negative token overlap, smaller than `chunk_size`. |
| `max_source_size_bytes` | System file-size limit, initially 100 MiB. | Positive per-call limit no greater than `SystemConfig.max_source_file_bytes`. |
| `max_document_pages` | System page limit, initially 1,000. | Positive per-call limit no greater than the matching system ceiling. |
| `operation` | `None`, creating an operation for the task. | Reuse an existing operation when composing work. |

All controls after `source_path` are keyword-only. The facade uses defaults
when their value is `None`; explicit choices apply to this ingestion only and
do not edit the saved runtime settings. The complete signature is linked
above rather than repeated here.

### 5.4 Reading files and sections

Navigation reads stored data without calling a model. The API's section reads
return full metadata and `raw_content`; they are not the smaller views exposed
by the agent's navigation tools.

`list_knowledge_files(knowledge_name)` returns file dictionaries with `file_id`,
`file_name`, `section_count`, `chunk_count`, `ingested_at`, and `navigation_type`.
The timestamp is a UTC ISO-8601 string. These are the stored file records, not
paths to retained original documents.

`list_file_sections(knowledge_name, file_name)` returns sections in document
order. Each contains its IDs, `section_index`, metadata, `raw_content`,
`source_element_ids`, and `source_range`. `get_knowledge_section()` returns
one section by its full section ID. The optional `file_name` is an assertion:
the section must belong to that exact filename, or the call fails.

Section IDs have the form `<file_id>-<section_index>`, with indices beginning
at 1. The suffix is the document-order index, not a separate random identity.
`navigation_type` belongs to the file; `source_range` belongs to each section.
The range is an inclusive pair for the supported navigation system, such as
PDF pages. TXT and Markdown use `navigation_type="none"` and no page range.
Page information for parsed files depends on the parser's available provenance.

`count_knowledge_files()` counts stored files.
`count_knowledge_vectors()` counts vector points, not files or sections.
Similarly, the `count` field returned by `list_knowledges()` is a vector-point
count. It should not be labelled as a file count in an interface.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.count_knowledge_files()</figcaption>

```python
async def count_knowledge_files(self, knowledge_name: str) -> int
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.count_knowledge_vectors()</figcaption>

```python
async def count_knowledge_vectors(self, knowledge_name: str) -> int
```

</figure>

The [`file-list`](#raven-list-knowledge-files),
[`section-list`](#raven-list-file-sections), and
[`section-lookup`](#raven-get-knowledge-section) signatures are in the overview.
These asynchronous reads return their values directly after awaiting them;
they do not return `OperationTask`. `get_knowledge()` and
`get_knowledge_details()` are synchronous reads.

### 5.5 Reading bounded pages

For larger stores, use the page methods instead of loading every record at
once. They return Python lists, not an HTTP response containing `items` and
`next_cursor`. Pass a positive integer `limit` and use the final item's cursor
for the next request. An empty list means there are no more items after it.

| Method | Required arguments | Initial cursor | Next cursor |
| --- | --- | --- | --- |
| `list_knowledges_page()` | Keyword `limit`. | `after_name=None` | Last item's `name`; pages are name-ordered. |
| `list_knowledge_files_page()` | `knowledge_name`, keyword `limit`. | `after_file_id=None` | Last item's `file_id`; pages follow insertion order. |
| `list_file_sections_page()` | `knowledge_name`, `file_name`, keyword `limit`. | `after_section_index=0` | Last item's `section_index`; pages follow document order. |

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_knowledges_page()</figcaption>

```python
async def list_knowledges_page(
    self,
    *,
    limit: int,
    after_name: str | None = None,
) -> list[dict[str, Any]]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_knowledge_files_page()</figcaption>

```python
async def list_knowledge_files_page(
    self,
    knowledge_name: str,
    *,
    limit: int,
    after_file_id: str | None = None,
) -> list[dict[str, Any]]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_file_sections_page()</figcaption>

```python
async def list_file_sections_page(
    self,
    knowledge_name: str,
    file_name: str,
    *,
    limit: int,
    after_section_index: int = 0,
) -> list[dict[str, Any]]
```

</figure>

The ingestion program demonstrates the complete section-page loop. Its cursor
starts at 0, advances to the last section's index after each page, and stops
on an empty page. Names, file IDs, and section indices have different meanings;
do not reuse one kind of cursor for another listing. Full knowledge listings
include `safe_name`; the paged knowledge listing instead returns `name`,
`user_summary`, `count`, and `created_at` for each item.

### 5.6 Deletion, duplicates, and retained sources

File and knowledge deletion return tasks. Await their results before treating
the resources as removed.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.delete_knowledge_file()</figcaption>

```python
async def delete_knowledge_file(
    self,
    knowledge_name: str,
    file_id: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.delete_knowledge()</figcaption>

```python
async def delete_knowledge(
    self,
    name: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

**`delete_knowledge_file()` removes the file's stored sections and vectors.**
It requires `knowledge_name` and `file_id`, obtained from the ingestion result
or a file listing. It does not take the filename. Its task result contains
`knowledge`, `file_id`, and `file` identifying the removed record. It does not
delete the application's original source file.

**`delete_knowledge()` removes the entire named knowledge directory and its
document/vector storage.** Its successful task result is `None`. The
management program's `--delete` flag demonstrates this action on its own
example home. Back up material that needs to be retained before using deletion
against a real workspace.

A new ingestion of an already stored filename fails with `file_already_exists`.
That code is also used when another ingestion already holds a claim on the
same filename; the error message explains that the file is being ingested.
The example's skip-on-repeat branch is application behavior, not automatic
replacement by `Raven.ingest()`. An explicit retry of an interrupted ingestion
uses its saved file identity to reconcile uncommitted vectors or recognize an
already committed file; it does not mean a fresh ingestion call is a retry.
Retry is covered in the later failure-handling section.

RAVEN retains parsed sections, metadata, provenance, and vectors. It does not
keep a permanent original document. The library's ingestion snapshot is
temporary, so applications that need original-file download or page navigation
must retain that file themselves and associate it with the saved file identity.
RAVEN reconstruction uses its stored sections and can work without the original
binary. A browser upload's private retry copy has separate server retention
rules; it is not an archive of original documents.

<div class="page-jump-target" id="raven-retrieval" aria-hidden="true"></div>

## 6. Retrieving sections and reconstructing their sources

A direct retrieval request answers **which stored sections are relevant to
a question**. It does not write an assistant answer or save a conversation.
This is useful for a search interface, a source browser, or an application
that wants to supply the passages to its own model workflow.

### 6.1 Choosing a strategy and scope

[`Raven.retrieve()`](#raven-retrieve) takes two required arguments: `mode`
selects the search mechanism and scope; `user_query` supplies the question.
Both models must already be configured. Supply `knowledge_name` for a local
search. A global search considers this user's knowledge base; it does not
search another user's directories.

| Strategy | Local mode | Global mode | When to use it |
| --- | --- | --- | --- |
| Embedded | `local_embedded` | `global_embedded` | Facts, numbers, requirements, and specific details. Embedding similarity selects chunks, then returns their complete sections. |
| Hierarchical | `local_hierarchical` | `global_hierarchical` | Narrative context and relationships. The LLM scores stored metadata to select sections; this requires more model work. |
| Vector-conditioned | `local_vector_conditioned` | `global_vector_conditioned` | A balance of similarity search and contextual scoring. Vector matches narrow the search before the LLM scores metadata. |
| Agreement | `local_agreement` | `global_agreement` | Questions worth checking through both embedded and hierarchical retrieval. It costs more and preserves their separate results when they differ. |

Pass an exact mode string from the table or the corresponding `RetrievalMode`
enum member from `nraven.agent.policy`. **`"auto"` is a session choice, not a
direct retrieval mode.** Direct retrieval has no agent choosing a mechanism
on the application's behalf.

`top_k` is the requested result limit. Omit it to use the current
`RuntimeConfig.retrieval_top_k`; an explicit positive value must fit
`SystemConfig.max_retrieval_top_k`. Agreement applies this output limit to
each result group. It can therefore return more than `top_k` records in total.
`Raven.retrieve()` exposes the retrieval mode, query, knowledge name, and
result limit. Additional tuning options are available through the individual
pipeline methods.

The call returns an `OperationTask`. Await its `.result()` to obtain the
retrieved records or receive the task's failure. Merely receiving the task
does not mean the search has finished.

### 6.2 Reading the result without losing its structure

Embedded, hierarchical, and vector-conditioned retrieval return a list.
Every record has exactly these four fields:

| Field | Meaning |
| --- | --- |
| `knowledge_name` | The knowledge containing the source file. |
| `file_name` | The stored filename within that knowledge. |
| `section_id` | The file's stored ID followed by the section's integer position, separated by `-`. Use this stored identifier rather than constructing one. |
| `raw_content` | The complete stored section text, not only its matching chunks. |

The output does not include similarity scores, chunk vectors, or section
metadata. An empty list means no sections were returned.

Agreement returns a dictionary with `agreement_type` and `retrieved_content`:

- **`Strong Agreement`**: the two underlying result lists are identical.
  `retrieved_content` is one list of the four-field records.
- **`Weak Agreement`**: the lists differ, but comparison of their corresponding
  entries finds no different file identity.
- **`Disagreement`**: comparison finds a different file identity at a
  corresponding position.

For the last two cases, `retrieved_content` is a dictionary with
`embedded_retrieval` and `hierarchical_retrieval`, each containing its own list.
These labels describe the pipelines' comparison, not the truth or safety of
an answer. In particular, equal empty lists can produce Strong Agreement.
Do not interpret that label alone as evidence that relevant material was found.

### 6.3 A complete search-and-reconstruction program

The following standalone program creates or reuses `engineering` in its own
`raven-retrieval-home`. It configures the local pair, ingests the supplied
document if that filename is absent, runs the selected retrieval, and
reconstructs its sources. Ollama must be running and the models from Section 4
must be installed. Use the downloadable
[Reaction_wheel.txt]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }})
as the input. As with the ingestion example, a repeated filename reuses the
saved content; it does not replace it with changes from disk.

Save this as `retrieve_sources.py`. Run
`python retrieve_sources.py Reaction_wheel.txt` for embedded retrieval, or
`python retrieve_sources.py Reaction_wheel.txt --mode local_agreement` to
inspect agreement's actual grouped output. Both invocations use the same home
and embedding model. Run them sequentially.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">retrieve_sources.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import ModelRole, ModelSpec, Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(Path.cwd() / "raven-retrieval-home")
    source = args.source.expanduser().resolve()
    try:
        await raven.start()
        configured = await raven.configure_models(
            ModelSpec(provider="ollama", model=args.llm, role=ModelRole.LLM),
            ModelSpec(provider="ollama", model=args.embedding, role=ModelRole.EMBEDDING),
        )
        await configured.result()
        if "engineering" not in {item["name"] for item in await raven.list_knowledges()}:
            created = await raven.create_knowledge("engineering")
            await created.result()
        files = await raven.list_knowledge_files("engineering")
        if not any(item["file_name"] == source.name for item in files):
            ingestion = await raven.ingest("engineering", source)
            print("Ingestion:", await ingestion.result(), flush=True)

        retrieval = await raven.retrieve(
            args.mode, args.query,
            knowledge_name="engineering" if args.mode.startswith("local_") else None,
            top_k=1,
        )
        records = await retrieval.result()
        print("Retrieval:", json.dumps(records, indent=2), flush=True)
        reconstruction = await raven.reconstruct(records)
        files = await reconstruction.result()
        print("Reconstruction:", json.dumps(files, indent=2))
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--mode", default="local_embedded", choices=[
        scope + "_" + strategy
        for scope in ("local", "global")
        for strategy in ("embedded", "hierarchical", "agreement", "vector_conditioned")
    ])
    parser.add_argument("--query", default="What reaction-wheel anomaly triggers safe-hold, and how quickly?")
    parser.add_argument("--llm", default="gemma4:e2b")
    parser.add_argument("--embedding", default="bge-m3:latest")
    asyncio.run(main(parser.parse_args()))
```

</figure>

**Retain the same embedding model for this home on later runs.** Existing
vectors cannot be searched reliably using an unrelated model, even when its
vector dimension happens to match. Rebuild the affected data if the embedding
model needs to change.

### 6.4 What reconstruction returns

[`Raven.reconstruct()`](#raven-reconstruct) accepts the result list or the
complete agreement dictionary directly. It also accepts reference-only
records containing `knowledge_name`, `file_name`, and `section_id`.
`raw_content` is not required as input: RAVEN reads the current stored text
from the identified knowledge. Repeated references to the same section are
combined, and references are grouped by knowledge and filename.

Awaiting the reconstruction task returns a list of file views:

| File field | Meaning |
| --- | --- |
| `knowledge_name`, `file_name` | Identify the file whose stored sections are shown. |
| `navigation_type` | File-level navigation, such as `page` or `none`. |
| `sections` | All stored sections of that file, in document order. |

Each element of `sections` contains `section_id`, `highlighted`, `raw_content`,
and `source_range`. `highlighted` is true for the supplied references and
false for the surrounding sections. A preserved page range such as `[12, 14]`
means pages 12 through 14, inclusive; an unavailable range is `None` in Python
(`null` when serialized to JSON).

This is a readable source view, not the original PDF/DOCX or a lossless layout
reproduction. The application decides how to render the highlights and how
to link a preserved range to an original file it has retained. Highlights
identify supplied sections; they do not verify every claim in a generated answer.

`None` or an empty reference list produces `[]`. A missing knowledge, file,
or referenced section produces an error rather than a fabricated source view.
Reconstruction does not require configured models, but it does require the
referenced stored material to remain available.

[`Raven.reconstruct_from_turn()`](#raven-reconstruct-from-turn) provides the
same file views for a saved answer. Supply its conversation ID and turn UUID.
RAVEN reads evidence references from successful canonical tool-result messages;
it does not rerun the agent, repeat retrieval, or rely on the earlier event log
still existing. A turn with no usable evidence returns `[]`. Section 8 includes
this call after a complete chat turn.

<div class="page-jump-target" id="raven-conversations" aria-hidden="true"></div>

## 7. Conversations, saved history, and preferences

A conversation is a persistent discussion. Its metadata identifies its scope,
title, and pinned state. Its storage retains completed turns, their messages,
derived model context, vector memory, and preferences. A session is the
temporary connection used to add a turn; creating a conversation does not
start inference.

### 7.1 Creating, finding, updating, and deleting a conversation

[`Raven.create_conversation()`](#raven-create-conversation) returns a task
whose result is a `Conversation` object:

- Omit `knowledge_name` to create a **global** conversation.
- Supply an existing knowledge name to create a **local** conversation bound
  to that knowledge. The binding is not changed by updating its title or pin.
- Pass the resulting object to [`Raven.session()`](#raven-session).
  Other facade methods identify the saved conversation by its
  `conversation_id` string. Treat that ID as opaque; it is not the UUID of a turn.

Use `get_conversation(id)` to obtain a previously created object and
`get_conversation_details(id)` to read its metadata dictionary. The object
must belong to the same `Raven` instance when supplied to `raven.session()`;
reopen it through that instance rather than passing a handle owned elsewhere.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation_details()</figcaption>

```python
def get_conversation_details(self, conversation_id: str) -> dict[str, Any]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.update_conversation()</figcaption>

```python
async def update_conversation(
    self,
    conversation_id: str,
    *,
    title: str | None = None,
    pinned: bool | None = None,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.delete_conversation()</figcaption>

```python
async def delete_conversation(
    self,
    conversation_id: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_conversations_page()</figcaption>

```python
def list_conversations_page(
    self,
    *,
    limit: int,
    after_conversation_id: str | None = None,
) -> list[dict[str, Any]]
```

</figure>

`update_conversation()` accepts `title`, `pinned`, or both. Its task result
is the updated `Conversation`; `.to_dict()` returns metadata. Leaving a field
as `None` preserves its current value; supplying neither changes nothing.
A manual title marks the conversation titled, preventing later automatic
title generation from replacing it.

`list_conversations()` returns all registered metadata records. For a bounded
listing, `list_conversations_page()` returns newest-first records. Start with
no cursor, then pass the final record's `conversation_id` as
`after_conversation_id`. The cursor identifies a conversation, not a page
number. Continue until an empty page; a cursor that no longer exists raises
`conversation_not_found`.

**`delete_conversation()` permanently removes the conversation's persisted
data, including messages, memory, and preferences.** Await its task result
(`None` on success) before treating deletion as complete. Closing a session
or `Raven` is a different action and retains that data.

### 7.2 A complete conversation-management program

This program needs no models. It creates a global conversation in its own
`raven-conversation-home`, sets a manual title and pin, saves a preference,
demonstrates removal by ID, and prints metadata and history. Each run creates
a new conversation; it does not reopen the last one automatically. With
`--delete`, it removes only the conversation created by that invocation.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">manage_conversation.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(Path.cwd() / "raven-conversation-home")
    try:
        await raven.start()
        created = await raven.create_conversation()
        conversation = await created.result()
        conversation_id = conversation.conversation_id
        print("Conversation:", conversation_id)
        updated = await raven.update_conversation(
            conversation_id, title="Engineering discussion", pinned=True
        )
        print("Metadata:", json.dumps((await updated.result()).to_dict(), indent=2))

        saved = await raven.save_conversation_preference(
            conversation_id, "Use concise answers unless detail is requested."
        )
        preference = await saved.result()
        print("Saved preference:", preference)
        removed = await raven.remove_conversation_preference(
            conversation_id, preference["preference_id"]
        )
        print("Removed preference:", await removed.result())
        print("Preferences:", await raven.get_conversation_preferences(conversation_id))
        print("Messages:", await raven.get_conversation_messages(conversation_id))
        print("Newest conversations:", raven.list_conversations_page(limit=5))
        print("Discovery issues:", [
            issue.model_dump(mode="json") for issue in raven.list_discovery_issues()
        ])
        if args.delete:
            deletion = await raven.delete_conversation(conversation_id)
            await deletion.result()
            print("Deleted:", conversation_id)
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--delete", action="store_true")
    asyncio.run(main(parser.parse_args()))
```

</figure>

The initial message list is empty because management calls do not create
chat turns. The next section supplies a complete model-backed turn.

### 7.3 Loading actual messages and individual turns

[`Raven.get_conversation_messages()`](#raven-get-conversation-messages)
returns the complete canonical transcript as LlamaIndex `ChatMessage` objects.
For a successful tool-using turn, that sequence includes:

1. The user message.
2. Assistant messages requesting tools and the corresponding tool-result messages.
3. The final assistant response.

Tool calls can be structured blocks, so `.content` alone is not a complete
serialization of every message. Use `message.model_dump(mode="json")` when
the full model-compatible representation is required.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation_messages_page()</figcaption>

```python
async def get_conversation_messages_page(
    self,
    conversation_id: str,
    *,
    limit: int,
    after_message_id: int = 0,
) -> list[dict[str, Any]]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation_turn()</figcaption>

```python
async def get_conversation_turn(
    self,
    conversation_id: str,
    turn_id: UUID | str,
) -> dict[str, Any]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation_turn_messages()</figcaption>

```python
async def get_conversation_turn_messages(
    self,
    conversation_id: str,
    turn_id: UUID | str,
) -> list[ChatMessage]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation_turn_messages_page()</figcaption>

```python
async def get_conversation_turn_messages_page(
    self,
    conversation_id: str,
    turn_id: UUID | str,
    *,
    limit: int,
    after_message_id: int = 0,
) -> list[dict[str, Any]]
```

</figure>

For a UI, `get_conversation_messages_page()` returns bounded records with
`message_id`, `turn_id`, `message_order`, and `message`. **In the Python API,
`message` is a `ChatMessage` object**, not an already serialized JSON object.
`message_order` is its position within the turn. Start `after_message_id` at
zero, then use the last record's `message_id` for the next page. These are
durable increasing IDs, not array offsets.

`get_conversation_turn()` retrieves a committed turn using the UUID from
`run.turn_id`. Its dictionary contains `turn_id`, `operation_id`, `user_query`,
`result`, `memory_indexed`, and `committed_at`. The `result` holds the saved
run result when available; `memory_indexed` reports whether the turn's semantic
memory indexing has completed. An unknown turn raises
`conversation_turn_not_found`.

For the messages belonging to that turn, use `get_conversation_turn_messages()`
or its paged counterpart. The paged version uses the same durable message IDs
as full-history pagination, even though it filters to one turn. There is no
need to extract a turn by guessing where user/assistant pairs begin or end.

This transcript is separate from **model context**. RAVEN can summarize older
context to fit the memory budget while preserving the full canonical messages
for the interface. The application should load canonical messages for its
history view, rather than displaying a compacted summary as if it were the
original exchange.

### 7.4 Saving and removing conversation-local preferences

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_conversation_preferences()</figcaption>

```python
async def get_conversation_preferences(
    self,
    conversation_id: str,
) -> list[dict[str, str]]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.save_conversation_preference()</figcaption>

```python
async def save_conversation_preference(
    self,
    conversation_id: str,
    text: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.remove_conversation_preference()</figcaption>

```python
async def remove_conversation_preference(
    self,
    conversation_id: str,
    preference_id: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

A preference is an explicit instruction for this conversation, represented
by `{"preference_id": "<UUID>", "text": "<instruction>"}`. Saving nonempty text
returns that record through an operation task. Saving the same trimmed text
again returns its existing record, rather than creating a second ID.
Listing returns records directly without changing them. Removal returns the
removed record through a task and requires its exact `preference_id`, not
a rephrased instruction. An invalid or absent ID produces an error.

Preferences are included in the conversation's system policy. Saved changes
are used when the prompt is next obtained for an agent run; they do not
rewrite past messages or retroactively change a response already being
generated. They are not shared with other conversations. The agent also has
preference tools for explicit user requests, so the application need not
parse such requests and imitate tool execution itself.

### 7.5 Inspecting resources that could not be discovered

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_discovery_issues()</figcaption>

```python
def list_discovery_issues(self) -> list[DiscoveryIssue]
```

</figure>

`list_discovery_issues()` reports invalid knowledge or conversation resources
found during discovery. Each `DiscoveryIssue` has `resource_type`,
`directory_name`, `code`, and `message`; `.model_dump(mode="json")` serializes
it. A malformed resource is excluded from normal listings while other valid
resources remain available. This method reports the problem; it does not
repair the directory, delete it, or make it safe to overwrite. A missing item
should therefore be checked against discovery issues before assuming it was
never saved.

<div class="page-jump-target" id="raven-sessions" aria-hidden="true"></div>

## 8. Sessions and streamed responses

A `Session` connects a conversation to RAVEN's agent. It holds the interaction
settings and accepts questions; it is not saved as another conversation on
disk. After it is closed, a new session can reopen the same conversation
and continue using its saved history.

### 8.1 Preparing a session and choosing retrieval tools

Configure the models first. Obtain a conversation from the same `Raven`,
pass that **object** to [`raven.session(conversation)`](#raven-session), then
await `session.start()`. This prepares the session to accept turns. It does
not send a question. `generate_response()` starts the work and returns a
`SessionRun`, which identifies that specific turn.

The optional `raven.session()` overrides have distinct purposes:

- `max_iterations` limits the agent's tool-using loop.
- `top_k` controls retrieval result limits used by that session's tools.
- `memory_token_limit` sets the budget for model-facing conversation memory.
  It is not a request to resize the model's physical context window.
- `memory_top_k` limits semantic memory recall results.

Omitting these arguments captures the current runtime defaults when the
session is created. Later runtime changes do not reconfigure an existing
session. Explicit values must satisfy system policy. Allow enough context
for the prompt, tools, retrieved sections, and a useful response; a tiny
memory budget can prevent the turn from running.

`generate_response(user_query, retrieval_mode=...)` controls which retrieval
tools the model can use **for this turn**:

| Conversation | Omitted mode or `"auto"` | Exact mode |
| --- | --- | --- |
| Local | The four local strategies, bound to its knowledge. | One permitted local strategy. A global mode is rejected. |
| Global | Local and global strategies. | One permitted local or global strategy. A local tool still needs a knowledge name, supplied through the query or discovered by the agent. |

This filters retrieval-tool exposure; it does not remove navigation, memory,
or preference tools. It also does not force a tool call for a casual question.
For a document-grounded answer, explicitly request retrieval in the query.
Small models can otherwise answer from general knowledge or select an
unhelpful tool sequence. The chosen mode restricts what is permitted, not
whether the model will reason well.

### 8.2 A complete streamed turn, saved history, and source inspection

The following program uses its own `raven-session-home`, creates or reuses
`engineering`, and ingests the supplied file if absent. It creates a new
local conversation unless `--conversation-id` identifies one already saved
in this same home. On reuse, it checks that the conversation is local to
`engineering`. Ollama and the models from Section 4 are required.

Save this as `stream_session.py` and run
`python stream_session.py Reaction_wheel.txt`. To follow up, run it again with
the printed `--conversation-id` and a new `--query`. Keep the same embedding
model. The source path supplies ingestion input on the first run; later runs
reuse the saved filename, as in Section 6.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">stream_session.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import EventType, ModelRole, ModelSpec, Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(Path.cwd() / "raven-session-home")
    session = None
    source = args.source.expanduser().resolve()
    try:
        await raven.start()
        configured = await raven.configure_models(
            ModelSpec(provider="ollama", model=args.llm, role=ModelRole.LLM),
            ModelSpec(provider="ollama", model=args.embedding, role=ModelRole.EMBEDDING),
        )
        await configured.result()
        if "engineering" not in {item["name"] for item in await raven.list_knowledges()}:
            created = await raven.create_knowledge("engineering")
            await created.result()
        files = await raven.list_knowledge_files("engineering")
        if not any(item["file_name"] == source.name for item in files):
            ingestion = await raven.ingest("engineering", source)
            print("Ingestion:", await ingestion.result(), flush=True)

        if args.conversation_id:
            conversation = raven.get_conversation(args.conversation_id)
            if conversation.knowledge_name != "engineering":
                raise ValueError("Choose a conversation local to engineering.")
        else:
            created = await raven.create_conversation("engineering")
            conversation = await created.result()
        conversation_id = conversation.conversation_id
        print("Conversation:", conversation_id, flush=True)
        session = raven.session(conversation)
        await session.start()
        run = await session.generate_response(args.query, retrieval_mode="local_embedded")
        print("Turn:", run.turn_id, "Operation:", run.operation_id, flush=True)
        async for event in run.stream:
            if event.type == EventType.CHAT_THINKING_DELTA:
                print("[thinking]", event.data.get("delta", ""), flush=True)
            elif event.type == EventType.CHAT_TOOL_CALL:
                print("\n[tool call]", event.data.get("name"), flush=True)
            elif event.type == EventType.CHAT_TOOL_RESULT:
                print("[tool result]", event.data.get("name"), event.data.get("ok"), flush=True)
            elif event.type == EventType.CHAT_RESPONSE_DELTA:
                print(event.data.get("delta", ""), end="", flush=True)
            elif event.type == EventType.RECONSTRUCTION_FILE:
                print("\n[source]", event.data["knowledge_name"], event.data["file_name"], flush=True)
            elif event.type in {
                EventType.CONVERSATION_MEMORY_COMPACTION_STARTED,
                EventType.CONVERSATION_MEMORY_COMPACTION_COMPLETED,
                EventType.CONVERSATION_MEMORY_COMPACTION_FAILED,
            }:
                print("\n[memory]", event.type.value, flush=True)

        result = await run.collect()
        print("\nCollected:", json.dumps(result.model_dump(mode="json"), indent=2))
        await session.close()
        session = None
        turn = await raven.get_conversation_turn(conversation_id, run.turn_id)
        print("Saved turn:", json.dumps(turn, indent=2))
        cursor = 0
        while True:
            page = await raven.get_conversation_messages_page(
                conversation_id, limit=5, after_message_id=cursor
            )
            if not page:
                break
            for record in page:
                print("Saved message:", record["message_id"], record["turn_id"],
                      json.dumps(record["message"].model_dump(mode="json")))
            cursor = page[-1]["message_id"]
        reconstruction = await raven.reconstruct_from_turn(conversation_id, run.turn_id)
        print("Sources from saved turn:", json.dumps(await reconstruction.result(), indent=2))
    finally:
        try:
            if session is not None:
                await session.close()
        finally:
            await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--conversation-id")
    parser.add_argument("--query", default=(
        "Use embedded retrieval in engineering to find the reaction-wheel safe-hold timing. "
        "Report the numbers and units from the document."
    ))
    parser.add_argument("--llm", default="gemma4:e2b")
    parser.add_argument("--embedding", default="bge-m3:latest")
    asyncio.run(main(parser.parse_args()))
```

</figure>

The event branches keep thinking, tool activity, response text, and sources
separate. The application can render them differently. Thinking is only
available when the model/provider exposes it. Automatic reconstruction uses
the agent's successful evidence references; a turn with no evidence can
complete with no source files.

If the agent attempts a knowledge-content tool but obtains no usable evidence,
the harness substitutes an insufficient-evidence response rather than presenting
that attempted search as a grounded answer. A successful tool call and useful
source evidence are separate things; check the result's evidence and source
views when presenting document-backed information.

The final `reconstruct_from_turn()` call runs after the session is closed.
It uses persisted tool evidence, demonstrating that source inspection need
not depend on keeping that temporary session alive. The message-page loop
also shows why a canonical `ChatMessage` is serialized explicitly rather than
passed directly to `json.dumps()`.

### 8.3 What the run handle and collected result mean

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">SessionRun.events()</figcaption>

```python
async def events(self, after_event_id: int = 0) -> AsyncIterator[Event]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">SessionRun.collect()</figcaption>

```python
async def collect(self) -> AgentRunResult
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">SessionRun.wait()</figcaption>

```python
async def wait(self) -> OperationStatus
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">SessionRun.cancel()</figcaption>

```python
async def cancel(self) -> None
```

</figure>

`SessionRun` provides these ways to follow the **same** submitted turn:

- `run.stream` creates an iterator over its replayed and live events,
  including its nested tasks. `run.events(after_event_id=...)` starts after
  a saved numeric event cursor instead. A new iterator can replay events;
  reading one does not erase them.
- `await run.collect()` waits for the session task and returns its
  `AgentRunResult`, or raises its failure. Internally the agent result is
  assembled from events, then the session saves the turn before returning it.
  Calling `collect()` after streaming does not ask the model a second time.
- `await run.wait()` waits and returns the terminal `OperationStatus`.
  It does not return the answer or raise the task's result error; inspect the
  status and use `collect()` when that error should reach the caller.
- `await run.cancel()` requests cancellation of this turn and waits for its
  task to finish. A UI's Stop action can await this from a separate handler
  while another coroutine is reading the stream. Abandoning the iterator is
  not a cancellation request.

`run.operation_id` identifies the containing operation; `run.task` exposes
the session's `OperationTask`; `run.status` is that task's status.
`run.turn_id` is the UUID used to find the saved turn. Store the conversation
ID and turn UUID together when an interface needs to reopen this answer.

The collected `AgentRunResult` has the following fields:

| Field | Meaning |
| --- | --- |
| `operation_id` | The operation producing this result. |
| `response` | Final assistant text. |
| `thinking` | Collected thinking deltas, or an empty string when none are exposed. |
| `iteration_limit_reached` | Whether the tool-loop limit forced final-response generation. |
| `tool_calls` | Tool names, arguments, call IDs, success/error status, and UI summaries. These records do not contain the complete raw tool-result messages. |
| `evidence` | Source references: `knowledge_name`, `file_name`, and `section_id`. |
| `reconstructed_sources` | The reconstructed file views collected from reconstruction events. |

Use `.model_dump(mode="json")` for a JSON-compatible dictionary, as in the
example. The result does not contain the turn UUID; take that from the run.

### 8.4 Completion, memory, cancellation, and competing turns

**A successful new turn finishes in this order:**

1. The agent finishes its answer and reconstructs any sources it collected.
   It publishes `chat.completed`. At this point, the session still has work
   to do before the turn is saved.
2. The session saves the user message, assistant tool calls, tool results,
   final assistant response, and collected run result as one turn in
   `messages.sqlite3`. It then updates the conversation's model context and
   vector memory.
3. If the conversation is untitled, the session asks the LLM to generate a
   title through a separate completion. That request and its response are
   excluded from the conversation's messages.
4. The session task completes, and `await run.collect()` returns its
   `AgentRunResult`.

Use successful completion of `run.collect()` to confirm that the session
finished saving the turn. Response text arriving on the stream, or even a
`chat.completed` event, comes before that confirmation.

If title generation fails, the saved answer remains available and the session
can still complete successfully. A later completed turn tries again while
the conversation remains untitled. Once a title is generated or set through
`update_conversation()`, automatic generation leaves it alone.

**After cancellation or failure, check whether the turn was saved.** The
outcome depends on how far the session progressed:

- If the turn has not been saved, the event log may contain partial response
  text while the conversation's message history contains no completed turn.
- If the turn was already saved, a later cancellation or error leaves it in
  history. This can happen while updating memory or generating the title.
  A database write already in progress can also finish before cancellation
  takes effect.

Use `Raven.get_conversation_turn(conversation_id, run.turn_id)` to check that
specific turn. It returns the stored record when present and raises
`conversation_turn_not_found` when absent. A cancelled or failed task status
alone does not establish whether the transcript was saved.

**Saved history and model memory serve different purposes:**

- **Saved history** contains the original messages for the application's
  conversation view. Tool calls and results are included, allowing later
  model context to retain those interactions and reconstruction to find
  their source references.
- **Compacted context** contains recent messages and a summary of older
  history for the model's next response. Summarizing this context leaves the
  original saved messages intact. Compaction emits a started event, followed
  by a completed or failed event, so the interface can explain the delay.
- **Vector memory** supports searching earlier discussion. It indexes the
  original committed user messages and final assistant responses, rather
  than replacing them with the compacted summary.

Thinking is stored in operation events and the turn's collected result, but
is excluded from the message sequence used as conversation history. The
harness supplies the current system prompt, including conversation preferences,
separately from that history.

**The iteration limit stops further tool use.** `max_iterations` bounds the
agent's tool-using loop. When the limit is reached, RAVEN publishes
`chat.max_iterations` and attempts a final answer without another round of
tool use. If that succeeds, the result has `iteration_limit_reached=True`.
That answer may have incomplete research because the agent ran out of
iterations. If final answer generation fails, `run.collect()` raises the
task's error instead of returning a successful result.

**Each conversation accepts one active turn at a time.** For example, two
application windows may each create a session for the same conversation.
While the first window's turn is running, a submission from the second is
rejected with `conversation_turn_active`. The application can wait for the
active turn to finish or cancel it before submitting the next question.
RAVEN does not queue that competing submission automatically.

Different conversations can run concurrently within configured resource
limits. Closing a session cancels its own active turns and leaves the saved
conversation available to other sessions. Close sessions before closing
their owning `Raven` instance.

<div class="page-jump-target" id="raven-operations" aria-hidden="true"></div>

## 9. Operations, tasks, and event history

A request such as ingestion or a session turn can involve several methods
and many updates before it finishes. An **operation** groups that complete
action under one ID and one ordered event history. A **task** identifies one
method invocation within it, with its own status and result. An **event** is
an update published while the work runs or changes status.

Earlier examples let each method create its operation automatically. This
section shows how an application can inspect those operations, replay their
events, and put several component calls inside one operation of its own.

### 9.1 Understanding the handles, records, and IDs

The objects have different responsibilities:

| Object | What it provides | What remains after restarting the process |
| --- | --- | --- |
| `Operation` | Live operation status, task execution, event access, and cancellation. | Its persisted status and events can be reopened through a new handle. Its original worker does not restart. |
| `OperationTask` | The handle returned for one invocation. Await `.result()` for its Python return value or error. | A persisted task record can be read by ID; it is not a replacement for the original live handle. |
| `OperationRecord` | Stored operation name, status, timestamps, last event ID, and error. | The record remains until operation-history cleanup deletes it. |
| `OperationTaskRecord` | Stored task name, status, timestamps, retry input/policy, attempt links, and error. | The record remains with its owning operation. It does not store arbitrary Python return objects. |
| `Event` | A typed update with event-specific `data` and operation/task identifiers. | Retained events can be read again without rerunning the work. |

Keep `task.operation_id` and `task.task_id` when a caller needs to find the
work later. Both are UUIDs; treat them as opaque identifiers. A session run
also has a separate `turn_id`, which identifies the conversation turn rather
than its operation task.

Records and events are stored together in the user's
`raven_home/<user_id>/operations/operations.sqlite3`. Reopening the same home
and user ID makes that retained history available again.

**Operation history is not a general-purpose result store.** The live task
returns the method's native result. After restart, read the resource that
method saved, such as a conversation turn or knowledge file. Some domain
events also carry useful output, such as a reconstructed file, but the generic
operation/task records do not preserve every method's Python return value.

An operation and its tasks use `queued`, `running`, `completed`, `failed`,
and `cancelled` statuses. The last three are terminal: that execution has
finished. Once an operation is terminal, it cannot accept another task.

### 9.2 Running application work inside an operation

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.run_operation()</figcaption>

```python
async def run_operation(
    self,
    name: str,
    worker: TaskWorker,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.create_operation()</figcaption>

```python
async def create_operation(self, name: str) -> Operation
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Operation.run()</figcaption>

```python
async def run(
    self,
    name: str,
    worker: TaskWorker,
    *,
    retry_input: dict[str, Any] | None = None,
    retry_of: OperationTaskRecord | None = None,
) -> "OperationTask"
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.submit_operation()</figcaption>

```python
async def submit_operation(
    self,
    name: str,
    worker: OperationWorker,
) -> Operation
```

</figure>

A worker is an async function accepting an `Operation`. It performs the
work, optionally publishes events, and returns a result:

- `run_operation(name, worker)` creates the operation, starts the worker, and
  returns an `OperationTask`.
- `create_operation(name)` creates a queued operation. Calling
  `operation.run(name, worker)` starts its first, or **root**, task. The root
  task name must match the operation name.
- `submit_operation(name, worker)` starts the same kind of root work but
  returns its `Operation`. Use it when operation-wide events/status are the
  caller's main concern; use `run_operation()` when the task result is needed.

Creating an operation alone does not run anything. Start its root task, or
cancel it if the application abandons that request. For application-defined
names, use a stable nonempty string such as `example.prepare_discussion`.
Reserve RAVEN's `OperationType` names for the corresponding built-in work;
using one does not turn an arbitrary worker into that built-in operation.

The following program needs no models. Its worker creates a knowledge and a
local conversation, then sets the title. Each component call receives the
same operation and becomes a child task. The worker awaits each child before
using its result. All events share one operation ID.

Save it as `shared_operation.py` and run `python shared_operation.py`.
It uses its own `raven-operations-home`. Each run creates a new, uniquely
named knowledge and conversation so that existing resources are retained.
Run with `--entry run` to use `raven.run_operation()` instead of explicitly
creating the operation; the worker and its child calls remain the same.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">shared_operation.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from nraven import Event, EventType, Operation, Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(Path.cwd() / "raven-operations-home")
    name = "example.prepare_discussion"
    knowledge_name = "engineering-" + uuid4().hex[:8]

    async def worker(operation: Operation) -> dict[str, str]:
        created = await raven.create_knowledge(knowledge_name, operation=operation)
        await created.result()
        created = await raven.create_conversation(knowledge_name, operation=operation)
        conversation = await created.result()
        updated = await raven.update_conversation(
            conversation.conversation_id, title="Engineering discussion", operation=operation
        )
        await updated.result()
        await operation.publish(Event(
            type=EventType.WORK_PROGRESS,
            data={"stage": "prepared", "knowledge_name": knowledge_name},
        ))
        return {"knowledge_name": knowledge_name,
                "conversation_id": conversation.conversation_id}

    try:
        await raven.start()
        if args.entry == "run":
            task = await raven.run_operation(name, worker)
        else:
            operation = await raven.create_operation(name)
            task = await operation.run(name, worker)
        print("Operation:", task.operation_id, "Task:", task.task_id, flush=True)
        async for event in task.events():
            print(event.event_id, event.type.value, event.task_name, flush=True)
        print("Result:", json.dumps(await task.result(), indent=2))
        record = await raven.get_operation_record(task.operation_id)
        print("Operation record:", json.dumps(record.model_dump(mode="json"), indent=2))
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--entry", choices=("create", "run"), default="create")
    asyncio.run(main(parser.parse_args()))
```

</figure>

The first task is the root. Tasks started inside its worker are children;
two tasks started by the same parent are siblings. The live task's
`parent_task_id` identifies its parent. Pass the active operation into nested
calls; omitting it lets those methods create separate operations instead.

A root operation waits for its active child tasks before completing. A child
can finish while the parent continues working. Awaiting the root task's
`.result()` waits for that whole operation; awaiting a child's `.result()`
waits for that invocation. Use operation status for the complete action and
task status for an individual invocation. If a parent awaits a child's
`.result()`, catches its error, and handles it, the parent can still succeed;
the child's own record remains failed. An unhandled child error, or a child
failure that the parent never observes, prevents successful operation completion.
Creating an operation does not provide a database transaction across all
the component calls: earlier successful writes are not automatically undone
when a later child fails.

### 9.3 Finding work and reading bounded history

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_operation_record()</figcaption>

```python
async def get_operation_record(
    self,
    operation_id: UUID | str,
) -> OperationRecord
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_operation_task()</figcaption>

```python
async def get_operation_task(
    self,
    operation_id: UUID | str,
    task_id: UUID | str,
) -> OperationTaskRecord
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_operations()</figcaption>

```python
async def list_operations(
    self,
    *,
    status: OperationStatus | str | None = None,
    limit: int | None = None,
    after_operation_id: UUID | str | None = None,
) -> list[OperationRecord]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_operation_tasks()</figcaption>

```python
async def list_operation_tasks(
    self,
    operation_id: UUID | str,
    *,
    status: OperationStatus | str | None = None,
    limit: int | None = None,
    after_task_id: UUID | str | None = None,
) -> list[OperationTaskRecord]
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.wait_operation()</figcaption>

```python
async def wait_operation(self, operation_id: UUID | str) -> Operation
```

</figure>

[`get_operation()`](#raven-get-operation) returns an `Operation` handle.
`get_operation_record()` reads its persisted status without replaying all
events. `get_operation_task()` reads one task record and checks that it
belongs to the supplied operation.

`list_operations()` and `list_operation_tasks()` return newest-first pages.
Use `status` to select one state, or omit it for all states. `limit` must be
positive; omitting it uses `SystemConfig.operation_page_size`. For the next
page, pass the last record's `operation_id` or `task_id` as the corresponding
`after_...` cursor. A cursor must identify a stored record in that listing's
scope; it is not a page number or an arbitrary UUID.

`wait_operation(id)` waits for the root execution and returns its `Operation`.
It does not raise the worker's result error. Inspect the returned status/error,
or await the original task's `.result()` when the caller needs that exception.
On a reopened terminal operation there is no worker left to wait for, so the
method returns without restarting it. Calling it on a merely created, queued
operation also does not start a worker or mark the operation completed.

### 9.4 Replaying events and following live work

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.read_operation_events()</figcaption>

```python
async def read_operation_events(
    self,
    operation_id: UUID | str,
    *,
    after_event_id: int = 0,
    limit: int | None = None,
) -> list[Event]
```

</figure>

[`operation_events()`](#raven-operation-events) is an async iterator. It
replays retained events after the supplied cursor, then waits for new events
until the operation finishes. `read_operation_events()` returns a finite
list of events already available. Give it a positive `limit` for a bounded
page; omitting it reads all retained events after the cursor. The live iterator
reads history internally in configured batches.

The event envelope tells an application how to interpret each update:

| Field | Meaning |
| --- | --- |
| `type` | An `EventType`; `.value` gives the event name as a string. |
| `data` | Payload specific to that event type, such as a progress stage or reconstructed file. |
| `operation_id` | The operation to which the event belongs. |
| `task_id`, `task_name` | The task publishing it, when available. |
| `event_id` | Increasing sequence number within this operation, starting at one. |
| `timestamp` | A UTC datetime; serialize with `.model_dump(mode="json")` for JSON. |
| `is_final` | True for the operation's terminal event. A nested task's completion does not finish the entire operation. |

`after_event_id` is exclusive. Save the ID only after handling an event;
a cursor of `12` resumes with retained events numbered above 12. Zero starts
at the beginning. A negative cursor raises `invalid_event_cursor`. A cursor
beyond the available history raises `event_history_gap`. After cleanup deletes
the operation, lookup raises `operation_not_found`; a cursor cannot recreate
deleted history.

`task.events()` follows that task and its descendants. For a root task it
includes the whole operation. For a child it ends at that child's terminal
task event, even if siblings or the parent are still running. Its event IDs
still come from the operation-wide sequence, so gaps between returned IDs
are expected when unrelated task events are filtered out.

The following standalone program reads an existing home's operation history
without models. After running `shared_operation.py`, save this program as
`operation_history.py` and run
`python operation_history.py --home raven-operations-home` to list recent
operations. Add `--operation-id` with an ID printed by that program to read
its tasks and replay its events. Add `--after-event-id` to resume after an
event already handled. A running operation keeps the replay iterator open
until it reaches a terminal state.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">operation_history.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(args.home.expanduser().resolve())
    try:
        await raven.start()
        page = await raven.list_operations(limit=10)
        print("Operations:", json.dumps([
            record.model_dump(mode="json") for record in page
        ], indent=2))
        if args.operation_id is None:
            return
        operation = await raven.get_operation(args.operation_id)
        print("Status:", operation.status.value)
        cursor = None
        while True:
            tasks = await raven.list_operation_tasks(
                args.operation_id, limit=10, after_task_id=cursor
            )
            if not tasks:
                break
            print("Tasks:", json.dumps([
                record.model_dump(mode="json") for record in tasks
            ], indent=2))
            cursor = tasks[-1].task_id
        snapshot = await raven.read_operation_events(
            args.operation_id, after_event_id=args.after_event_id, limit=5
        )
        print("Snapshot event IDs:", [event.event_id for event in snapshot])
        async for event in raven.operation_events(
            args.operation_id, after_event_id=args.after_event_id
        ):
            print("Replay:", json.dumps(event.model_dump(mode="json")), flush=True)
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--operation-id")
    parser.add_argument("--after-event-id", type=int, default=0)
    asyncio.run(main(parser.parse_args()))
```

</figure>

Each example uses the default user ID. For another user's history, the
application must reopen the same home **and user ID**; a home path alone does
not select a different user's records.

These Python methods yield `Event` objects. They do not produce HTTP SSE
frames or send heartbeat comments. The optional server serializes the events
for its SSE endpoints; the event cursor identifies the same underlying history.

<div class="page-jump-target" id="raven-failures" aria-hidden="true"></div>

## 10. Errors, explicit retry, and history cleanup

An application needs to distinguish an invalid request from work that started
and then failed. It also needs to decide whether to offer a retry and how
long to retain the history supporting that action. RAVEN provides the error,
retry, recovery, and deletion mechanisms; the application decides when to
request them.

### 10.1 Catching errors at the point they are raised

Import `RavenError` and `ErrorCode` from `nraven.core.errors`.
`error.code` is a stable enum for program logic, `error.message` is a readable
explanation, and `error.details` provides structured context. Prefer comparing
codes to matching words in the message. `.as_payload()` returns a
JSON-compatible error dictionary with `code`, `message`, and, when nonempty,
`details`.

Catch errors around both stages of an operation-producing call:

- **Before a task is returned:** validation may reject the request immediately.
  For example, creating an operation with an empty name raises
  `invalid_operation_name`.
- **After a task is returned:** the worker may fail later. Awaiting
  `task.result()` raises that error. For example, creating an already existing
  knowledge produces `knowledge_already_exists` through the task result.

Reading an event stream alone does not substitute for checking the task
result. It can expose failure events, but callers expecting a completed value
must still await `.result()` or, for a session, `.collect()`.

The examples catch expected `RavenError` failures. A custom worker can also
raise an ordinary Python exception, which its task's `.result()` propagates
to the caller. Not every library failure is converted into `RavenError`.

The following program deliberately demonstrates both errors and cancellation.
It uses its own `raven-errors-home`, creates or reuses `engineering`, and
requires no models. Its printed failures are expected outcomes. It catches
them so the program can continue and inspect the persisted task record.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">handle_errors.py</figcaption>

```python
import asyncio
import json
from pathlib import Path

from nraven import Event, EventType, Operation, Raven
from nraven.core.errors import ErrorCode, RavenError


async def main() -> None:
    raven = Raven(Path.cwd() / "raven-errors-home")
    started = asyncio.Event()

    async def wait_for_cancellation(operation: Operation) -> None:
        await operation.publish(Event(
            type=EventType.WORK_PROGRESS, data={"stage": "waiting_for_stop"}
        ))
        started.set()
        await asyncio.Event().wait()

    try:
        await raven.start()
        try:
            await raven.create_operation("")
        except RavenError as error:
            print("Immediate error:", error.code.value, error.message)
        if "engineering" not in {item["name"] for item in await raven.list_knowledges()}:
            created = await raven.create_knowledge("engineering")
            await created.result()
        duplicate = await raven.create_knowledge("engineering")
        try:
            await duplicate.result()
        except RavenError as error:
            print("Task error:", json.dumps(error.as_payload()))
        record = await raven.get_operation_task(duplicate.operation_id, duplicate.task_id)
        print("Failed task:", json.dumps(record.model_dump(mode="json"), indent=2))

        task = await raven.run_operation("example.wait_for_stop", wait_for_cancellation)
        await started.wait()
        operation = await raven.cancel_operation(task.operation_id)
        print("After cancellation:", operation.status.value)
        try:
            await task.result()
        except RavenError as error:
            if error.code != ErrorCode.OPERATION_CANCELLED:
                raise
            print("Cancelled task result:", error.code.value)
    finally:
        await raven.close()


if __name__ == "__main__":
    asyncio.run(main())
```

</figure>

[`cancel_operation()`](#raven-cancel-operation) cancels the complete operation,
including its active tasks. `task.cancel()` on a root has the same scope;
on a child, it targets that task. Cancellation is cooperative: a provider
request or a protected database write may need to finish before the worker
can stop. Already saved knowledge or conversation data is not undone by
cancellation. Use the resource inspection methods to determine what remains,
as explained for conversation turns in Section 8.4.

### 10.2 What reopening a home recovers

`Raven.start()` checks the user's saved operations for work left unfinished
by a previous process termination. It marks unfinished tasks and operations
failed with `operation_interrupted` and appends recovery failure events.
Reopening the history therefore reports an interrupted execution rather than
a task that appears to run forever without a worker.

Recovery does not restart the worker. It also cannot recreate a result that
was never saved or make every external side effect reversible. Inspect the
saved resource and task record, then decide whether to request an eligible
retry. Ingestion retry reconciles the saved file identity so a committed file
is not duplicated. Session retry retains the turn UUID and reuses an already
committed result when present. An uncommitted session turn can require running
the agent again; this is not general checkpoint-by-checkpoint continuation.

### 10.3 Checking retry eligibility and starting another attempt

The [`list_retryable_tasks()`](#raven-list-retryable-tasks) method returns
failed task records whose stored policy, input, error code, and attempt count
permit a user-confirmed retry. Tasks that already have a linked retry are
excluded from this list. It uses newest-first pages with `limit` and
`after_task_id`, as with task history. Its scope is all operations for the
current user.

| Built-in task type | What is retained for retry | Model requirements |
| --- | --- | --- |
| `ingestion.run` | Knowledge name, source information, file identity, and ingestion settings. | The LLM and the compatible embedding model must be configured again after restart. The source or retained snapshot must remain available. |
| `reconstruction.reconstruct` | Normalized knowledge/file/section references. | No model is required; the referenced stored material must be available. |
| `session.generate_response` | Conversation ID, turn UUID, user query, retrieval mode, and run settings. | Both models must be configured, and the conversation and required data must remain available. |

These types allow **three total attempts: the original execution plus at most
two retries**. Other task types use the `never` retry policy. A retryable task
type can also occur as a child, such as reconstruction within a session; an
eligible child can be retried independently through its own task ID.

For one record, `can_retry`, `attempt`, `max_attempts`, and `attempts_remaining`
describe its policy eligibility. `can_retry` is a computed property, so access
it directly rather than expecting it in `.model_dump()` output.
Eligibility requires a failed task, saved retry input, an approved error code,
and an attempt below the limit. Completed or cancelled tasks are ineligible.
The record's `can_retry` property alone does not check for an existing linked
retry; the listing and retry request perform that additional check.

[`retry_task(operation_id, task_id)`](#raven-retry-task) also checks prerequisites
when called. A listed task can still be rejected because it already has a
linked retry, its required models are unconfigured, or its retained browser
upload has expired. The listing is not a reservation or a promise of success.

A successful retry request returns a new `OperationTask`. It has a new
`operation_id` and `task_id`, an incremented `attempt`, and
`retry_of_operation_id` / `retry_of_task_id` identifying the failed attempt.
The previous record and events remain unchanged. A second request to retry
that same failed attempt raises `operation_task_already_retried`; its error
details identify the existing retry. If that new attempt fails and is still
eligible, request the next retry using the **new** operation/task IDs.

Save the following program as `retry_failed_task.py`. It is a command for an
existing home, not a program that deliberately creates a failure. First use
`operation_history.py` or the application's operation view to find the failed
operation and task IDs. Run it with `--home` and those two positional IDs;
the example uses the default user ID, as do the earlier scripts.
The positional order is `operation_id`, then `task_id`; `--home` takes the
workspace path. Run `python retry_failed_task.py --help` to display the command's
arguments and options.

For ingestion/session retry, the program configures the local pair before
retrying. Supply `--llm` and `--embedding` if the originals differ from the
defaults. **Use the embedding identity that generated the saved vectors.**
For cloud models, adapt the configuration block using the provider setup in
Section 4. Reconstruction retry does not configure or load models.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">retry_failed_task.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path

from nraven import ModelRole, ModelSpec, OperationType, Raven
from nraven.core.errors import RavenError


async def main(args: argparse.Namespace) -> None:
    raven = Raven(args.home.expanduser().resolve())
    try:
        await raven.start()
        record = await raven.get_operation_task(args.operation_id, args.task_id)
        print("Retry policy:", record.retry_policy.value,
              "can_retry:", record.can_retry,
              "attempts_remaining:", record.attempts_remaining, flush=True)
        if record.name in {
            OperationType.INGESTION_RUN.value,
            OperationType.SESSION_GENERATE_RESPONSE.value,
        }:
            configured = await raven.configure_models(
                ModelSpec(provider="ollama", model=args.llm, role=ModelRole.LLM),
                ModelSpec(provider="ollama", model=args.embedding, role=ModelRole.EMBEDDING),
            )
            await configured.result()
        task = await raven.retry_task(args.operation_id, args.task_id)
        print("New operation:", task.operation_id, "New task:", task.task_id, flush=True)
        async for event in task.events():
            print(event.event_id, event.type.value, flush=True)
        result = await task.result()
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        print("Retry result:", json.dumps(payload, indent=2))
        attempt = await raven.get_operation_task(task.operation_id, task.task_id)
        print("Attempt record:", json.dumps(attempt.model_dump(mode="json"), indent=2))
    except RavenError as error:
        print("Retry error:", json.dumps(error.as_payload()), flush=True)
        raise SystemExit(1)
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("operation_id")
    parser.add_argument("task_id")
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--llm", default="gemma4:e2b")
    parser.add_argument("--embedding", default="bge-m3:latest")
    asyncio.run(main(parser.parse_args()))
```

</figure>

The script waits for the retry's result and exits with an error if the new
attempt fails. Starting another attempt does not correct a missing source,
invalid provider response, or unavailable file. Resolve the cause before
offering Retry. A retry of `session.generate_response` returns its
`AgentRunResult` through `task.result()`; it does not return a `SessionRun`.

### 10.4 Running an explicit operation-history cleanup pass

Operation history takes disk space and may contain queries, source references,
and saved retry inputs. `OperationCleanupService` deletes a bounded batch of
terminal operations older than a chosen retention period. Construct it with
the started facade's `operation_manager`; that operation manager owns the
user's store.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OperationCleanupService.__init__()</figcaption>

```python
def __init__(
    self,
    manager: OperationManager,
    retention: timedelta,
    *,
    batch_size: int = DEFAULT_OPERATION_CLEANUP_BATCH_SIZE,
) -> None
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OperationCleanupService.run_once()</figcaption>

```python
async def run_once(
    self,
    now: datetime | None = None,
) -> OperationCleanupResult
```

</figure>

**Neither `Raven` nor the bundled FastAPI server schedules operation-history
cleanup automatically.** The application's bridge or host backend must call
`run_once()` when desired or arrange a periodic job. Setting retention and
interval fields in `SystemConfig` supplies policy values, not a running job.
The host owns starting/stopping that job and handling its failures.

This program needs no models. It creates or reuses a knowledge and completes
a sample operation in its own `raven-cleanup-home`, then runs one cleanup
pass. With no flags, it uses the system's default retention, so the newly
finished history is retained. Run `python cleanup_operations.py --expire-now`
to use zero retention in this example home and make its terminal history
immediately eligible for deletion. This flag affects that home's operation
history from previous example runs as well; it does not delete knowledges.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">cleanup_operations.py</figcaption>

```python
import argparse
import asyncio
from datetime import timedelta
import json
from pathlib import Path

from nraven import Operation, OperationCleanupService, Raven


async def main(args: argparse.Namespace) -> None:
    raven = Raven(Path.cwd() / "raven-cleanup-home")

    async def sample(operation: Operation) -> dict[str, bool]:
        operation.raise_if_cancelled()
        return {"example": True}

    try:
        await raven.start()
        if "keep-me" not in {item["name"] for item in await raven.list_knowledges()}:
            created = await raven.create_knowledge("keep-me")
            await created.result()
        task = await raven.run_operation("example.cleanup_sample", sample)
        await task.result()
        print("Sample operation:", task.operation_id)
        settings = raven.system_config
        seconds = 0 if args.expire_now else settings.operation_retention_seconds
        cleanup = OperationCleanupService(
            raven.operation_manager, retention=timedelta(seconds=seconds),
            batch_size=settings.operation_cleanup_batch_size,
        )
        result = await cleanup.run_once()
        print("Cleanup:", json.dumps(result.model_dump(mode="json"), indent=2))
        print("Knowledge retained:", raven.get_knowledge("keep-me").name)
    finally:
        await raven.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--expire-now", action="store_true")
    asyncio.run(main(parser.parse_args()))
```

</figure>

The result reports `deleted_operation_ids`, `skipped_operation_ids`, and
`failures` keyed by operation ID. A pass considers up to `batch_size` expired
operations and continues when an individual deletion fails. Active operations
are not eligible; a finished operation being read can be skipped. Check the
reported outcome instead of assuming every candidate was deleted.

Deleting an operation removes its task records, retry inputs, and events.
Those IDs can no longer be used for replay or retry. It does not remove the
knowledge files or conversation turns produced by the work. Conversely,
removing a knowledge or conversation is a separate resource-deletion action.
Cache eviction only releases in-memory handles; it is not history deletion.

Retention expiry alone does not remove a record. It becomes a candidate for
the next explicitly scheduled pass, and a bounded pass may leave more expired
records for later passes. Use `operation_retention_seconds` for the retention
duration, `operation_cleanup_batch_size` for each pass, and
`operation_cleanup_interval_seconds` if arranging a periodic schedule. Run
cleanup for each user's own operation manager and avoid deleting live database files
manually.

Browser-upload retention is a separate server mechanism. Successful staged
uploads are removed after ingestion; failed uploads can be retained for the
`upload_retry_retention_seconds` window and cleaned up automatically by the
server. Once that retry copy expires, the client must upload the source again,
even if the old task record remains in operation history. Operation-history
cleanup does not extend that upload window.

<div class="page-jump-target" id="component-api-overview" aria-hidden="true"></div>

## 11. Overview of the component API

The component API exposes the classes that `Raven` connects internally.
Instead of asking `Raven` to create and connect them, the application creates
the components it needs. It chooses their models, passes shared managers into
them, and calls each processing stage directly. For example, it can parse a
document for inspection without creating a knowledge, or search stored vectors
without constructing a chat agent.

The operations, stored resources, and result formats described earlier still
apply. A retrieval pipeline returns the same section records whether called
directly or through `Raven.retrieve()`. A `Conversation` still owns its saved
messages when obtained through `ConversationManager` rather than the facade.
Direct component use changes who connects and manages these objects.

This part of the guide therefore concentrates on **what direct composition
adds**. The earlier sections remain the reference for familiar behavior such
as creating a knowledge, reading messages, interpreting retrieval results,
and replaying events. Those method families do not need to be learned again.

### 11.1 Why expose components when Raven already exposes their methods?

The `Raven` class creates a user workspace, an operation manager, a provider,
and knowledge and conversation managers. It constructs retrieval pipelines
when models are configured and creates sessions when requested. It starts
the managers, applies system limits and runtime defaults, supplies the
configured models to pipelines, and selects the appropriate pipeline or
session when retrying a failed task.

A **model adapter** is the Python object through which a component sends model
requests. Components receive these objects as `llm` or `embed_model` arguments.

A particular application may need a different arrangement. For example:

- A source-inspection service may need only `KnowledgeBase`, `Reconstructor`,
  and an `OperationManager`. Reconstructing stored sections requires neither
  a chat agent nor configured models.
- A vector-search service can construct `EmbeddedRetrievalPipeline` with a
  knowledge base, an embedding adapter, and an operation manager. It can then
  search existing vectors without configuring an LLM. `Raven.retrieve()`
  requires the facade's configured model pair; this pipeline uses only the
  embedding adapter for inference.
- An ingestion worker may use one LLM for metadata extraction while the chat
  application uses another for conversation. Components accept the models
  needed for their own work, so they need not all share the facade's single
  configured LLM.
- A document-review interface may need to inspect parser output before
  deciding whether to ingest a file. Direct parsing exposes that intermediate
  representation without committing the document to a knowledge.

These examples change which components are used or how they are configured.
Ordinary resource-management calls still perform the work described earlier:
for example, `Raven.create_knowledge()` delegates to `KnowledgeBase.create()`.

### 11.2 What more can an application control?

Direct composition provides several concrete controls beyond the facade's
main method arguments:

| Control | What the component API makes available |
| --- | --- |
| Which components are constructed | Build a focused parsing, ingestion, search, reconstruction, or chat service without constructing the rest of the facade. |
| Which model serves each stage | Supply an LLM or embedding adapter directly where required. For example, ingestion metadata extraction, hierarchical scoring, and the chat agent can use different LLM instances. |
| Preliminary search and scoring batches | Set how many embedded results are considered, how many candidates are scored in each batch, and how previously scored candidates are supplied to later batches. |
| A search restricted to selected files | `HierarchicalRetrievalPipeline.retrieve_by_file()` accepts explicit filenames. This supports an application that has already selected documents rather than asking the pipeline to choose them through a local/global facade mode. |
| Document-processing setup | Supply a configured Docling converter to `DocumentParser` for supported formats, inspect its elements, or supply a sentence-splitting function to `ProvenanceAwareSemanticSplitter`. `IngestionPipeline` also accepts a parser instance. |
| Whether a completed turn is saved | `AgentHarness.start()` runs the agent and exposes its output. `Session.generate_response()` also saves the user message, tool interactions, final response, and collected result as a conversation turn. |

The retrieval pipelines expose these controls as named arguments:

- `embedded_top_k` limits the preliminary embedded search used by
  vector-conditioned retrieval. The pipeline selects files from those
  results before scoring their sections.
- `max_per_read` limits the number of candidates in one scoring batch.
- `anchor_count` controls how many previously scored, highly ranked candidates
  are included with their scores as references for later scoring batches.

`top_k` still limits the final section results. An application can therefore
increase the preliminary search limit without increasing the number of
sections returned. The later retrieval section shows which methods accept
these arguments.

Direct access also has defined limits. The current agent exposes its built-in
retrieval, navigation, memory, and preference tools according to its policy;
constructing a harness does not provide an arbitrary tool-registration API.
Likewise, using a custom parser configuration does not remove the parser's
supported-format checks. The sentence-splitting hook belongs to direct splitter
construction; the current ingestion constructor accepts a parser instance,
but does not accept a replacement splitter instance.

### 11.3 The components and their boundaries

The following map identifies the objects an application connects. Resources
and their managers are separate entries because they serve different purposes.

| Component | Responsibility in a composed application |
| --- | --- |
| `PathConfig` | Defines one user's fixed paths beneath the chosen home. Components operating on that workspace should use the same path configuration. |
| `OperationManager` | Owns operation storage, discovery/recovery, event streams, and operation handles. The operation/task/event contracts from Section 9 remain the same. |
| `Knowledge` | Represents one knowledge and manages its file/section storage and vectors. |
| `KnowledgeBase` | Creates, discovers, finds, and deletes knowledges; manages their open resources. |
| `Conversation` | Owns one conversation's metadata, canonical messages, turns, model context, vector memory, and preferences. |
| `ConversationManager` | Creates, discovers, finds, updates, and deletes conversations; manages their open resources. |
| `Provider`, `OllamaManager`, `LiteLLMManager` | `Provider` reads a `ModelSpec` and selects a manager. `OllamaManager` connects to Ollama; `LiteLLMManager` creates adapters through LiteLLM. Their loading tasks return adapters for pipelines or the harness to use. |
| `DocumentParser` | Reads a supported file into ordered elements with source information, before semantic sectioning or storage. |
| `ProvenanceAwareSemanticSplitter` | Uses embeddings to group parsed content into sections while preserving element references and source ranges. |
| `IngestionPipeline` | Connects parsing, sectioning, metadata extraction, chunk embedding, and the knowledge's persistence workflow. |
| Retrieval pipelines | Search prepared knowledges using the selected mechanism and return source records. Each pipeline exposes its own dependencies and tuning arguments. |
| `Reconstructor` | Uses source references to read complete stored file views and identify highlighted sections. It does not need a model. |
| `AgentHarness` | Runs the model/tool loop, publishes events, collects evidence, and reconstructs sources. It produces an agent run and its model-compatible messages. |
| `Session` | Connects a conversation to a harness, coordinates a turn, and saves its completed messages and result. |

For example, a `ConversationManager` can find a conversation without running
an agent. A `Reconstructor` can produce source views without creating a
session. Neither action requires the application to start a chat turn.

### 11.4 Following the dependencies through a workflow

For a manually composed **ingestion service**, the application first creates
a `PathConfig` and an `OperationManager`, then a `KnowledgeBase` using those
dependencies. An `IngestionPipeline` receives that knowledge base and operation
manager. Its `run()` call receives the source, destination knowledge, LLM,
and embedding adapter.

Set the semantic-splitting and metadata-extraction retry options when
constructing `IngestionPipeline`. A configured `DocumentParser` can also be
passed into that constructor. Supply `llm` and `embed_model` when calling
`run()`. The pipeline then performs the ingestion flow described in Section 5.

Continue to use `IngestionPipeline.run()` when ingesting source files.
`Knowledge.ingest()` receives already prepared section data; calling it
directly does not perform the pipeline's file parsing or LLM metadata
extraction.

For a manually composed **chat service**, the application creates the
knowledge and conversation managers, constructs the retrieval pipelines,
and supplies those pipelines to `AgentHarness`. It then creates a `Session`
with a conversation, that harness, and the shared operation manager. The
session's `llm` supports memory summarization and title generation; its
`embed_model` supports vector memory. The harness already holds the LLM used
for the agent's own inference calls.

During a turn, the harness uses `ToolBuilder` to expose the permitted retrieval
tools to the model. When the model selects one, its tool wrapper calls the
pipeline's `retrieve_local_context()` or `retrieve_global_context()` directly.
It does not call `Raven.retrieve()`. For sessions created through `Raven`,
the tools and that facade method use the same pipeline instances.

`Session.generate_response()` streams the turn and saves its completed
messages and result, as described in Section 8. `AgentHarness.start()` returns
an `AgentRun` with events, a collected result, and model-compatible messages,
but does not save a complete conversation turn. An application calling the
harness directly must save those messages itself if it needs persistent
conversation history.

In both services, passing the same active `Operation` into nested calls
keeps the work in one operation and event history. Omitting it lets each
operation-aware method create an operation using its supplied operation manager.
Direct composition does not require replacing the operation-first design.

The application can obtain adapters from `Provider` or construct suitable
LlamaIndex adapters itself. A provider loading task returns its adapter
through `.result()`. The application then passes it into the relevant
constructor or method:

- The splitter needs an embedding adapter supporting asynchronous batch
  embedding.
- Embedded retrieval needs an embedding adapter for query embeddings.
- Ingestion metadata extraction and hierarchical retrieval need an LLM
  supporting structured prediction.
- The chat agent needs a function-calling-capable LLM.

**Separate model choices do not make embedding identities interchangeable.**
Use an embedding model compatible with the vectors already stored in each
knowledge or conversation. Supplying models directly does not make old
vectors searchable with an unrelated embedding model.

### 11.5 A focused example: inspect a document before ingestion

`DocumentParser` is useful on its own when the application needs to inspect
what a file reader produced. The following program parses a file and prints
its element types, source ranges, and short text previews. It creates no
knowledge or conversation and makes no LLM or embedding requests.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">DocumentParser.parse()</figcaption>

```python
async def parse(
    self,
    source_path: str | Path,
    *,
    file_id: str,
    max_file_size_bytes: int = DEFAULT_MAX_SOURCE_FILE_BYTES,
    max_pages: int = DEFAULT_MAX_DOCUMENT_PAGES,
) -> ParsedDocument
```

</figure>

Save the following as `preview_document.py` and run
`python preview_document.py Reaction_wheel.txt`, using the
[sample file]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }})
from Section 5. The package installation is sufficient for this text-file
example; no `Raven.start()`, Ollama server, or model configuration is needed.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">preview_document.py</figcaption>

```python
import argparse
import asyncio
import json
from pathlib import Path
from uuid import uuid4

from nraven import DocumentParser


async def main(arguments: argparse.Namespace) -> None:
    document_parser = DocumentParser()
    file_id = uuid4().hex
    parsed_document = await document_parser.parse(arguments.source, file_id=file_id)
    print("File:", parsed_document.file_name)
    print("Preview file ID:", file_id)
    print("Navigation:", parsed_document.navigation_type.value)
    print("Conversion status:", parsed_document.conversion_status)
    print("Warnings:", parsed_document.warnings)
    print("Elements:", len(parsed_document.elements))
    for element in parsed_document.elements:
        print(json.dumps({
            "element_id": element.element_id,
            "element_type": element.element_type.value,
            "source_range": element.source_range,
            "text_preview": element.text[:180],
        }, ensure_ascii=False))


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("source", type=Path)
    asyncio.run(main(argument_parser.parse_args()))
```

</figure>

The parser uses `file_id` as the prefix of each element ID. This script
generates a fresh UUID for its preview and creates no knowledge file record.
A later ingestion generates its own file ID. In a custom processing flow,
use the same file ID for the parser's element IDs and the section IDs derived
from those elements, so they continue to identify the same file.

Parser elements are not semantic sections yet. Text/Markdown reading and
PDF/DOCX conversion can produce different element structures; the splitter
is the separate component that groups them into sections. The program only
shows the parser boundary, so it neither creates embeddings nor saves a file
record. This is one capability the facade's complete ingestion method does
not expose as an intermediate result.

`parse()` returns its `ParsedDocument` directly, without allocating an
`OperationTask`. If a preview needs operation status and event history in the
application, run the parsing call inside an application worker as in Section 9.

### 11.6 What the application takes responsibility for

Without the facade, the application must perform these steps itself:

- **Dependencies and user scope.** Share the appropriate `PathConfig` and
  `OperationManager` across components in one user's workspace. Keep another
  user's components and stores separate.
- **Startup and crash recovery.** Call `OperationManager.recover()` before
  accepting new work. It starts the operation manager if needed and marks
  interrupted operations failed. Then start the knowledge and conversation
  managers the application will use. Calling `OperationManager.start()`
  separately is valid, but it does not perform recovery on its own.
- **Configuration and policy.** Pass the desired settings into each component.
  Components validate their own arguments, but do not automatically read the
  facade's runtime-settings file or apply its system-wide ceilings. For
  example, an application calling a retrieval pipeline directly must check
  its requested result limit against `SystemConfig.max_retrieval_top_k` if
  it wants to enforce that policy.
- **Models and compatibility.** Load suitable adapters and pass them to the
  components that use them. Loading a second adapter does not replace the
  model already held by an existing pipeline. Keep embedding choices
  compatible with the vectors in the relevant knowledge or conversation.
- **Task results and retry handling.** Await task results and decide whether
  to retry failures. `OperationManager` stores task records and runs workers;
  it does not choose an ingestion pipeline, reconstructor, or session for a
  retry. Without `Raven.retry_task()`, the application must select that
  component and supply the saved retry inputs and required models.
- **Shutdown.** Finish or cancel active work before closing the resource
  managers and provider clients it uses, then close the shared operation
  manager. For example, `KnowledgeBase.close()` closes its managed knowledge
  databases but does not close the shared `OperationManager`.
- **History retention.** Arrange operation-history cleanup when desired,
  following Section 10. Direct composition does not add automatic scheduling.

An application can also use a component already owned by a started `Raven`,
such as `raven.knowledge_base` or `raven.operation_manager`. For example, a
direct pipeline can reuse those objects to access the same knowledges and
operation history. Leave their shutdown to `Raven.close()` while they are
shared with facade calls. Reuse `raven.operation_manager` rather than opening
another `OperationManager` against the same user's operation database.

### 11.7 Using the rest of this part of the guide

The following sections concentrate on construction, dependencies, additional
parameters, intermediate results, and responsibilities that change when
components are used directly. Familiar CRUD calls, retrieval result shapes,
message formats, and event cursors refer back to Sections 1–10.

Continue using `Raven` for workflows its methods already support. Use
components directly when the application needs intermediate parser output,
different models for different stages, additional retrieval parameters, or
explicit control over turn persistence.

<div class="page-jump-target" id="component-workspace" aria-hidden="true"></div>

## 12. Constructing and closing a component workspace

Without `Raven`, the application creates the managers that its components
will use. This section builds a small workspace with a knowledge base,
conversation manager, and operation manager. It needs no models because it
only creates knowledge and conversation metadata.

Creating and updating resources still returns an `OperationTask`. Await
`.result()` to obtain its result; read its events to observe progress, as
explained in Sections 5, 7, and 9. This section explains which objects to pass
into each manager, how to start them, and how to stop active tasks before
closing the databases they use.

### 12.1 Constructing the shared dependencies

Create one `PathConfig` for the chosen home and user. Pass it to the
`OperationManager` and `KnowledgeBase`. The `ConversationManager` receives
that same path configuration, knowledge base, and operation manager:

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OperationManager.__init__()</figcaption>

```python
def __init__(
    self,
    paths: PathConfig,
    *,
    sync_interval: float = OPERATION_SYNC_INTERVAL_SECONDS,
    max_cached_finished_operations: int = (
        DEFAULT_FINISHED_OPERATION_CACHE_SIZE
    ),
    event_replay_page_size: int = DEFAULT_EVENT_REPLAY_PAGE_SIZE,
) -> None
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">KnowledgeBase.__init__()</figcaption>

```python
def __init__(
    self,
    paths: PathConfig,
    operation_manager: OperationManager,
    *,
    max_open_knowledges: int = DEFAULT_OPEN_KNOWLEDGE_LIMIT,
) -> None
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">ConversationManager.__init__()</figcaption>

```python
def __init__(
    self,
    paths: PathConfig,
    knowledge_base: KnowledgeBase,
    operation_manager: OperationManager,
    *,
    max_open_conversations: int = DEFAULT_OPEN_CONVERSATION_LIMIT,
) -> None
```

</figure>

Each constructor argument has a specific purpose:

- `paths` selects the user's directories and operation database. Reuse the
  same `PathConfig` object for these managers so they refer to the same user.
  The example names that object `path_config`; `paths` is the constructor's
  parameter name.
- `operation_manager` is the shared manager that `KnowledgeBase` and
  `ConversationManager` use to create an operation when a method is called
  without an `operation` argument. Passing this existing instance keeps their
  operations, task records, and events in the same user's operation store.
- `knowledge_base` lets the conversation manager check that a requested
  knowledge exists before creating a conversation bound to it.
- `sync_interval` sets the number of seconds between periodic checks that
  checkpoint pending operation-storage changes to durable storage.
- `max_cached_finished_operations` limits how many finished `Operation`
  objects remain cached in memory. Removing an object from this cache does
  not delete its stored history.
- `event_replay_page_size` sets how many stored events are read in each replay
  batch. It does not limit the total number of events that can be replayed.
- `max_open_knowledges` and `max_open_conversations` limit how many managed
  resources can keep their storage connections open. Evicting a connection
  from these caches does not delete the saved knowledge or conversation.

These constructors accept their individual settings, not a `SystemConfig`
object. The example creates `SystemConfig()` and passes the relevant field
values explicitly. It does not load a system-settings file or create/load a
runtime-settings file. If an application needs saved configuration, it must
load the values and pass them into the components it constructs.

### 12.2 Recovering history and starting the managers

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OperationManager.recover()</figcaption>

```python
async def recover(self) -> list[Operation]
```

</figure>

Call `operation_manager.recover()` before submitting new work. It calls
`operation_manager.start()` if needed, opens its store, starts periodic storage
synchronization, and marks unfinished operations from a previous process
failed with `operation_interrupted`. Its result is the list of recovered
`Operation` objects, which is empty when there is no interrupted work. These
objects represent interrupted operations now marked failed; their workers
are not restarted.

Then await `knowledge_base.start()` and `conversation_manager.start()`. Each
manager scans its user directory, reads saved metadata, and records the
knowledges or conversations it can successfully load. Their storage
connections are opened when needed; startup does not require every knowledge
or conversation database to remain open at once. These two lifecycle calls
return `None`, not an `OperationTask`.

Start the knowledge base before the conversation manager, and create a local
conversation only after its knowledge exists. Keep the same home and user ID
on later runs to discover the previously saved resources.

### 12.3 One runnable workspace example

Save the following as `component_workspace.py` and run
`python component_workspace.py`. It uses `raven-component-workspace-home`
beneath the working directory and the default user ID. It creates or reuses
`engineering`, then creates a new local conversation on each run. Existing
knowledges and conversations are retained.

The variable `conversation_manager` refers to the `ConversationManager`
instance; `conversation` refers to one `Conversation` returned by a creation
task. Similarly, `knowledge_base` is the `KnowledgeBase` instance, and
`operation_manager` is the shared `OperationManager`. These names distinguish
the managers from the resources and tasks they manage.

`operation.run(operation.name, prepare_workspace)` runs `prepare_workspace()`
and supplies its `operation` argument. That function passes the same object
into the knowledge and conversation calls, so their tasks share one operation
history while retaining separate task IDs. The read-only
`knowledge_base.list()` call returns its list directly, without creating a
task. The operation name is also used for its root task, as required by
`Operation.run()`.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">component_workspace.py</figcaption>

```python
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path

from nraven import (
    ConversationManager, Event, EventType, KnowledgeBase,
    Operation, OperationManager, PathConfig, SystemConfig,
)


async def main() -> None:
    path_config = PathConfig(Path.cwd() / "raven-component-workspace-home")
    system_config = SystemConfig()
    operation_manager = OperationManager(
        path_config,
        sync_interval=system_config.operation_sync_interval_seconds,
        max_cached_finished_operations=system_config.finished_operation_cache_size,
        event_replay_page_size=system_config.event_replay_page_size,
    )
    knowledge_base = KnowledgeBase(
        path_config,
        operation_manager,
        max_open_knowledges=system_config.open_knowledge_limit,
    )
    conversation_manager = ConversationManager(
        path_config,
        knowledge_base,
        operation_manager,
        max_open_conversations=system_config.open_conversation_limit,
    )

    async def prepare_workspace(operation: Operation) -> dict[str, str]:
        existing_knowledge_names = {
            knowledge_metadata["name"]
            for knowledge_metadata in await knowledge_base.list()
        }
        if "engineering" not in existing_knowledge_names:
            create_knowledge_task = await knowledge_base.create(
                "engineering", "Engineering reference documents.", operation=operation
            )
            await create_knowledge_task.result()
        create_conversation_task = await conversation_manager.create(
            "engineering", operation=operation
        )
        conversation = await create_conversation_task.result()
        update_conversation_task = await conversation_manager.update(
            conversation.conversation_id,
            title="Component workspace discussion", operation=operation,
        )
        await update_conversation_task.result()
        workspace_result = {
            "knowledge_name": "engineering",
            "conversation_id": conversation.conversation_id,
        }
        await operation.publish(Event(
            type=EventType.WORK_PROGRESS, data={"stage": "ready", **workspace_result}
        ))
        return workspace_result

    async with AsyncExitStack() as exit_stack:
        exit_stack.push_async_callback(operation_manager.close)
        exit_stack.push_async_callback(knowledge_base.close)
        exit_stack.push_async_callback(conversation_manager.close)
        exit_stack.push_async_callback(operation_manager.cancel_active)

        recovered_operations = await operation_manager.recover()
        print("Recovered operations:", [
            str(recovered_operation.operation_id)
            for recovered_operation in recovered_operations
        ])
        await knowledge_base.start()
        await conversation_manager.start()
        operation = await operation_manager.create("example.component_workspace")
        root_task = await operation.run(operation.name, prepare_workspace)
        print("Operation:", operation.operation_id, "Task:", root_task.task_id, flush=True)
        async for event in root_task.events():
            print(event.event_id, event.type.value, event.task_name, flush=True)
        workspace_result = await root_task.result()
        print("Result:", json.dumps(workspace_result, indent=2))
        conversation = conversation_manager.get(workspace_result["conversation_id"])
        print("Conversation:", json.dumps(conversation.to_dict(), indent=2))

    print("Workspace closed; saved resources remain on disk.")


if __name__ == "__main__":
    asyncio.run(main())
```

</figure>

`exit_stack` is an `AsyncExitStack` from Python's standard library. Its
registered async callbacks run in reverse registration order when its block exits,
including when a startup call or the worker raises an exception. This keeps
the cleanup steps together instead of requiring several nested `finally`
blocks. Section 12.4 explains their order.

The successful result contains the knowledge name and new conversation ID.
On a repeat run, the knowledge listing prevents another creation of
`engineering`; a different conversation ID is expected. No messages are added
because the example does not run a chat turn.

The shared operation groups the work and its events; it does not make all
three component calls one database transaction. If a later step fails, an
earlier saved knowledge or conversation can remain. Inspect the resources
and task records before deciding how to retry, as in Section 10.

### 12.4 Stopping tasks before closing their dependencies

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OperationManager.cancel_active()</figcaption>

```python
async def cancel_active(self) -> None
```

</figure>

For an application that accepts requests, first stop accepting new ones.
Then shut down the objects in this order:

1. **Cancel or finish active operations.** `operation_manager.cancel_active()`
   requests cancellation and waits for active operations before returning.
   The example's root task has already finished, so there is normally nothing
   left to cancel.
2. **Close the conversation manager.** It closes the conversations it manages,
   including their open message and vector-memory stores.
3. **Close the knowledge base.** It closes the knowledge databases it manages.
4. **Close the operation manager.** It stops its synchronization service,
   flushes pending storage work, and closes operation/event storage.

The example registers these callbacks in the opposite order because
`AsyncExitStack` runs them last-in, first-out. They are registered before
startup, so the exit stack attempts their cleanup even if startup fails. If a
cleanup callback raises, the remaining callbacks are still attempted, and the
error propagates to the caller.

`OperationManager.close()` also cancels active work. Closing the resource
managers first, however, could close databases that active tasks still need.
Call `operation_manager.cancel_active()` and await it before closing those
databases, so their tasks can stop while the stores are still available.

The resource managers use the operation manager passed to their constructors;
they do not close it themselves. Closing the whole workspace leaves its
metadata, messages, knowledge files, and operation history on disk. It does
not perform retention cleanup. Reopen the same home and user to use that data
again, or apply the explicit deletion/cleanup methods from earlier sections.

If a started `Raven` owns these managers, use its existing instances and let
`Raven.close()` handle their shutdown. The manual shutdown sequence above is
for an application that constructed and owns them itself.

<div class="page-jump-target" id="component-adapters" aria-hidden="true"></div>

## 13. Loading model adapters for individual components

`Raven.configure_models()` installs a model pair into the facade and rebuilds
its retrieval pipelines. With the component API, an application instead
obtains model adapters and passes them to the components that need them.
Loading an adapter does not assign it to every existing pipeline or session.

The `ModelSpec` fields, provider identifiers, credentials, and embedding
compatibility rules remain those in [Section 4](#raven-models). This section
focuses on the adapter objects returned by loading tasks, configurable manager
clients, and who closes them.

### 13.1 Receiving an adapter rather than a configuration summary

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Provider.load()</figcaption>

```python
async def load(
    self,
    spec: ModelSpec,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

`Provider.load(spec)` chooses `load_llm()` or `load_embedding()` from the
specification's role. Both role-specific methods are also available; they
reject a spec whose role does not match the method. All three return an
`OperationTask`.

Await that task's `.result()` to get the actual LlamaIndex adapter:

- For an Ollama LLM, the result is an `Ollama` adapter.
- For Ollama embeddings, the result is an `OllamaEmbedding` adapter.
- For LiteLLM, the result is the corresponding LlamaIndex LiteLLM adapter.
  RAVEN's embedding adapter also supplies asynchronous calls without running
  the underlying synchronous embedding work on the event-loop thread.

These results are Python objects with inference methods, not the JSON model
summary returned by the facade's configuration task. Keep the object and pass
it as a component's `llm` or `embed_model` argument. It is not necessary to
serialize the adapter, and its representation may contain configuration that
does not belong in an application log.

Loading and inference are separate steps. `OllamaManager` checks availability
and can pull a missing model while creating its adapter. It does not preload
the model into VRAM merely by returning that adapter. An inference request,
or an explicit preload call, makes Ollama load it for execution. LiteLLM adapter
construction likewise does not establish that a later cloud request will
succeed; that request still depends on valid credentials and service access.

### 13.2 Configuring the provider's clients

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Provider.__init__()</figcaption>

```python
def __init__(
    self,
    *,
    operation_manager: OperationManager,
    ollama_manager: OllamaManager | None = None,
    litellm_manager: LiteLLMManager | None = None,
    max_cached_llms: int = DEFAULT_LLM_ADAPTER_CACHE_SIZE,
    max_cached_embeddings: int = DEFAULT_EMBEDDING_ADAPTER_CACHE_SIZE,
) -> None
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OllamaManager.__init__()</figcaption>

```python
def __init__(
    self,
    host: str | None = None,
    request_timeout: float = 300.0,
    embedding_batch_size: int = 10,
    *,
    operation_manager: OperationManager,
    max_cached_llms: int = DEFAULT_LLM_ADAPTER_CACHE_SIZE,
    max_cached_embeddings: int = DEFAULT_EMBEDDING_ADAPTER_CACHE_SIZE,
) -> None
```

</figure>

`Provider` creates its own Ollama and LiteLLM managers unless instances are
supplied. To change the Ollama connection, construct an `OllamaManager` and
pass it as `ollama_manager`:

- `host` selects the Ollama endpoint. If omitted, the manager reads
  `OLLAMA_HOST`, falling back to `http://127.0.0.1:11434`.
- `request_timeout` configures the timeout used by its Ollama requests and
  LLM adapters.
- `embedding_batch_size` configures batching on its embedding adapters.
- `max_cached_llms` and `max_cached_embeddings` bound the adapter references
  retained in that manager's caches.

The injected manager should share the `operation_manager` used by the provider
and its consumers. `Provider` uses a supplied manager as configured; its own
cache-size arguments apply to managers it constructs, not to an injected
manager's existing cache configuration.

These are client and batching settings. They do not enable Ollama model-level
`ModelSpec.options` in v0.2.0. Nonempty Ollama options are still rejected, as
explained in Section 4. RAVEN connects to an externally managed Ollama service;
it does not start that service or choose its CPU/GPU backend.

### 13.3 Calling a provider-specific manager directly

The provider-specific managers can also be used without `Provider`. Their
loading methods take the individual arguments shown below, not a `ModelSpec`.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OllamaManager.load_llm()</figcaption>

```python
async def load_llm(self, model: str, *, operation: Operation | None = None) -> OperationTask
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">OllamaManager.load_embedding()</figcaption>

```python
async def load_embedding(
    self,
    model: str,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

For Ollama, supply its model name and tag. The manager already holds the
endpoint, timeout, batching, and shared operation manager. The returned task
still provides the adapter through `.result()`.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">LiteLLMManager.__init__()</figcaption>

```python
def __init__(
    self,
    *,
    operation_manager: OperationManager,
    max_cached_llms: int = DEFAULT_LLM_ADAPTER_CACHE_SIZE,
    max_cached_embeddings: int = DEFAULT_EMBEDDING_ADAPTER_CACHE_SIZE,
) -> None
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">LiteLLMManager.load_llm()</figcaption>

```python
async def load_llm(
    self,
    model: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    options: dict[str, Any] | None = None,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>
<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">LiteLLMManager.load_embedding()</figcaption>

```python
async def load_embedding(
    self,
    model: str,
    *,
    provider: str | None = None,
    api_key: str | None = None,
    options: dict[str, Any] | None = None,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

For LiteLLM, `model` identifies the model, `provider` can select the service,
and `options` contains supported adapter settings. `api_key` accepts the
credential value when needed. It is different from `ModelSpec.api_key_ref`,
which names an environment variable: `Provider` resolves that reference before
calling this manager. A direct caller must obtain the credential through the
application's environment or secret handling and pass it itself, or use
provider-supported ambient credentials. Do not pass an environment-variable
name as though it were the key value.

The low-level loading methods also accept an active `operation` so that their
model events can belong to a larger workflow. They return adapters rather
than installing a facade-wide model pair. Use the provider-specific managers
for their extra connection/loading controls; use `Provider` when routing by
`ModelSpec` is sufficient.

### 13.4 A runnable example with real adapters

Save this program as `component_adapters.py`. Run
`python component_adapters.py Reaction_wheel.txt`, using the sample from
Section 5. Ollama must be running with `gemma4:e2b` and `bge-m3:latest`
installed. The `--llm`, `--embedding`, and `--ollama-host` options can select
different installed models or an endpoint.

The program uses its own `raven-adapters-home` for operation history. It loads
one LLM and one embedding adapter, passes the embedding adapter into the
semantic splitter, and makes a small direct chat request through the LLM.
It stores no knowledge or conversation. The second embedding-load call checks
whether the same manager reuses its cached adapter; it is not a second
embedding inference request.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">component_adapters.py</figcaption>

```python
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from llama_index.core.llms import ChatMessage
from nraven import (
    DocumentParser, Event, EventType, ModelRole, ModelSpec, OllamaManager,
    Operation, OperationManager, PathConfig, Provider,
    ProvenanceAwareSemanticSplitter,
)


async def main(arguments: argparse.Namespace) -> None:
    path_config = PathConfig(Path.cwd() / "raven-adapters-home")
    operation_manager = OperationManager(path_config)
    ollama_manager = OllamaManager(
        host=arguments.ollama_host,
        request_timeout=300.0,
        embedding_batch_size=10,
        operation_manager=operation_manager,
    )
    provider = Provider(
        operation_manager=operation_manager, ollama_manager=ollama_manager
    )
    llm_spec = ModelSpec(provider="ollama", model=arguments.llm, role=ModelRole.LLM)
    embedding_spec = ModelSpec(
        provider="ollama", model=arguments.embedding, role=ModelRole.EMBEDDING
    )

    async def run_adapter_example(operation: Operation) -> dict[str, Any]:
        load_llm_task = await provider.load(llm_spec, operation=operation)
        llm = await load_llm_task.result()
        load_embedding_task = await provider.load(embedding_spec, operation=operation)
        embed_model = await load_embedding_task.result()
        cached_embedding_task = await provider.load_embedding(
            embedding_spec, operation=operation
        )
        cached_embed_model = await cached_embedding_task.result()

        document_parser = DocumentParser()
        parsed_document = await document_parser.parse(arguments.source, file_id=uuid4().hex)
        semantic_splitter = ProvenanceAwareSemanticSplitter(embed_model)
        semantic_sections = await semantic_splitter.split(parsed_document)
        chat_response = await llm.achat([
            ChatMessage(role="user", content="Reply with only the word RAVEN.")
        ])
        adapter_result = {
            "llm_adapter_type": type(llm).__name__,
            "embedding_adapter_type": type(embed_model).__name__,
            "embedding_cache_reused": cached_embed_model is embed_model,
            "file_name": parsed_document.file_name,
            "section_count": len(semantic_sections),
            "llm_reply": chat_response.message.content,
        }
        await operation.publish(Event(
            type=EventType.WORK_PROGRESS,
            data={"stage": "adapters_used", **adapter_result},
        ))
        return adapter_result

    async with AsyncExitStack() as exit_stack:
        exit_stack.push_async_callback(operation_manager.close)
        exit_stack.push_async_callback(provider.close)
        exit_stack.push_async_callback(operation_manager.cancel_active)
        await operation_manager.recover()
        operation = await operation_manager.create("example.component_adapters")
        root_task = await operation.run(operation.name, run_adapter_example)
        print("Operation:", operation.operation_id, flush=True)
        async for event in root_task.events():
            print(event.event_id, event.type.value, event.task_name, flush=True)
        print("Adapter result:", json.dumps(await root_task.result(), indent=2))


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("source", type=Path)
    argument_parser.add_argument("--llm", default="gemma4:e2b")
    argument_parser.add_argument("--embedding", default="bge-m3:latest")
    argument_parser.add_argument("--ollama-host")
    asyncio.run(main(argument_parser.parse_args()))
```

</figure>

The model-loading tasks return adapter objects inside the worker. The outer
worker returns a JSON-compatible summary because that is the result chosen
by this example, not because the adapters were converted into configuration
records. The section count comes from actual semantic splitting with the
loaded embedding model.

The model-loading calls publish RAVEN model events. Direct calls to
`llm.achat()` and the splitter return their own results; they do not
automatically produce chat-response or ingestion-progress events. The example
keeps them within its root operation and publishes `work.progress` explicitly.
Its chat request is not a harness run or a saved conversation turn.

### 13.5 Choosing models separately for different consumers

The same adapter object can be passed to more than one component that accepts
it. It is also possible to supply different LLM objects to different stages.
For example, pass one LLM to `IngestionPipeline.run()` for metadata extraction
and another to `HierarchicalRetrievalPipeline` for scoring. A manually
constructed `AgentHarness` can use a separate chat LLM. The `Session` receives
its own `llm` for memory summarization/title generation and `embed_model` for
vector memory, as described in Section 11.4.

Each model must support its consumer's methods: hierarchical scoring needs
structured prediction, the splitter needs asynchronous batch embeddings,
and the chat agent needs function calling. A provider loading task does not
perform the facade's complete capability and existing-knowledge validation
sequence. Creating an adapter alone does not prove it supports all these uses.

Keep the embedding identity compatible with previously stored vectors. Loading
another model does not rebuild existing vectors or replace adapter references
already held by a pipeline. Reconfigure the relevant consumers explicitly
when the application changes models.

### 13.6 Adapter caches, shutdown, and model residency

The manager caches hold adapter objects, not generated answers:

- `OllamaManager` maintains separate LLM and embedding caches keyed by the
  supplied model name.
- `LiteLLMManager` includes the model, provider, options, and credential
  fingerprint in its cache identity, so materially different configurations
  can produce different adapters.
- `max_cached_llms` and `max_cached_embeddings` limit the references retained
  by a manager. Removing an adapter from that cache does not unload an Ollama
  model or remove another component's reference to the adapter.

`Provider.close()` calls `close()` on both of its managers, including injected
ones. That is why the example registers `provider.close()` once rather than
also registering `ollama_manager.close()`. Stop or finish their active work
first and close the shared operation manager last, following Section 12.
If `Raven` owns the provider, leave this shutdown to `Raven.close()`.

The Ollama manager clears its adapter caches and closes its client. The
LiteLLM manager clears its caches, including their retained credential
references. Neither action stops an external model service. In particular,
**closing the provider does not unload Ollama models from VRAM**. Ollama can
retain them according to its keep-alive behavior. An application that needs
explicit residency control can use `OllamaManager.preload_llm()`,
`preload_embedding()`, `unload_llm()`, and `unload_embedding()` with model names.
The distinction between adapters and residency, and preload/unload behavior,
is covered in Section 4.5.

<div class="page-jump-target" id="component-knowledge" aria-hidden="true"></div>

## 14. Working directly with stored knowledge

`KnowledgeBase` finds and manages knowledges for one user. A `Knowledge`
represents one of those stores: its metadata, saved file and section records,
and chunk vectors. The first example shows how to create, obtain, list,
update, and delete a knowledge through these components. The following
subsections then explain the additional control available over stored data,
vector search, and open storage connections.

Direct access is useful when an application needs the stored section metadata
or wants to supply its own query vector and inspect individual chunk matches.
The application can then decide which sections to read, how to rank or combine
matches, and what to pass to a later processing stage.

`Raven.get_knowledge()` also returns a `Knowledge` from its owned knowledge
base. These methods are therefore usable through that handle as well; using
them does not require constructing a second manager or copying the data.
The examples below construct the managers themselves and make their calls
without a `Raven` instance.

### 14.1 A compact knowledge-management example

The component calls use two different objects:

- `knowledge_base.create()` creates a knowledge and returns a task whose
  result is a `Knowledge`. `knowledge_base.get()` returns the registered
  object directly; it does not create another copy.
- `await knowledge_base.list()` returns the current knowledge records
  directly, without an operation task.
- `knowledge.set_summary()` updates that individual knowledge's user summary.
  The method belongs to `Knowledge`, rather than `KnowledgeBase`; await its
  task's `.result()` to wait for the saved change. The result is `None`.
- `knowledge_base.delete()` returns a task that closes the knowledge, removes
  its directory and contents, and unregisters it. Its completed result is
  `None`. Closing a knowledge alone does not delete it.

Save this program as `manage_component_knowledge.py` and run
`python manage_component_knowledge.py`. It uses its own
`raven-component-knowledge-home` beneath the working directory. No model or
Ollama service is needed. The startup and shutdown order follows
[Section 12](#component-workspace).

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">manage_component_knowledge.py</figcaption>

```python
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from nraven import KnowledgeBase, Operation, OperationManager, PathConfig


async def main() -> None:
    path_config = PathConfig(Path.cwd() / "raven-component-knowledge-home")
    operation_manager = OperationManager(path_config)
    knowledge_base = KnowledgeBase(path_config, operation_manager)

    async def manage_knowledge(operation: Operation) -> dict[str, Any]:
        knowledge_name = f"example_{uuid4().hex[:8]}"
        create_knowledge_task = await knowledge_base.create(
            knowledge_name, "Initial document summary.", operation=operation
        )
        knowledge = await create_knowledge_task.result()
        created_metadata = knowledge.meta
        registered_knowledge = knowledge_base.get(knowledge_name)
        listed_knowledges = await knowledge_base.list()

        update_summary_task = await knowledge.set_summary(
            "Updated document summary.", operation=operation
        )
        await update_summary_task.result()
        updated_metadata = knowledge.meta

        delete_knowledge_task = await knowledge_base.delete(
            knowledge_name, operation=operation
        )
        await delete_knowledge_task.result()
        remaining_knowledges = await knowledge_base.list()
        return {
            "knowledge_name": knowledge_name,
            "created": created_metadata,
            "registered_handle_is_same": registered_knowledge is knowledge,
            "listed_before_deletion": listed_knowledges,
            "updated": updated_metadata,
            "listed_after_deletion": remaining_knowledges,
        }

    async with AsyncExitStack() as exit_stack:
        exit_stack.push_async_callback(operation_manager.close)
        exit_stack.push_async_callback(knowledge_base.close)
        exit_stack.push_async_callback(operation_manager.cancel_active)
        await operation_manager.recover()
        await knowledge_base.start()
        operation = await operation_manager.create("example.manage_knowledge")
        root_task = await operation.run(operation.name, manage_knowledge)
        print("Operation:", operation.operation_id, flush=True)
        async for event in root_task.events():
            print(event.event_id, event.type.value, event.task_name, flush=True)
        print("Result:", json.dumps(await root_task.result(), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
```

</figure>

The worker passes the same `operation` to creation, summary update, and
deletion, so their tasks and events share one operation history. Each run
uses a fresh example name and deletes only the knowledge it created. The
printed result includes the original metadata, the listing before deletion,
the updated metadata, and the listing after deletion. The two metadata values
remain distinct because `knowledge.meta` returns a copy.

After a successful run, the example knowledge's directory is gone; its
operation history remains in the example home. This demonstrates the
component management calls without leaving a disposable knowledge behind.
For stored-document inspection, the later example uses a separate, existing
ingestion home instead of this empty knowledge.

### 14.2 Obtaining a handle and opening its storage

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">KnowledgeBase.get()</figcaption>

```python
def get(self, name: str) -> Knowledge
```

</figure>

After `knowledge_base.start()`, `knowledge_base.get(name)` returns the
registered `Knowledge` object synchronously. It raises an error if that
knowledge was not found or its directory could not be loaded. It does not
create a missing knowledge, and returning the object does not necessarily
open its Qdrant database.

There are three different kinds of access:

- `knowledge.meta` and `knowledge.get_summary()` read metadata already loaded
  into the object. They need no model inference.
- File and section methods such as `list_files_page()` and `get_section()`
  synchronously read `file.sqlite3`. If the knowledge's storage is not already
  open, these methods use a temporary file-store connection. In async code,
  call these synchronous database methods through `asyncio.to_thread()` so
  they do not block the event loop.
- Async methods such as `search()`, `retrieve_context()`, and `count()` open
  the knowledge's storage when needed and keep it in use while performing
  their database work. Call them with `await`.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Knowledge.start()</figcaption>

```python
async def start(self) -> None
```

</figure>

`await knowledge.start()` explicitly opens its file store and local Qdrant
client. It returns `None`, not an operation task. It is valid to call before
several vector operations, but the async methods above already arrange
opening when needed. Merely reading a section does not require loading an
LLM or an embedding model.

### 14.3 Searching with a vector supplied by the application

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Knowledge.search()</figcaption>

```python
async def search(self, query_vector: list[float], top_k: int = 5) -> list[dict[str, Any]]
```

</figure>

`search()` accepts an embedding vector, not query text. It does not generate
an embedding, select a retrieval strategy, or ask an LLM to score anything.
Its result is a list of chunk matches with these fields:

| Field | Meaning |
| --- | --- |
| `file_id` | Identity of the stored file containing the matched chunk. |
| `section_id` | Identity of the section containing that chunk. |
| `chunk_index` | The matched chunk's index within its section. |
| `score` | The vector store's similarity score for that match; it is not a probability. |

`top_k` limits chunk matches. Several matches can belong to the same section,
so five matches do not necessarily identify five different sections. Matches
do not include raw text; read a selected section separately:

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Knowledge.get_section()</figcaption>

```python
def get_section(self, section_id: str) -> dict[str, Any] | None
```

</figure>

`get_section()` returns that section's stored record, including its
`raw_content` and metadata, or `None` if it cannot be found. The example
deduplicates section IDs before reading them and prints only short previews.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Knowledge.retrieve_context()</figcaption>

```python
async def retrieve_context(
    self,
    query_vector: list[float],
    top_k: int = 5,
) -> list[dict[str, Any]]
```

</figure>

`retrieve_context()` is a convenience when the application already has a
query vector and wants section text. It searches chunks, removes repeated
section IDs, and returns records containing `knowledge_name`, `file_name`,
`section_id`, `raw_content`, and the first matching chunk's `score` for that
section. Its `top_k` still limits the initial chunk search, so its result can
contain fewer than `top_k` sections.

This lower-level result includes a score. It differs from the four-field
records returned by the retrieval pipelines and `Raven.retrieve()` in
[Section 6](#raven-retrieval). Use a retrieval pipeline when its strategy,
section selection, events, and standard output already fit the application.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Knowledge.validate_embedding_model()</figcaption>

```python
async def validate_embedding_model(self, embed_model: Any) -> None
```

</figure>

When generating the query vector with a model adapter, first await
`knowledge.validate_embedding_model(embed_model)`. It checks the adapter's
identity against the identity saved for that knowledge. If no identity has
been saved yet, validation records the supplied identity. This call does not
re-embed any stored content.

**`search()` receives only numbers and cannot identify the model that produced
them. A vector with the right dimensions can still come from an incompatible
model.** Use the same embedding identity as ingestion; the example validates
the adapter before making its query embedding.

These methods return their values directly rather than an `OperationTask`.
For observable custom search work, run them inside an operation worker and
publish the application-specific progress it needs, as in the example.

### 14.4 A runnable stored-data and vector-search example

Save the following as `inspect_knowledge.py`. It opens an **existing** home and
knowledge using the default user ID. It does not create a knowledge or ingest
a file. Use the home prepared by Section 5's ingestion example:

```text
python inspect_knowledge.py raven-ingestion-home engineering
```

That command prints knowledge metadata and up to three file records without
loading a model or contacting Ollama. To also search the indexed chunks, add
a query:

```text
python inspect_knowledge.py raven-ingestion-home engineering --query "When must the reaction wheel enter safe-hold?"
```

The query command needs Ollama and the embedding model used during ingestion.
The default is `bge-m3:latest`; `--embedding` and `--ollama-host` select another
compatible adapter or server. No LLM is needed in either mode. Close another
application using this same knowledge before running the standalone script,
so two separate processes do not try to open its local Qdrant storage.

<figure class="code-snippet api-example" markdown="1">
<figcaption class="code-snippet-filename">inspect_knowledge.py</figcaption>

```python
import argparse
import asyncio
from contextlib import AsyncExitStack
import json
from pathlib import Path
from typing import Any

from nraven import (
    Event, EventType, KnowledgeBase, OllamaManager,
    Operation, OperationManager, PathConfig,
)


async def main(arguments: argparse.Namespace) -> None:
    if not arguments.home.is_dir():
        raise ValueError("Choose an existing Raven home containing ingested documents.")
    path_config = PathConfig(arguments.home)
    operation_manager = OperationManager(path_config)
    knowledge_base = KnowledgeBase(path_config, operation_manager)
    ollama_manager = None

    async with AsyncExitStack() as exit_stack:
        exit_stack.push_async_callback(operation_manager.close)
        exit_stack.push_async_callback(knowledge_base.close)
        if arguments.query:
            ollama_manager = OllamaManager(
                host=arguments.ollama_host, operation_manager=operation_manager
            )
            exit_stack.push_async_callback(ollama_manager.close)
        exit_stack.push_async_callback(operation_manager.cancel_active)

        await operation_manager.recover()
        await knowledge_base.start()
        knowledge = knowledge_base.get(arguments.knowledge)
        print("Knowledge:", json.dumps(knowledge.meta, indent=2))
        files = await asyncio.to_thread(knowledge.list_files_page, limit=3)
        print("First files:", json.dumps(files, indent=2))
        if not arguments.query or ollama_manager is None:
            return

        async def inspect_matches(operation: Operation) -> dict[str, Any]:
            load_embedding_task = await ollama_manager.load_embedding(
                arguments.embedding, operation=operation
            )
            embed_model = await load_embedding_task.result()
            await knowledge.validate_embedding_model(embed_model)
            query_vector = await embed_model.aget_query_embedding(arguments.query)
            hits = await knowledge.search(query_vector, top_k=arguments.top_k)
            seen_section_ids: set[str] = set()
            section_previews = []
            for hit in hits:
                section_id = hit["section_id"]
                if section_id in seen_section_ids:
                    continue
                seen_section_ids.add(section_id)
                section = await asyncio.to_thread(knowledge.get_section, section_id)
                if section is None:
                    continue
                section_previews.append({
                    "knowledge_name": knowledge.name,
                    "file_name": section["file_name"],
                    "section_id": section_id,
                    "summary": section["summary"],
                    "raw_content_preview": section["raw_content"][:300],
                })
            await operation.publish(Event(
                type=EventType.WORK_PROGRESS,
                data={"chunk_matches": len(hits), "sections": len(section_previews)},
            ))
            return {
                "query": arguments.query,
                "query_dimensions": len(query_vector),
                "hits": hits,
                "section_previews": section_previews,
            }

        operation = await operation_manager.create("example.knowledge_vector_search")
        root_task = await operation.run(operation.name, inspect_matches)
        print("Operation:", operation.operation_id, flush=True)
        async for event in root_task.events():
            print(event.event_id, event.type.value, event.task_name, flush=True)
        print("Search result:", json.dumps(await root_task.result(), indent=2))


if __name__ == "__main__":
    argument_parser = argparse.ArgumentParser()
    argument_parser.add_argument("home", type=Path)
    argument_parser.add_argument("knowledge")
    argument_parser.add_argument("--query")
    argument_parser.add_argument("--top-k", type=int, default=5)
    argument_parser.add_argument("--embedding", default="bge-m3:latest")
    argument_parser.add_argument("--ollama-host")
    asyncio.run(main(argument_parser.parse_args()))
```

</figure>

Without `--query`, the program ends after its stored-data inspection. With a
query, `inspect_matches()` runs under a root operation, loads one embedding
adapter, validates it, embeds the query, and searches chunk vectors. It reads
each matching section at most once. The final JSON contains the chunk hits
and section previews for inspection; it is this example's chosen result,
not a replacement for the retrieval pipelines' output format.

`work.progress` reports how many chunks and distinct sections were found.
The direct knowledge reads do not automatically emit retrieval-pipeline
events. The operation history is saved under the supplied home's user
directory. The script does not add or replace documents; its validation can
record an embedding identity if the knowledge does not already have one.

### 14.5 Releasing storage and keeping ingestion in the pipeline

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Knowledge.release_resources()</figcaption>

```python
async def release_resources(self) -> bool
```

</figure>

`release_resources()` closes idle storage connections while leaving the
`Knowledge` object usable. It returns `False` if an active use prevents
release, and `True` when storage is released or already unopened. A later
async database operation can reopen it. `KnowledgeBase` uses this mechanism
to release idle stores as other knowledges are opened; active work can keep
stores open temporarily beyond `max_open_knowledges`.

`knowledge.close()` instead ends that object's lifecycle: it cannot be
restarted, and `get_section()` raises `knowledge_closed`. Leave final closure
of a manager-owned knowledge to `knowledge_base.close()`. If `Raven` owns the manager, leave its
shutdown to `Raven.close()`, following [Section 12](#component-workspace).

Keep source-file ingestion in `IngestionPipeline.run()`. `Knowledge.ingest()`
is the persistence stage used after parsing, semantic splitting, and metadata
extraction; it does not replace those stages. The next section explains which
document-processing dependencies and intermediate results can be customized.

Finally, backend section methods are not agent navigation tools.
`Knowledge.list_sections()` reads complete stored section records; it does
not acquire the agent tool's content flags or output-size safeguards. A custom
application must choose how much of that data to display or send to a model.
Calling a backend method directly does not automatically apply the harness's
tool-selection policy.
