---
layout: default
title: In action
description: "Build a terminal document assistant with RAVEN and Textual, from document ingestion to streamed answers and source inspection."
---

# Building a document assistant with RAVEN

## 1. What we're building

This walkthrough examines how to build a small document assistant using RAVEN
and Textual, a Python framework for terminal user interfaces. The application
lets a reader add documents, ask questions in a persistent conversation, and
inspect the source material supplied to the model. Our focus is the integration:
how user actions reach RAVEN, and how its results and events become a usable
interface.

The interface is intentionally chat-first. A message box handles questions,
while slash commands open forms and lists for managing knowledges, models, and
conversations. List entries are clickable, so opening a saved conversation does
not require remembering its identifier. These are application design choices,
not requirements imposed by RAVEN.

![The terminal assistant showing a saved follow-up answer about battery state of charge, with a View sources button beneath the earlier answer.]({{ '/assets/images/in-action-chat.svg' | relative_url }})

*Figure 1. The working application during a local-model test with
`gemma4:e2b` and `bge-m3:latest`. The earlier answer used retrieval; the follow-up
shown here used conversation memory without another tool call. The technical
document is an illustrative test input, not a validated engineering reference.*

### The workflow

We will follow one document through the application:

1. **Create a knowledge and ingest the document.** A knowledge is a named,
   persistent place for related documents. The interface submits the file to
   RAVEN and displays ingestion progress while the backend processes it.
2. **Open a conversation and ask a question.** A conversation can be local,
   bound to one knowledge, or global, with access across the knowledge base.
   For a document question, the agent can use the retrieval tools available in
   that scope. The interface displays tool activity separately from the streamed
   answer.
3. **Inspect the supporting material.** A source-backed answer has its own
   **View sources** button. It opens a readable reconstruction of the relevant
   files, highlighting the sections supplied to the model. Those highlights
   support inspection; they do not establish that the answer is correct.
4. **Continue and return later.** Follow-up questions use the same conversation.
   After closing the application, the reader can reopen its saved messages and
   inspect sources associated with earlier answers.

### What the application implements

The application uses the high-level `Raven` Python API directly, within the same
Python process. It does not start the optional FastAPI server or send HTTP
requests to RAVEN. Textual supplies the terminal interface; the application
connects its controls to backend calls and renders their events.

RAVEN remains responsible for document processing, retrieval, agent execution,
conversation memory, persistence, and source reconstruction. We do not reproduce
those mechanisms in the interface. The implementation sections will explain
where that boundary sits and how the two sides communicate.

### What we deliberately leave out

This example uses a subset of RAVEN's capabilities. We have deliberately left
some features without interface controls and omitted deployment-specific
integration work to keep the document-questioning workflow easy to follow.

| RAVEN capability | Scope of this example |
| --- | --- |
| Retrieval-mode selection and direct retrieval | Chat uses `auto`, allowing the agent to choose among scope-appropriate retrieval tools. The TUI has no mode selector or separate direct-retrieval interface. |
| FastAPI server and replayable Server-Sent Events | The application calls the Python API directly. It does not demonstrate HTTP requests, authentication, browser uploads, or reconnecting an SSE client. |
| Durable operation history and user-confirmed retry | The TUI displays progress and supports cancellation, but does not provide an operation-history browser, event-replay controls, or retry actions for eligible failed tasks. Submitting a request again is not the same as invoking RAVEN's durable retry API. |
| Operation-history retention and cleanup | RAVEN provides an `OperationCleanupService`, but the demo does not schedule it. The surrounding application or host backend must choose when to run cleanup and which retention policy to apply; configuring a retention period alone does not delete history. |
| Runtime settings and per-call controls | RAVEN exposes settings for ingestion, retrieval, agent execution, and memory. The demo does not provide a settings editor or expose these parameters in its forms. |
| Resource administration | The walkthrough covers creating, listing, and opening resources. It leaves out interfaces for deleting knowledges or files, editing conversation metadata, and directly managing saved preferences. |
| Per-user isolation and concurrent work | RAVEN supports user-scoped storage and concurrent operations. This demo uses one local user context and permits only one backend action at a time; it does not implement account authentication, user routing, or a multi-user interface. |

These omissions concern the surrounding application, not the removal of backend
protections. The example still uses RAVEN's native persistence, cancellation,
and startup recovery mechanisms. A deployable application must additionally
decide how to expose recovery actions, manage retained data, and enforce the
access controls appropriate to its deployment. Not every application needs
every feature above, but those decisions should be explicit rather than
inferred from this deliberately small interface.

