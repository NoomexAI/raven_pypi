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
The next section explains the complementary navigation, memory, and preference
tools. Section 6 then connects those capabilities through the harness without
turning the conversation into a compulsory query–retrieval–answer sequence.

## 5. Navigation, memory, and preference tools

The retrieval pipelines in Section 4 are not the only way the model can obtain
information. RAVEN also exposes tools for discovering and reading known source
sections, recalling a conversation, and saving explicit behavioural preferences.
These tools solve different problems; treating every information request as a
relevance search would obscure those differences.

A navigation tool answers “what is stored here?” or “read these sections.”
A memory tool answers “what was discussed in this conversation?” A preference
tool changes how future runs should behave. None requires a call to one of the
four knowledge-retrieval pipelines, although navigation can also help the model
supply a missing argument to one.

### 5.1 Knowledge navigation and direct section access

Navigation addresses the stored structure rather than ranking passages against
an open-ended query. The tool wrappers delegate reads to the same knowledge
resources used by retrieval; they do not maintain a second document store.

| Tool | What it returns | Intended use |
| --- | --- | --- |
| `list_knowledges` | Available knowledge metadata, including summaries. | Discover a knowledge name or inspect the available scope. |
| `list_files` | Stored file records in a knowledge. | Discover file names before browsing their sections. |
| `list_sections` | A page of section IDs and metadata, with optional raw content. | Understand a file's sections and select material for explicit inspection. |
| `get_sections` | Metadata and raw content for selected section IDs. | Read particular sections whose identities are already known. |

The conversation determines the accessible scope:

- **Local conversation:** the wrappers supply its bound knowledge. File and
  section tools do not offer a model-controlled replacement knowledge argument,
  and `list_knowledges` exposes only that knowledge.
- **Global conversation:** `list_knowledges` exposes the current user's
  knowledges. File and section tools require an explicit `knowledge_name`.
  Global does not mean access to another user's data.
- **Restricted retrieval mode:** navigation remains available. Selecting a
  particular retrieval tool does not remove the ability to inspect sections.

Section navigation separates inexpensive selection metadata from potentially
large source text:

- `list_sections` returns IDs, summaries, keywords, conditions, definitions,
  and available source ranges without `raw_content` by default. Its default
  page size is 20, with a maximum of 50. The exclusive `after_section_index`
  cursor advances through the file; the UI summary reports whether more
  sections remain and supplies the next cursor.
- With `get_content` set to true, `list_sections` also returns raw text. Its
  default page size drops to 3, with a maximum of 5, for explicit sequential
  inspection.
- `get_sections` accepts 1–5 section IDs from one knowledge and returns their
  metadata and raw text in requested order. A missing ID fails the call rather
  than presenting a partial result as complete.

These are **agent-tool limits**, not changes to the underlying knowledge API.
They bound section counts, not each section's token length. Listing knowledges
or files also does not imply that every navigation tool has a paged, fixed-size
response.

#### Navigation can bypass the retrieval pipelines

A model can obtain document content entirely through navigation. For example,
an illustrative inspection sequence can:

1. Discover Engineering with `list_knowledges`.
2. Find `reaction_wheel.txt` through `list_files`.
3. Read its section summaries through `list_sections` and choose relevant IDs.
4. Fetch those IDs through `get_sections`, then answer from their contents.

No embedded, hierarchical, vector-conditioned, or agreement retrieval call is
required by that sequence. Content-bearing navigation results produce the same
kind of knowledge/file/section evidence references as retrieval, so the harness
can reconstruct their sources too. Metadata-only listings do not establish
that section content was supplied.

This capability is deliberate: “show these sections” should not require a
similarity search to rediscover their known identities. It also means the model
can take the navigation route for a general question, even when a retrieval
strategy has been selected.

**That is possible, but it is not the recommended v0.2.0 answering behaviour.**
The system prompt directs the model to use retrieval for open-ended document
questions, use navigation only to discover missing retrieval arguments, and
reserve direct content inspection for explicit browsing requests. Once the
model has source-content evidence, there is no code-enforced rule that requires
a retrieval call before answering.

The distinction is between capability and guidance, not two descriptions of
the same restriction. Navigation-driven exploration can be useful, but repeated
metadata pages and complete section reads can consume substantial context.
Retrieval supplies a relevance-selection mechanism intended to reduce that
work. Its result limit is not a token guarantee either; neither path makes
unbounded source text free to read.

### 5.2 Conversation memory and preferences

Knowledge navigation concerns documents. Conversation tools concern the
persistent conversation supplied to the run, regardless of whether that
conversation has local or global knowledge scope.

#### Recalling earlier discussion

`search_memory(query)` delegates semantic recall to the conversation's vector
memory. The model receives matching message records with roles and text; the
UI summary reports the message count. It does not search the knowledge base,
another conversation, or all conversations owned by the user.

The active model context is already prepared before agent execution.
`search_memory` is an additional tool the model can choose when a question
concerns an earlier decision or discussion that is not sufficiently represented
in that context. It does not itself compact history, replace the active
context, or replay the entire transcript.

Recall and document grounding remain separate. An earlier assistant message
may contain a claim about a file, but finding that message is not the same as
reading the file now. Memory results therefore do not create knowledge-section
evidence or trigger source reconstruction by themselves. The conversation
storage and compaction mechanisms are covered in Section 7.

#### Saving enduring behavioural preferences

Preferences are explicit, conversation-local instructions such as a request to
keep future responses concise. They are not another vector search result or a
global profile shared by every conversation.

| Tool | Responsibility |
| --- | --- |
| `list_preferences` | Return the saved `preference_id` and text records. |
| `save_preference(text)` | Ask the conversation to persist an explicit preference and return its record. |
| `remove_preference(preference_id)` | Remove the identified preference rather than matching its wording approximately. |

The prompt asks the model to save only explicitly requested enduring
behaviour, not turn every ordinary instruction into a lasting preference.
Removal can first list the records so that the model uses the correct ID
instead of trying to reproduce the original text.

The conversation owns persistence. Successful preference-tool mutations
invalidate that conversation's cached harness prompt, and the next run builds
its prompt from the updated preferences. They do not rewrite the system prompt
inside an already running agent loop. Preference instructions also do not
override system rules or tool-scope restrictions.

### 5.3 Tool results and corrective feedback

Each wrapper returns a serialized `ToolResult`. Its fields separate the
information needed to reason from the information needed to display activity:

- **`result`:** model-facing data, including complete retrieved or inspected
  section text when appropriate.
- **`ui_summary`:** a compact presentation-oriented summary, such as selected
  source references, navigation cursors, or the number of recalled messages.
- **`evidence`:** knowledge/file/section references obtained by successful
  retrieval or content inspection. Structural listings and conversation recall
  do not count as document-content evidence.
- **`ok`, `error`, and `next_action`:** outcome and corrective guidance. Known
  argument or resource mistakes can return `ok=False` so the model can adjust
  the call instead of losing the whole run.

For example, a missing section ID can produce an instruction to check IDs from
`list_sections` and retry. Preference removal uses a stable `preference_id`,
which can first be discovered with `list_preferences`, rather than relying on
the model to reproduce a preference's wording exactly.

Recovery is selective. The wrapper handles an explicit set of domain errors
and argument-like exceptions; cancellation is propagated, and infrastructure
or unhandled persistence failures are not relabelled as ordinary mistakes.
Nor does a corrective result guarantee that the model will follow the advice.

The full result goes back to the agent. The translated `chat.tool_result`
event carries the outcome, UI summary, evidence, and applicable error
information—not the full model-facing `result`. Corrective `next_action` text
is part of the tool response seen by the model; the current event translator
does not forward it as a separate event field. This avoids treating the tool's
model response and the UI activity event as interchangeable objects.

## 6. The agent harness

Retrieval pipelines know how to find sections. They do not know whether a
message needs retrieval, whether an earlier conversation decision should be
recalled, or whether a missing knowledge name needs to be discovered first.
Those choices belong to the model. The **agent harness** supplies the tools,
context, execution limits, and observable output around that decision-making.

RAVEN uses LlamaIndex's `FunctionAgent` as the agent engine rather than building
another tool-calling loop. `AgentHarness` supplies RAVEN's policy and connects
that engine to its knowledge, memory, operations, and reconstruction components.
A `Session` coordinates the complete conversation turn around the harness; it
does not become the implementation of the agent's behaviour.

### 6.1 Tool availability and run policy

`AgentPolicy` describes the conversation scope, optional retrieval restriction,
iteration budget, and retrieval result limit. It does **not** classify the
meaning of the user's query. `ToolBuilder` uses the policy to construct the
actual `FunctionTool` objects supplied to the model for that run.

| Conversation and selection | Retrieval tools supplied |
| --- | --- |
| Local conversation, `"auto"` or no selection | The four local retrieval tools, bound to the conversation's knowledge. |
| Global conversation, `"auto"` or no selection | All four local and all four global retrieval tools. |
| A specific retrieval mode | Only that retrieval tool, provided it is valid for the conversation scope. |

