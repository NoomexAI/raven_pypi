---
layout: default
title: Architecture
description: "The components, execution model, and architectural tradeoffs behind RAVEN's document-backed conversations."
stylesheets:
  - /assets/css/architecture.css
scripts:
  - /assets/js/architecture.js
---

# RAVEN's architecture and design

This page examines RAVEN v0.2.0: how its components divide responsibility,
how work moves between them, and why those boundaries exist. The
[In Action walkthrough]({{ '/demo/' | relative_url }}) builds an application
with RAVEN. Here, the subject is the backend itself, rather than the terminal
interface or its setup instructions.

## 1. Architectural overview

### 1.1 What the architecture is designed to solve

Retrieving a passage and generating an answer is only one part of a persistent
document assistant. Documents must first be prepared for search. A conversation
must retain the messages and tool exchanges needed to continue it. Long-running
work needs visible progress and an identifiable outcome. After an answer has
been generated, its supporting material should remain available for inspection.

RAVEN separates these responsibilities instead of placing them inside a single
chat function. **Ingestion prepares the material; retrieval selects it; the
agent decides how to use the available tools; the conversation retains the
interaction; reconstruction makes the supplied source sections inspectable.**
Operations and events provide a common way to observe and manage the work
across those components.

This separation has two consequences. First, a capability can be used without
the complete chat workflow: a Python application can ingest a document, run a
retrieval pipeline, or reconstruct sources independently. Second, composing
those capabilities requires explicit ownership of state, tasks, and resources.
RAVEN supplies that coordination, but the additional lifecycle and persistence
machinery is a cost of the design, not a requirement for every RAG application.

The storage model is local-first: knowledge data, conversation records, and
operation history live beneath an explicitly supplied home directory, in
user-scoped folders. That does not require inference to be local. Ollama and
supported cloud providers can supply the models; cloud inference sends the
relevant input to the remote provider even when the resulting records are
stored locally.

### 1.2 The domain model and its vocabulary

Several objects participate in one question without representing the same
thing. The distinctions below establish the vocabulary used throughout this
page.

| Term | Responsibility |
| --- | --- |
| **Knowledge** | A named persistent resource containing documents, section records, metadata, and a vector store. It is more than a set of embeddings. |
| **Knowledge base** | The set of knowledges managed through `KnowledgeBase`, which handles their discovery and lifecycle. |
| **File and parsed element** | A file is an ingested document. An element is an ordered unit produced during parsing, such as text or a table, with source-location information when available. |
| **Section and chunk** | A section groups source content into a semantic unit. Smaller chunks support vector search; retrieved chunks are resolved back to the sections supplied as context. |
| **Conversation** | The persistent identity, scope, history, memory, preferences, and saved turn results of an interaction. |
| **Conversation manager** | `ConversationManager` creates, discovers, opens, and manages the lifecycle of conversation resources. |
| **Session** | A temporary interaction medium over a conversation. It connects a request to the harness and coordinates saving the completed turn; it is not another history store. |
| **Agent harness** | The runtime boundary around model behaviour: prompts, permitted tools, agent execution, event translation, and evidence tracking. |
| **Operation, operation task, and event** | An operation groups managed work. Its tasks represent individual executions with results and lifecycle state. Events record observable activity attributed to that work. |
| **Evidence and reconstructed file** | Evidence identifies source sections obtained during a run. Reconstruction uses those references to present the file's sections in order, highlighting the supplied material alongside its surrounding context. |

These identities serve different purposes. A conversation ID identifies the
persistent resource; a turn ID identifies one saved interaction within it;
operation and task IDs identify executions. A retry is therefore not
necessarily a new conversation or a new saved turn. Their precise relationships
are covered in the execution and recovery sections.

Document identity is similarly explicit. An ingested file has a UUID-derived
file ID, and its section IDs use the form `<file_id>-<section_number>`. The suffix identifies
the section's ordinal within that file; it is not a separately generated UUID
or a section title. This connection lets retrieval references resolve to the
stored source sections without asking the model to reconstruct their identity.

### 1.3 Component ownership and the dependency map

RAVEN has two application-facing entry points: the Python API and the optional
FastAPI server. The server obtains a user-scoped `Raven` instance and delegates
backend work to it. A Python caller can use `Raven` directly, without HTTP, or
compose the lower-level components when more control is needed.

{% include diagram.html id="architecture-overview" label="Component ownership" width="960" height="590" alt="Ownership overview: Python applications access Raven directly; HTTP applications reach it through FastAPI. Raven composes persistent-resource managers, document workflows, conversation execution, and model providers, with operations and events spanning the work." %}

*The high-level API composes backend capabilities rather than replacing
them. The connectors show composition and delegation, not a mandatory order of
execution. Persistent resources outlive individual sessions and agent runs.*

`Raven` is the **composition root**: the place where the managers, provider,
operation manager, and workflows are brought together. It provides convenient
entry points and manages their lifecycle. It does not implement a second parser,
retrieval engine, or conversation database alongside the underlying components.
Model-dependent pipelines and the harness are configured when the model pair
is prepared; constructing the facade does not itself start model inference.

The source packages reflect these boundaries:

| Packages | Architectural role |
| --- | --- |
| `data_management` | Knowledge and conversation resources, their managers, persistent records, and discovery. |
| `document_processing` and `pipelines` | Parsing, provenance-aware sectioning, ingestion, the four retrieval strategies, and source reconstruction. |
| `agent` and `session` | Model behaviour and tool execution, separated from the session's coordination of a conversation turn. |
| `providers` | Model specifications, provider routing, and adapters for the LLM and embedding roles. |
| `core` | Configuration, errors, operations, tasks, events, shared operation storage, and supporting lifecycle mechanisms. |
| `h_api` and `server` | The high-level Python facade and the optional HTTP transport/runtime boundary. |

The same distinction applies to external dependencies. LlamaIndex supplies
model interfaces, agent execution, and memory primitives; Qdrant provides
vector storage and search; Docling handles formatted-document conversion;
SQLite stores transactional records; FastAPI supplies the HTTP framework.
RAVEN defines how these pieces work together: source identities and provenance,
retrieval workflows, conversation ownership, operation tracking, recovery,
and reconstruction. These are responsibilities coordinated around those
dependencies, not claims that RAVEN implements each underlying engine itself.

Two execution paths make that division concrete:

1. **Standalone retrieval.** The caller selects a retrieval pipeline, supplies
   a query and scope, and receives an operation task. The pipeline selects
   source sections and returns its result through that task. No conversation,
   session, or chat agent is required. Some strategies still use an LLM for
   relevance scoring; “without chat” does not mean “without model calls.”
