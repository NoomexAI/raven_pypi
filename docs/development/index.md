---
layout: default
title: Development notes
description: "What changed from RAVEN v0.2.0 to v0.2.2, current operating boundaries, and proposed directions for v0.3.0."
---

# Development notes

RAVEN is developed around a distinction between model behavior and application
state. A model chooses how to investigate a question; the backend supplies
tools, preserves conversations and source references, and records how the work
finishes. Improving either side should not obscure the responsibilities of
the other.

This page records the current release's boundaries and the directions being
considered next. **The scope and timing of the v0.3.0 proposals are open.** The
other pages cover the implemented backend and identify the version used in
their examples.

## 1. Current release: v0.2.2

RAVEN v0.2.2 retains the document-first workflow introduced in v0.2.1.
Applications can browse documents, retrieve sections, and reconstruct sources
directly. A session can record those actions so an agent can continue the same
investigation in a later turn. This patch adds Ollama model options and keeps
the HTTP server responsive during the first use of the sentence tokenizer.

| Area | Implemented behavior |
| --- | --- |
| Document ingestion | Parse text, Markdown, PDF, and DOCX sources; produce semantic sections with metadata and source ranges where available; store section content alongside searchable vectors. Sentence tokenization runs on a worker thread. |
| Retrieval scope | Combine individual documents and whole knowledges across the knowledge base. An empty `RetrievalScope` covers the whole knowledge base. A conversation keeps the scope selected at creation. Navigation can still browse the full knowledge base. |
| Direct retrieval and reconstruction | Run embedded, hierarchical, vector-conditioned, or agreement retrieval through dedicated Python methods and HTTP endpoints. Reconstruct selected sections in their surrounding file context without starting an agent turn. |
| Session activity | Record browsing, source inspection, retrieval, and reconstruction actions in a conversation's activity log. A later agent turn can receive a batch of pending actions as context. Applications can page through activity history without creating an entry. |
| Agent execution | A model-directed tool loop with scope and retrieval-mode constraints, structured tool feedback, and distinct thinking, tool, response, and reconstruction events. |
| Conversations and memory | Persistent turns and tool exchanges, compacted model context, vector memory, and conversation-local preferences. `search_memory` returns matches grouped by turn ID and includes an `activity_action` hint for turns with linked activities. |
| Observable work | Durable operation and task records, replayable events, cancellation, interrupted-operation recovery, and explicit retry for eligible tasks. |
| Integration | A Python facade and an optional authenticated FastAPI server; model configuration with provider-specific options for Ollama and supported LiteLLM providers. |

The agent can use `list_activities_from_turn` to inspect the actions linked
to a recalled exchange, or `list_all_activities` to browse the conversation's
activity history. Both tools return paginated records. A local Gemma test
completed the turn-specific lookup when the question asked about both the
earlier exchange and the actions. The model's choice of tools can still vary
with the way the question is phrased.