There is no separate `auto` tool. Automatic mode makes several concrete tools
available; a restricted mode omits the others from the agent configuration.
Requesting a global retrieval mode for a local conversation fails before model
execution.

Knowledge binding also changes the model-facing arguments:

- **Local conversation:** a local retrieval tool accepts the query; its wrapper
  supplies the bound knowledge. The model cannot replace that binding by
  inventing a different `knowledge_name` argument.
- **Global conversation:** a local retrieval tool requires an explicit
  `knowledge_name` alongside the query. This allows a globally scoped
  conversation to focus one call on a knowledge named in the discussion.
- **Missing scope information:** the prompt asks the model to request the name
  or discover a clear match with `list_knowledges`. A tool wrapper also rejects
  a missing or invalid local knowledge name when it reaches that validation
  path. Discovery does not silently turn a local tool into a global search.

Selecting a retrieval mode restricts **which retrieval tool can run**, not
**whether the model must call it**. Memory, preferences, and scoped navigation
remain available, and a casual message can still receive a direct reply.

The navigation, memory, preference, and tool-result contracts in Section 5
remain available within that policy. The harness binds them to the run; it
does not turn them into alternative conversation or knowledge stores.

### 6.2 The agent harness and autonomous execution

#### Preparing a run without rebuilding the conversation

`AgentHarness.start()` validates the query and policy, creates a transcript for
the current interaction, and starts a managed agent task. A caller can supply
an existing operation; a session does so to keep agent work and the eventual
conversation commit within one operation.

Preparation has three distinct responsibilities:

1. **Construct the permitted tools.** Fresh tool wrappers bind this run's
   conversation, operation, and retrieval policy. Available function schemas
   and descriptions are supplied through LlamaIndex, not maintained as a second
   exhaustive tool list inside the prompt.
2. **Prepare the system prompt.** `PromptBuilder` combines stable behavioural
   instructions with the conversation scope, retrieval selection, and explicit
   preferences. A bounded, least-recently-used cache keys the rendered prompt
   by conversation, scope, selection, and preference IDs/text. Unchanged prompts
   can be reused; a preference-tool mutation invalidates that conversation's
   cached entries. Changes apply to the next run, not a rewritten prompt midway
   through an active workflow.
3. **Obtain model context.** The harness asks the conversation for context,
   reserving the system prompt's token count in the memory budget. Context
   preparation is itself a child operation task and can compact history before
   returning. The conversation owns that memory machinery; the harness consumes
   its result and supplies the system prompt separately to the agent.

{% include diagram.html id="agent-harness" label="Agent execution" width="960" height="920" alt="A session delegates a run to the harness. The harness prepares policy, tools, prompt, and conversation context. FunctionAgent chooses tool calls or an answer, tools return model-facing results, and the loop can continue. The harness tracks evidence, reconstructs sources when available, and emits chat.completed; the session separately commits the turn. Workflow output is translated into operation events throughout." %}

*The loop represents model decisions, not a mandatory tool sequence. Tool
results return to the agent; UI events are a separate projection of the same
execution. Conversation persistence remains outside the agent task.*

#### The model chooses the sequence; the harness limits the execution

Each run creates a lightweight `FunctionAgent` configuration with streaming
enabled and `allow_parallel_tool_calls=False`. A tool result returns to the
agent, which can answer, choose another tool, or correct an earlier call.
Sequential tool execution does not mean a fixed retrieval pipeline, and the
iteration budget is not simply a count of individual tool invocations.

For an illustrative global conversation restricted to a local retrieval mode,
the model might discover Engineering through `list_knowledges`, call the selected
local retrieval tool with that name, and answer from the returned sections.
Another request might need only conversation recall or an explicit section
inspection. These are possible model-selected chains, not recorded traces or
branches chosen by a separate RAVEN intent classifier.

The numbered system instructions guide the model to distinguish those cases,
use retrieval for document questions, keep navigation secondary, correct
recoverable mistakes, and stop once enough evidence is available. **Prompt
guidance is not a deterministic enforcement mechanism.** The model's ability
to follow it still depends on its function-calling and instruction-following
quality. Omitting an unpermitted tool is a hard boundary; telling a model when
to call an available one is guidance.

#### One execution, several observable outputs

`WorkflowEventTranslator` converts LlamaIndex workflow events into RAVEN's
event contract:

| Event | What the consumer receives |
| --- | --- |
| `chat.thinking_delta` | A separate thinking-text delta when the model/adapter supplies one. It is not fabricated from response text. |
| `chat.response_delta` | Incremental assistant text, distinct from thinking and tool activity. |
| `chat.tool_call` | Tool name, arguments, `call_id`, and increasing tool-call `step`. |
| `chat.tool_result` | The corresponding `call_id` and `step`, outcome, UI summary, evidence, and applicable error information. |
| `chat.max_iterations` | The iteration budget was reached and early final-response generation is requested. |
| `reconstruction.file` | A reconstructed source file emitted by the reconstruction child task. |
| `chat.completed` / `chat.failed` | The agent's final response and evidence summary, or its normalized failure. |

Tool steps correlate calls and results; they are not section ordinals or the
agent-iteration count. Pipeline progress and task lifecycle events can also
appear in the same operation stream, so an interface need not infer ongoing
work from response tokens alone.

`AgentRun` provides replayable `stream`/`events()` access, `collect()`, status,
waiting, and cancellation over that work. `collect()` first requires the task
to succeed, then builds `AgentRunResult` from its events: response, optional
thinking, correlated tool activity, evidence, reconstruction, and iteration-limit
state. It does not run a second inference or create an independent non-streaming
answer path.

The model-compatible transcript has a different purpose. It records the user
message, assistant tool calls, tool-result messages, and final assistant text
for the session to persist. Thinking deltas are not inserted into that
transcript as ordinary assistant messages. Streaming activity, a collected
result, and model conversation history are related representations—not one
object used for all three jobs.

#### Evidence and the limits of grounding enforcement

The harness deduplicates evidence by knowledge, file, and section identity.
Successful retrieval, `get_sections`, and content-bearing `list_sections` can
provide those references. A metadata-only section list or a memory search
cannot establish that document content was obtained.

There is a deliberately narrow deterministic guard: once the agent has called
a knowledge-content tool, response deltas are held while no evidence has been
obtained. If the workflow ends without evidence, the harness substitutes an
insufficient-evidence response. With evidence, it releases held deltas and
reconstructs the referenced files before publishing `chat.completed`.

This is not a query classifier or a claim-level verifier. If a model answers
without attempting a knowledge tool, the guard does not independently classify
the question and force retrieval. Text emitted before that tool attempt also
cannot be recalled from a consumer that already received it. Even valid source
references show what material was supplied, not whether every statement follows
from it. Source inspection remains useful precisely because those are different
questions.

#### Limits, cancellation, and completion boundaries

The harness passes the iteration budget to the engine and configures its
early-stopping method as `"generate"`. The translator tracks agent-output
iterations and emits `chat.max_iterations` when the limit is reached. The
intended outcome is an early final answer rather than indefinitely continuing
the tool loop. A successful collected result records
`iteration_limit_reached=True`; this limit is not a wall-clock timeout.

If finalization fails after the limit has been observed, the harness reports
`agent_max_iterations` through the failure path. Other model, tool, or
reconstruction failures can likewise fail the agent task. `collect()` raises on
failure or cancellation instead of returning partial text as a successful
`AgentRunResult`. Already published events remain available to explain the
interrupted work.

Cancellation checks occur throughout workflow consumption, and the harness
requests cancellation of the active engine handler. This stops managed work;
it does not shut down an external Ollama service or promise that a remote
provider can instantly undo an in-flight request.

Finally, **agent completion is not conversation commit**. The harness supplies
the completed result and transcript. The session then asks the conversation to
append the turn, including tool history, and waits for that persistence work.
A `chat.completed` event therefore precedes the complete session task's success.
The next section follows that outer lifecycle and explains how canonical
history, compacted context, vector memory, and preferences fit together.

## 7. Conversations, sessions, and memory

A conversation must outlive the model invocation that produced its latest
answer. It must also serve two different readers: an interface that needs the
actual interaction history, and a model that cannot receive an indefinitely
growing transcript. RAVEN separates persistent conversation state, derived
memory views, and the temporary session used to conduct a turn.

The distinction prevents a context-window decision from becoming a data-loss
decision. Summarizing older material for the model must not replace the
messages that a reader can reopen and inspect.

### 7.1 Conversation persistence and canonical history

#### The resource and its manager

`ConversationManager` discovers, creates, finds, updates, and deletes
conversation resources within the current user's root. It owns their registry
and manages idle storage connections. `Conversation` owns one resource's
metadata, message store, preferences, and model-memory adapters. A session
refers to that managed object rather than opening another database for the
same conversation.

The current manager creates a UUID-derived, 12-character conversation ID and
uses it as the directory name. This identifies the conversation independently
of its title or any operation. The knowledge binding determines its type:

- **Local:** a supplied `knowledge_name` binds the conversation to an existing
  knowledge. Its tools inherit that scope.