2. **A conversation turn.** A session runs the harness against a conversation.
   The harness exposes scope-appropriate tools, and the model may retrieve,
   navigate, consult memory, or answer directly. Tool calls and model output
   produce events; source evidence can feed reconstruction. The session then
   coordinates the durable turn commit. Generating answer text and successfully
   saving the full turn are distinct completion boundaries.

When these steps belong to one composed workflow, they can share an operation
and event history while retaining separate task results. The interface need
not infer backend state from a spinner or a partially displayed answer.
Conversely, the backend does not prescribe how that activity should appear in
a terminal, desktop window, or website.

This is the organizing principle for the rest of the architecture: **persistent
resources own state, workflows perform work against that state, and application
boundaries expose the work without absorbing its implementation**. The following
sections examine those decisions, including their limits, in detail.

## 2. Configuration and model providers

Configuration and model selection answer different questions. Configuration
defines where a runtime stores data, which limits it must respect, and which
defaults apply to new work. Model selection supplies the implementations that
perform generation and embedding. Keeping these concerns separate avoids
making every document pipeline responsible for deployment settings or
provider-specific clients.

### 2.1 Configuration, paths, and user isolation

#### Three scopes, not one global settings object

RAVEN uses three configuration types because their values have different
owners and lifetimes:

| Type | Scope | Examples | How it changes |
| --- | --- | --- | --- |
| `SystemConfig` | Deployment policy; shared across user runtimes in one server | Request limits, allowed origins, cache bounds, operation capacity, and maximum retrieval/agent limits | Construct or load a new configuration and restart the server or create a new library runtime. |
| `PathConfig` | One user runtime | The home directory, user UUID, and derived storage paths | Construct a runtime for the intended home and user; paths are not reassigned during its lifetime. |
| `RuntimeConfig` | One user's adjustable workflow defaults | Semantic-splitting threshold, chunk settings, retrieval count, agent iterations, and memory limits | Validate and persist a replacement snapshot through the `Raven` settings API. |

All three are frozen dataclasses. In particular, **mutable runtime settings do
not mean a mutable `RuntimeConfig` object shared by active tasks**. The `Raven`
instance owns the current snapshot and replaces it after an accepted update.
This makes the distinction between a setting's lifetime and a running task's
inputs explicit.

System settings also constrain runtime choices. For example, a user's
`retrieval_top_k` cannot exceed `SystemConfig.max_retrieval_top_k`, and
`agent_max_iterations` cannot exceed `SystemConfig.max_agent_iterations`.
These ceilings let a deployment permit adjustment without giving each caller
control of the deployment's resource policy.

#### Startup policy versus adjustable defaults

The server loads system policy at startup. By default, it discovers
`system_settings/system_settings.json` beneath the supplied home, creating
that file from defaults if it is absent. An explicit file can instead be
selected with the CLI's `--system-settings` argument; that file must exist
and is read in place. The running server does not hot-reload either file.
Editing system policy therefore requires a restart.

Library construction is deliberately different. `Raven` accepts a
`system_config` object and otherwise uses an in-memory `SystemConfig()`.
Its constructor does not discover or create a system-settings file. An
application that wants file-backed policy can load it explicitly before
constructing the runtime. Filesystem discovery is a server-startup convention,
not a hidden side effect of every library constructor.

Per-user runtime defaults are loaded or created during `Raven.start()`. They
are stored in `runtime_settings/runtime_settings.json` beneath that user's
root. After startup, the public update/reset methods change the stored and
in-memory configuration together; editing the JSON file directly does not
hot-reload the running instance.

A state-changing update follows a short sequence: check the optional
`expected_revision`, validate the proposed values and system ceilings,
persist the replacement, and install the new snapshot. The revision check
rejects an update based on stale settings instead of silently overwriting
another caller's change. A no-op update keeps the existing revision; resetting
defaults creates another revision. The update methods return operation tasks,
so accepting a request is distinct from confirming that its settings were saved.

These values are **defaults for subsequently constructed work**, not live
controls on an existing execution. Ingestion captures its effective settings
when the facade call prepares the task. A session captures its retrieval,
iteration, and memory limits when `Raven.session()` constructs it. Updating
defaults does not rewrite that existing session or an ingestion already in
progress. Supported per-call overrides can choose different values without
changing the persisted defaults, subject to the same validation and ceilings.

#### A home directory with user-scoped paths

`raven_home` is supplied explicitly; RAVEN does not choose a platform-specific
default home. `PathConfig` validates and normalizes `user_id` as a UUID and
derives a user root at `raven_home/<user_id>/`. Its fixed paths include:

| Path beneath the user root | Purpose |
| --- | --- |
| `data/knowledge_base/` | Knowledge resources and their document/vector stores |
| `data/conversations/` | Conversation records and memory stores |
| `operations/operations.sqlite3` | Operation/task records and event history |
| `runtime_settings/runtime_settings.json` | The user's adjustable workflow defaults |
| `temp/uploads/` | Controlled upload staging and retry-source retention |

The server's system-settings file is outside these user roots because its
policy applies across them. The library's default user UUID provides a stable
single-user context, not a new randomly generated identity on every launch.
Hosted applications supply their own verified user context instead.

The following trees illustrate a populated home rather than showing an actual
installation. The local user root uses RAVEN's fixed default UUID; hosted user
UUIDs, conversation IDs, upload IDs, and document names are fictional. Fixed
directory and storage filenames reflect RAVEN's layout; resources and optional
files appear as they are created or used.

**Local desktop use — one user root.** This example assumes that a desktop
application launches the server through the CLI using its default single-user
context. The backend uses `00000000-0000-0000-0000-000000000001` as the user
root: this identity is fixed, not randomly generated at startup.

```text
raven-home/
├── system_settings/
│   └── system_settings.json
├── temp/
│   └── server_auth/
│       ├── server.lock
│       └── bearer_token
└── 00000000-0000-0000-0000-000000000001/
    ├── data/
    │   ├── knowledge_base/
    │   │   ├── engineering/
    │   │   │   ├── metadata.json
    │   │   │   ├── file.sqlite3
    │   │   │   └── qdrant/
    │   │   └── research/
    │   │       ├── metadata.json
    │   │       ├── file.sqlite3
    │   │       └── qdrant/
    │   └── conversations/
    │       ├── 5c72a09e13bd/
    │       │   ├── metadata.json
    │       │   ├── messages.sqlite3
    │       │   ├── preferences.json
    │       │   └── qdrant/
    │       └── 7da431c8e052/
    │           ├── metadata.json
    │           ├── messages.sqlite3
    │           └── qdrant/
    ├── operations/
    │   ├── operations.sqlite3
    │   └── operations.sqlite3.lock
    ├── runtime_settings/
    │   └── runtime_settings.json
    └── temp/
        └── uploads/
```