## 2. Set up and run the example

Start by running the complete application before examining its implementation.
Python runs the application, Textual provides
its terminal interface, and Ollama runs the local models used by RAVEN.

Run all commands below in **PowerShell inside VS Code's integrated terminal**.
Use the same terminal throughout the walkthrough so that shortcuts such as
Shift+Enter work as described.

### Watch the workflow

The recording follows environment setup, model configuration, knowledge
creation, document ingestion, chat, and source inspection. Play it for an
overview, then follow the instructions below to reproduce the workflow. Use
the player's fullscreen control to read the terminal at a larger size.

<video controls playsinline preload="metadata" width="1920" height="1080" aria-label="RAVEN terminal demo: setup, model configuration, ingestion, chat, and source inspection">
  <source src="{{ '/assets/videos/raven_tui_demo.mp4' | relative_url }}" type="video/mp4">
  This browser cannot play the embedded recording. Use the download link below to watch it locally.
</video>

*Recording: 2 minutes 22 seconds. The recorded chat uses a global conversation;
the written steps below select the local `engineering` scope for a focused
document question. Both use the same model-configuration, ingestion, and
source-inspection controls.*

[Download the recording (MP4, approximately 41 MiB)]({{ '/assets/videos/raven_tui_demo.mp4' | relative_url }}){: download="raven_tui_demo.mp4"}

### Prerequisites

| Requirement | Purpose |
| --- | --- |
| Python 3.11 | Runs the application. The commands below select this version explicitly. |
| [VS Code](https://code.visualstudio.com/) | Provides the editor and integrated terminal used in this walkthrough. |
| [Ollama 0.34.2](https://ollama.com/download) | Runs the local language and embedding models as a separate service. |

Install these applications before continuing. The next step installs the RAVEN
and Textual Python packages. Allow sufficient disk space for Python dependencies,
model weights, and documents. Local models
also require sufficient system memory and, when used, GPU memory.

Ollama is separate from the Python application. Installing RAVEN does not
install or start it, and closing the demo will not stop it.

### Prepare the source folder

Download the [complete demo source (ZIP)]({{ '/assets/downloads/tui_example.zip' | relative_url }})
and save it in a working directory. Open that directory in VS Code, open an
integrated PowerShell terminal, and extract the archive:

```powershell
Expand-Archive -LiteralPath .\tui_example.zip -DestinationPath .
```

The archive contains a top-level `tui_example/` folder. Keep all its Python
files, `styles.tcss`, and `requirements.txt` together: `app.py` is the entry
point, but it imports the other modules. An alternative extraction tool can be
used, provided the resulting folder layout is the same.

The demo is an application built *with* RAVEN, not a component installed *by*
RAVEN. This source folder is therefore not supplied by installing the
`noomexai-raven` package alone.

Keep the terminal in the **parent directory containing `tui_example/`**. Run
the following commands from that parent directory, not from inside
`tui_example/`.

### Install the Python dependencies

Create a virtual environment in the working directory:

```powershell
py -3.11 -m venv .venv
```

This creates `.venv/`, an isolated Python environment for the demo's packages.
`py -3.11` selects the Python 3.11 installation through the Windows Python
launcher. If `py` is not found, install Python with its launcher and reopen
the terminal before proceeding.

Install the packages listed in the example's dependency file:

```powershell
.\.venv\Scripts\python.exe -m pip install -r .\tui_example\requirements.txt
```

The file installs `noomexai-raven==0.2.0` and `textual==8.2.8`, along with their
dependencies. RAVEN supplies the backend; Textual supplies the terminal widgets
and application framework. RAVEN's optional server extra is not required for
this example.

The environment's interpreter, `.\.venv\Scripts\python.exe`, is referenced
explicitly so that installation and execution use the same environment.
Activation and changes to PowerShell's script execution policy are unnecessary.
Wait for installation to finish before proceeding.

### Download the example files

- [Download the demo source — tui_example.zip]({{ '/assets/downloads/tui_example.zip' | relative_url }}){: download="tui_example.zip"}
- [Download Reaction_wheel.txt]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }}){: download="Reaction_wheel.txt"}
- [Download EPS.txt]({{ '/assets/downloads/test_files/EPS.txt' | relative_url }}){: download="EPS.txt"}
- [Download Clinical.txt]({{ '/assets/downloads/test_files/Clinical.txt' | relative_url }}){: download="Clinical.txt"}