- **Global:** no knowledge is bound. Tools can explore the current user's
  knowledge base, subject to the per-run restrictions already described.
- **Mutable presentation:** title and pinned state can change without changing
  the conversation's identity. The metadata-update API does not offer a change
  of knowledge binding.

Storage follows the same ownership distinction as the knowledge resources:

| Storage | What it holds | Architectural role |
| --- | --- | --- |
| `metadata.json` | Conversation identity, knowledge binding, title, `is_titled`, pinned state, creation time, and schema version. | Identifies and describes the conversation. |
| `messages.sqlite3` | Committed turns and results, canonical messages, a separate model-context snapshot, and storage settings. | Keeps authoritative history apart from its compacted working view. |
| `qdrant/` | Derived conversation-memory vectors with the indexed message payloads. | Supports semantic recall without scanning the complete transcript. |
| `preferences.json` | Explicit preference IDs and text. | Keeps enduring behavioural instructions separate from ordinary message history. |

Discovery can report malformed resources without preventing valid conversations
from loading. Idle storage can be released and reopened while the managed
conversation identity remains available; an active turn protects its resource
from idle eviction. As with knowledges, embedded Qdrant ownership is not a
multi-process storage-sharing mechanism.

#### Canonical messages, turns, and result snapshots

**Canonical history** is the ordered, uncompacted record of committed
interactions. A tool-using turn contains the user message, assistant tool-call
messages, corresponding tool-result messages, and final assistant response.
Tool calls retain their structured arguments and correlation IDs rather than
being reduced to display labels such as “searching documents.” Later model
context can therefore retain the actual exchanges when those messages still
fit its working window.

A turn has a full UUID `turn_id`. It groups those messages and the saved
`AgentRunResult`, and is distinct from the shorter conversation ID and the
operation/task IDs that identify execution. The result snapshot can contain
thinking, tool summaries, evidence, and reconstructed sources in addition to
the final response.

**Thinking is excluded from the canonical chat-message sequence, not necessarily
from disk.** It can remain in persisted operation events and the turn's saved
result. Excluding it from ordinary model history does not mean that RAVEN
discards every recorded representation of it.

`append_turn()` commits the turn record, canonical messages, and their addition
to the current context snapshot in one SQLite transaction. Vector-memory
indexing happens afterward and is tracked separately. Repeating a commit with
the same turn ID and query reuses the existing record instead of appending
duplicate messages or replacing its original result. Reusing the ID for a
different query fails as a conflict.

For an interface, full history and paged history come from these canonical
records—not from the compacted model context. Message pages expose a durable
numeric `message_id` cursor, the owning `turn_id`, position within the turn,
and serialized `ChatMessage`. Turn-specific reads use the same stored message
identities. This supports reopening an older conversation without asking the
model to reconstruct what it previously said.

#### Titles are metadata, not another conversation turn

`Conversation.generate_title()` makes a separate completion from the supplied
user message, then updates the title if the conversation is still untitled.
Its short system instruction and completion do not enter chat memory. Once
`is_titled` is true, subsequent calls return the current title without another
generation. A manual title update also marks it titled, so a pending automatic
attempt cannot overwrite that choice.

The session attempts this after committing a completed turn. Title generation
is best-effort: a failure does not undo the turn or normally fail its parent
session task. If the conversation remains untitled, a later completed turn can
trigger another attempt using that turn's query. Automatic first-turn title
generation is therefore an intended path, not a guarantee that the first
attempt always succeeds or is the only attempt ever made.

### 7.2 Context memory, vector memory, and preferences

The conversation holds several representations because they answer different
questions. None should be mistaken for the complete function of “memory.”

{% include diagram.html id="conversation-memory" label="Conversation memory views" width="960" height="700" alt="A committed turn remains in canonical SQLite history for the interface. A separate context snapshot is compacted into summary plus recent messages for the next model run. User and final assistant text form a per-turn vector-memory node for optional semantic recall. Preferences separately enter the harness system prompt; compaction does not overwrite canonical history." %}

*The branches are different uses of the same conversation, not interchangeable
databases. The compacted summary is not the vector-memory input, and the UI
does not load its history from that summary.*

#### Active context and compaction

RAVEN uses LlamaIndex's `ChatSummaryMemoryBuffer` over a `SimpleChatStore` loaded
from SQLite's separate `context_messages` table. The harness requests this
context before running the agent and supplies the rendered system prompt's
token count as an initial reservation.

When older history exceeds the working budget, the memory buffer retains a
recent-message window and summarizes older messages with the configured LLM.
RAVEN's summary instruction asks it to preserve facts, identifiers, numbers,
decisions, corrections, constraints, and unresolved requests while separating
user statements from assistant claims. That is guidance to a lossy model
completion, not a promise of exact recall.

The context-loading task then replaces only the stored context snapshot with
the resulting summary and recent messages. Canonical messages and already
indexed conversation text remain intact. Subsequent turns append new messages
to that working context, which can be compacted again when requested later.

This work is observable through
`conversation.memory_compaction.started`, `.completed`, and `.failed` events.
They belong to the context child task within the turn's operation. The UI can
explain a pre-answer delay without interpreting silence as a stalled response.
The reported message counts describe the compaction stage, not a precise
percentage of model work.

The summary and the harness policy are separate inputs:

- **Harness system prompt:** current RAVEN instructions, scope, retrieval
  selection, and explicit preferences. It is supplied outside the stored chat
  context, so context compaction does not summarize away the active policy.
- **Compacted summary:** the installed LlamaIndex memory implementation emits
  this as a system-role message at the start of returned history. Its purpose
  is historical context, not a replacement policy or a new preference record.
- **Provider boundary:** the model adapter receives the system prompt and
  history through LlamaIndex. Role handling and the model's treatment of these
  inputs remain provider/model compatibility considerations; two system-role
  messages should not be confused with two separately owned policy systems.

The configured memory limit is a context-management budget, not an unlimited
conversation guarantee. If the harness prompt alone exhausts it, context
preparation fails with `context_token_limit_too_small`. Token estimates,
summary length, the new query, tool schemas/results, and generation capacity
still matter to the model's actual context window. Preparing a compacted history
before a run does not promise automatic RAVEN compaction after every tool call
inside that run.

#### Vector memory recalls original conversation text

`VectorMemory`, backed by the conversation's local Qdrant store, supports the
`search_memory` tool from Section 5. It indexes committed user text and final
assistant text, not the compacted summary or internal tool traces.

The current index unit is **one node per committed turn**, identified by its
turn UUID. The node embeds the joined indexable text and retains the original
message payloads for reconstruction into `ChatMessage` objects on recall.
Consequently, `memory_top_k` limits matched memory nodes, not a strict number
of returned messages: one matching turn can yield both a user message and an
assistant response.

An index-completion marker in SQLite records whether a committed turn's derived
memory work finished. Memory initialization checks pending turns, and explicit
reconciliation can repeat indexing with the stable node identity rather than
appending another canonical turn. This separates “the conversation was saved”
from “its semantic index is ready.” Embedding identity and dimensions still
have to match the persisted vector space.

Semantic recall can miss a detail, return irrelevant discussion, or recover an
earlier assistant mistake. It supports continuity; it is not authoritative
evidence for a claim about knowledge documents.

#### Preferences survive independently of context windows

Preferences are persisted as explicit UUID/text records rather than inferred
from a summary or searched as vector memories. Exact duplicate preference text
reuses its saved record. Removing a preference uses its ID and does not erase
the request or confirmation already present in canonical history.

Preference tools invalidate the affected harness prompt cache after a successful
mutation. Prompt construction also keys cached content by the current preference
records, so an unchanged conversation ID does not imply an unchanged prompt.
The next run incorporates the current preferences; earlier turns and an active
agent's already supplied system prompt are not rewritten.

### 7.3 Sessions and the complete turn lifecycle

#### A temporary interaction medium over a persistent resource

`Session` connects one managed conversation to an agent harness and the model
pair used for memory. It is not stored in the conversation directory and is not
restored after a restart. Continuing a conversation means obtaining that
conversation and constructing another session around it.

In the high-level API, `Raven.session(conversation, ...)` checks that the actual
conversation object belongs to its manager, captures the effective per-session
settings, and supplies the harness and model dependencies. `Session.start()`
opens conversation storage; it does **not** initialize model memory by itself.
Memory initialization occurs when `generate_response()` submits a turn and can
also reconcile previously committed, unindexed turns.

#### The new-turn path

{% include diagram.html id="session-turn" label="Session turn lifecycle" width="960" height="940" alt="A temporary session opens storage, then submission assigns a turn UUID, claims the conversation and initializes memory. A managed session task runs the agent and receives chat.completed, commits canonical turn records in SQLite, reconciles context and vector memory, attempts a title and releases the claim. Only then is the session task successful. Competing turns are rejected, and a committed retry can reuse its result instead of following the new-turn path." %}

*This is the ordinary new-turn success path, not automatic worker resumption.
The storage commit occurs before vector indexing and before the session task
can report success. A committed-result retry follows the reuse path described
below instead of repeating agent generation.*