Here, `engineering/file.sqlite3` might contain the parsed text and section
metadata for a fictional `sensor-guide.pdf`. The original filename is a record
inside the knowledge store, not a requirement to create a permanent
`engineering/sensor-guide.pdf` beside it. The conversation's directory uses
its conversation ID, while its readable title belongs in `metadata.json`.
`preferences.json` appears when preferences are saved.

The CLI's `temp/server_auth/` belongs to the **server home**, not to a user.
It holds the server-ownership lock and the private token file for that launch;
the token value is not shown here. A local application using only the Python
API has the same user-data layout, but does not automatically create these
CLI authentication files or discover a system-settings file. It can supply
SystemConfig directly, as described above.

**Hosted use — several user roots under the same home.** The host resolves
authenticated accounts to internal UUIDs. Each runtime then derives its paths
from that UUID; no second home-directory layout is needed.

```text
raven-home/
├── system_settings/
│   └── system_settings.json
├── temp/
│   └── server_auth/
│       ├── server.lock
│       └── bearer_token
├── 29b87f10-d452-4c63-a9e6-0d8132fbc795/
│   ├── data/
│   │   ├── knowledge_base/
│   │   │   ├── engineering/
│   │   │   │   ├── metadata.json
│   │   │   │   ├── file.sqlite3
│   │   │   │   └── qdrant/
│   │   │   └── mission_notes/
│   │   │       ├── metadata.json
│   │   │       ├── file.sqlite3
│   │   │       └── qdrant/
│   │   └── conversations/
│   │       ├── a4e19c70b263/
│   │       │   ├── metadata.json
│   │       │   ├── messages.sqlite3
│   │       │   ├── preferences.json
│   │       │   └── qdrant/
│   │       ├── 631fc9a20d7e/
│   │       │   ├── metadata.json
│   │       │   ├── messages.sqlite3
│   │       │   └── qdrant/
│   │       └── b5702e8d164c/
│   │           ├── metadata.json
│   │           ├── messages.sqlite3
│   │           ├── preferences.json
│   │           └── qdrant/
│   ├── operations/
│   │   ├── operations.sqlite3
│   │   └── operations.sqlite3.lock
│   ├── runtime_settings/
│   │   └── runtime_settings.json
│   └── temp/
│       └── uploads/
│           ├── b6c3a970-84d2-4e15-9f68-31a0d247c5eb.pdf
│           └── browser/
│               └── f3d2108a-6e45-4927-bc19-05a8d7642ef0/
│                   ├── manifest.json
│                   └── sensor-guide.pdf
└── d6e13075-8a29-4fbc-b702-693de815a04c/
    ├── data/
    │   ├── knowledge_base/
    │   │   ├── engineering/
    │   │   │   ├── metadata.json
    │   │   │   ├── file.sqlite3
    │   │   │   └── qdrant/
    │   │   ├── literature/
    │   │   │   ├── metadata.json
    │   │   │   ├── file.sqlite3
    │   │   │   └── qdrant/
    │   │   └── field_reports/
    │   │       ├── metadata.json
    │   │       ├── file.sqlite3
    │   │       └── qdrant/
    │   └── conversations/
    │       ├── 9b2f604d7ac1/
    │       │   ├── metadata.json
    │       │   ├── messages.sqlite3
    │       │   └── qdrant/
    │       └── ec1386d4a095/
    │           ├── metadata.json
    │           ├── messages.sqlite3
    │           └── qdrant/
    ├── operations/
    │   ├── operations.sqlite3
    │   └── operations.sqlite3.lock
    ├── runtime_settings/
    │   └── runtime_settings.json
    └── temp/
        └── uploads/
```

The local example has two knowledges and two conversations. In the hosted
example, the first user has two knowledges and three conversations; the second
has three knowledges and two conversations. These counts vary independently:
a conversation does not require its own knowledge, and multiple conversations
can use the same knowledge.

Both users can have a knowledge named `engineering`: the resources are
different because their roots are different. Their conversations, operation
databases, runtime defaults, and upload areas are likewise separate. By
contrast, `system_settings/` and the CLI's `temp/server_auth/` remain shared
at home level. Selecting an external system-settings file replaces discovery
of the illustrated default file; it does not create a copy in every user root.

The first hosted user illustrates two temporary source forms. The UUID-named
PDF directly inside `uploads/` is an ingestion working snapshot, removed when
that pipeline finishes its cleanup. The file inside `uploads/browser/<id>/`
is a browser-upload source retained for the bounded retry window; its
`manifest.json` records expiry and operation/task references once submitted.
Neither is the permanent parsed-document store. An empty upload area is normal
when no such sources are present.

These trees show application-level storage, not every engine-generated file.
Qdrant manages files beneath each `qdrant/` directory, and SQLite can create
`-wal` and `-shm` sidecars while databases are in use. Atomic writes can also
leave transient temporary files while they execute. Ollama's downloaded model
weights, provider secrets, and the host's account database are not stored in
these user folders by RAVEN.

This layout separates resource ownership, but it is not an authentication
system. A syntactically valid UUID does not establish that the caller may
access its directory, nor do separate folders encrypt data. The host must
authenticate accounts and authorize their internal identities before passing
them across the configured server boundary. RAVEN then uses that context to
select the runtime and storage paths. The HTTP trust boundary is examined
later; it should not be inferred from the path layout alone.

### 2.2 Model providers and capability boundaries

#### The problem: workflows need capabilities, not provider-specific clients

RAVEN's document and conversation workflows need two kinds of model work:

- **Language generation:** the LLM reads context, uses tools, extracts metadata,
  and produces responses.
- **Embedding:** the embedding model converts text into vectors for semantic
  sectioning, retrieval, and conversation-memory search.

Neither workflow should need a different implementation merely because a
model runs through Ollama rather than a cloud API. If provider-specific
requests were embedded in ingestion, retrieval, and chat, changing providers
would require changes throughout the backend. RAVEN instead places a model
access boundary between those workflows and the services running the models.

#### What an adapter is, and why RAVEN uses one

An **adapter** is a Python object that exposes a common interface while
handling the service-specific request and response details behind it. For
example, a workflow can request an asynchronous chat response through the
same LlamaIndex interface whether the underlying service is Ollama or a
supported remote provider.