Keep the sample files beside the extracted `tui_example/` folder, in the same
parent directory used for the commands above. The walkthrough uses
`Reaction_wheel.txt`; the other two files provide additional material to explore.

### Step 1 — Run the application

Start the application from the same parent directory:

```powershell
.\.venv\Scripts\python.exe -B .\tui_example\app.py
```

The chat interface will appear in the terminal. Once **Ready** appears in the
status line, enter `/help` in the message box and press Enter to display the
available commands. No models are loaded at this stage; that is the next step.

Type `/` to open the command menu and continue typing to filter it. Click an
entry, or select it with the arrow keys and press Enter. For ordinary messages,
Enter sends and Shift+Enter inserts a new line.
Ctrl+C closes the application gracefully.

### Step 2 — Configure the models

Before configuration, open Ollama and leave its local service running at
`http://127.0.0.1:11434`. For startup guidance, refer to the
[Ollama documentation](https://docs.ollama.com/quickstart). To start the service
manually, run `ollama serve` in a separate terminal and leave it active. Do not
start a second instance if Ollama is already running.

Enter `/models/configure` in the TUI's message box and press Enter. A form will
appear with two columns:

- **LLM:** the model that reads document material, uses tools, and generates
  answers.
- **Embedding:** the model that converts text into vectors for semantic
  splitting, retrieval, and conversation-memory search.

For a local setup, complete the fields as follows:

| Field | LLM | Embedding |
| --- | --- | --- |
| Provider | `ollama` | `ollama` |
| Model name | `gemma4:e2b` | `bge-m3:latest` |
| API key reference | Leave empty | Leave empty |

![The model configuration form with the LLM set to ollama and gemma4:e2b and the embedding model set to ollama and bge-m3:latest.]({{ '/assets/images/in-action-models.svg' | relative_url }})

*Figure 2. The model configuration form. Both model roles must be supplied before
ingestion or chat can begin; local Ollama models do not require API keys.*

After completing the fields, select **Configure**. RAVEN downloads any missing
local models and prepares the pair for use. Progress appears in the transcript;
the first configuration may take time while model weights are downloaded. Once
configuration completes, enter `/models` to display the configured pair. Use
`/providers/ollama/models` to check the exact names of installed Ollama models.

If configuration fails because Ollama cannot be reached, start the service
before submitting the form again. Other provider errors are displayed
in the form without closing the application.

> **Choose the embedding model before adding documents, and keep it unchanged
> throughout the walkthrough.** Stored vectors must be searched using
> the same embedding model that generated them. Switching to another requires
> rebuilding the affected embeddings. The LLM can be changed independently.

#### Alternative: configure a cloud LLM

To use a cloud LLM instead, configure the LLM column for a provider supported
by RAVEN's LiteLLM adapter. Select a model with tool-calling support and ensure
the provider account has access to it. The demo does not require a particular
cloud provider.

The API key must be available in the environment inherited by the application.
If it was not set before launch, close the TUI with Ctrl+C. Run the following
commands in the same PowerShell terminal to enter the key and reopen the
application:

```powershell
$env:RAVEN_DEMO_API_KEY = [System.Net.NetworkCredential]::new(
    "", (Read-Host "Cloud provider API key" -AsSecureString)
).Password
.\.venv\Scripts\python.exe -B .\tui_example\app.py
```

Enter the key at the prompt rather than placing it literally in command
history. The command does not save it to a file. A `.env` file alone is
insufficient because the demo does not load one automatically.

Reopen `/models/configure` and complete the LLM fields as follows:

| Field | Value to enter |
| --- | --- |
| Provider | The service's LiteLLM provider identifier, such as `openai`, `anthropic`, or `gemini`. |
| Model name | The chosen provider's model identifier, for a model available to the account and supporting tool calling. |
| API key reference | `RAVEN_DEMO_API_KEY` |

Find the correct provider identifier and model name in the
[LiteLLM provider documentation](https://docs.litellm.ai/docs/providers).
Provider identifiers are routing values, not necessarily company names. Replace
the `YOUR_API_KEY` placeholder with the environment-variable name, **not the
secret**. To use an existing variable instead, enter its name
in the form.

Keep the embedding column set to `ollama` and `bge-m3:latest`, leave its key
reference empty, and keep Ollama running. Then select **Configure**. Cloud
configuration prepares adapters; remote inference occurs when ingestion or
chat needs the model. Normal provider quotas and charges apply. Document
material is sent to the cloud provider during metadata extraction and chat,
so use only documents permitted to be sent.

### Step 3 — Create a knowledge

With the models configured, enter `/knowledges/create` in the message box.
Complete the form with these values:

| Field | Value |
| --- | --- |
| Name | `engineering` |
| Summary | `Engineering reference documents` |

![The knowledge creation form with engineering as the name and Engineering reference documents as the summary.]({{ '/assets/images/in-action-create-knowledge.svg' | relative_url }})

*Figure 3. Creating the knowledge that will hold the example document.*

Select **Create**. A confirmation and the knowledge's details will appear.
Its file count will initially be zero. `/knowledges` displays the saved
knowledges; clicking `engineering` opens the same details as entering
`/knowledges/engineering` directly.

If a knowledge named `engineering` already exists from an earlier run, open it
rather than creating it again. Creating a knowledge itself does
not require models; it follows configuration here so the document can be
ingested immediately afterward.

### Step 4 — Ingest the example document

Download [Reaction_wheel.txt]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }})
and save it beside `tui_example/`, in the parent directory from which the
application was launched. If the browser displays its contents, save the file
as plain text with that exact name, not as an HTML page.