The sequence is intentionally more precise than “generate and save an answer”:

1. **Validate and reserve.** Submission validates the query and mode, assigns a
   turn UUID, and claims the conversation. Memory is initialized with the
   selected models and memory settings before the managed session task is
   submitted. A submission failure releases the claim even when no run handle
   was returned.
2. **Run the agent inside the session task.** The harness prepares context,
   executes the model/tool loop, reconstructs available evidence, and publishes
   `chat.completed`. The session collects the agent result and its model-compatible
   transcript. The outer session task is still running.
3. **Commit the turn.** The conversation atomically writes canonical messages,
   the turn/result record, and the context addition to SQLite. It then reloads
   in-memory context and updates the derived vector index. The
   `conversation.turn_commit.completed` event follows successful reconciliation;
   it is not the exact instant of the earlier SQLite commit.
4. **Attempt metadata work.** If still untitled, the conversation is asked to
   generate a title. Ordinary title errors are tolerated independently of the
   saved turn; cancellation is not converted into a successful title attempt.
5. **Release and finish.** Cleanup releases the conversation claim. The session
   task returns its result, and its run handle reports the terminal state.

`SessionRun.stream` and `events()` expose that task and its descendants, not
just assistant tokens. `SessionRun.collect()` waits for the complete outer task
and returns the agent result it produced. The harness originally assembles
that result from events; collecting the session handle does not perform another
inference or replay a second persistence action.

Three milestones must therefore remain distinct: text has arrived, the agent
has completed, and the session has successfully saved and finalized its turn.
An interface can render text immediately, but should use the session task's
outcome to report whether the complete interaction succeeded.

#### Turn ownership, cancellation, and reuse

Several temporary sessions can reference one managed conversation—for example,
two browser tabs—but only one turn can hold its conversation-wide claim. A
competing submission receives `conversation_turn_active`; it is rejected rather
than queued against an ambiguous future context. Turns on different
conversations can proceed concurrently, subject to broader runtime limits.

Closing one session cancels only its own active turns and waits for its cleanup.
It does not delete the conversation, close the manager's shared resource, or
cancel another session's turn. The conversation manager remains responsible
for the persistent resource's eventual closure.

Stopping a run is also not the same as rolling back everything it has done.
Cancellation before the turn commit can leave streamed text absent from
canonical history. Cancellation or a failure after SQLite commit can leave a
saved turn even if later memory indexing or finalization did not succeed.
Durable transitions are allowed to settle before cancellation propagates;
an already committed turn is not deleted merely to make the final status look
like a clean rollback.

A retry uses the recorded turn identity. If that turn is already committed,
the session verifies its query, loads its saved result, reconciles context and
pending memory indexing, and emits `chat.result_reused` under the new execution.
It does not ask the agent to generate and append the same committed turn again.
If no committed record exists, eligible retry can execute the generation path
again. Reopening durable state is therefore more reliable than assuming a
partial UI response buffer describes the final conversation state.

These boundaries explain why a turn UUID, a conversation ID, and an operation
ID are all needed. The next section examines the execution records, event
streams, durability policy, and retry machinery that coordinate this work.

## 8. Operations, events, and recovery

Ingestion, a model download, and a conversation turn perform different work,
but an application needs to ask the same questions about each: has it started,
what is happening, can it be stopped, and what remains after a failure? RAVEN
gives these actions a common execution boundary rather than leaving each
component to invent its own background-task and progress mechanism.

That boundary is an **operation**. Its tasks perform the work; its events
describe the work; its persisted records allow the outcome to be inspected
without retaining the original Python objects. None of these is a substitute
for the knowledge or conversation records that the work may change.

### 8.1 Operations and operation tasks

#### One operation, several identifiable invocations

`OperationManager` creates, locates, recovers, and closes operations for one
user. Each `Operation` owns its lifecycle, task handles, and `EventStream`.
The manager also owns their shared storage and synchronization service;
there is no second, independent event-stream registry.

An operation-aware component method accepts an optional `operation`. Without
one, it creates an operation through its injected manager. With one, it submits
its task to that existing operation. The method returns an `OperationTask`
handle; `await task.result()` waits for its outcome and either returns the
domain value or raises the failure. Some submission-time validation and
preparation can occur before the handle is returned.

This makes standalone and composed calls compatible. A standalone retrieval
can have its own operation. Retrieval invoked during a session turn belongs
to the turn's operation instead of producing an unrelated root stream.
Not every API method needs this mechanism: direct metadata reads and resource
lookups can return ordinary values.

{% include diagram.html id="operation-execution" label="Operation ownership and a composed session turn" width="960" height="740" alt="An OperationManager owns an operation and its shared store. The operation owns one stream and a root session task. That task invokes the agent, conversation commit and optional title generation. The agent invokes context preparation, retrieval and reconstruction as descendants. All events retain one operation ID while each task has a distinct UUID." %}

*The tree shows ownership, not simultaneous execution. Actual calls retain
their dependency order; an agent may make several retrieval calls or none.*

The first `operation.run(name, worker)` establishes its root task; that name
must match the operation name. Calls made from a running task under the same
operation become children, with the current task recorded as their parent.
Children of the same parent are siblings. The parent relationship on live
handles supports descendant filtering during execution. Persisted task records
retain operation ownership and root status, but do not recreate that complete
live handle tree after restart.

The identifiers are deliberately separate:

| Identifier | What it identifies | What it does not imply |
| --- | --- | --- |
| `operation_id` | A UUID for one complete execution and its event stream. | A conversation, ingested file, or reusable business identity. |
| `task_id` | A separate UUID for one invocation inside the operation. | An operation-ID prefix or a result that can always be restored after restart. |
| `parent_task_id` | The invoking task, when there is one; the root has no parent. | A new operation or a separate process. |
| `event_id` | A one-based sequence number within one operation. | A globally ordered identifier across users or operations. |

#### A task finishing is not the whole operation finishing

Both operations and tasks use `queued`, `running`, `completed`, `failed`, and
`cancelled`. A child can finish while its parent continues. Even the root
worker returning is not sufficient for operation success: the operation joins
its remaining children before its own terminal transition.

Unobserved child failures or cancellations prevent successful operation
completion. A parent can instead await a child's `result()`, catch its error,
and deliberately recover or continue. That observed failure remains in the
task history without necessarily making the operation fail. Merely scheduling
a child and ignoring it must not silently hide its failure.

`await operation.wait()` waits for the root execution and returns the
operation for status inspection; it is not the task's native result or a
success assertion. Root `task.result()` waits for the operation's outcome and
surfaces its failure. Cancelling a root task targets the whole operation and
its active children. Cancelling a non-root handle targets that invocation,
not unrelated siblings. Cancellation remains cooperative: protected storage
work may have to finish before the cancellation can be reported.

Live handles and durable records have different purposes. `OperationTask`
holds a worker, a native result, and control over the running invocation.
`OperationTaskRecord` describes persisted status, errors, attempts, and retry
input. Generic Python return objects are **not** serialized into every task
record or completion event. After restart, an application can inspect status
and replay retained events, but must not expect the old worker or arbitrary
native result to reappear. Domain-specific persistence, such as the saved
conversation-turn result, supplies reuse where explicitly implemented.

### 8.2 Events as an observable execution contract

#### A retained stream, not a broadcast broker

An `Event` is a validated Pydantic envelope with an `EventType`, event-specific
`data`, a UTC timestamp, operation/task correlation, a sequence ID, and an
`is_final` flag. Component code normally supplies the type and payload.
Publication attaches the operation and active task; `EventStream` assigns the
next sequence number and persists the event before notifying readers.

Each operation has its own ordered stream backed by SQLite. It is not an
application-wide publish/subscribe broker: components do not register arbitrary
callbacks to receive every event. Readers request retained events after a
cursor, consume them in bounded pages, then wait on an asynchronous condition
for additional events. Waiting suspends that reader rather than busy-polling
or blocking the main event loop.

For example, suppose a client has processed event 8 of one operation:

| Stage | Stored sequence | Reader behaviour |
| --- | --- | --- |
| Reconnect with `after_event_id=8` | Events 9–12 already exist. | Replay 9–12 and advance the reader's local cursor to 12. |
| Catch up | No event after 12 exists yet. | Wait; the producer continues independently. |
| New progress arrives | Publication commits event 13. | Wake, read 13, and advance the cursor again. |
| Execution finishes | A final operation event is appended. | Yield the remaining events and end the reader. |

The local cursor prevents repeated reads during one iterator's lifetime.
The application retains its own last processed ID for reconnection. Replaying
an earlier cursor intentionally returns previously delivered events, so a UI
should deduplicate by operation ID and event ID when needed. This is not an
exactly-once delivery or acknowledgement protocol. A cursor ahead of available
recovered history is reported as `event_history_gap`, not silently accepted.

#### Different events describe different milestones

- `operation.*` and `operation.task.*` describe execution lifecycle, with task
  IDs distinguishing repeated invocations of the same component.