The adapter is **not the model's weights**, and constructing one does not
by itself mean that a model is resident in GPU memory. It is the object through
which RAVEN asks a backend to perform inference.

RAVEN uses LlamaIndex's model adapters rather than defining a separate model
protocol for every pipeline. Its own provider layer selects and prepares
those adapters. The responsibilities within RAVEN are distinct:

| RAVEN component | Architectural responsibility |
| --- | --- |
| `ModelSpec` | Describes the selected provider, model, and role. It is configuration data, not a running model or client. |
| `Provider` | Resolves that description to the appropriate manager. It keeps provider selection out of the document and conversation workflows. |
| `OllamaManager` / `LiteLLMManager` | Build or reuse the LlamaIndex-compatible adapter for the requested role, using their backend-specific rules. |

The adapters and model backends sit outside this list of RAVEN-defined
components. LlamaIndex supplies the adapter interfaces and implementations;
the external Ollama service or the service reached through LiteLLM performs
inference. RAVEN's components prepare access to those dependencies rather
than implementing the models themselves.

This separation allows the same retrieval and agent components to consume
different model implementations. It does not make the models equivalent:
the adapter can translate an interface, but it cannot give a model reliable
tool calling or structured output that the model does not support.

#### Selection: describing the two model roles

Selection begins with two specifications, one for each role. Each selection
identifies the **provider**, **model**, and **role**, together with an
**`api_key_ref`** when the service needs an explicitly supplied API key.
That reference names the environment variable containing the credential;
it is not the secret itself.

For example, a keyed LiteLLM selection combines the provider's routing
identifier, its model identifier, and the environment-variable name in
`api_key_ref`. Provider resolves that reference when preparing the adapter.
It is part of choosing how RAVEN accesses the model, not an advanced tuning
option. The reference can be omitted for Ollama or a provider using ambient
credentials instead.

This local-model example leaves both key references unset. It only constructs
the descriptions; it does not contact a service, download weights, or
configure a running Raven instance:

```python
from nraven import ModelRole, ModelSpec


llm_spec = ModelSpec(
    provider="ollama",
    model="gemma4:e2b",
    role=ModelRole.LLM,
    api_key_ref=None,
)

embedding_spec = ModelSpec(
    provider="ollama",
    model="bge-m3:latest",
    role=ModelRole.EMBEDDING,
    api_key_ref=None,
)
```

Provider routing then follows the specification:

- `provider="ollama"` selects `OllamaManager`.
- Other supported provider identifiers select `LiteLLMManager`, which uses
  LlamaIndex's LiteLLM integrations.
- The roles are independent. A cloud LLM and a local embedding model can form
  one configured pair; both roles need not use the same backend.

The lower-level specification rules remain deliberately narrower than the
architectural interface:

- Non-secret `options` provide adapter-specific tuning where supported;
  they are not a place to supply API keys.
- Unknown fields are rejected. Provider identifiers must be valid routing
  values for the selected integration, not merely a vendor's display name.
- Options are not universally portable. Ollama specifications currently reject
  nonempty `options`; LiteLLM options depend on the adapter and cannot override
  manager-controlled routing or credential fields.

#### Configuration: connecting the selection to RAVEN's workflows

`Raven.configure_models()` turns the selected pair into the interfaces used
by subsequent work. At the architectural level, it connects **descriptions →
provider managers → validated adapters → model-dependent workflows**.

That configuration follows this sequence:

1. **Resolve the pair.** Check the two roles and ask Provider to build or reuse
   their adapters. For Ollama, this includes checking availability and pulling
   missing weights through the external service.
2. **Check compatibility.** Validate the required interfaces and, when the pair
   changes, the embedding model's compatibility with existing knowledge stores.
3. **Attach the pair.** Install the adapters into the Raven runtime and rebuild
   its model-dependent retrieval components for subsequent work.
4. **Prepare local residency where applicable.** Unload replaced Ollama models
   that are no longer selected, then preload the configured embedding model
   and LLM. Cloud roles have no local residency step to perform.

The distinction between selection and configuration is important: naming a
model does not prepare it, and preparing an adapter does not prove that a
later inference request will succeed.

Execution details qualify this high-level flow:

- Configuration is serialized by the runtime's model lock and returns an
  operation task. Its result confirms completion, not merely request acceptance.
- New sessions are rejected while replacement is in progress. Existing sessions
  keep the adapters supplied when they were constructed.
- Pre-installation validation failures preserve the old pair. An unload or
  preload failure can occur **after** the new adapters are installed; model
  configuration is not an atomic transaction over an external Ollama service.
- Facade checks cover async chat, streamed chat, structured prediction, async
  query/batch embedding, and similarity support. The agent also needs a
  compatible function-calling LLM; successful adapter construction alone is
  not an end-to-end tool-use test.

#### Residency: where weights live is a separate concern

For Ollama, **installed** means that weights are available in the external
service's storage; **resident** means that the service has loaded a model into
runtime memory. RAVEN's adapter is neither of those things. This distinction
explains why an adapter can remain configured after its model is unloaded.

| Action | What changes |
| --- | --- |
| Manager `load_llm()` / `load_embedding()` | Prepare or reuse an adapter; Ollama may also need to download missing weights. These methods are not the explicit residency controls. |
| `Raven.configure_models()` | Apply the adapter pair to RAVEN and perform the Ollama residency preparation described above. |
| Preload a configured model | Ask Ollama to make its weights resident without selecting a different adapter. |
| Unload a configured model | Release Ollama residency without clearing the adapter or deleting installed weights. A later inference can load the model again. |

Cloud models retain the same adapter-facing workflow, but their weights remain
with the remote service. Preload/unload requests for cloud roles report that
local residency does not apply.

The remaining resource details follow from that separation:

- **Adapter caching:** both managers keep bounded, least-recently-used caches
  of Python adapter objects. These are not answer caches or stores of weights.
  Cache eviction does not unload Ollama, and an active workflow may still hold
  an adapter reference.
- **Cache identity:** Ollama's manager-local caches use model names. LiteLLM
  additionally distinguishes role, provider, options, and a fingerprint of an
  explicitly supplied API key so differently configured clients are not
  unintentionally treated as one.
- **External lifecycle:** the app or host owns the Ollama process. RAVEN does
  not choose its CPU/GPU backend or stop that service when Raven closes.
- **Shared residency:** separate user folders do not create separate Ollama
  servers. Unloading a model can affect other users of the same service, so a
  hosted application must control model-administration access and policy.