This fictional technical document contains a reaction-wheel fault and safe-hold
response, with specific values that can be checked against an answer. It is
sample material, not engineering guidance.

Three sample files are available for exploring the same workflow:

| File | Subject |
| --- | --- |
| [Reaction_wheel.txt]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }}) | Reaction-wheel fault conditions and a safe-hold response; used for the questions below. |
| [EPS.txt]({{ '/assets/downloads/test_files/EPS.txt' | relative_url }}) | Electrical power-system conditions and responses. |
| [Clinical.txt]({{ '/assets/downloads/test_files/Clinical.txt' | relative_url }}) | Clinical terminology and numerical conditions for document-questioning examples. |

These files are illustrative inputs, not validated engineering or medical
guidance. Continue with `Reaction_wheel.txt` for this walkthrough; the other
files can be downloaded and ingested separately afterward.

In the TUI, enter `/knowledges/engineering/ingest`. The target is already fixed
to `engineering`. Enter `Reaction_wheel.txt` in the **Local file path** field.

![The ingestion form targeting engineering with Reaction_wheel.txt entered as the local file path.]({{ '/assets/images/in-action-ingest.svg' | relative_url }})

*Figure 4. The document path is resolved from the application's launch directory.
An absolute path can be used instead if the document was saved elsewhere.*

Select **Ingest**. Progress will appear for parsing, sectioning,
metadata extraction, embedding, and storage. Counts apply to the current stage,
not to an overall percentage. The interface remains available while this work
runs. **Stop ingestion** requests cancellation and waits for safe cleanup.

On completion, refreshed knowledge details will show the document in the file
list. The original file is read, not moved. If the file is already listed from
an earlier run, skip re-ingestion. If the file cannot be found, correct the
path before resubmitting.

**Attach** is an alternative way to open the ingestion form. If no local
conversation is active, also choose a target knowledge. The scoped
command above avoids that additional choice for this walkthrough.

### Step 5 — Open a conversation

Before sending a question, enter `/conversations/create`. The form defaults
to **Global · All knowledges**. For this walkthrough, select `engineering`
from the scope dropdown, then select **Create**.

![The conversation creation form with engineering selected as the optional knowledge scope.]({{ '/assets/images/in-action-create-conversation.svg' | relative_url }})

*Figure 5. Selecting a knowledge creates a local conversation. Leaving the global
option selected instead permits access across the knowledge base.*

The new conversation opens immediately, and its title and scope appear in the
header. The initial title is **New Conversation**; it will be updated
automatically after the first chat turn. Existing conversations can be opened
through `/conversations` by clicking their titles, without entering their IDs.

### Step 6 — Ask a document question

Enter the following question as an ordinary message, without a slash-command
prefix, and submit it with Enter or **Send**:

```text
Use retrieval to search engineering. According to Reaction_wheel.txt, what is the safe-hold execution window in milliseconds? Give the number and name the file.
```

Tool activity appears separately from the streamed response. Retrieval is set
to `auto`, so the agent selects among the tools allowed by the conversation's
scope. The explicit instruction to use retrieval helps small models follow the
document-based workflow rather than answer from general knowledge.

The relevant passage specifies **ninety-five milliseconds**. The answer should
identify `Reaction_wheel.txt` so its source can be inspected. Wording and tool
choice may vary; a response alone should not be taken as verification of the
document's contents.

Then send a follow-up in the same conversation:

```text
In that same passage, what battery state-of-charge percentage must be exceeded? Use retrieval if needed and name the file.
```

![The saved conversation showing a follow-up answer about the fourteen-percent battery threshold and a View sources button on the earlier source-backed answer.]({{ '/assets/images/in-action-chat.svg' | relative_url }})

*Figure 6. A continued conversation. The document specifies a threshold above
fourteen percent; information already available in conversation memory may be
used without another retrieval call.*

**Send** becomes **Stop** while a response is running. Cancellation leaves any
partial response marked incomplete. Otherwise, **Saved to conversation**
indicates that the turn has been persisted, not merely that text generation
has ended. Thinking, when supplied by the model, can be expanded separately.

### Step 7 — Inspect the reconstructed source

After the source-backed answer is saved, select its **View sources** button.
The viewer opens the files associated with that particular turn,
including when an earlier answer is selected rather than the latest one.

![The reconstructed-source viewer showing engineering and Reaction_wheel.txt, with a supplied section highlighted and the saved-snapshot status visible.]({{ '/assets/images/in-action-sources.svg' | relative_url }})

*Figure 7. The source viewer displays a readable reconstruction, with sections
supplied to the model distinguished from surrounding context.*

Select a reconstructed file from the dropdown and scroll through its sections.
**SUPPLIED TO MODEL** marks the material used as model input. Compare the
passages containing the execution window and battery threshold with the answers.
These highlights identify supplied material; they do not guarantee that every
claim in an answer is correct.

The saved reconstruction is opened first. **Rebuild from saved evidence**
reconstructs from the current knowledge data without repeating the chat or
retrieval. It does not replace the saved historical result. **Close** or Escape
returns to the conversation. `/sources` is a shortcut to the latest source-backed
answer; per-answer buttons remain available for earlier turns. A casual answer
or a follow-up using memory alone may have no new source button.

### Close and return to the workspace

On first launch, `tui_example/raven_home/` is created. The demo's knowledges,
conversations, and operation records are stored inside this home. Preserve the
directory when closing or updating the demo to retain documents and saved
conversations.

This home always lives beside `app.py`, regardless of where the application is
launched. Document paths work differently: a relative path entered during
ingestion starts from the terminal's working directory. With the sample saved
beside `tui_example/` and the application launched from that parent directory,
the ingestion path is simply `Reaction_wheel.txt`.

On reopening, documents and conversations remain available. Configure the models
again and select a conversation. Model residency is
managed separately by Ollama; that service is not shut down when the demo exits.

Press Ctrl+C to close the application gracefully. Run the launch command in
Step 1 again to return to the workspace, then use `/conversations` to open the
saved conversation and its source-backed answers.

This completes the application workflow. The following sections examine the
source files and explain how each interaction is connected to RAVEN.

## 3. Rebuilding the TUI

Section 2 runs the complete application. This guide begins a separate walkthrough
of rebuilding its RAVEN integration; it is not an additional setup step required
to use the downloaded demo.

The application needs a way to turn a request in the terminal into backend
work, then bring the outcome back to the screen. We start with that connection,
rather than the terminal layout. Textual supplies the controls; RAVEN supplies
the knowledge base, models, conversations, agent execution, and persistence.

For a concrete first feature, we build the connection for **creating a knowledge**.
It establishes the pattern we will reuse for ingestion and chat:

1. The interface collects the inputs.
2. An application-side adapter calls the corresponding `Raven` method.
3. RAVEN performs the work and exposes its result and, for operation-backed
   work, its events.
4. The interface renders those outputs without implementing the backend itself.

The excerpts below are taken from the downloaded `tui_example/` source.
Methods belong to their named classes and use that file's imports and
surrounding definitions; they are not separate scripts. Repeated class declarations
identify the owner of each excerpt: the methods belong to a single class in the
corresponding file, not separate class definitions to add repeatedly. The expandable
complete-file panels provide the surrounding context. They show the finished
application, including features we will build in later sections.

### 3.1 Giving the application one RAVEN instance

We create an application-side adapter in `raven_client.py`. Its first
responsibility is to hold the backend instance that subsequent actions will use:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename">raven_client.py</figcaption>

```python
class RavenClient:

    def __init__(self, home: Path) -> None:
        self.home = home
        self.raven: Raven | None = None


    async def start(self) -> None:
        # Import the model/document dependencies without freezing the UI.
        module = await asyncio.to_thread(import_module, "nraven")
        self.raven = module.Raven(self.home)
        await self.raven.start()


    async def close(self) -> None:
        if self.raven is not None:
            await self.raven.close()
```