- `chat.thinking_delta` and `chat.response_delta` keep optional model thinking
  separate from answer text. Tool call/result events retain call IDs and steps.
- Ingestion progress, context compaction, turn commit, and reconstruction
  events describe their own stages rather than pretending every wait is an
  assistant response.
- Failures carry structured error payloads. A failed recoverable tool call is
  not automatically a failed operation; the agent may correct the call.

Only an operation's terminal event has `is_final=true`. A child task completing,
or `chat.completed` arriving before conversation persistence, does not finish
the operation. A root task's event reader follows the full operation stream.
A non-root task's reader filters its own events and descendants, excludes
unrelated siblings, and ends at that task's terminal event rather than waiting
for an enclosing operation that may still be running.

Streaming and collecting observe the same execution. In particular,
`AgentRun.collect()` derives its structured result from agent events; it does
not run a second completion. A component task's `result()` instead returns
the worker's native value. Either way, receiving some deltas is not proof
that the final outcome succeeded.

The HTTP layer serializes operation events as SSE frames and maps
`Last-Event-ID` to the replay cursor. Its periodic heartbeat is an SSE comment,
not a stored domain event with a new sequence number. Losing the connection
does not itself cancel the worker; cancellation is a separate request.

### 8.3 Async execution, storage ownership, and durability

#### Scheduling work does not make blocking work asynchronous

`OperationTask` uses `asyncio` tasks, not a process per operation. Several
operations can make progress while awaiting models, storage, or network I/O,
but synchronous work inside a coroutine would still block the event loop.
RAVEN therefore combines task scheduling with explicit executor boundaries:

- The operation store owns one SQLite connection and a dedicated single-worker
  executor. Its connection operations run on that worker, with an asynchronous
  lock serializing access.
- Knowledge and conversation storage paths offload synchronous Qdrant and
  SQLite work through thread helpers; relevant file operations and resource
  opening/closure are also offloaded. Resource locks still protect mutations.
- Document conversion runs behind a thread boundary. Async model calls remain
  distinct from synchronous parsing, filesystem, and database operations.

This does not make every instruction in the backend parallel or remove all
CPU cost. It keeps known blocking work out of the request's event-loop path.
Nor does serializing one user's store serialize all model inference: operations
can await external work concurrently, then take short turns using the database.
Each user has a separate store and executor, not one shared database for all
users.

#### One store, three related record types

The shared store lives at
`<raven-home>/<user-id>/operations/operations.sqlite3`.
`SQLiteOperationStore` is separate from both `Operation` and `EventStream`
because it implements persistence, not either object's execution policy.

| Table | Responsibility |
| --- | --- |
| `operations` | Root identity, name, current status, timing, and last event ID. |
| `tasks` | Invocation state, retry policy/input, attempt count, links, and errors. |
| `events` | Ordered envelopes, task correlation, payloads, and terminal markers. |

Appending a lifecycle event updates its corresponding operation or task state
and inserts the event in one transaction. This avoids a committed terminal
event disagreeing with a separately written status. Foreign keys tie tasks
and events to their operation. An ownership-file lock prevents two runtimes
from independently owning the same operation store; SQLite alone would not
coordinate their in-memory workers or stream sequence counters.

Cancellation-safe boundaries wait for an already started durable transition
to settle before propagating cancellation. A thread cannot be treated as
stopped merely because the coroutine awaiting it was cancelled. This matters
for releasing resource locks, advancing stream state after a committed write,
and reporting an honest final outcome.

#### Commit and synchronization are distinct

The operation database uses SQLite WAL mode with `synchronous=NORMAL`.
Events are committed individually without combining or delaying response
deltas into a new token stream. They are readable before every commit has
received an explicit durable checkpoint.

The synchronization policy is:

- **Immediate checkpoints** at root operation queue/start/terminal boundaries,
  including an explicitly final event. Task registration with persisted
  retryable input also synchronizes before its worker proceeds.
- **Periodic checkpoints** of a dirty user store, normally every **1 second**,
  controlled by `operation_sync_interval_seconds`. One checkpoint covers
  pending writes across that user's operations.
- **Final synchronization and closure** during graceful shutdown. Checkpoint
  failure marks the manager unhealthy and is surfaced to publishers/readers
  rather than silently continuing with a durability claim.

Ordinary task lifecycle events and individual token deltas do not each force
an immediate checkpoint. This avoids the previous per-token synchronization
cost while preserving event granularity. The interval is a scheduling policy,
not a hard real-time bound: load and storage delays can extend it.

A process kill with an intact operating system is different from power loss
or an OS crash. SQLite can recover committed WAL transactions after a process
dies; recent unsynchronized transactions can still be lost after a power/OS
failure. Checkpoints reduce that exposure but do not protect against every
filesystem, hardware, or storage failure. The operation database also cannot
make a transaction spanning Qdrant, knowledge records, and a cloud API call.

### 8.4 Crash recovery, idempotency, and explicit retry

#### Recover the outcome before considering another attempt

`Raven.start()` invokes operation recovery before declaring its own startup
complete. Non-terminal records from the previous process have no worker to
finish them. Recovery marks their unfinished tasks failed, then appends a
final `operation.failed` event with `operation_interrupted` and
`recovered_after_restart=true`. Existing terminal operations are left alone;
running recovery again does not append another interruption to the same
finished operation.

{% include diagram.html id="operation-recovery" label="Interruption recovery and a user-confirmed retry" width="960" height="760" alt="After a process exits, a new runtime reads retained operation records. Previously queued or running work is finalized as failed with operation_interrupted. No worker resumes automatically. An explicit retry request checks policy, error, inputs, attempts and existing retry links. If permitted, a new operation and task link back to the failed attempt, then perform domain-specific reconciliation before reuse or re-execution." %}

*Restoring records, replaying events, and retrying a task are three different
actions. None reconstructs an arbitrary Python coroutine's instruction pointer.*

Graceful shutdown can cancel active work, allow protected transitions to settle,
run final knowledge reconciliation, close resources, and synchronize the store.
A forced kill cannot execute that cleanup. Recovery therefore records an
interrupted execution, not a successful shutdown or a blanket rollback of
everything the worker touched. Malformed resources and reconciliation failures
remain errors or discovery issues, not proof that no data existed.

#### Retry is a task-level policy, not a universal replay button

RAVEN v0.2.0 provides user-confirmed retry for `ingestion.run`,
`reconstruction.reconstruct`, and `session.generate_response`, each with at
most **three total attempts**, including the initial attempt. Other task names
default to `never`. Eligibility requires a failed task, recorded JSON retry
input, an allowed error code, and remaining attempts. Completed and cancelled
tasks are not eligible. An existing direct retry link prevents retrying the
same failed attempt twice.

`Raven.retry_task(operation_id, task_id)` dispatches the recorded task type
and creates a **new operation and task**. The retry records
`retry_of_operation_id`, `retry_of_task_id`, and the incremented attempt.
The old failure and its event history remain unchanged. If that attempt
fails and remains eligible, a further retry follows the new failed task;
it does not branch repeatedly from the original one.

Eligibility is not restricted to original root tasks. A failed reconstruction
child can be retried as the root of a new operation using its normalized
evidence input. That rebuilds sources without repeating the preceding agent
loop or retrieval. It also does not change the original chat's terminal state.
Retry input is internal execution data, not a public instruction to deserialize
arbitrary workers or an API response containing provider credentials.

#### Safe repetition depends on the domain's commit boundary

The retry dispatcher cannot make an unsafe write safe by scheduling it again.
The resource being changed must recognize repeated work:

| Work | Reconciliation and reuse |
| --- | --- |
| Ingestion | Retry retains the original file ID and preparation parameters. It checks that ID against committed records first: an existing commit is returned without duplicating the file; otherwise vectors for the unfinished file ID are removed before a new attempt. Source identity/hash checks prevent silently substituting changed content. |
| Conversation turn | Retry retains the turn ID and query. A matching committed turn reuses its saved result and reconciles derived memory instead of appending duplicate messages. Without a commit, generation can run again and may produce a different answer. |
| Reconstruction | Retry reads current source sections using saved evidence references. It does not regenerate the answer, restore a deleted source, or resume at an interrupted rendering instruction. |

Ingestion's ordinary error path and graceful shutdown perform targeted cleanup
of known pending writes. After a hard kill, the ingestion retry supplies the
stable file ID needed for its cleanup; reopening the knowledge alone is not
a promise of a full orphan-vector sweep. Pending file deletions use a durable
SQLite marker and are reconciled when the knowledge storage opens. Conversation
memory initialization can repair committed turns whose vector indexing did not
finish. These are specific reconciliation paths, not one distributed transaction
or a universal exactly-once guarantee.

Retry also depends on surviving inputs and runtime dependencies. Models must
be configured again where needed. A browser-upload source is retained privately
after failure only within `upload_retry_retention_seconds`; success removes it.
After expiry, `upload_retry_expired` requires another upload. A trusted local
path must still be available and compatible with its recorded identity. A
repeated cloud completion can incur another request and cost even when a
local database commit is idempotent.