v0.2.2 uses the same storage schemas as v0.2.1. For a v0.2.0 upgrade, back up
the old home and create new conversations using v0.2.2 because the conversation
metadata schema changed in v0.2.1. The upgrade notes cover the retrieval and
conversation API changes. [README and upgrade notes]({{ site.repository_url }}/blob/main/README.md#upgrading-to-v022)

The terminal and MCP walkthroughs use v0.2.0. Follow their pinned dependencies
to run those examples. [In Action]({{ '/demo/' | relative_url }}) · [Integration guides]({{ '/integration_guides/' | relative_url }})

## 2. What changed from v0.2.0 to v0.2.2

In v0.2.0, the agent turn was the main workflow for investigating documents.
Python callers could already retrieve and reconstruct sources directly, while
the HTTP API offered reconstruction from a saved turn. In v0.2.2, an
application can start with a document selection, let the user investigate it
directly, and bring an agent into the same investigation later.

### 2.1 Select the documents to search

In v0.2.0, a conversation was either local to one knowledge or global. RAVEN
v0.2.2 uses `RetrievalScope` to represent that selection. A scope can include
two files from one knowledge, a third file from another, and a whole knowledge
for background material. A conversation saves that selection at creation.

This lets a user define which documents retrieval can draw from for a
particular investigation. The pipelines apply the scope before ranking
candidates, so other documents cannot take up result slots. Navigation still
covers the full knowledge base so users can inspect a file or decide what to
include in another conversation.

### 2.2 Run retrieval and reconstruction without a chat turn

The generic `Raven.retrieve()` method was replaced by four strategy-specific
methods, each with a matching HTTP endpoint. Standalone reconstruction also has
an HTTP endpoint. A UI can now offer a search panel and a source viewer that
follow operation events without opening a chat turn. Dedicated methods and
routes make each strategy's controls explicit. For example, vector-conditioned
retrieval can limit the number of shortlisted files separately from the number
of returned sections.

### 2.3 Carry manual work into the conversation

In v0.2.2, a session can record completed actions such as opening a section,
running a search, or reconstructing a file. When the user starts an agent
turn, RAVEN includes a bounded batch of pending actions and links those entries
to the turn when it commits. An application can also run standalone searches
when they should stay outside the conversation's history.

The handoff lets the user search and inspect sources first, then ask a question
without restating what they did. The activity log keeps compact references to
the sources, so a turn can carry that context without copying the full text of
every section the user inspected.

### 2.4 Look up recorded actions after compaction

In v0.2.0, semantic recall returned relevant messages. In v0.2.2, `search_memory`
groups those messages by turn ID. A match with linked actions includes an
`activity_action` hint that tells the agent how to look them up. The agent can
fetch a page for that turn or browse the conversation's activity history.

Compaction can omit details from an earlier exchange. The turn ID gives the
agent a way back to the saved activity records when the summary leaves those
details out. The lookup hint supplies the exact ID for the next tool call.

### 2.5 Configure Ollama model options

Both `Raven.configure_models()` and `POST /api/v1/provider/models/configure`
accept Ollama settings in `ModelSpec.options`. For example, `num_ctx` sets the
context window for either model role, while `num_predict` and `temperature`
apply to the LLM. RAVEN validates the settings for each role and forwards them
to Ollama during preload and inference. Changing settings on the same model
selects an adapter configured for those settings. [Supported options]({{ site.repository_url }}/blob/main/README.md#ollama-model-options)

The embedding `num_ctx` is part of the stored embedding identity because it
can change which input tokens are embedded. Existing indexes keep the value
they were built with, including the default when no value was supplied.

### 2.6 Keep HTTP ingestion responsive during first-use tokenization

The first ingestion after server startup could pause the event loop during
semantic sectioning. Sentence tokenization initialized LlamaIndex's tokenizer
and loaded dependencies including SciPy on that same loop. While it was busy,
the server could delay SSE progress events and fail to answer status requests
before the client timed out.

v0.2.2 runs sentence tokenization on a worker thread with `asyncio.to_thread()`.
In a fresh `nraven serve` process, the patched server kept responding to status
requests during sectioning, streamed `operation.completed`, and listed the
uploaded file in the knowledge after ingestion.

## 3. Known limitations and operating boundaries

### Model behavior and source fidelity

Tool availability does not guarantee good tool selection. Models may choose
an expensive path, inspect more material than necessary, skip retrieval, or
answer from general knowledge. Small tool-calling models can struggle with
multi-step sequences and may refuse some sensitive questions. Explicit
retrieval instructions can clarify the intended task, but they do not replace
checking the answer against the source.

Source reconstruction serves that checking process. Its purpose is to show
the sections supplied to the model in surrounding parsed context, with source
ranges for navigation where available. It is **not a lossless reproduction**
of the original file, and a highlighted section is not proof that every claim
in the generated answer follows from it. Parsing, context compaction, and
semantic recall can each lose or omit information.

### Models, storage, and deployment

Several practical boundaries matter when building an application:

- **Embedding identity must remain consistent.** Existing vectors must be
  searched with the embedding model that generated them. Replacing that model
  requires an appropriate rebuilding or re-ingestion workflow, not just a new
  configuration value. Matching vector dimensions alone is not sufficient.
- **Local storage does not imply local inference.** Using cloud LLMs or cloud
  embedding models can send document and conversation material to those
  providers. The application must choose providers and communicate that
  boundary appropriately.
- **Embedded storage requires explicit ownership.** The bundled server uses
  one Uvicorn worker. Multiple users and different conversations can operate
  concurrently within its limits, but independent server processes must not
  share the same live home as a distributed storage arrangement. Competing
  turns in one conversation are rejected rather than run simultaneously.
- **Model configuration is runtime state.** Configure models after a server
  restart or recreation of an evicted user runtime. A configuration failure
  during preloading can leave the new adapters installed rather than restore
  the previous pair; configuration is not transactional.

### Recovery and application-owned lifecycle

Event replay recovers retained observations, not a vanished worker. After a
crash, unfinished operations are identified as interrupted rather than
presented as successful. Eligible tasks support explicit, idempotency-aware
retry, but RAVEN does not resume arbitrary execution at the exact instruction
where it stopped or guarantee exactly-once external model requests.

Cancellation is similarly cooperative: it requests that work stop, not that
every committed change be rolled back. The application should preserve
operation and turn identifiers, inspect uncertain outcomes, and avoid blindly
resubmitting writes after a timeout.

The surrounding application or host also owns:

- Starting, supervising, and stopping the external Ollama service.
- Authenticating public users and authorizing the internal user identity
  forwarded to RAVEN in hosted deployments.
- Protecting credentials and storage, arranging backups, and managing capacity.
- Scheduling operation-history cleanup with `OperationCleanupService`.
  Neither `Raven` nor the bundled HTTP server schedules it automatically;
  retention settings alone do not delete history. The server's automatic
  staged-upload cleanup is a separate mechanism.

These responsibilities are part of integrating the backend, not missing
buttons in the demonstration applications. The
[deployment and security reference]({{ site.repository_url }}/blob/main/README.md#security-and-deployment-responsibilities)
and [recovery architecture]({{ '/architecture/' | relative_url }}#84-crash-recovery-idempotency-and-explicit-retry)
provide the detailed boundaries.

## 4. Directions for v0.3.0

The next version has three proposed improvements. Their interfaces,
implementation details, and compatibility implications remain design work.

### 4.1 Context-efficient, navigation-first exploration

The proposed direction is to make navigation the primary path through a
knowledge base: identify promising knowledges, narrow the candidate files,
then select relevant sections before reading their contents. The motivation
is to give the model an explicit exploration process without filling its
context with large lists or complete source sections at every step.

Navigation tools already exist in v0.2.2. What changes in this proposal is
their role and efficiency: they would become the main investigation path,
with bounded results and query-dependent relevance information at each level.
The current agent guidance instead treats navigation as support for retrieval
or an explicit navigation request.

The four retrieval mechanisms would not be discarded. They would be
repurposed to help rank candidates at those navigation levels—for example,
which knowledges relate to a query, which files within a knowledge deserve
attention, and which sections should be read next. How those scores become
useful relevance distributions remains to be designed and evaluated; a score
should not be presented as a calibrated probability without justification.

### 4.2 A dual reviewer system

A separate reviewer is proposed to examine the model's selected tool chain.
The intended default is an additional chat completion from the active LLM,
reasoning about the proposed actions. It would not require configuring a
second model, but it would introduce additional inference work and latency.

A deterministic state-machine reviewer is proposed as the lower-cost
alternative when hardware or runtime constraints make that extra completion
impractical. Its role would be to enforce defined sequencing rules. Such
structural checks are different from judging whether a chain is relevant or
whether its eventual answer is correct.

The v0.2.2 harness uses scope filtering, tool validation, corrective tool
results, and prompt guidance. The proposed reviewer would add a separate check
of the model's selected tool chain.

### 4.3 llama.cpp as a local model provider

The proposal is to add llama.cpp alongside Ollama as a provider for local
models. Applications would select llama.cpp through RAVEN's model
configuration and use the same ingestion, retrieval, and conversation APIs.

The integration would need to define how applications supply local model
files and runtime options, and which chat, tool-calling, and embedding
capabilities the provider supports. Those capabilities would be tested
against RAVEN's existing workflows before the provider is released.

## 5. Development and feedback

Useful feedback distinguishes a backend failure from a model-quality problem.
An upload rejected before ingestion, an interrupted operation, and an answer
that omits a source condition are different cases, even if each appears to
the reader as an unsuccessful interaction.

For a reproducible issue, include:

- The RAVEN version, operating system, Python version, and whether the call
  uses the library or HTTP server.
- The relevant provider and model names, including the embedding model when
  ingestion or retrieval is involved.
- The smallest safe sequence of calls or tool inputs that reproduces the
  behavior, along with the expected and actual outcome.
- Relevant error codes, operation status, and a redacted traceback or event
  excerpt. For answer-quality issues, include a shareable source passage and
  explain which fact or qualifier was missed.

Do not post API keys, bearer tokens, `.env` contents, private documents, or an
unredacted RAVEN home. A small synthetic document that preserves the failure
is preferable to uploading an entire private knowledge or conversation database.
Public issue reports and attachments should contain only material suitable
for public access.

Reports and focused proposals can be submitted through
[GitHub Issues]({{ site.repository_url }}/issues). For a proposed change,
describe the use case and the current limitation before prescribing an
implementation. Roadmap items can change as their costs and behavior are
evaluated; they do not carry a promised delivery date or an assumption of
unchanged APIs and storage formats.

The goal is to improve model-guided investigation while retaining clear
execution records, persistent conversation state, and inspectable source
material. These notes will evolve with the implementation; the release-specific
contracts remain the reference for what an application can rely on today.