</figure>

The module imports `asyncio`, `Path` from `pathlib`, and `import_module`
from `importlib`. It uses postponed annotations and imports `Raven` under
`TYPE_CHECKING` for its type annotation; the actual backend import happens in
`start()`.

`module.Raven(self.home)` constructs the high-level API. `await self.raven.start()`
starts its owned backend resources. Later calls use this same instance, and
`close()` releases it when the application exits. Starting RAVEN does not
configure the models; that will be a separate action.

This adapter is named `RavenClient`, but it is **not an HTTP client**.
Everything runs in one Python process. The adapter is optional application glue:
it gives the TUI a small interface for calling RAVEN and preparing display data.
It neither owns a second knowledge database nor replaces RAVEN's managers.

In `app.py`, we anchor the home beside the application:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename">app.py</figcaption>

```python
RAVEN_HOME = Path(__file__).resolve().parent / "raven_home"
```

</figure>

That location is passed to `RavenClient`. The application will create the
directory and call `start()` during startup. Section 4 will connect those
lifecycle calls to the running interface, including readiness and shutdown.

### 3.2 Passing form inputs to a RAVEN operation

The creation form supplies two values: the knowledge's `name` and `summary`.
We connect those inputs to RAVEN through this method in `RavenClient`:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename">raven_client.py</figcaption>

```python
class RavenClient:

    async def create_knowledge(self, name: str, summary: str) -> dict[str, Any]:
        assert self.raven is not None
        task = await self.raven.create_knowledge(name, summary)
        knowledge = await task.result()
        return knowledge.meta
```

</figure>

The important boundary is the call to `self.raven.create_knowledge(name, summary)`.
The adapter does not create directories, open Qdrant, or write metadata.
RAVEN performs those tasks.

This RAVEN method returns an `OperationTask`, not the knowledge itself.
`await task.result()` waits for that task to finish and returns the created
`Knowledge` object. If the task fails, awaiting its result raises the failure
instead of returning a successful object.

The adapter then returns `knowledge.meta`, the metadata dictionary needed by
the interface. The knowledge object and its resources remain managed by
RAVEN; the interface receives the information needed to display the outcome.

We therefore preserve an important distinction when building around the API:
**receiving a task is not confirmation that its work succeeded**.
A success message must follow the confirmed result.

### 3.3 Bringing the confirmed result back into the TUI

Next, we connect the form to `RavenDemo`, which will pass its values to the
adapter. When `/knowledges/create` is selected, `RavenDemo.submit_text()` mounts
`CreateKnowledgeForm` in the transcript with
`await chat.mount(CreateKnowledgeForm())`. The form is imported from
`widgets.py`; it collects the name and optional summary but does not call RAVEN.

We give the form a nested `Submitted` message to carry those values back to
the application:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename">widgets.py</figcaption>

```python
class CreateKnowledgeForm(Vertical):

    class Submitted(Message):

        def __init__(self, form: CreateKnowledgeForm, name: str, summary: str) -> None:
            super().__init__()
            self.form = form
            self.name = name
            self.summary = summary


    @on(Input.Submitted, ".knowledge-summary")
    @on(Button.Pressed, ".create-knowledge")
    def submit(self, event: Input.Submitted | Button.Pressed) -> None:
        event.stop()
        name = self.query_one(".knowledge-name", Input).value.strip()
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or not name.strip("_")
            or name.strip("_").casefold() == "create"
        ):
            self.show_error("Use letters, numbers, _ or -. The name 'create' is reserved for the command.")
            return
        summary = self.query_one(".knowledge-summary", Input).value.strip()
        self.post_message(self.Submitted(self, name, summary))
```

</figure>

This excerpt shows the message definition and submission method from the same
form class. Its input fields, error display, and imports are in the complete
file below. `Message` comes from `textual.message`; `on`, `Input`, and
`Button` provide the Textual event handlers shown here.

Clicking **Create**, or pressing Enter in the summary field, invokes `submit()`.
After checking the inputs, the form posts `Submitted(self, name, summary)`.
The message carries three pieces of information: the originating `form`, the
knowledge `name`, and the `summary`. Keeping the form reference lets the
application disable it, display an error, or remove it after success.

This is a **Textual UI message**, not a persisted RAVEN event. Because the form
is mounted within `RavenDemo`, its message bubbles to the application. We connect
it to a handler with `@on(CreateKnowledgeForm.Submitted)`:

{% include source_file.html name="widgets.py" source="tui_example/widgets.py" language="python" id="complete-widgets-py" %}

In `app.py`, we import `CreateKnowledgeForm` from `widgets`. The
`create_knowledge()` handler below receives the posted message, checks backend
readiness, marks the form busy, and schedules `save_knowledge()` as a managed
Textual worker. That worker passes the submitted values to the adapter:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename">app.py</figcaption>

```python
class RavenDemo(App[None]):

    @on(CreateKnowledgeForm.Submitted)
    def create_knowledge(self, event: CreateKnowledgeForm.Submitted) -> None:
        event.stop()
        if not self.backend_ready or self._backend_busy:
            event.form.show_error("Please wait for RAVEN to finish its current action.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self.query_one("#status", Static).update(f"Creating {event.name}…")
        self.run_worker(self.save_knowledge(event), name="Create knowledge", group="backend")


    async def save_knowledge(self, event: CreateKnowledgeForm.Submitted) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        try:
            metadata = await self.client.create_knowledge(event.name, event.summary)
            name = metadata["name"]
            self._knowledge_names = [item["name"] for item in await self.client.list_knowledges()]
            details = await self.client.knowledge_details(name)
            await event.form.remove()
            await chat.mount(ChatMessage("Created", f"Knowledge '{name}' was saved."))
            await chat.mount(KnowledgeDetails(details))
            self.query_one("#status", Static).update("Knowledge created · /knowledges to browse")
            self.query_one("#message", PromptEditor).focus()
        except Exception as error:
            event.form.show_error(str(error))
            self.query_one("#status", Static).update("Knowledge creation failed · Edit the name or cancel")
        finally:
            self._backend_busy = False
            if event.form.is_mounted:
                event.form.set_busy(False)
            chat.scroll_end(animate=False)
```

</figure>

`event.name` and `event.summary` are the values collected by the form.
After `self.client.create_knowledge()` returns, the worker has confirmed metadata,
not a guessed outcome. It uses the returned name to request the current
knowledge list and file details, then displays those backend records.

`ChatMessage` and `KnowledgeDetails` are display components from the demo.
Their role here is to present the confirmation and details; neither creates
the knowledge. If the call raises an exception, the form displays the error
and remains available for correction. The `finally` block restores the
interface's busy state.

The data flow is therefore:

**Form inputs → Textual `Submitted` message → `RavenDemo` handler and worker →
adapter → RAVEN operation task → knowledge metadata → refreshed list and details.**

We ask RAVEN for the refreshed state through the interface. The interface does not
assume that a button click changed the database, or read RAVEN's storage files
to discover what happened.

{% include source_file.html name="app.py" source="tui_example/app.py" language="python" id="complete-app-py" %}

### 3.4 Adding progress without replacing the result

Knowledge creation needs little progress display, but document ingestion may
take much longer. We keep the same request/result boundary and add an event path
alongside it.

In `RavenClient`, we add an `ingest()` method that checks model readiness,
validates the source path, and consumes the RAVEN operation:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename">raven_client.py</figcaption>

```python
class RavenClient:

    async def ingest(
        self,
        knowledge_name: str,
        source_path: str,
        on_progress: Callable[[str, str], Awaitable[None]],
    ) -> dict[str, Any]:
        from nraven.core.events import EventType

        if not self.models_ready:
            raise RuntimeError("Configure both models with /models/configure before ingestion.")
        assert self.raven is not None
        text = source_path.strip()
        if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
            text = text[1:-1]

        def validate_path() -> Path:
            path = Path(text).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(f"Source file '{path}' was not found.")
            if path.suffix.lower() not in {".txt", ".md", ".pdf", ".docx"}:
                raise ValueError("This demo accepts .txt, .md, .pdf, and .docx files.")
            return path

        path = await asyncio.to_thread(validate_path)
        task = await self.raven.ingest(knowledge_name, path)
        try:
            async for event in task.events():
                if event.type == EventType.INGESTION_PROGRESS:
                    await on_progress(str(event.data.get("stage", "ingestion")), ingestion_progress_text(event.data))
            return await task.result()
        except BaseException:
            if not task.is_finished:
                await task.cancel()
            raise
```

</figure>

Here, `knowledge_name` is the target, `path` is the validated source file,
`EventType` comes from `nraven.core.events`, and `on_progress` is an async
callback supplied by the application. `ingestion_progress_text()`, defined in
the same adapter file, formats the event's stage and counts for display.

Two outputs now travel back from RAVEN:

- **Events** describe work while it happens. The adapter forwards
  `INGESTION_PROGRESS` as stage-specific display updates.
- **The task result** confirms the outcome. After consuming the events,
  `await task.result()` returns the ingestion result or raises the failure.

These are not interchangeable. A progress event is not proof that the whole
ingestion succeeded, and the final result does not replace live feedback.

In `RavenDemo.save_ingestion()`, we connect that callback to the visible progress
component. The method creates and mounts an `IngestionProgress` view, while
`event` carries the submitted knowledge name and source path:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename">app.py</figcaption>

```python
class RavenDemo(App[None]):

    async def save_ingestion(self, event: IngestionForm.Submitted) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        progress = IngestionProgress()
        try:
            await chat.mount(progress)
            chat.scroll_end(animate=False)
            self.query_one("#status", Static).update("Ingestion starting… · Stop ingestion to cancel")

            async def show_progress(stage: str, text: str) -> None:
                progress.update_progress(stage, text)
                self.query_one("#status", Static).update(text)

            result = await self.client.ingest(event.knowledge_name, event.source_path, show_progress)
        except asyncio.CancelledError:
            if self.is_running and progress.is_attached:
                progress.finish("Ingestion stopped. The file list below reflects any completed writes.")
                self.query_one("#status", Static).update("Ingestion stopped · You can edit the form and try again")
                await self.refresh_ingestion_details(event.knowledge_name)
            raise
        except Exception as error:
            if self.is_running and progress.is_attached:
                progress.finish(f"Ingestion failed: {error}")
                event.form.show_error(str(error))
                self.query_one("#status", Static).update("Ingestion failed · Edit the path or cancel")
        else:
            progress.finish(
                f"Ingested {result['file']} into {event.knowledge_name} · "
                f"{result['section_count']} sections · {result['chunk_count']} chunks"
            )
            await event.form.remove()
            refreshed = await self.refresh_ingestion_details(event.knowledge_name)
            self.query_one("#status", Static).update(
                "Ingestion completed · File details refreshed below" if refreshed
                else "Ingestion completed · File refresh failed; use /knowledges to check"
            )
            self.query_one("#message", PromptEditor).focus()
        finally:
            self._backend_busy = False
            self._ingestion_worker = None
            if event.form.is_attached:
                event.form.set_busy(False)
            if progress.is_attached:
                progress.query_one(".ingestion-spinner").display = False
                progress.query_one(".stop-ingestion", Button).disabled = True
            if self.is_running:
                chat.scroll_end(animate=False)
```

</figure>

The callback runs whenever the adapter forwards a relevant event. It updates
the stage display and status line; it does not parse the document, split
sections, generate embeddings, or write vectors. Those remain RAVEN's work.
The handler displays success only after `ingest()` returns, or reports
failure/cancellation. Section 6 will connect this method to the ingestion form
and examine the document workflow in detail.

The adapter's exception path also forwards cancellation to an unfinished
RAVEN task before propagating the exception. Cancelling a UI worker must not
silently leave the backend operation running.

{% include source_file.html name="raven_client.py" source="tui_example/raven_client.py" language="python" id="complete-raven-client-py" %}

### 3.5 Reusing the boundary for the remaining features

The rest of the application follows the same division of responsibility.
The form or message box changes; the backend remains behind the `Raven` API.

| Interface action | Information sent to RAVEN | Information returned to the interface |
| --- | --- | --- |
| Configure models | LLM and embedding `ModelSpec` values | Configuration events and the confirmed task result. |
| Ingest a document | Knowledge name and local source path | Stage progress and the final ingestion result. |
| Open a conversation | Conversation ID | Conversation metadata and canonical saved messages, rather than compacted model context. |
| Send a question | A temporary session over the active conversation, the query, and `retrieval_mode="auto"` | Separate thinking, tool-activity, and answer events, followed by the collected run result. |
| Inspect an answer's sources | Conversation ID and turn ID | A saved reconstruction, or reconstructed files from that turn's evidence. |

Chat will use `run.stream` and `run.collect()` rather than the ingestion
task's `events()` and `result()` calls. The integration principle stays the
same: we display live events, then confirm completion through the corresponding
result API. The chat section will also distinguish answer generation from
successful turn persistence.

No part of this arrangement requires the interface to implement a retrieval
pipeline or an agent loop. The interface supplies inputs and presents outputs;
RAVEN manages the work between them. With that boundary established, the next
step is to connect backend startup and shutdown to the application lifecycle.