#### Retention is an application policy

`OperationCleanupService.run_once()` can remove a bounded batch of terminal
operations older than the chosen retention period, reporting deleted, skipped,
and failed IDs. Deletion cascades to their tasks, retry inputs, and events:
the operation can no longer be replayed or retried. Finished-object LRU eviction
is different—it frees runtime cache entries without deleting durable history.

**Neither the `Raven` facade nor the bundled server schedules operation-history
cleanup automatically.** The application's bridge layer or hosting backend
must own that schedule and policy for each user's manager. Having retention
settings does not itself erase records. Server upload expiry is a separate,
implemented maintenance path and should not be mistaken for operation cleanup.

This division keeps execution observable without claiming that observation
alone repairs every side effect. Operations establish what ran and how it
ended; domain stores establish what committed; explicit reconciliation and
eligible retries connect the two after interruption.

## 9. Source reconstruction

A source reference identifies where an answer's supporting material can be
found. It does not show the passage in its surrounding document. RAVEN's
reconstructor bridges that gap: it expands section references into readable
file views and marks the sections supplied during the model's investigation.

**The goal is source transparency, not a 100% accurate, lossless reproduction
of the original file with highlights added.** Reconstruction is intended to
help a reader identify the source material supplied for an answer, inspect
its surrounding context, and locate the relevant place in the original
document if that document is available elsewhere. Where source-navigation
ranges were preserved, an application can use them to guide that navigation.
The original document remains the reference for its exact contents and layout.

The unit being reconstructed is the **stored, parsed representation of a
file**, not its original binary or page layout. This follows from ingestion's
separation of semantic sections, searchable chunks, and source-navigation
metadata. Retrieval chooses sections; reconstruction puts them back into their
stored sequence so that an interface can show both the selection and the
context around it.

### 9.1 Evidence and source reconstruction

#### Evidence records connect tools to sources

Content-bearing tools return the source text needed by the model and a
separate list of evidence references. Each reference contains only
`knowledge_name`, `file_name`, and `section_id`. It describes the origin of
supplied content, not a relevance score or a claim written by the model.

References can originate from any retrieval strategy, including both branches
of an agreement result, or from navigation that explicitly reads section
content. Metadata-only navigation, conversation-memory search, and preference
tools do not supply document-content evidence. The same section returned by
several tool calls remains one reference, identified by the complete
knowledge/file/section triple.

The model-facing tool payload includes these references alongside its result.
RAVEN also exposes them in `chat.tool_result` events, and the harness accumulates
them across the run. Its transcript preserves the tool-result payloads for the
session to commit with the turn. Consequently, the evidence is not dependent
on an interface retaining a temporary list of displayed citations.

There is an important limit to this lineage: **the references identify
sections supplied to the model, not a verified list of passages that caused
each sentence in its answer**. A model may ignore some supplied material,
misinterpret it, or make an unsupported claim. Reconstruction makes that
comparison inspectable; it does not infer the model's internal reasoning.

#### From references to complete file views

`Reconstructor` depends on the knowledge base and operation manager, not on a
particular retrieval pipeline, session, model, or HTTP transport. Its input can
therefore come from a standalone retrieval, accumulated agent evidence, or
another caller providing the same valid reference contract.

{% include diagram.html id="source-reconstruction" label="From supplied section references to inspectable file views" width="960" height="820" alt="References from tool outputs include a duplicate section in engineering/flight-notes.pdf and a section in narrative/short_story.txt. Reconstruction deduplicates knowledge/file/section triples, groups them by knowledge and file, and reloads each file's complete stored section sequence. Only referenced sections are highlighted; other sections remain visible as context. Page ranges are preserved where available. Full file views are returned and emitted as reconstruction.file events, without model inference." %}

The reconstruction path performs five steps:

1. **Normalize the input.** Accept a section-reference list, an agreement
   envelope containing one list, or the agreement envelope's embedded and
   hierarchical lists. `None` and an empty list produce an empty result.
   Each reference requires non-empty knowledge, file, and section strings.
   Extra retrieval fields such as `raw_content` are not used as the source
   of reconstructed text.
2. **Deduplicate and group.** Remove duplicate triples and group the remaining
   IDs by `(knowledge_name, file_name)`. Two files with the same name in
   different knowledges remain separate groups. For agreement retrieval, the
   references from both branches are combined: reconstruction does not
   restrict highlights to their intersection or reevaluate the agreement label.
3. **Resolve the current stored source.** Find the file in that knowledge and
   load its complete section sequence from `file.sqlite3`. Check every requested
   ID against that file's sections. An existing section in a different file
   is not accepted merely because its ID can be found elsewhere.
4. **Build the view in source order.** Return every stored section for that
   file, setting `highlighted=true` only for referenced IDs. Unselected sections
   remain present with `highlighted=false`. The order is the stored section
   order, not retrieval rank or the order in which tools returned references.
5. **Publish and return the same views.** Emit one `reconstruction.file` event
   for each completed file view and return the list through the operation
   task. `reconstruction.completed` reports the file count and the total number
   of sections in those views—including unhighlighted context sections.

This is why reconstruction does not concatenate the text fragments from a
tool result and call the result a document. The knowledge store is the source
of the complete section sequence; the references determine only which of those
sections are highlighted. No embedding search or LLM completion is involved
in this read-and-assemble step.

#### The file-view contract and navigation ranges

A reconstructed file has three file-level fields—`knowledge_name`,
`file_name`, and `navigation_type`—plus a `sections` list. Each section contains
its ID, highlight flag, `raw_content`, and `source_range`. File IDs, ranking
scores, chunk payloads, and section-selection summaries are not added to this
view.

For example, this is a complete illustrative view of a three-section file
when only its second section was supplied:

```json
{
  "knowledge_name": "engineering",
  "file_name": "flight-notes.pdf",
  "navigation_type": "page",
  "sections": [
    {
      "section_id": "a83fd91c20b4-1",
      "highlighted": false,
      "raw_content": "The mission uses reaction wheels for attitude control.",
      "source_range": [1, 1]
    },
    {
      "section_id": "a83fd91c20b4-2",
      "highlighted": true,
      "raw_content": "A wheel-current anomaly triggers safe-hold assessment.",
      "source_range": [2, 3]
    },
    {
      "section_id": "a83fd91c20b4-3",
      "highlighted": false,
      "raw_content": "The recovery checklist follows the anomaly assessment.",
      "source_range": [3, 3]
    }
  ]
}
```

`navigation_type` belongs to the file; each section's range is interpreted
using that type. With `page`, `[2, 3]` is an inclusive span from page 2 through
page 3, while `[3, 3]` points to one page. These values are preserved from
ingestion, not inferred from the reconstructed text or its section ordinal.

Where page provenance was not available, a section can have a null range.
Plain-text and Markdown files use `navigation_type="none"`; DOCX should not be
assumed to carry reliable original pagination simply because PDF usually does.
The view does not invent a line number, bounding box, or page coordinate when
the parser did not preserve one.

A host can use known ranges to link a section to the original document's
pages. That requires the application to retain or otherwise make the original
file accessible and implement its viewer/link behaviour. Reconstruction itself
does not store the original binary, create those links, or reopen the original
upload. It can operate from the stored sections even after a temporary ingestion
upload has been removed.

#### Automatic reconstruction and rebuilding a saved turn

During an ordinary agent run, the harness reconstructs accumulated evidence
after response generation and before publishing `chat.completed`. It passes
the same operation through this child task. An interface can receive the
`reconstruction.file` events, while `AgentRun.collect()` places those file
objects in `reconstructed_sources`. This is not another model call, and the
full reconstructed files are not automatically sent back into the model's
context merely because they are available to the UI.

Standalone `Raven.reconstruct(...)` uses the same mechanism independently of
the agent. `Raven.reconstruct_from_turn(conversation_id, turn_id)` adds a
different entry point: it reads that turn's canonical messages, extracts
deduplicated references from successful JSON tool results, and invokes the
reconstructor under the caller's operation. It does not rerun retrieval,
regenerate an answer, inspect a compacted summary for citations, or require
the original session to remain alive. A casual turn with no successful
document evidence reconstructs to `[]`; an unknown turn is an error rather
than an empty evidence set.

Three ways of revisiting sources must remain distinct:

| Action | What is read | Relationship to the original turn |
| --- | --- | --- |
| Read its saved result | The committed turn's `reconstructed_sources` snapshot, when present. | Shows the view that was saved with that result; no current source lookup is required. |
| Replay its operation | Retained `reconstruction.file` events from that execution. | Shows emitted views, subject to operation-history retention; replay does not rebuild them. |
| Reconstruct from the turn | Saved tool-result references, then current knowledge records. | Builds a new view and new events without overwriting the original saved result. |

The third path is useful for a “show sources again” action, but it is not an
immutable historical archive. A removed knowledge or file, or a replaced file
with different section identities, can make the old references unresolvable.
The reconstruction call fails rather than silently substituting unrelated
sections. A previously saved snapshot can still be readable independently of
the live source. Applications needing historical evidence should preserve the
relevant conversation records and snapshots rather than assume that rebuilding
will always be possible.

