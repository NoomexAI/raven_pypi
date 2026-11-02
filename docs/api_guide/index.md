---
layout: default
title: API guide
description: "Understand RAVEN's Python API: prepare documents, manage conversations, inspect sources, and observe the work behind an answer."
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
The component model gives the application direct control over that connection.
These are two ways of using RAVEN, not successive setup stages.

<details class="page-links-group" markdown="1">
<summary>High-level API</summary>

- [Overview of the Raven API](#raven-api-overview) — The capabilities available
  through `Raven`, the methods serving them, and where they fit in an application.
  Section 1.

</details>

<details class="page-links-group" markdown="1">
<summary>Component model</summary>

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
data will be used. `Raven.start()` prepares access to that workspace, loading
its saved defaults, discovering existing knowledges and conversations, and
recovering records of interrupted work. It does not choose models or start
a model server. `Raven.close()` releases the running resources when the
application finishes; saved documents and conversations remain on disk.

This separates the application's lifetime from its data. A later launch can
start a new `Raven` instance against the same home and continue working with
what was saved there.

<figure class="code-snippet api-signature" markdown="1">
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

`Raven.configure_models()` connects those choices to the backend. Each choice
is described by a `ModelSpec`, which identifies its provider, model, role, and
any supported options or credential reference. A local Ollama model and a
supported cloud model are alternative ways to supply a role; the document
and conversation APIs remain the same.

<figure class="code-snippet api-signature" markdown="1">
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
itself. `options` holds provider-supported settings; `dict` in this signature
denotes a default factory that creates a fresh empty dictionary for each
specification, not a value that needs to be passed explicitly.

<figure class="code-snippet api-signature" markdown="1">
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
Model configuration and model residency are also separate concerns: having
a configured connection is not a claim that weights occupy GPU memory.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_ollama_models()</figcaption>

```python
async def list_ollama_models(
    self,
    *,
    operation: Operation | None = None,
) -> OperationTask
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
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
`Raven.get_knowledge()` opens access to an existing one, while
`Raven.list_knowledges()` lets an interface discover the available names.
Creating a knowledge does not add documents to it. That is the role of
`Raven.ingest()`, which prepares a source file and stores its searchable
representation in the chosen knowledge.

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_knowledge_files()</figcaption>

```python
async def list_knowledge_files(
    self,
    knowledge_name: str,
) -> list[dict[str, Any]]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.list_file_sections()</figcaption>

```python
async def list_file_sections(
    self,
    knowledge_name: str,
    file_name: str,
) -> list[dict[str, Any]]
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
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
conversation context and persists the completed turn; the interface does not
need to recreate the agent loop or maintain a separate model transcript.

Closing a session leaves its conversation intact. An application can later
use `Raven.list_conversations()` to find a saved discussion,
`Raven.get_conversation()` to access it, and
`Raven.get_conversation_messages()` to display its actual history. Preferences
saved through the conversation APIs guide that particular discussion, while
memory mechanisms help the model continue as its history grows.

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

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
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
through its conversation and turn IDs. This lets an interface offer source
inspection for an earlier answer, not only the latest response on screen.

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
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

Many high-level actions return an `OperationTask` through which the caller
obtains the confirmed result. A session turn returns a `SessionRun`, which
supports streaming its events and collecting its final answer. These handles
keep observation connected to the same execution; reading progress does not
start the action again. Ordinary inspection methods can return values directly
instead, so the detailed sections explain the contract of each method.

The operation methods support interfaces that must keep working after the
initial request. `Raven.get_operation()` finds an operation,
`Raven.operation_events()` exposes its retained events, and
`Raven.cancel_operation()` requests that its work stop. If an action fails,
`Raven.list_retryable_tasks()` identifies tasks eligible for explicit retry
through `Raven.retry_task()`. Interrupted work is detected after a restart;
that is not a promise that every action can automatically resume.

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.get_operation()</figcaption>

```python
async def get_operation(self, operation_id: UUID | str) -> Operation
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
<figcaption class="code-snippet-filename">Raven.cancel_operation()</figcaption>

```python
async def cancel_operation(self, operation_id: UUID | str) -> Operation
```

</figure>

<figure class="code-snippet api-signature" markdown="1">
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

<figure class="code-snippet api-signature" markdown="1">
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

`SystemConfig` supplies policy and limits when `Raven` is constructed.
`PathConfig`, available through `raven.paths`, derives where the
user's data belongs. `RuntimeConfig` holds the current defaults for later
work, such as document splitting, retrieval, and conversation memory.

`Raven.get_runtime_settings()` reads those defaults.
`Raven.update_runtime_settings()` requests changes, and
`Raven.reset_runtime_settings()` restores the built-in defaults within the
system's limits. Methods can also accept per-call choices, allowing one
request to differ without changing the user's saved defaults. The detailed
configuration section explains which controls can change during use and which
require constructing a new runtime.

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