#### Boundaries the common interface does not remove

The model boundary keeps provider plumbing out of the pipelines. It does not
remove credential ownership, model limitations, or the relationship between
an embedding model and stored vectors:

- **Credentials:** `api_key_ref` names an environment variable resolved when
  loading a non-Ollama adapter. Secret values are not part of ModelSpec or its
  returned configuration metadata; credential-like options are rejected. The
  host supplies the environment—RAVEN does not automatically read `.env` or
  provide a key vault.
  - An adapter can retain its credential in memory. Its cache fingerprint is
    an identity mechanism, not encryption or a security boundary.
  - Changing an environment variable does not rewrite an existing adapter.
    Credential rotation requires loading/configuring the intended adapter again.
- **Capabilities:** adapters preserve a common request interface, not uniform
  model behaviour. Tool-calling quality, structured output, optional thinking,
  remote quotas, and inference failures still depend on the selected service.
- **Embedding compatibility:** RAVEN records an identity derived from the
  adapter/model and relevant settings, and checks identity and vector dimensions
  in the relevant storage paths.
  - Changed-pair configuration checks existing knowledge stores. Conversation
    vector memory also checks compatibility when initialized or used.
  - Identity checks detect known mismatches; they cannot guarantee that an
    external model behind an unchanged name or mutable tag stays identical.
    Reproducible deployments still need a model-version policy.

> **An embedding-model switch is a data change, not just a provider change.**
> Existing knowledge and conversation vectors belong to the embedding space
> that produced them. Equal dimensions do not make another model compatible,
> and configuring a replacement does not re-embed the stored data. Changing
> models requires rebuilding the affected embeddings from their source data.

The LLM can change independently of this vector-space constraint, provided
its capabilities remain suitable. The resulting architecture leaves each
concern with a clear owner: specifications describe the choice, Provider and
its managers prepare model access, pipelines consume the adapters, and the
external services execute inference and manage their weights.

## 3. Knowledge and ingestion

Ingestion prepares a document for several different uses: searching its text,
reasoning over its metadata, and presenting the source after retrieval. A
knowledge therefore needs more than a vector index. It must retain the text,
document order, and source relationships that the index alone cannot recover.

### 3.1 Knowledge storage and resource management

`KnowledgeBase` manages the user's knowledges: discovering existing resources,
creating and finding them, reporting discovery problems, and controlling their
lifecycle. Each `Knowledge` owns the contents and storage connections of one
knowledge. Its directory name identifies it within that user's knowledge base.

The storage is divided by responsibility:

| Storage | Responsibility | Why it is separate |
| --- | --- | --- |
| `metadata.json` | Knowledge identity, creation time, schema version, and user-written summary. | Describes the knowledge itself, independently of its individual documents. |
| `file.sqlite3` | File records, ordered section text and metadata, provenance, embedding identity, and pending file-deletion records. | Provides transactional records that can be inspected and navigated without a similarity search. |
| `qdrant/` | Embedded chunks and references back to stored sections. | Supports vector search without making vectors the authoritative source text. |

For example, a vector match identifies a section; the section's `raw_content`
comes from SQLite. The model does not have to reconstruct the passage from
overlapping chunks. The same stored section can also be read through navigation
or included in a reconstructed file.

The manager/resource separation also gives storage ownership a clear boundary:

- **Discovery:** knowledge metadata is inspected without opening every Qdrant
  client. Malformed resources produce discovery issues; they are not silently
  treated as though they never existed.
- **Mutations:** per-knowledge locks coordinate writes. File-name checks and
  ingestion claims prevent concurrent submissions from adding the same named
  file twice. File and section records are committed together in SQLite.
- **Resource use:** the manager tracks recently used handles and releases idle
  storage connections when the configured open-resource target is reached.
  Active resources are not evicted to satisfy that target, so it is not a hard
  limit on concurrent work.
- **Deletion and closure:** the knowledge owns vector deletion and record
  removal, including reconciliation of pending deletions. Closing resources
  releases their database clients; it does not delete the knowledge.

This lifecycle matters particularly for embedded Qdrant. Separate handles must
not open the same storage directory independently and compete for its lock.
The managed ownership model supports concurrent work on different resources;
it does not turn local storage into a distributed, multi-worker database.

### 3.2 Document parsing and source provenance

Documents do not arrive with RAVEN sections already defined. `DocumentParser`
first produces **ordered elements**: recognizable pieces of the parsed source,
such as text, a heading, a table, or a caption. Semantic sectioning happens
afterward. Keeping these stages separate allows section boundaries to change
without losing the connection to the original document.

The parser provides one normalization boundary for the supported formats:

- **TXT and Markdown:** UTF-8 source is read natively as one text element.
  Markdown syntax remains part of that text; this path does not build a separate
  element tree from its headings and lists.
- **PDF and DOCX:** Docling supplies document elements and any available page
  provenance. RAVEN normalizes these into `ParsedElement` objects inside a
  `ParsedDocument`.
- **Tables and other elements:** tables are exported as Markdown where
  available. Other elements contribute their available text. Picture elements
  are recognized, but this ingestion path does not send their pixels to a
  vision model; an empty picture element contributes no searchable text.

**Provenance** is the record of where parsed material came from. Ingestion
creates the file ID and passes that same identity into the parser. Ordered
elements receive IDs shaped as `<file_id>-element-<number>`, rather than
generating unrelated identities inside parsing. Later sections retain references
to the contributing elements.

Source navigation is deliberately less ambitious than reproducing the original
layout:

- A file uses `navigation_type="page"` when the parsed elements supply page
  provenance, otherwise `"none"`. The extension alone does not establish that
  usable page numbers exist; DOCX in particular must not be assumed to have
  PDF-like pagination.
- A section's `source_range` combines the available locations of its elements.
  `[12, 12]` identifies page 12; `[12, 13]` spans pages 12 through 13, inclusively.
  Missing locations remain absent rather than being invented.
- TXT and Markdown currently have no page navigation or generated line ranges.
  Their text is still ordered and identifiable through elements and sections.

These ranges allow an application to link a reconstructed section to the
relevant original pages. They do not describe an exact bounding box or promise
that the reconstructed text has the original document's visual layout.
Conversion can be lossy, especially for complex tables, images, and reading
order; preserving provenance makes the resulting representation inspectable,
not lossless.

### 3.3 Semantic sections and searchable chunks

RAVEN separates the unit used to **find** material from the unit supplied as
**evidence**. A small chunk can be a good vector match while omitting the
conditions or explanation around it. Returning its containing section provides
that context without returning the entire file.