#### What reconstruction guarantees—and what it does not

- **It validates source membership.** Missing knowledges/files and section IDs
  outside the named file raise explicit errors. Malformed references fail
  validation instead of producing a plausible-looking partial highlight.
- **It retains surrounding sections.** The output contains full file views,
  not just a citation list or highlighted extracts. This can be substantial
  data; the interface may need incremental or selective rendering. It is not
  a paginated or token-budgeted document response.
- **It preserves parsed content, not original layout.** Text and Markdown can
  remain close to their sources. PDF/DOCX tables, images, and layout reflect
  the parser's textual projection and its losses; reconstruction cannot
  recover information discarded during ingestion or promise a faithful PDF.
  This is an intentional scope boundary: an inspectable source view with
  surrounding context and available navigation references, not a replacement
  for the original document.
- **It exposes failure rather than certifying partial success.** A later file
  can fail after earlier file events have been published. The task then fails
  instead of returning those earlier views as a successful complete result.
  In an automatic agent run, that error can fail the agent before the session
  commits a turn. Published events remain available for inspection, and eligible
  reconstruction tasks have the explicit retry path described in Section 8.
- **It supports verification, not factual certification.** All supplied
  sections may be highlighted even if the answer uses only a subset. Neither
  a highlight nor nearby context proves that a claim is correct, complete,
  up to date, or supported by the displayed material.

The architectural boundary is therefore concrete: evidence records preserve
source identity, knowledge stores preserve the parsed content, reconstruction
assembles an inspectable view, and the application decides how to present it.
The final section follows that application boundary across the Python facade,
HTTP server, and the host around RAVEN.

## 10. Application boundaries

The preceding sections describe mechanisms that can be used without a web
server: knowledge resources, pipelines, conversations, agent runs, and
observable operations. An application still needs an entry point that connects
those mechanisms to its interface and owns their lifetime. RAVEN provides two:
the Python `Raven` facade and an optional HTTP server built around that facade.

They are alternative entry points into the same backend, not mandatory layers
that every application must traverse. The TUI in the
[In Action walkthrough]({{ '/demo/' | relative_url }}) uses the Python API
directly. A separate application backend can use HTTP instead. Neither route
turns RAVEN into a frontend, an account system, or a deployment supervisor.

### 10.1 The high-level Raven API

#### Composition rather than another implementation

`Raven` is a composition root: it constructs compatible components, shares
their dependencies, and exposes a convenient API over them. Its constructor
takes the required `raven_home`, a user UUID with a fixed single-user default,
and an optional `SystemConfig`. It owns that user's paths, runtime-settings
snapshot, provider, operation manager, knowledge base, conversation manager,
and reconstructor. Model-dependent pipelines are wired after configuration;
session creation constructs a harness with the selected dependencies and
effective settings.

This arrangement solves a wiring problem, not a domain problem. A caller should
not have to connect several managers just to ingest a document, but the facade
should not contain a second ingestion algorithm. `Raven.ingest()` therefore
routes through `IngestionPipeline.run()`, not directly to `Knowledge.ingest()`:
parsing, splitting, metadata extraction, limits, and retry preparation remain
part of the normal high-level workflow.

The same principle applies elsewhere:

- Knowledge methods delegate creation, navigation, metadata changes, and
  deletion to the knowledge components.
- Conversation methods expose metadata, canonical messages, turns, and
  preferences through the conversation manager and resources.
- `Raven.session(conversation, ...)` accepts a managed conversation object and
  returns a temporary interaction medium. It is not a conversation-ID store
  or a permanently cached session.
- Retrieval and reconstruction wrappers select the existing pipelines;
  operation inspection, cancellation, retry, and discovery-issue methods
  expose their existing runtime machinery.

Applications that need lower-level control can still use the components
directly. They then take responsibility for supplying compatible models,
sharing the intended operation, respecting resource ownership, and closing
what they create. The facade centralizes this work; it does not make the
component API a separate or incompatible execution engine.

#### Different return types represent different interactions

The high-level API remains operation-first without pretending that every
method is the same kind of action:

| Interaction | Typical entry point | What the caller receives |
| --- | --- | --- |
| Read stored state or inspect configuration | `get_knowledge_details()`, `get_runtime_settings()` | Ordinary metadata/settings values; some reads are async because they use storage. |
| Start observable component work | `ingest()`, `retrieve()`, `reconstruct()` | An `OperationTask` with events, status, cancellation, and a native `result()`. |
| Build an interaction medium | `session(conversation)` | A temporary `Session`, not an executing chat turn. |
| Submit a turn | `session.generate_response()` | A `SessionRun` exposing the turn identity, event stream, collection, and cancellation. |
| Compose application-defined work | `create_operation()` followed by `operation.run()` | One operation shared by the root and its component invocations. |

Per-call overrides are validated against system limits; otherwise workflow
methods use the current runtime defaults. A session captures its effective
settings when constructed, rather than silently changing an active run when
another request updates defaults.

`Raven.start()` loads runtime settings, recovers operation lifecycle, and opens
the resource registries. It does not configure models or start Ollama.
`configure_models()` supplies the provider specifications, validates the
adapters, and wires the model-dependent components. Its Ollama residency work
is described in Section 2; a cloud adapter is not a local copy of cloud weights.
`Raven.close()` owns cancellation, reconciliation, and closure of its resources.
A forced process termination still bypasses graceful cleanup.

### 10.2 The FastAPI boundary and hosted runtime

#### One server, reusable user-scoped runtimes

`create_app(raven_home, system_config)` creates the ASGI application, typed
routes, error translation, request limits, CORS policy, and a
`UserRuntimeRegistry`. FastAPI's lifespan starts that registry and closes it
on shutdown. The optional server package adds transport; the domain modules
do not depend on FastAPI request objects or SSE response classes.

The registry is a map from user UUID to a reusable `Raven` instance. It does
**not** construct another instance for every GET or POST. On first access,
it coordinates initialization for that UUID and starts the runtime. Concurrent
requests for the same user share it; different users receive independent
runtimes rooted in their own directories.

A **lease** keeps a runtime alive while a request uses it. Accepted background
work receives an additional lease until its task reaches a terminal state,
so returning `202 Accepted` does not make the runtime idle or discard its
worker. Long-lived SSE readers also retain their request lease. Unleased
runtimes can be evicted after the idle interval or to make room at capacity.
Failed closure remains tracked as pending cleanup, rather than losing ownership
of a resource that may still be open.

The default maximum is 100 resident/initializing/pending-cleanup runtime slots,
with a 30-minute idle interval. Long-running HTTP submissions also have
configurable admission limits: normally **8 active submissions per user and
64 across the server**. These count admitted jobs, not every child task inside
an operation. Excess work is rejected rather than placed into a durable job
queue. This is concurrency admission control, not a general requests-per-second
or billing-quota system.

Runtime eviction does not delete knowledge, conversations, or operation
history. It does discard in-memory adapters and model selection. After a
runtime is recreated—whether following eviction or a process restart—an
application must check its status and configure the model pair again before
model-dependent work. An external Ollama model remaining resident does not
restore RAVEN's lost adapter configuration.

#### Startup and readiness describe different layers

The `nraven serve` CLI requires `--home`. It loads the server's system settings
from the normal home location, or an explicit `--system-settings` path, and
starts one Uvicorn worker. The default bind address is `127.0.0.1:8765`.
Host, port, and other system settings belong to startup configuration, while
per-user runtime-default changes remain separate HTTP/library operations.

Health has deliberately narrow meanings:

- `GET /api/v1/health/live` confirms that the application can answer its
  liveness request.
- `GET /api/v1/health/ready` checks that the runtime registry and its maintenance
  task are available. It does not eagerly open every user's resources or
  verify every user's models.
- `GET /api/v1/runtime` reports the selected user's runtime state, including
  `models_loaded` and operation-store health. That runtime is initialized
  lazily, with its own recovery, when first acquired.

An application should not treat server readiness as “the model is loaded in
VRAM and every user is ready to chat.” Adapter configuration, external-provider
availability, and actual model residency are separate concerns.

#### HTTP commands adapt the operation contract

Request and response schemas define the HTTP boundary rather than exposing
raw resource objects. The generated `/openapi.json` and `/docs` describe that
wire contract; they are not a serialization of every Python method. The main
route families are:

| Family | Responsibility at the HTTP boundary |
| --- | --- |
| Runtime | User-scoped status, revision-guarded runtime settings, and discovery issues. |
| Models | Provider-neutral model-pair configuration and configured-role residency; external Ollama status, listing, inspection, pull, and deletion. |
| Knowledges | CRUD, paged files/sections, section detail, bounded multipart ingestion, and explicitly enabled trusted-path ingestion. |
| Conversations | CRUD, canonical messages, turn submission/results, preferences, and reconstruction from a persisted turn. |
| Operations | Status and task history, replayable SSE, cancellation, retryable-task discovery, and explicit retry. |

