---
layout: default
title: Development notes
description: "The current capabilities and limitations of RAVEN v0.2.0, proposed directions for v0.3.0, and how to provide useful development feedback."
---

# Development notes

RAVEN is developed around a distinction between model behavior and application
state. A model chooses how to investigate a question; the backend supplies
tools, preserves conversations and source references, and records how the work
finishes. Improving either side should not obscure the responsibilities of
the other.

This page records the current release's boundaries and the directions being
considered next. **The v0.3.0 items below are proposals, not features available
in v0.2.0 or commitments to a release date.** Usage instructions on the other
pages describe the implemented backend, not this roadmap.

## 1. Current release: v0.2.0

The [v0.2.0 release]({{ site.repository_url }}/releases/tag/v0.2.0) provides
the document-to-conversation backend described throughout this site:

| Area | Implemented behavior |
| --- | --- |
| Document ingestion | Parse text, Markdown, PDF, and DOCX sources; produce semantic sections with metadata and source ranges where available; store section content alongside searchable vectors. |
| Knowledge access | Embedded, hierarchical, vector-conditioned, and agreement retrieval, with local and global scope, plus navigation tools for inspecting the knowledge base. |
| Agent execution | A model-directed tool loop with scope and retrieval-mode constraints, structured tool feedback, and distinct thinking, tool, response, and reconstruction events. |
| Conversations and memory | Persistent turns and tool exchanges, a canonical transcript separate from compacted model context, vector memory, and conversation-local preferences. |
| Observable work | Durable operation and task records, replayable events, cancellation, interrupted-operation recovery, and explicit retry for eligible tasks. |
| Integration | A Python facade and an optional authenticated FastAPI server; model configuration through Ollama and supported LiteLLM providers. |

These capabilities are backend mechanisms rather than a bundled end-user
interface. The [In Action walkthrough]({{ '/demo/' | relative_url }}) builds
a terminal interface with the Python API. The
[integration guide]({{ '/integration_guides/' | relative_url }}) connects an
external tool client through HTTP. Both examples deliberately expose a subset
of RAVEN rather than every available control.

For exact method signatures, request fields, settings, and error contracts,
refer to the [README]({{ site.repository_url }}/blob/main/README.md). The
[architecture page]({{ '/architecture/' | relative_url }}) explains the
ownership and information flow behind those contracts.

## 2. Known limitations and operating boundaries

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
- **Ollama options are restricted in this release.** Nonempty
  `ModelSpec.options` are rejected for Ollama by the provider interface. This
  restriction does not apply uniformly to the LiteLLM path; the proposed
  Ollama wiring work is described below.
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

## 3. Directions for v0.3.0

The next version is being considered around three related improvements.
Their interfaces, implementation details, and compatibility implications
remain design work. Existing v0.2.0 applications should not assume that these
mechanisms already run underneath the current API.

### 3.1 Context-efficient, navigation-first exploration

The proposed direction is to make navigation the primary path through a
knowledge base: identify promising knowledges, narrow the candidate files,
then select relevant sections before reading their contents. The motivation
is to give the model an explicit exploration process without filling its
context with large lists or complete source sections at every step.

Navigation tools already exist in v0.2.0. What changes in this proposal is
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

### 3.2 A dual reviewer system

A separate reviewer is proposed to examine the model's selected tool chain.
The intended default is an additional chat completion from the active LLM,
reasoning about the proposed actions. It would not require configuring a
second model, but it would introduce additional inference work and latency.

A deterministic state-machine reviewer is proposed as the lower-cost
alternative when hardware or runtime constraints make that extra completion
impractical. Its role would be to enforce defined sequencing rules. Such
structural checks are different from judging whether a chain is relevant or
whether its eventual answer is correct.

Neither reviewer exists in v0.2.0. Current scope filtering, tool validation,
corrective tool results, and prompt guidance should not be interpreted as an
already implemented review stage. A reviewer would supplement those boundaries,
not make source verification unnecessary.

### 3.3 Ollama model-options support

The proposed provider improvement is to carry supported `ModelSpec.options`
through `Provider` and `OllamaManager` into the appropriate LlamaIndex adapters
and Ollama requests. The current rejection of nonempty options avoids silently
accepting settings that the RAVEN provider path does not forward. This is a
RAVEN integration limitation, not a claim that Ollama itself lacks model options.

The work needs to distinguish adapter-construction settings from request
options, define which controls apply to generation and embeddings, and reject
unsupported or conflicting values. Context-window settings, generation limits,
and sampling controls are among the intended areas to examine where applicable.
Embedding-affecting settings must also be accounted for in model identity and
compatibility checks.

Completion means verifying the effective values sent by both adapter paths,
including repeated configurations of the same model with different settings,
and updating the public contracts and documentation. Until then, adding an
Ollama option to a v0.2.0 specification is not a supported workaround.

## 4. Development and feedback

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