`ProvenanceAwareSemanticSplitter` creates sections using the semantic-breakpoint
approach associated with LlamaIndex's semantic splitting, but implements the
grouping with RAVEN's provenance requirements. It is not an unmodified
`SemanticSplitterNodeParser` call.

Its sequence is:

1. **Form text units.** Text, headings, and list items are split into sentences;
   other non-empty elements, such as a projected table, remain individual units.
   Each unit retains its source element and available range.
2. **Compare neighbouring context.** The embedding model embeds a window around
   each unit. Distances between adjacent window embeddings indicate changes in
   the text's semantic context.
3. **Choose boundaries.** Distances above the configured percentile threshold
   become breakpoints, producing contiguous groups rather than rearranging
   passages by similarity.
4. **Carry provenance forward.** Each section contains the joined text,
   contributing element IDs, and combined available source range. Text is taken
   from the parser output, not rewritten by the metadata-extraction LLM.

The distinction between the resulting units is important:

- **Element:** a parser-level source reference. One element can contribute to
  several sections, and one section can span several elements.
- **Section:** an ordered evidence unit with `raw_content` and descriptive
  metadata. Stored IDs use `<file_id>-<section_number>`, starting at 1. Sections
  do not acquire model-generated names as their identity.
- **Chunk:** a smaller piece created within a section by LlamaIndex's
  `SentenceSplitter` for vector indexing. Overlap can preserve text around chunk
  boundaries without duplicating the section record.

`navigation_type` belongs to the stored **file**; `source_range` and
`source_element_ids` belong to each stored **section**. A Qdrant point carries
the file ID, section ID, and chunk index. Its point ID is derived deterministically
from that identity, so repeating the same file/section/chunk identity does not
require a new random vector-point ID.

The controls address different tradeoffs:

- **Breakpoint percentile:** a higher threshold generally produces fewer
  sections; a lower threshold generally permits more boundaries. Tied distances
  and the document's content also affect the outcome, so it is not a requested
  section count or a universal quality setting.
- **Buffer size:** changes how much neighbouring text informs each semantic
  comparison. It affects boundary selection, not a later expansion of retrieved
  evidence.
- **Chunk size and overlap:** control the smaller vector-search units. They do
  not change the already selected semantic section boundaries.

### 3.4 The ingestion pipeline

`IngestionPipeline` coordinates preparation; `Knowledge.ingest()` performs the
storage mutation. The ordinary application-facing entry point is
`Raven.ingest()`, which connects the configured models, runtime settings, and
operation management to that pipeline. The distinction keeps parsing and model
work out of the resource's basic storage responsibilities.

{% include diagram.html id="ingestion-workflow" label="Ingestion workflow" width="1000" height="840" alt="Ingestion success path: Raven starts a pipeline task; validation and a source snapshot lead to parsing, semantic sectioning, and LLM metadata extraction. Knowledge.ingest then chunks and embeds the text, writes Qdrant points, and commits SQLite file and section records before returning the result to the pipeline." %}

The arrows show execution order, not separate root operations. Preparation
passes its sections into a storage task within the same operation; progress
events span both. The embedding model is used twice: to choose semantic
boundaries during preparation, then to index chunks during storage.

A new ingestion follows this path:

1. **Identify and validate the source.** The pipeline assigns a UUID-derived
   12-character file ID, records the source identity for retry, and claims the
   file name in the destination knowledge. File and document limits bound the
   accepted input.
2. **Create a working snapshot.** Parsing reads a controlled temporary copy.
   Its digest is checked against the submitted source identity so that a file
   changed during preparation is not silently treated as the original input.
3. **Parse and section.** The parser receives that file ID; the provenance-aware
   splitter uses the embedding model to form sections from the parsed elements.
4. **Describe the sections.** A structured LLM completion extracts a summary,
   keywords, conditions, and definitions for each section. These fields support
   later reasoning-based retrieval. Invalid extraction is retried up to the
   configured limit; it does not replace `raw_content` with an LLM-authored
   version of the passage.
5. **Index and commit.** `Knowledge.ingest()` chunks the section text, obtains
   embeddings, checks compatibility, and upserts Qdrant points. It then commits
   the file and section records together in SQLite.
6. **Report and release.** The pipeline returns the ingestion result through
   its operation task, releases the file-name claim, and removes its working
   snapshot during normal cleanup. A browser upload retained for retry is a
   separate temporary input, governed by the upload-retention policy.

Both pipeline and storage work publish progress through the same operation.
`ingestion.progress` describes stages such as parsing, sectioning, and metadata
extraction; storage-level progress covers chunking and vector work. Counts are
specific to their stage, not an invented percentage of total remaining time.
An interface can therefore distinguish ongoing work from a committed result
without assuming every stage takes the same amount of time.

There is an important consistency boundary in step 5: **SQLite and Qdrant do
not share one transaction.** Vectors can be written before the authoritative
file record is committed.

- If an in-process failure occurs before that record commit, ingestion removes
  the inserted vectors. Cancellation-safe write handling waits for the durable
  transition to settle before deciding whether rollback is appropriate.
- A hard process kill can bypass that cleanup. Retrying the recorded ingestion
  reuses its file identity and reconciles the affected vectors first. A committed
  file is recognized instead of being inserted again; an uncommitted attempt is
  cleaned before replaying preparation.
- Graceful shutdown also checks tracked pending ingestions. Restarting does
  not mean the old worker resumes, nor does a failed operation prove that no
  storage change committed. The execution and recovery section develops this
  distinction further.

Finally, a storage commit, an operation's terminal state, and a refreshed UI are
three different things. The backend owns the first two. The application uses
the events and task result to refresh its file list or display a failure; the
knowledge does not reach into the interface to update it.

## 4. Retrieval pipelines

Ingestion creates searchable representations; retrieval decides which stored
sections to supply for a particular question. These are separate decisions.
The same prepared knowledge can support a narrow factual search, a broader
metadata-based search, or a combination of the two without being ingested again.

RAVEN implements four mechanisms rather than requiring every question to follow
one fixed search path. A pipeline selects evidence; it does not generate the
answer. Answer generation and the decision to call a retrieval tool belong to
the agent harness, discussed in the next section.

### 4.1 Retrieval contracts and local/global scope

Three strategies—embedded, hierarchical, and vector-conditioned—return a list
whose items have exactly these fields:

| Field | Meaning |
| --- | --- |
| `knowledge_name` | The knowledge that owns the source. |
| `file_name` | The stored source file name within that knowledge. |
| `section_id` | The identity of the selected section. |
| `raw_content` | The complete stored text of that section. |