The provider-neutral configuration routes use
`/api/v1/provider/models/configure` and
`/api/v1/provider/models/configured`; the separate
`/api/v1/providers/ollama/...` family administers the external Ollama service.
The distinction prevents a list of installed Ollama models from being confused
with the pair configured for the requesting user's runtime.

Long-running commands return an accepted reference containing `operation_id`,
`task_id`, `status`, and `events_url`; turn submission also includes the
conversation and turn IDs. The status reflects the task at response creation,
not a promise that it will remain queued. Some short mutations, such as
knowledge creation, instead await their task and return the resulting resource
with `201 Created`. An operation-first backend does not require every HTTP
response to be `202`.

The client can then stream typed events, inspect terminal status, and read
the affected resources. A generic task-status response does not expose an
arbitrary Python result object or private retry input. HTTP failures use a
structured error body and request ID; failures after acceptance are observable
through task/operation state and events, not retroactively attached to the
already returned HTTP response.

SSE is a transport view of the operation stream described in Section 8:

- `id` carries the sequence number; `event` carries the type; `data` carries
  the serialized envelope.
- `Last-Event-ID` requests retained events after the client's cursor and
  then live continuation. An invalid/ahead cursor receives a clear error;
  deleted operation history is no longer an available resource.
- Periodic heartbeat comments keep an otherwise idle connection active.
  They are not persisted events and do not consume event IDs.
- The terminal operation event closes the stream. A dropped reader does not
  itself cancel the accepted job; cancellation remains an explicit action.

This server is intentionally not a complete mirror of every component API.
For example, standalone retrieval and arbitrary-input reconstruction are
available to Python callers but do not have general-purpose direct endpoints
in the bundled v0.2.0 routers. Turn-based reconstruction is exposed. Full
request formats and endpoint fields belong to the
[README's HTTP request reference]({{ site.repository_url }}/blob/main/README.md#http-request-reference),
rather than another exhaustive catalogue here.

#### Service access and end-user identity are separate

{% include diagram.html id="application-boundary" label="Desktop and hosted requests share one service boundary" width="960" height="940" alt="A desktop UI uses its trusted bridge, which attaches the private launch token and normally omits the user header. A hosted browser uses the host backend, which authenticates the account and attaches the same service token plus the authorized internal UUID. RAVEN validates service access and UUID format, then leases the matching user runtime. The runtime and its data remain separate from the host's account system. The shared token is not a user's account credential." %}

There is one bearer-token boundary, not a weaker desktop authentication system
and a separate hosted one. `create_app()` generates a random launch token;
the CLI writes it privately under `<raven-home>/temp/server_auth/` for the
owning application or host. It is regenerated at the next launch. Possession
of that token authorizes access to the internal service, so it must not be
handed to arbitrary web users or exposed with the storage directory.

User routing is a separate request context:

- **Desktop:** the trusted application/bridge attaches the token. If
  `X-Raven-User-ID` is omitted, RAVEN selects
  `00000000-0000-0000-0000-000000000001`, its fixed single-user default.
- **Hosted:** the host authenticates the account, determines its authorized
  internal UUID, and supplies both that UUID in `X-Raven-User-ID` and the
  private service token. `require_user_id_header=true` prevents accidental
  fallback when the host omits the identity.
- **RAVEN:** validates the token and UUID format, selects that user's runtime,
  and resolves resources within its root. It does not verify that the claimed
  UUID belongs to the person using the host's website.

The header requirement changes how missing identity is handled; it is not
an end-user authentication protocol. No separate “register user” endpoint is
needed to select a runtime, and supplying another valid UUID with the shared
service token is not an account-authorization check. A hosted backend must
set the header from trusted account state, not forward an unchecked value
chosen by the browser. Folder separation is not encryption or protection
against a compromised process with filesystem access.

#### Protection supplied by RAVEN, policy supplied by the host

RAVEN applies Host allowlisting, application-route Origin checks, explicit
CORS configuration, request/query/upload bounds, typed input validation,
and operation-capacity limits. Health and OpenAPI/documentation routes are
token-exempt but still subject to Host validation. Uploads use controlled
user-scoped staging and filename checks; local-path ingestion is disabled
unless configured and restricted to allowed roots.

The surrounding application still owns:

- End-user authentication, account-to-UUID authorization, and access policy
  for destructive and model-administration actions.
- Provider secrets and authorization of which model specifications or API-key
  references an account may use. A reference to an environment variable is
  not, by itself, a tenant-specific secret-access policy.
- Public TLS, reverse-proxy buffering/timeouts, internet-facing rate limits,
  quotas, monitoring, filesystem permissions, and consistent backups.
- The RAVEN server process and, where used, the external Ollama process and
  host-level GPU/VRAM policy. Per-user adapters do not partition a shared
  Ollama server's model residency.
- The interface: displaying progress/errors, reconnecting safely, exposing
  appropriate cancellation/retry actions, presenting sources, and linking
  available originals. Operation-history cleanup scheduling also remains an
  application responsibility; upload expiry is a different server service.

The CLI holds an exclusive per-home server lock and runs one worker. Embedded
Qdrant and the operation-store ownership constraints require that single-owner
deployment model. Applications embedding `create_app()` must preserve it too;
adding Uvicorn workers or replicas that share the same home is not a supported
way to increase concurrency. Async requests and concurrent conversations are
possible inside the one process; distributed ownership is a different problem.

### 10.3 Architectural comparisons and deployment limits

RAVEN can be compared with a basic fixed retrieve-then-answer design without
claiming that these capabilities are absent from every other framework. The
useful comparison is between decisions and their consequences:

| Decision | What it enables | What it costs or leaves to integration |
| --- | --- | --- |
| Multiple retrieval tools and a model-directed loop | Different search paths and multi-step investigation rather than one mandatory search per query. | More variable latency and model calls; tool selection depends on model capability and guidance. |
| Persistent conversations with separate memory views | Complete UI history, bounded model context, and semantic recall. | Compaction, indexing, and reconciliation work; summaries and recall remain imperfect. |
| Operation-first execution and retained events | Visible progress, reconnection, cancellation, and inspectable failures. | Storage writes/history growth, lifecycle management, and application-owned retention policy. |
| Source reconstruction | Inspectable supplied sections in surrounding parsed context. | Potentially large file views, current-source availability, and no guarantee of lossless layout or answer correctness. |
| Per-user local storage and one owning runtime | Explicit data location and independent knowledge/conversation roots. | Embedded-store locks and capacity limits; no built-in distributed storage or job scheduler. |
| Provider-independent model interfaces | Local/cloud or mixed model roles without rewriting the pipelines. | Provider capabilities, credentials, external-service availability, and embedding compatibility still matter. |

These tradeoffs establish the current deployment envelope:

- **Local-first storage is not necessarily local processing.** Cloud embedding
  and LLM requests can transmit document text, selection metadata, conversation
  content, and preferences as their workflows require. Data remaining in a
  local directory does not mean inference never leaves the machine.
- **A tool interface does not guarantee a good tool chain.** Models can skip
  retrieval, choose costly or unsuitable actions, refuse a request, or answer
  from general knowledge. Small models can be particularly limited in
  multi-tool sequences. Scope filtering constrains available actions; it does
  not certify the answer or replace an application-level evaluation.
- **Recovery is not arbitrary execution resumption.** Interrupted work is
  recorded honestly, and selected tasks have idempotency-aware explicit retry.
  That does not promise exactly-once cloud calls or a distributed workflow
  engine's checkpoints.
- **Concurrency is bounded within one owner.** A host can serve multiple users
  and conversations, but must plan model throughput, resident runtimes, storage,
  and admission limits. Several independent hosts require an additional routing
  and storage-ownership design; sharing a directory is not that design.

Navigation-first ranking and a separate reviewer system are future design
proposals, not hidden stages in the v0.2.0 agent described here. The current
release uses the tool exposure, prompt guidance, evidence handling, and recovery
mechanisms explained on this page. Understanding those actual boundaries is
more useful than assuming that a roadmap proposal already enforces behaviour.

## Conclusion

RAVEN's architecture connects model-directed investigation to persistent,
inspectable application state. Ingestion prepares source-linked sections;
retrieval and navigation make them accessible to the agent; conversations
retain the interaction beyond one model call. Operations and events make the
work observable, while reconstruction places supplied sections back into
their surrounding context so that a reader can examine the source.

These capabilities depend on explicit boundaries. A model response is not a
committed turn, an event replay is not a resumed worker, and a highlighted
section is not proof of a claim. Keeping those distinctions visible allows
an application to report outcomes accurately and offer recovery where the
underlying task supports it.

The tradeoff is additional state and lifecycle management in exchange for
reusable workflows, inspectable source inputs, and recoverable execution
records. RAVEN supplies those backend mechanisms through Python and HTTP;
the surrounding application supplies the interface, account policy,
deployment supervision, and retention schedule. The
[In Action walkthrough]({{ '/demo/' | relative_url }}) shows how those
responsibilities meet in a small document assistant.