This is the model-facing retrieval contract, not a dump of the storage record.
Internal similarity scores, scoring metadata, chunk IDs, and page ranges are
not added to it. Section identities let later reconstruction resolve the
surrounding file and its navigation information without duplicating those
details in every retrieval result. Agreement retrieval preserves an additional
comparison envelope, explained in Section 4.5.

Each mechanism has two scopes:

- **Local:** an explicit `knowledge_name` restricts the search to one knowledge.
- **Global:** the strategy searches or selects among knowledges belonging to
  the current user. It does not cross user roots. The mechanism determines how
  that wider scope is searched; global is not a single shared implementation
  wrapped around all four local strategies.

Four mechanisms with two scopes give eight concrete retrieval modes.
`Raven.retrieve()` runs one explicitly selected mode and returns an
`OperationTask`; its result is the selected evidence. It does not accept
`"auto"`, because automatic tool choice belongs to an agent turn. The harness
can call the same pipelines as tools, passing its existing operation so that
the retrieval tasks and events remain part of the turn's execution history.

Result limits and relevance are also distinct. `top_k` caps the number of
returned sections, not their combined token count or a minimum confidence
level. A request can return fewer sections when candidates are exhausted.
Neither vector ranking nor metadata scoring guarantees that the highest-ranked
available material actually answers the question.

### 4.2 Embedded retrieval

`EmbeddedRetrievalPipeline` searches the chunk vectors produced during
ingestion, then resolves those matches back to their complete sections. It
uses the embedding model, not an LLM scoring completion.

The local path is:

1. **Check compatibility and embed the query.** The configured embedding model
   must match the knowledge's vector space.
2. **Search chunk vectors.** Qdrant returns candidates ranked by cosine
   similarity. The current implementation requests up to `4 × top_k` chunk hits
   to allow for several hits belonging to the same section.
3. **Resolve and deduplicate.** Each valid hit points to a stored section.
   Repeated hits for that section are skipped, as are references that no longer
   resolve to a section.
4. **Return sections.** The first `top_k` distinct resolved sections become the
   result. Their text comes from the section store, not from stitching matching
   chunks together.

{% include retrieval_diagram.html id="embedded-retrieval" label="Embedded retrieval" local_alt="Local embedded retrieval searches chunk vectors across two Engineering files, then returns four ranked, distinct sections." global_alt="Global embedded retrieval pools ranked chunk hits from Engineering and Narrative, then returns distinct sections across the user's knowledges." %}

*Use the Local/Global control to inspect each scope separately. File names,
section ordinals, and scores are illustrative, not recorded retrievals. Two
matches in the same section produce one evidence item, and the final ranking
can interleave files or knowledges. A chunk
match does not mean every sentence in its section independently matched the
query. Similarity scores remain internal.*

The global path embeds the query once, queries each knowledge's index in turn,
and pools its chunk hits. It sorts that combined pool by similarity before
resolving and deduplicating sections. Deduplication uses the pair
`(knowledge_name, section_id)`, and `top_k` limits the **combined** result—not
`top_k` results from every knowledge. The per-knowledge candidate limit still
applies, so this is a ranking over the collected candidates, not an exhaustive
comparison of every stored chunk.

The design favours focused facts, definitions, numbers, and requirements: a
small passage can identify the relevant section while the full section retains
its conditions and explanation. However, **semantic similarity is not exact
matching**. Embedding quality, terminology, chunk boundaries, and candidate
limits influence what is found. The search does not independently verify a
number, apply a lexical exact-match constraint, or certify the generated answer.

### 4.3 Hierarchical retrieval

`HierarchicalRetrievalPipeline` addresses a different problem: relevant material
may be recognizable from its subject, circumstances, or narrative relationships
without containing a particularly strong vector match. It asks the LLM to rank
descriptions of candidates, then returns the selected sections' stored text.

The LLM acts as a **relevance scorer**, not a source-text generator:

- **Section candidates:** the scoring prompt receives section ID, file name,
  summary, keywords, conditions, and definitions. It does not receive each
  candidate's full `raw_content` for scoring.
- **Knowledge candidates:** when global selection is needed, the prompt
  receives knowledge names and their user-written summaries.
- **Structured response:** Pydantic score models require an identifier and an
  integer from 0 to 10 for every candidate. RAVEN rejects unknown identifiers,
  duplicate identifiers, and missing scores, and retries invalid scoring up to
  the configured limit.

The scope changes the selection path:

- **Local:** score section metadata across the named knowledge and return the
  highest-ranked sections. There is no mandatory intermediate file-scoring
  completion.
- **Global:** first score knowledge summaries and select up to
  `top_k_knowledge` knowledges; then rank sections across that selected scope.
  `top_k_section` limits the combined section result. The lower-level
  `full_retrieval=True` option skips knowledge-summary selection and considers
  sections from all available knowledges instead.
- **Targeted helpers:** `retrieve_by_file()` and `retrieve_by_knowledge()` score
  sections within explicitly chosen files or knowledges. These helpers support
  vector-conditioned retrieval without repeating a broader selection step.

{% include retrieval_diagram.html id="hierarchical-retrieval" label="Hierarchical retrieval" local_alt="Local hierarchical retrieval scores section metadata within Engineering and returns the highest-ranked stored sections." global_alt="Global hierarchical retrieval first scores knowledge summaries, then section metadata within selected knowledges; full_retrieval can bypass knowledge selection." %}

*The local view scores sections directly. The global view adds knowledge
selection, with a dashed bypass for `full_retrieval`. Example scores illustrate
relevance judgments, not probabilities.*

Scoring a large candidate set requires several completions. RAVEN batches
metadata by candidate count and a serialized-character budget, with bounded
metadata fields. Later batches include selected, previously scored candidates
as **anchors**: examples of what the same query previously scored on the 0–10
scale. This encourages comparable judgments across separate completions; it
does not make those judgments perfectly calibrated. The batching budget is
not a guarantee about the total prompt's token count, which also includes the
query, instructions, and anchors.

The final ranking sorts by descending score, retaining candidate order when
scores tie. Scores are relevance judgments, not probabilities or factual
confidence. There is no minimum-score filter in this selection: a low-scoring
candidate can still enter the result if it ranks within the requested count.

This mechanism can help with themes, chronology, relationships, and broader
conceptual context. Its limitations follow directly from the design:

- Poor or incomplete ingestion metadata can hide relevant content from the
  scorer, even though that content exists in the stored section.
- Global knowledge selection depends on the quality of user-written summaries.
- Repeated LLM completions add latency and inference cost as the candidate set
  grows. Batching controls individual reads; it does not make a broad scan cheap.
- Structured-output validation checks the response contract, not whether the
  model's relevance judgments are correct.

### 4.4 Vector-conditioned retrieval

`VectorConditionedRetrievalPipeline` combines the two selection mechanisms.
Vectors first identify a promising scope; hierarchical scoring then evaluates
sections within that scope. The purpose is to avoid scoring the metadata of
every possible section when vector search can narrow the work.

The two scopes deliberately narrow at different levels:

| Scope | Vector stage selects | Hierarchical stage considers |
| --- | --- | --- |
| Local | Files represented by the embedded section results within one knowledge. | Sections from those files. |
| Global | Knowledges represented by the global embedded section results. | Sections across those knowledges. |

The stages run in sequence and share the parent operation. File or knowledge
names from the embedded result are deduplicated, then passed to the appropriate
hierarchical helper. `embedded_top_k` controls the initial section shortlist;
the final `top_k` or `top_k_section` controls the returned evidence count.

Importantly, this is **not merely reranking the original vector-hit sections**.
If embedded retrieval finds Section 2 of a file, local vector-conditioned
retrieval can subsequently select Section 5 of that same file based on its
metadata. In the global path, the second stage can consider other files inside
a selected knowledge as well. That expansion is what allows vector matching
to guide a broader contextual search.

{% include retrieval_diagram.html id="vector-conditioned-retrieval" label="Vector-conditioned retrieval" local_alt="Local vector-conditioned retrieval uses embedded hits to select files, then scores sections in those files and may return a section outside the initial hits." global_alt="Global vector-conditioned retrieval uses embedded hits to select knowledges, then scores their sections and may return a different file within a selected knowledge." %}

*The final sections are illustrative possibilities, not recorded results. The
local route expands within selected files; the global route expands within
selected knowledges. Neither is limited to reranking the initial section hits.*

The tradeoff is an exclusion boundary. If the vector stage misses an important
file locally, or an important knowledge globally, metadata scoring cannot
recover material outside the selected scope. Conversely, selecting a very large
knowledge globally can still leave substantial scoring work. More initial
candidates widen the opportunity for discovery but can also increase LLM work.

The agent's tool descriptions recommend vector-conditioned retrieval as the
balanced default when no more specific strategy is clearly needed. That is
selection guidance, not a claim that it wins every benchmark or an enforced
replacement for the other tools. Actual cost and quality depend on document
organization, candidate limits, metadata, and model behaviour.

### 4.5 Agreement retrieval and strategy comparison

`AgreementBasedRetrievalPipeline` asks whether embedded and hierarchical
retrieval select the same material. Unlike vector-conditioned retrieval,
neither result narrows the other's candidate scope. Embedded retrieval finishes
first, then hierarchical retrieval runs, and their results are compared. The
current implementation does not execute the two paths in parallel.

The comparison preserves a specific contract rather than calculating an overlap
percentage:

| Classification | Current comparison rule |
| --- | --- |
| `Strong Agreement` | The two complete, ordered result lists are equal, including their result-item values. |
| `Disagreement` | If the lists differ, at least one pair at the same position has different file IDs, derived from its section IDs. |
| `Weak Agreement` | The lists differ, but every compared pair has the same file ID. |

Order and list length therefore matter:

- Reordering results can change the classification. Strong agreement is not
  simply equality of section-ID sets.
- File comparisons stop at the shorter list. Extra trailing results are not
  compared, so unequal-length lists can receive weak agreement.
- Two empty lists receive strong agreement because they are equal. One empty
  list and one non-empty list receive weak agreement because there are no
  paired file mismatches. Neither case demonstrates corroborating evidence.

These details are important when interpreting the labels. They describe the
implemented comparison, not a statistical measure of agreement or an assessment
of whether the sources are true.

The returned envelope has two fields:

- **`agreement_type`:** one of the three classifications above.
- **`retrieved_content`:** a single section list for strong agreement; otherwise
  an object containing separate `embedded_retrieval` and
  `hierarchical_retrieval` section lists. Each item still uses the four-field
  contract from Section 4.1.

Comparison happens before the final output limit is applied. Strong agreement
returns at most `top_k` items in its single list; other classifications retain
up to `top_k` items **per pathway**. The latter can therefore provide more than
`top_k` total items, including sections present in both lists. Preserving the
paths lets the consumer inspect differences instead of presenting a flattened
list as unqualified consensus. Reconstruction understands both shapes.

{% include retrieval_diagram.html id="agreement-retrieval" label="Agreement retrieval" local_alt="Local agreement retrieval runs embedded then hierarchical retrieval within Engineering and compares their ordered section results." global_alt="Global agreement retrieval runs global embedded then global hierarchical retrieval across the user's knowledge base, then compares their ordered section results." %}

*The example changes the selected section at two positions without changing
the paired file identities, so it receives weak agreement. The three lower
boxes show alternative classifications, not three simultaneous outputs.*

Agreement combines vector-search work with hierarchical scoring and its
possible knowledge-selection stage. It is intended for cases where comparing
two selection routes justifies that combined cost, rather than ordinary
questions that one route can address. It is not an independent reviewer, an
answer validator, or proof that two sources corroborate a claim: both paths can
select the same irrelevant material or depend on the same underlying source.

#### Choosing among the four mechanisms

| Mechanism | Selection work | Useful starting point | Principal limitation |
| --- | --- | --- | --- |
| Embedded | Query embedding and chunk-vector search; no retrieval-scoring LLM call. | Focused facts, definitions, numbers, and requirements. | Similarity and candidate limits can miss context or exact distinctions. |
| Hierarchical | LLM scoring of section metadata; knowledge-summary scoring where global selection is enabled. | Narrative, thematic, and conceptual relationships. | Metadata quality, judgment variability, and broad scoring cost. |
| Vector-conditioned | Embedded selection of files or knowledges, followed by targeted LLM section scoring. | A balance of focused matching and contextual selection. | The first stage can exclude useful scope; the remaining scope can still be large. |
| Agreement | Sequential embedded and hierarchical retrieval, then ordered-result comparison. | Expensive cross-checking of evidence selection when justified. | Combined cost; agreement is neither calibrated confidence nor factual verification. |

These are qualitative architectural tradeoffs, not fixed latency rankings.
The next section explains how the harness exposes the applicable tools and
guides the model's choice without turning the conversation into a compulsory
query–retrieval–answer sequence.
