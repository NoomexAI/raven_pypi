---
layout: default
title: In action
description: "Build a terminal document assistant with RAVEN and Textual, from document ingestion to streamed answers and source inspection."
scripts:
  - /assets/js/source-links.js
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

We create an application-side adapter in [`raven_client.py`](#complete-raven-client-py "raven_client.py"). Its first
responsibility is to hold the backend instance that subsequent actions will use:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

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
[`start()`](#complete-raven-client-py "raven_client.py").

`module.Raven(self.home)` constructs the high-level API. `await self.raven.start()`
starts its owned backend resources. Later calls use this same instance, and
[`close()`](#complete-raven-client-py "raven_client.py") releases it when the application exits. Starting RAVEN does not
configure the models; that will be a separate action.

This adapter is named [`RavenClient`](#complete-raven-client-py "raven_client.py"), but it is **not an HTTP client**.
Everything runs in one Python process. The adapter is optional application glue:
it gives the TUI a small interface for calling RAVEN and preparing display data.
It neither owns a second knowledge database nor replaces RAVEN's managers.

In [`app.py`](#complete-app-py "app.py"), we anchor the home beside the application:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
RAVEN_HOME = Path(__file__).resolve().parent / "raven_home"
```

</figure>

That location is passed to [`RavenClient`](#complete-raven-client-py "raven_client.py"). The application will create the
directory and call [`start()`](#complete-raven-client-py "raven_client.py") during startup. Section 4 will connect those
lifecycle calls to the running interface, including readiness and shutdown.

### 3.2 Passing form inputs to a RAVEN operation

The creation form supplies two values: the knowledge's `name` and `summary`.
We connect those inputs to RAVEN through this method in [`RavenClient`](#complete-raven-client-py "raven_client.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

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

Next, we connect the form to [`RavenDemo`](#complete-app-py "app.py"), which will pass its values to the
adapter. When `/knowledges/create` is selected, [`RavenDemo.submit_text()`](#complete-app-py "app.py") mounts
[`CreateKnowledgeForm`](#complete-widgets-py "widgets.py") in the transcript with
[`await chat.mount(CreateKnowledgeForm())`](#complete-widgets-py "widgets.py"). The form is imported from
[`widgets.py`](#complete-widgets-py "widgets.py"); it collects the name and optional summary but does not call RAVEN.

We give the form a nested [`Submitted`](#complete-widgets-py "widgets.py") message to carry those values back to
the application:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-widgets-py">widgets.py</a></figcaption>

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

Clicking **Create**, or pressing Enter in the summary field, invokes [`submit()`](#complete-widgets-py "widgets.py").
After checking the inputs, the form posts [`Submitted(self, name, summary)`](#complete-widgets-py "widgets.py").
The message carries three pieces of information: the originating `form`, the
knowledge `name`, and the `summary`. Keeping the form reference lets the
application disable it, display an error, or remove it after success.

This is a **Textual UI message**, not a persisted RAVEN event. Because the form
is mounted within [`RavenDemo`](#complete-app-py "app.py"), its message bubbles to the application. We connect
it to a handler with [`@on(CreateKnowledgeForm.Submitted)`](#complete-widgets-py "widgets.py"):

{% include source_file.html name="widgets.py" source="tui_example/widgets.py" language="python" id="complete-widgets-py" %}

In [`app.py`](#complete-app-py "app.py"), we import [`CreateKnowledgeForm`](#complete-widgets-py "widgets.py") from [`widgets`](#complete-widgets-py "widgets.py"). The
[`create_knowledge()`](#complete-app-py "app.py") handler below receives the posted message, checks backend
readiness, marks the form busy, and schedules [`save_knowledge()`](#complete-app-py "app.py") as a managed
Textual worker. That worker passes the submitted values to the adapter:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

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
After [`self.client.create_knowledge()`](#complete-raven-client-py "raven_client.py") returns, the worker has confirmed metadata,
not a guessed outcome. It uses the returned name to request the current
knowledge list and file details, then displays those backend records.

[`ChatMessage`](#complete-app-py "app.py") and [`KnowledgeDetails`](#complete-widgets-py "widgets.py") are display components from the demo.
Their role here is to present the confirmation and details; neither creates
the knowledge. If the call raises an exception, the form displays the error
and remains available for correction. The `finally` block restores the
interface's busy state.

The data flow is therefore:

**Form inputs → Textual [`Submitted`](#complete-widgets-py "widgets.py") message → [`RavenDemo`](#complete-app-py "app.py") handler and worker →
adapter → RAVEN operation task → knowledge metadata → refreshed list and details.**

We ask RAVEN for the refreshed state through the interface. The interface does not
assume that a button click changed the database, or read RAVEN's storage files
to discover what happened.

{% include source_file.html name="app.py" source="tui_example/app.py" language="python" id="complete-app-py" %}

### 3.4 Adding progress without replacing the result

Knowledge creation needs little progress display, but document ingestion may
take much longer. We keep the same request/result boundary and add an event path
alongside it.

In [`RavenClient`](#complete-raven-client-py "raven_client.py"), we add an [`ingest()`](#complete-raven-client-py "raven_client.py") method that checks model readiness,
validates the source path, and consumes the RAVEN operation:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

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
callback supplied by the application. [`ingestion_progress_text()`](#complete-raven-client-py "raven_client.py"), defined in
the same adapter file, formats the event's stage and counts for display.

Two outputs now travel back from RAVEN:

- **Events** describe work while it happens. The adapter forwards
  `INGESTION_PROGRESS` as stage-specific display updates.
- **The task result** confirms the outcome. After consuming the events,
  `await task.result()` returns the ingestion result or raises the failure.

These are not interchangeable. A progress event is not proof that the whole
ingestion succeeded, and the final result does not replace live feedback.

In [`RavenDemo.save_ingestion()`](#complete-app-py "app.py"), we connect that callback to the visible progress
component. The method creates and mounts an [`IngestionProgress`](#complete-ingestion-widgets-py "ingestion_widgets.py") view, while
`event` carries the submitted knowledge name and source path:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

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
The handler displays success only after [`ingest()`](#complete-raven-client-py "raven_client.py") returns, or reports
failure/cancellation. Section 6 will connect this method to the ingestion form
and examine the document workflow in detail.

The adapter's exception path also forwards cancellation to an unfinished
RAVEN task before propagating the exception. Cancelling a UI worker must not
silently leave the backend operation running.

{% include source_file.html name="ingestion_widgets.py" source="tui_example/ingestion_widgets.py" language="python" id="complete-ingestion-widgets-py" %}

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

## 4. Connecting RAVEN's lifecycle to the application

Section 3 established how a user action reaches RAVEN and how its result returns
to the interface. That connection assumes the backend is ready. We now connect
its lifetime to the application's: startup begins when the TUI opens, actions
check whether startup succeeded, and shutdown releases the resources that RAVEN
owns.

We keep the [`RavenClient.start()`](#complete-raven-client-py "raven_client.py") and [`close()`](#complete-raven-client-py "raven_client.py") methods already introduced in
Section 3.1. This section builds the application side of that connection in
[`RavenDemo`](#complete-app-py "app.py"). As before, the class declarations identify the owner of each
excerpt; they are not additional classes to define in the same file.

### 4.1 Preparing the backend owner and startup state

We create one adapter for the lifetime of the application. The backend-related
beginning of [`RavenDemo.__init__()`](#complete-app-py "app.py") is:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    def __init__(self, *, connect_backend: bool = True) -> None:
        super().__init__()
        self.raven_home = RAVEN_HOME
        self._menu_dismissed_for: str | None = None
        self.client = RavenClient(self.raven_home) if connect_backend else None
        self.backend_ready = False
        self._startup_complete = asyncio.Event()
        self._backend_busy = False
        self._knowledge_names: list[str] = []
```

</figure>

This excerpt uses the `RAVEN_HOME` constant and [`RavenClient`](#complete-raven-client-py "raven_client.py") from Section 3.
`App` comes from `textual.app`, and `asyncio` supplies the startup signal.
The rest of the constructor initializes conversation and worker references for
later features; it is available in the [complete `app.py` source](#complete-app-py "app.py").

The adapter exists immediately, but its `Raven` instance is created by
[`await self.client.start()`](#complete-raven-client-py "raven_client.py") later. Creating [`RavenDemo`](#complete-app-py "app.py") therefore does not
mean the backend has finished opening its resources.

We use two fields to distinguish that progress:

- `backend_ready` records whether backend startup and the initial knowledge
  listing succeeded.
- `_startup_complete` is an `asyncio.Event` that lets a waiting action continue
  once the startup attempt has ended, whether it succeeded or not.

This `asyncio.Event` is an in-process coordination signal, not a persisted
RAVEN event or a Textual UI message. Its `wait()` method suspends the waiting
coroutine without blocking the application's event loop.

`_backend_busy` serves a different purpose: it tracks the demo's one-action-at-a-time
policy after an action is accepted. It is not a substitute for startup readiness.

The normal launch uses `connect_backend=True`. The optional false value creates
a UI-only preview without an adapter; it is not the document-assistant workflow
being built here.

### 4.2 Starting after the interface has mounted

Textual calls [`on_mount()`](#complete-app-py "app.py") when the application has mounted its controls.
We use that point to start RAVEN, so the status line is already available while
the backend opens.

The backend-related end of [`RavenDemo.on_mount()`](#complete-app-py "app.py") is:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def on_mount(self) -> None:
        if self.client is not None:
            self.query_one("#status", Static).update("Starting RAVEN · No models are loaded")
            self.run_worker(self.start_backend(), name="RAVEN startup", group="backend")
```

</figure>

The earlier statements in the complete method initialize the terminal controls;
they do not change the RAVEN connection. We show only the backend portion here.

`self.run_worker()` schedules [`start_backend()`](#complete-app-py "app.py") as a managed Textual worker.
The mount handler can return while startup continues, leaving the interface
able to render its status and process input. This worker is not a RAVEN
`OperationTask`, nor does scheduling it automatically move synchronous work
into another thread.

Inside the adapter, the dependency import uses `asyncio.to_thread()`, as shown
in Section 3.1. We then await `Raven.start()` through its asynchronous API.
The split keeps application scheduling in the TUI and backend initialization
inside RAVEN.

### 4.3 Reporting startup success or failure

We put the startup sequence and its visible outcome in [`start_backend()`](#complete-app-py "app.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def start_backend(self) -> None:
        assert self.client is not None
        try:
            await asyncio.to_thread(self.raven_home.mkdir, exist_ok=True)
            await self.client.start()
            self._knowledge_names = [item["name"] for item in await self.client.list_knowledges()]
            self.backend_ready = True
            self.query_one("#status", Static).update("Ready · /knowledges to browse · No models loaded")
        except Exception as error:
            self.query_one("#welcome").display = False
            chat = self.query_one("#chat", VerticalScroll)
            await chat.mount(ChatMessage("Backend error", str(error)))
            chat.scroll_end(animate=False)
            self.query_one("#status", Static).update("RAVEN could not start · Close and reopen after fixing the error")
        finally:
            self._startup_complete.set()
```

</figure>

The sequence has four responsibilities:

1. **Ensure the home exists.** We create `raven_home/` beside [`app.py`](#complete-app-py "app.py"),
   using the location already assigned to `self.raven_home`. The filesystem
   call runs through `asyncio.to_thread()`.
2. **Start RAVEN.** The adapter constructs its `Raven` instance and awaits
   `Raven.start()`. RAVEN loads the runtime settings, recovers operation
   history, and starts its knowledge and conversation managers.
3. **Read the initial knowledge names.** We use RAVEN's list API to populate
   the command suggestions, rather than scanning storage from the interface.
4. **Publish readiness in the UI.** Only after those steps succeed do we set
   `backend_ready=True` and display the ready status.

An empty knowledge list is a successful startup. It means there are no
knowledges yet, not that RAVEN is unavailable. The knowledge list component
introduced in the example presents that empty state separately.

If startup or the initial list request raises an exception, we leave
`backend_ready` false, display the exception in the transcript, and show a
failure status. This demo asks for the underlying problem to be corrected and
the application reopened; it does not silently retry startup.

The `finally` block sets `_startup_complete` even when startup fails.
Otherwise, an action already waiting for startup could remain suspended
without learning that the attempt had ended.

**Backend readiness is not model readiness.** At this point, knowledge and
conversation management can work, but no model pair has been configured.
Ingestion and chat have their own model-readiness checks. We will connect the
model configuration form in Section 5 rather than loading models as an
unannounced side effect of opening the TUI.

### 4.4 Keeping requests behind the readiness check

Once startup runs independently of the interface, an action can arrive before
it finishes. We handle that explicitly rather than letting the adapter be
called with an uninitialized backend.

For knowledge browsing, we wait for the attempt to end and then check whether
it succeeded:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def show_knowledge_route(self, path: str) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        try:
            await self._startup_complete.wait()
            if not self.backend_ready:
                raise RuntimeError("RAVEN could not start. Check the backend error above.")
            if path.casefold() == "/knowledges":
                knowledges = await self.client.list_knowledges()
                self._knowledge_names = [item["name"] for item in knowledges]
                await chat.mount(KnowledgeList(knowledges))
            else:
                details = await self.client.knowledge_details(path.split("/")[2])
                await chat.mount(KnowledgeDetails(details))
            self.query_one("#status", Static).update("Ready · Knowledge view only; conversation scope is unchanged")
        except Exception as error:
            await chat.mount(ChatMessage("Knowledge error", str(error)))
            self.query_one("#status", Static).update("Knowledge action failed · You can try another command")
        finally:
            self._backend_busy = False
            chat.scroll_end(animate=False)
```

</figure>

This method uses [`KnowledgeList`](#complete-widgets-py "widgets.py") and [`KnowledgeDetails`](#complete-widgets-py "widgets.py") from [`widgets.py`](#complete-widgets-py "widgets.py"),
and [`ChatMessage`](#complete-app-py "app.py") from [`app.py`](#complete-app-py "app.py"), to display the outcome. The important backend
boundary comes before either list or details request:
`await self._startup_complete.wait()` followed by the `backend_ready` check.

The two values express different facts:

| Startup state | `_startup_complete` is set | `backend_ready` |
| --- | --- | --- |
| Not started or still starting | No | `False` |
| Startup succeeded | Yes | `True` |
| Startup failed | Yes | `False` |

The signal alone does not authorize a backend call. It says that the wait can
end; the boolean determines whether the request can proceed. A failed startup
therefore produces an error in the interface rather than an attempt to browse
an unopened backend.

Not every action waits. The creation handler in Section 3.3 checks
`backend_ready` immediately and keeps the form available if RAVEN is not ready.
We use waiting for a read-only browse request and immediate feedback for form
submission; neither path assumes that constructing the adapter made it usable.

### 4.5 Closing the owned resources

When the application is unmounted during a normal exit, we delegate closure
through the same adapter:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def on_unmount(self) -> None:
        if self.client is not None:
            await self.client.close()
```

</figure>

The chain is [`RavenDemo.on_unmount()`](#complete-app-py "app.py") → [`RavenClient.close()`](#complete-raven-client-py "raven_client.py") →
`Raven.close()`. The interface does not close individual databases or provider
adapters itself. RAVEN handles cancellation of active operations, pending
knowledge reconciliation, and closure of its managers, provider resources,
and operation storage.

We await this cleanup rather than treating application exit as an instruction
to discard every running task immediately. Cancellation may need time to reach
a safe boundary, and cleanup errors are not evidence of a successful shutdown.

This is a graceful-exit path. A forcibly terminated process cannot be relied
on to run [`RavenDemo.on_unmount()`](#complete-app-py "app.py") or `Raven.close()`. RAVEN's persistent storage and startup
recovery address interrupted work separately; shutdown hooks do not make a
hard crash equivalent to normal closure.

Closing RAVEN also **does not stop the external Ollama service**. The
application owns its RAVEN instance, not the Ollama process. Managing that
service remains outside this demo.

We now have a complete lifecycle around the connection from Section 3:
the interface opens, RAVEN starts, requests check readiness, and normal exit
closes the owned backend resources. Next, we will connect model specifications
from the configuration form to that running instance.

## 5. Configuring the model pair

RAVEN is now running, but ingestion and chat still need models. We keep their
selection separate from startup: the application opens without downloading
weights or choosing a cloud provider on the reader's behalf.

We build this action around the same boundary as knowledge creation:
the form collects values, [`RavenDemo`](#complete-app-py "app.py") forwards them through the adapter,
and RAVEN performs configuration as observable operation-backed work.
The difference is that we now supply **two model specifications** and display
progress while they are being applied.

### 5.1 Collecting the two model roles

We use [`ModelConfigurationForm`](#complete-model-widgets-py "model_widgets.py") from [`model_widgets.py`](#complete-model-widgets-py "model_widgets.py") for the form shown in
Section 2. When `/models/configure` is opened,
[`RavenDemo.show_model_route()`](#complete-app-py "app.py") waits for startup and mounts
an instance of [`ModelConfigurationForm`](#complete-model-widgets-py "model_widgets.py"), initialized with [`self.client.configured_models()`](#complete-raven-client-py "raven_client.py"). Passing the current
configuration lets the form prefill the provider and model names when changing
an existing pair.

Each column describes one role:

| Form value | Meaning in the RAVEN request |
| --- | --- |
| LLM provider and model | The service and model used for metadata extraction, agent tool use, and response generation. |
| Embedding provider and model | The service and model used to create and search vectors, including document processing and conversation memory. |
| Role | Assigned by the form as `"llm"` or `"embedding"`; it is not typed into a field. |
| API key reference | An optional environment-variable name used to resolve credentials, not the credential itself. |

The two roles do not have to share a provider. For example, the cloud-LLM setup
from Section 2 keeps the embedding model local. The TUI collects provider
identifiers rather than deciding how to instantiate each provider's adapter.

We give the form a submission message and collect the fields as follows:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-model-widgets-py">model_widgets.py</a></figcaption>

```python
class ModelConfigurationForm(Vertical):

    class Submitted(Message):

        def __init__(self, form: ModelConfigurationForm, specs: dict[str, dict[str, Any]]) -> None:
            super().__init__()
            self.form = form
            self.specs = specs


    @on(Button.Pressed, ".configure-models")
    def submit(self) -> None:
        specs: dict[str, dict[str, Any]] = {}
        for role in ("llm", "embedding"):
            provider = self.query_one(f"#{role}-provider", Input).value.strip().lower()
            model = self.query_one(f"#{role}-model", Input).value.strip()
            key_ref = self.query_one(f"#{role}-key-ref", Input).value.strip()
            if not provider or not model:
                self.show_error(f"Provide a provider and model name for {role}.")
                return
            if key_ref and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key_ref):
                self.show_error("API key reference must be an environment-variable name, not the key itself.")
                return
            specs[role] = {"provider": provider, "model": model, "role": role, "api_key_ref": key_ref or None}
        self.post_message(self.Submitted(self, specs))
```

</figure>

As with [`CreateKnowledgeForm.Submitted`](#complete-widgets-py "widgets.py"), this is a Textual UI message.
`event.specs` will contain two dictionaries, under `"llm"` and `"embedding"`,
and `event.form` will identify the form that submitted them.

The method strips surrounding whitespace, normalizes the provider to lowercase,
and checks that a provider and model were supplied for each role. A nonempty
key reference must have the syntax of an environment-variable name.
An empty reference becomes `None`.

These are input checks, not a provider connection test. They do not establish
that the model exists, that a secret is present in the environment, or that an
account has access to the model. Those concerns remain with RAVEN and the
selected provider.

The excerpt uses `on`, `Button`, `Input`, `Vertical`, and `Message` from
Textual, `Any` from `typing`, and `re` for the reference check. The full module
also defines the controls and result views used below:

{% include source_file.html name="model_widgets.py" source="tui_example/model_widgets.py" language="python" id="complete-model-widgets-py" %}

### 5.2 Connecting the submitted pair to the application

In [`app.py`](#complete-app-py "app.py"), we import [`ModelConfigurationForm`](#complete-model-widgets-py "model_widgets.py"), [`ModelProgress`](#complete-model-widgets-py "model_widgets.py"), and
[`ConfiguredModels`](#complete-model-widgets-py "model_widgets.py") from [`model_widgets`](#complete-model-widgets-py "model_widgets.py"). We handle the form's message in
[`RavenDemo`](#complete-app-py "app.py"), then run the configuration worker:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    @on(ModelConfigurationForm.Submitted)
    def configure_models(self, event: ModelConfigurationForm.Submitted) -> None:
        event.stop()
        if not self.backend_ready or self._backend_busy:
            event.form.show_error("Please wait for RAVEN to finish its current action.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self.run_worker(self.save_model_configuration(event), name="Configure models", group="backend")


    async def save_model_configuration(self, event: ModelConfigurationForm.Submitted) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        progress = ModelProgress()
        try:
            await chat.mount(progress)
            self.query_one("#status", Static).update("Configuring models… · Ctrl+C closes the demo and cancels the operation")

            async def show_progress(key: str, text: str) -> None:
                progress.update_progress(key, text)
                self.query_one("#status", Static).update(text)

            await self.client.configure_models(
                event.specs["llm"], event.specs["embedding"], show_progress,
            )
            progress.update_progress("done", "Model pair configured successfully.")
            await event.form.remove()
            await chat.mount(ConfiguredModels(self.client.configured_models()))
            self.query_one("#status", Static).update("Models configured · Attach a document or use /knowledges/<name>/ingest")
            self.query_one("#message", PromptEditor).focus()
        except Exception as error:
            progress.update_progress("failed", f"Configuration failed: {error}")
            event.form.show_error(str(error))
            self.query_one("#status", Static).update("Model configuration failed · Edit the form or cancel")
        finally:
            self._backend_busy = False
            if event.form.is_mounted:
                event.form.set_busy(False)
            chat.scroll_end(animate=False)
```

</figure>

The decorated [`configure_models()`](#complete-app-py "app.py") method receives the posted form message.
It checks backend readiness and the demo's busy flag before accepting the action.
Once accepted, we disable the form and schedule [`save_model_configuration()`](#complete-app-py "app.py").
The worker receives the **same message**, so the two specifications and their
originating form remain connected.

Inside the worker, we mount a [`ModelProgress`](#complete-model-widgets-py "model_widgets.py") view and define an async
[`show_progress(key, text)`](#complete-app-py "app.py") callback. That callback is passed to
[`self.client.configure_models()`](#complete-raven-client-py "raven_client.py") alongside `event.specs["llm"]` and
`event.specs["embedding"]`.

This creates the return path for progress without putting provider logic in
the controls. The adapter will supply a display key and message; the callback
updates the progress view and status line.

After the adapter returns successfully, we remove the form and display
[`ConfiguredModels`](#complete-model-widgets-py "model_widgets.py") with the data from [`self.client.configured_models()`](#complete-raven-client-py "raven_client.py"). The interface reads the
configured pair back from RAVEN rather than treating the submitted values as
proof of success.

If configuration raises an ordinary exception, the worker displays the failure
and leaves the form available for correction. Its `finally` block clears the
busy flag and re-enables a form that is still mounted. The handler and worker
are part of the [complete `app.py` source](#complete-app-py "app.py").

### 5.3 Validating specifications and calling RAVEN

The adapter is where form dictionaries become RAVEN's typed model specifications.
We add this method to [`RavenClient`](#complete-raven-client-py "raven_client.py") in [`raven_client.py`](#complete-raven-client-py "raven_client.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
class RavenClient:

    async def configure_models(
        self,
        llm: dict[str, Any],
        embedding: dict[str, Any],
        on_progress: Callable[[str, str], Awaitable[None]],
    ) -> dict[str, Any]:
        from nraven.providers import ModelSpec

        assert self.raven is not None
        task = await self.raven.configure_models(
            ModelSpec.model_validate(llm),
            ModelSpec.model_validate(embedding),
        )
        try:
            async for event in task.events():
                message = model_progress_text(event.type.value, event.data)
                if message is not None:
                    await on_progress(f"{event.type.value}:{event.data.get('model', '')}", message)
            return await task.result()
        except BaseException:
            # A closing UI must not leave its configuration operation running.
            if not task.is_finished:
                await task.cancel()
            raise
```

</figure>

`ModelSpec` comes from `nraven.providers`. Its `model_validate()` method
validates each dictionary as a provider, model, role, optional key reference,
and options. The form does not expose advanced options, so the specifications
use the default empty options dictionary.

We pass the validated objects to `Raven.configure_models()`. RAVEN checks
that the first specification has the LLM role and the second has the embedding
role, and returns an `OperationTask` for the configuration work. The
application does not instantiate Ollama or LiteLLM adapters itself.

While consuming `task.events()`, we translate selected backend events through
[`model_progress_text()`](#complete-raven-client-py "raven_client.py") and forward only the resulting display messages to the
callback. We then await `task.result()` to confirm the configuration outcome.
The result identifies the configured provider, model, and role for each member
of the pair; it does not return API keys.

In this demo, the worker does not need that result dictionary directly. It uses
the successful return as confirmation, then reads the current configuration
through the adapter. Awaiting the result is still necessary: finishing event
iteration alone is not an instruction to display success.

The exception path checks for an unfinished task and requests its cancellation
before propagating the exception. This also handles cancellation of the waiting
worker, so closing the interface does not intentionally abandon an active
configuration operation.

`Any`, `Callable`, and `Awaitable` are imported in the
[complete adapter module](#complete-raven-client-py "raven_client.py"). The callback signature
`Callable[[str, str], Awaitable[None]]` means that it receives two strings and
can be awaited.

### 5.4 Translating model events into visible progress

The adapter's formatter is a module-level function, not a method of
[`RavenClient`](#complete-raven-client-py "raven_client.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
def model_progress_text(event_type: str, data: dict[str, Any]) -> str | None:
    """Render useful milestones, never raw provider payloads or credentials."""
    model = data.get("model") or "model"
    actions = {
        "model.load_llm": "Preparing LLM adapter",
        "model.load_embedding": "Preparing embedding adapter",
        "model.preload_llm": "Loading LLM into Ollama memory",
        "model.preload_embedding": "Loading embedding model into Ollama memory",
        "model.unload_llm": "Unloading previous LLM",
        "model.unload_embedding": "Unloading previous embedding model",
        "model.pull": "Downloading model",
    }
    if event_type == "model.pull.progress":
        completed, total = data.get("completed"), data.get("total")
        if isinstance(completed, (int, float)) and isinstance(total, (int, float)) and total > 0:
            return f"Downloading {model}: {min(100, max(0, completed / total * 100)):.0f}%"
        return f"Downloading {model}: {data.get('status') or 'in progress'}"
    action, _, stage = event_type.rpartition(".")
    if action in actions and stage in {"started", "completed", "failed"}:
        return f"{actions[action]} · {model} · {stage}"
    return None
```

</figure>

For example, `model.load_llm.started` becomes a message about preparing the
LLM adapter, while `model.preload_llm.started` describes loading it into
Ollama memory. Keeping those messages distinct avoids confusing an adapter
object with resident model weights.

For downloads, the function uses byte counts when available; otherwise it
displays the reported download status. Events outside this display mapping
return `None` and are not added to the progress view. The operation still owns
its complete event stream—the TUI is choosing what to present, not changing
what RAVEN publishes.

The callback key combines the event type and model name. [`ModelProgress`](#complete-model-widgets-py "model_widgets.py")
replaces the latest entry when that key repeats and retains its last eight
entries. Repeated download updates therefore update a compact display instead
of adding a transcript message for every progress event.

The formatter selects a small set of fields rather than printing the raw event
dictionary. This keeps the interface focused on model names, stages, and
download progress rather than provider-specific payloads.

### 5.5 Leaving provider behavior inside RAVEN

The same form-to-adapter connection supports both local and cloud
specifications. RAVEN's provider layer chooses the implementation from
`ModelSpec.provider`:

- With `provider="ollama"`, RAVEN connects to the separately running Ollama
  service. It checks model availability, pulls missing weights, and prepares
  the LlamaIndex adapters. Configuration unloads replaced Ollama models that
  are no longer in the selected pair and preloads the selected local models.
- For other supported provider identifiers, RAVEN uses its LiteLLM integration.
  Configuration prepares remote-model adapters; it does not download those
  models' weights or place them in local VRAM.

We therefore do not need separate cloud and local form handlers. A mixed pair
follows the same configuration operation, with the appropriate behavior applied
to each role.

For cloud models, the provider value must be the supported LiteLLM routing
identifier, not simply a company's display name. The LLM must support the tool
calling needed by the agent, as well as the other model capabilities RAVEN uses.
A completed configuration is not an end-to-end chat test: authentication,
quota, or model-access problems can still appear when remote inference is
requested.

The reference field preserves the boundary around credentials.
`YOUR_API_KEY` is placeholder text, not a default variable that RAVEN creates.
In Section 2, the actual reference is `RAVEN_DEMO_API_KEY`. We pass that
**name** in `api_key_ref`; RAVEN's provider layer looks up the secret in the
process environment. The TUI does not ask for the secret in its form or load
a `.env` file. The environment must be supplied before launching the application,
as in Section 2's cloud setup.

Cloud inference also changes where document material is processed. A cloud LLM
can receive document text during metadata extraction and chat; a cloud
embedding provider receives text submitted for embeddings. Local storage does
not make those remote requests local.

> **We choose the embedding model before the first ingestion and keep it
> unchanged for the stored data.** Vectors must be searched with the model
> that generated them. A different model is not interchangeable merely because
> it returns the same number of dimensions. Changing it requires rebuilding
> the affected embeddings, including relevant conversation memory. The LLM
> can be changed independently.

### 5.6 Reading configuration without implying VRAM residency

Finally, we expose the current configuration and the adapter-readiness check
through [`RavenClient`](#complete-raven-client-py "raven_client.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
class RavenClient:

    def configured_models(self) -> dict[str, Any]:
        assert self.raven is not None
        return self.raven.runtime_status()["models"]


    @property
    def models_ready(self) -> bool:
        return self.raven is not None and self.raven.models_loaded
```

</figure>

`/models` renders these configured identities through [`ConfiguredModels`](#complete-model-widgets-py "model_widgets.py").
It is not a live memory monitor. Likewise, [`models_ready`](#complete-raven-client-py "raven_client.py") checks whether
RAVEN holds both model adapters; it does not prove that both local models
remain resident in GPU memory or that a cloud request will succeed.

`/providers/ollama/models` answers another question: which models are installed
in Ollama. The adapter obtains that list through `Raven.list_ollama_models()`
and awaits its task result. Listing installed models does not configure a pair
or load them into memory.

The demo does not restore the model pair when restarted. Knowledge data and
conversations remain on disk, but a new application instance needs explicit
configuration again. This keeps model selection separate from opening existing
data and avoids silently choosing a provider or triggering model loading.

The complete information flow is now:

**Form fields → submitted pair → `ModelSpec` validation → RAVEN configuration
operation → progress callback → confirmed configured-model view.**

We have connected model selection without putting model management into the
TUI. Next, we will use the configured pair to process a document and connect
ingestion progress to the transcript.

## 6. Creating a knowledge and ingesting a document

With the model pair configured, we can connect a document to a persistent
knowledge. We have already built knowledge creation in Section 3. Here, we
complete the surrounding workflow: browsing the saved knowledge, choosing it
as an ingestion target, submitting a source file, and reflecting RAVEN's
confirmed outcome in the interface.

We keep the distinction between a **knowledge** and a **conversation** explicit.
Ingestion always needs a target knowledge. It does not require an open
conversation, and adding a document does not create or change a conversation's
scope.

### 6.1 Reading the knowledge state through RAVEN

The creation path remains [`CreateKnowledgeForm`](#complete-widgets-py "widgets.py") → [`RavenDemo.create_knowledge()`](#complete-app-py "app.py")
→ [`RavenDemo.save_knowledge()`](#complete-app-py "app.py") → [`RavenClient.create_knowledge()`](#complete-raven-client-py "raven_client.py"), as shown in
Section 3.3. We await the creation task's result before confirming that the
knowledge was saved. Creating or browsing a knowledge does not itself require
configured models.

Next, we give the interface the records it needs to browse that knowledge.
The adapter methods are:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
class RavenClient:

    async def list_knowledges(self) -> list[dict[str, Any]]:
        assert self.raven is not None
        knowledges = await self.raven.list_knowledges()
        return [
            {
                **knowledge,
                "file_count": len(await self.raven.list_knowledge_files(knowledge["name"])),
            }
            for knowledge in knowledges
        ]


    async def knowledge_details(self, name: str) -> dict[str, Any]:
        assert self.raven is not None
        return {
            "metadata": self.raven.get_knowledge_details(name),
            "files": await self.raven.list_knowledge_files(name),
        }
```

</figure>

[`RavenClient.list_knowledges()`](#complete-raven-client-py "raven_client.py") adds a display-only file count to each record
returned by RAVEN. That count describes documents, not vectors: one file can
produce many sections and chunks. [`RavenClient.knowledge_details()`](#complete-raven-client-py "raven_client.py") combines
the knowledge metadata with its current file list.

[`KnowledgeList`](#complete-widgets-py "widgets.py") displays the readable name and summary, while each selectable
entry carries a path such as `/knowledges/engineering`. A clicked entry reaches
[`RavenDemo.open_knowledge()`](#complete-app-py "app.py") and then [`RavenDemo.submit_text()`](#complete-app-py "app.py"), just as a typed
path does. [`RavenDemo.show_knowledge_route()`](#complete-app-py "app.py") requests the corresponding data
and mounts [`KnowledgeDetails`](#complete-widgets-py "widgets.py").

We therefore use RAVEN as the source of state at both ends: it confirms
creation, then supplies the data displayed during browsing. The TUI never
counts Qdrant points to infer the number of documents or opens the file store
to assemble its own metadata.

### 6.2 Choosing an ingestion target

We offer two entry points to the same ingestion action: a scoped command and
the **Attach** button. Their only difference is how the target knowledge is
supplied.

| Entry point | Target passed to the form | Result |
| --- | --- | --- |
| `/knowledges/engineering/ingest` | `"engineering"` | The form fixes the target to that knowledge. |
| Attach during a local conversation | That conversation's knowledge name | The form fixes the target to the conversation's knowledge. |
| Attach during a global conversation, or with none open | `None` | The form requires an explicit selection from the available knowledges. |

Browsing a knowledge does not bind it to the conversation or make it the
implicit Attach target. The scoped command supplies a name directly;
[`RavenDemo.attach_document()`](#complete-app-py "app.py") instead passes `self.conversation_knowledge`,
which comes from the active conversation.

We connect those entry points through [`RavenDemo.request_ingestion_form()`](#complete-app-py "app.py")
and prepare the form with current backend data:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    @on(Button.Pressed, "#ingest")
    async def attach_document(self) -> None:
        self.dismiss_command_menu()
        if self.client is None:
            self.notify("Backend disabled for this UI preview.", title="Demo")
            self.query_one("#message", PromptEditor).focus()
            return
        self.query_one("#welcome").display = False
        await self.request_ingestion_form(self.conversation_knowledge)


    async def request_ingestion_form(self, knowledge_name: str | None) -> None:
        chat = self.query_one("#chat", VerticalScroll)
        if self._backend_busy:
            await chat.mount(ChatMessage("Please wait", "A backend action is still running."))
        else:
            self._backend_busy = True
            self.query_one("#status", Static).update("Preparing ingestion form…")
            self.run_worker(self.prepare_ingestion_form(knowledge_name), name="Prepare ingestion", group="backend")
        chat.scroll_end(animate=False)


    async def prepare_ingestion_form(self, knowledge_name: str | None) -> None:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        form = None
        try:
            await self._startup_complete.wait()
            if not self.backend_ready:
                raise RuntimeError("RAVEN could not start. Check the backend error above.")
            self._knowledge_names = [item["name"] for item in await self.client.list_knowledges()]
            if not self._knowledge_names:
                raise ValueError("No knowledges yet. Use /knowledges/create before attaching a document.")
            if knowledge_name is not None and knowledge_name not in self._knowledge_names:
                raise ValueError(f"Knowledge '{knowledge_name}' does not exist.")
            if not self.client.models_ready:
                raise ValueError("Configure both models with /models/configure before ingestion.")
            for previous in self.query(IngestionForm):
                await previous.remove()
            form = IngestionForm(self._knowledge_names, knowledge_name)
            await chat.mount(form)
            self.query_one("#status", Static).update("Choose the target knowledge and local file · Enter or click Ingest")
        except Exception as error:
            await chat.mount(ChatMessage("Cannot ingest", str(error)))
            self.query_one("#status", Static).update("Ingestion needs a knowledge and configured models")
        finally:
            self._backend_busy = False
            if form is not None:
                target = form.query_one("#ingestion-path" if knowledge_name is not None else "#ingestion-knowledge")
                self.call_after_refresh(lambda: target.scroll_visible(animate=False))
            else:
                chat.scroll_end(animate=False)
```

</figure>

The scoped command also calls [`RavenDemo.request_ingestion_form()`](#complete-app-py "app.py"), passing
the knowledge name from its path. Both routes therefore share the same
startup, resource, and model checks.

[`RavenDemo.prepare_ingestion_form()`](#complete-app-py "app.py") waits for startup, lists knowledges,
checks an explicitly supplied target, and requires a configured model pair.
If no knowledge exists, it explains that one must be created first rather
than describing RAVEN as unavailable.

The [`IngestionForm`](#complete-ingestion-widgets-py "ingestion_widgets.py") constructor receives the available names and the optional
fixed target. Its selection control is disabled only when that target is
already fixed. A global conversation remains global; the selected knowledge
belongs to this ingestion request, not to a new conversation binding.

The busy flag protects form preparation and is released afterward. Preparing
a form does not begin document processing.

### 6.3 Passing a knowledge name and file path to the backend

The form shown in Section 2 collects a target knowledge and a local path.
We give it a submission message containing those two values and a reference
to the form:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-ingestion-widgets-py">ingestion_widgets.py</a></figcaption>

```python
class IngestionForm(Vertical):

    class Submitted(Message):

        def __init__(self, form: IngestionForm, knowledge_name: str, source_path: str) -> None:
            super().__init__()
            self.form = form
            self.knowledge_name = knowledge_name
            self.source_path = source_path


    @on(Input.Submitted, "#ingestion-path")
    @on(Button.Pressed, ".start-ingestion")
    def submit(self, event: Input.Submitted | Button.Pressed) -> None:
        event.stop()
        knowledge = self.query_one("#ingestion-knowledge", Select).value
        path = self.query_one("#ingestion-path", Input).value.strip()
        if not isinstance(knowledge, str):
            self.show_error("Choose a target knowledge first.")
        elif not path:
            self.show_error("Enter a local file path.")
        else:
            self.post_message(self.Submitted(self, knowledge, path))
```

</figure>

This is a Textual message, not an ingestion event from RAVEN. The form checks
that a target was selected and that a path was entered. It does not inspect the
document or decide how its content should be parsed.

In [`app.py`](#complete-app-py "app.py"), we import [`IngestionForm`](#complete-ingestion-widgets-py "ingestion_widgets.py") and [`IngestionProgress`](#complete-ingestion-widgets-py "ingestion_widgets.py") from
[`ingestion_widgets.py`](#complete-ingestion-widgets-py "ingestion_widgets.py"). We connect the message to the application's ingestion
worker:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    @on(IngestionForm.Submitted)
    def ingest_document(self, event: IngestionForm.Submitted) -> None:
        event.stop()
        assert self.client is not None
        if self._backend_busy:
            event.form.show_error("Please wait for the current backend action to finish.")
            return
        if not self.client.models_ready:
            event.form.show_error("Configure both models with /models/configure before ingestion.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self._ingestion_worker = self.run_worker(self.save_ingestion(event), name="Ingest document", group="backend")
```

</figure>

[`RavenDemo.ingest_document()`](#complete-app-py "app.py") receives the same form message, checks that the
demo can accept another action, and disables the form while ingestion runs.
It stores the managed worker in `self._ingestion_worker` so that Stop can
cancel the correct action later.

[`RavenDemo.save_ingestion()`](#complete-app-py "app.py"), shown in Section 3.4, passes
`event.knowledge_name` and `event.source_path` to [`RavenClient.ingest()`](#complete-raven-client-py "raven_client.py")
alongside its progress callback. The adapter:

1. Removes surrounding whitespace and a matching pair of enclosing quotes
   from the entered path.
2. Resolves it on the local machine and checks that it identifies a file with
   a supported extension: `.txt`, `.md`, `.pdf`, or `.docx`.
3. Runs that filesystem validation through `asyncio.to_thread()`.
4. Calls `Raven.ingest(knowledge_name, path)` and consumes the returned task.

A relative path is resolved from the directory in which the application was
launched, not from `raven_home/`. That is why `Reaction_wheel.txt` in Section 2
is placed beside the extracted demo folder. An absolute path identifies the
source independently of the launch directory.

These checks are application-side feedback, not replacements for RAVEN's own
validation and ingestion policies. The original file is read, not moved.
RAVEN manages the working snapshot, parsing, sectioning, metadata extraction,
embedding, and storage; the TUI supplies the target and source path.

### 6.4 Presenting stage progress without inventing a percentage

The adapter consumes the operation's events and forwards `INGESTION_PROGRESS`
updates. Its module-level formatter, [`ingestion_progress_text()`](#complete-raven-client-py "raven_client.py"), converts
the event data into a stage label, status, and counts when available:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
def ingestion_progress_text(data: dict[str, Any]) -> str:
    labels = {
        "source_validation": "Checking source file",
        "source_snapshot": "Preparing source snapshot",
        "parsing": "Parsing document",
        "sectioning": "Finding semantic sections",
        "metadata_extraction": "Extracting section metadata",
        "chunking": "Creating chunks",
        "embedding": "Embedding chunks",
        "vector_storage": "Writing vectors",
        "metadata_commit": "Saving file metadata",
    }
    stage = str(data.get("stage", "ingestion"))
    text = f"{labels.get(stage, stage.replace('_', ' ').capitalize())} · {data.get('status', 'running')}"
    completed, total = data.get("completed"), data.get("total")
    if isinstance(completed, int) and isinstance(total, int) and total > 0:
        text += f" · {completed}/{total}"
    return text
```

</figure>

The stage names describe distinct kinds of work:

| Stage | What RAVEN is doing |
| --- | --- |
| `source_validation` and `source_snapshot` | Checking the source and preparing the working copy used by ingestion. |
| `parsing` | Converting the document into content elements with source-navigation information where available. |
| `sectioning` | Grouping content into semantic sections using the embedding model. |
| `metadata_extraction` | Using the LLM to extract section metadata. |
| `chunking` and `embedding` | Preparing smaller texts for vector search and generating their embeddings. |
| `vector_storage` and `metadata_commit` | Storing vectors and committing the file and section records. |

We do not combine these stages into an overall percentage. A count of `2/5`
during metadata extraction means two of five sections for that stage, not that
the entire ingestion is 40% complete. Some stages report only started and
completed states because a meaningful intermediate count is unavailable.

The callback inside [`RavenDemo.save_ingestion()`](#complete-app-py "app.py") sends each formatted update
to [`IngestionProgress.update_progress()`](#complete-ingestion-widgets-py "ingestion_widgets.py"). The view keeps a bounded list of
recent stages and replaces its latest entry when the same stage reports again:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-ingestion-widgets-py">ingestion_widgets.py</a></figcaption>

```python
class IngestionProgress(Vertical):

    def __init__(self) -> None:
        super().__init__(classes="chat-message ingestion-progress")
        self.entries: list[tuple[str, str]] = []


    def update_progress(self, stage: str, text: str) -> None:
        if self.entries and self.entries[-1][0] == stage:
            self.entries[-1] = (stage, text)
        else:
            self.entries.append((stage, text))
            self.entries = self.entries[-8:]
        self.query_one(".ingestion-stages", Static).update("\n".join(text for _, text in self.entries))


    def finish(self, text: str) -> None:
        self.update_progress("outcome", text)
        self.query_one(".ingestion-spinner").display = False
        self.query_one(".stop-ingestion", Button).disabled = True
```

</figure>

The full class, including its controls, is available in [`ingestion_widgets.py`](#complete-ingestion-widgets-py "ingestion_widgets.py").
We keep presentation bounded without changing RAVEN's event stream. No content
processing happens inside this view.

[`IngestionProgress.finish()`](#complete-ingestion-widgets-py "ingestion_widgets.py") records the displayed outcome and disables the
spinner and Stop control. It does not decide whether ingestion succeeded:
[`RavenDemo.save_ingestion()`](#complete-app-py "app.py") calls it after the adapter returns, fails, or
propagates cancellation.

If a cloud LLM is configured, metadata extraction can send document text to
that provider. Cloud embedding models likewise receive the text being
embedded. A local file path and local storage do not imply local inference.

### 6.5 Refreshing the file list after a confirmed result

[`RavenClient.ingest()`](#complete-raven-client-py "raven_client.py") awaits the task result after consuming its events.
[`RavenDemo.save_ingestion()`](#complete-app-py "app.py") uses that confirmed result's `file`,
`section_count`, and `chunk_count` fields for the completion message.
It then removes the form and refreshes the knowledge details.

We put the refresh in a separate method because a completed ingestion and a
successful display refresh are different outcomes:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def refresh_ingestion_details(self, knowledge_name: str) -> bool:
        assert self.client is not None
        chat = self.query_one("#chat", VerticalScroll)
        try:
            details = await self.client.knowledge_details(knowledge_name)
        except Exception as error:
            await chat.mount(ChatMessage("Refresh error", f"Could not refresh file details: {error}. Use /knowledges/{knowledge_name} to check."))
            return False
        else:
            # Refresh earlier views too, so their counts/files do not remain stale.
            for view in tuple(self.query(KnowledgeDetails)):
                if view.metadata["name"] == knowledge_name:
                    view.metadata, view.files = details["metadata"], details["files"]
                    await view.recompose()
            for view in tuple(self.query(KnowledgeList)):
                view.knowledges = [
                    {**item, "file_count": len(details["files"])} if item["name"] == knowledge_name else item
                    for item in view.knowledges
                ]
                await view.recompose()
            await chat.mount(KnowledgeDetails(details))
            return True
```

</figure>

[`RavenDemo.refresh_ingestion_details()`](#complete-app-py "app.py") first requests current metadata and
files through [`RavenClient.knowledge_details()`](#complete-raven-client-py "raven_client.py"). It then refreshes matching
[`KnowledgeDetails`](#complete-widgets-py "widgets.py") views and updates that knowledge's file count in existing
[`KnowledgeList`](#complete-widgets-py "widgets.py") views. Finally, it mounts the latest details in the transcript.

If that read fails, the method reports a **refresh error** and returns `False`.
The ingestion worker can therefore state that ingestion completed but the
file list could not be refreshed. It does not relabel a committed ingestion
as failed or encourage submitting the same file again merely because a view
update failed.

The persisted knowledge remains managed by RAVEN, so reopening it can read its
files again. The interface's widgets are temporary views of that state, not
the state itself.

### 6.6 Stopping work and keeping errors recoverable

Before submission, **Cancel** simply removes the form; no ingestion operation
has started. During processing, the form is disabled and **Stop ingestion**
targets the worker that is already running:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    @on(Button.Pressed, ".stop-ingestion")
    def stop_ingestion(self, event: Button.Pressed) -> None:
        event.stop()
        if self._ingestion_worker is not None:
            event.button.disabled = True
            self.query_one("#status", Static).update("Stopping ingestion… · Waiting for safe cleanup")
            self._ingestion_worker.cancel()
```

</figure>

[`RavenDemo.stop_ingestion()`](#complete-app-py "app.py") requests cancellation of the managed worker.
The exception path in [`RavenClient.ingest()`](#complete-raven-client-py "raven_client.py") then awaits cancellation of an
unfinished RAVEN task before propagating the exception. We keep the distinction
between requesting Stop and finishing backend cleanup; neither the form nor
the progress view assumes cancellation is instantaneous.

The cancellation handler in [`RavenDemo.save_ingestion()`](#complete-app-py "app.py") marks the action as
stopped and refreshes the target knowledge. Cancellation can arrive after a
write has completed, so the interface reads the actual file list rather than
assuming the document must be absent. Its final cleanup clears the busy flag
and restores any remaining form controls.

For ordinary errors, we retain the form and display the backend message.
A missing source can be corrected, an unsupported extension can be replaced
with a supported document, and a duplicate-file conflict can be checked against
the existing knowledge instead of guessed from the UI state.

Submitting the corrected form is a new ingestion request. It is not the same
as invoking RAVEN's durable task-retry API; this demo deliberately does not
provide that retry interface.

The complete workflow is now:

**Created knowledge → explicit ingestion target → form submission → RAVEN
ingestion task → stage updates → confirmed result → refreshed persisted state.**

We have connected document management without reproducing RAVEN's ingestion
pipeline in the application. Next, we will connect a conversation to the
configured backend and stream an answer based on the ingested material.

## 7. Connecting conversations to streamed chat

A knowledge holds the documents; a conversation holds an interaction with the
assistant. We now connect the message box to that persistent conversation,
letting RAVEN manage context, tool use, response generation, and turn
persistence while the TUI presents the work as it happens.

We keep three objects distinct:

- The **conversation** is the persistent record: identity, scope, messages, and
  conversation memory.
- A **session** is a temporary interaction medium over that conversation.
  In this demo, we create one for each submitted question and close it afterward.
- [`StreamingResponse`](#complete-chat-widgets-py "chat_widgets.py") is a temporary view of one turn. Its text buffers and
  activity labels are not the conversation database.

That separation lets us reopen a conversation without restarting an agent,
and send a follow-up without keeping an old session alive.

### 7.1 Creating a conversation with optional knowledge scope

We begin with [`CreateConversationForm`](#complete-conversation-widgets-py "conversation_widgets.py") from [`conversation_widgets.py`](#complete-conversation-widgets-py "conversation_widgets.py").
[`RavenDemo.show_conversation_route()`](#complete-app-py "app.py") mounts it when
`/conversations/create` is selected, supplying the current knowledge names.

The form defaults to **Global · All knowledges**. Selecting a knowledge creates
a local conversation restricted to it; leaving the default creates a global
conversation without a knowledge binding. A knowledge is not mandatory for
creating a conversation, and neither creation nor history browsing requires
configured models.

The form converts its selection into a Textual message:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-conversation-widgets-py">conversation_widgets.py</a></figcaption>

```python
class CreateConversationForm(Vertical):

    class Submitted(Message):

        def __init__(self, form: CreateConversationForm, knowledge_name: str | None) -> None:
            super().__init__()
            self.form = form
            self.knowledge_name = knowledge_name


    @on(Button.Pressed, ".create-conversation")
    def submit(self, event: Button.Pressed) -> None:
        event.stop()
        knowledge = self.query_one(Select).value
        self.post_message(self.Submitted(self, knowledge if isinstance(knowledge, str) and knowledge else None))
```

</figure>

The global option has an empty-string value in the selection control.
[`CreateConversationForm.submit()`](#complete-conversation-widgets-py "conversation_widgets.py") converts that value to `None`; a selected
knowledge remains its name. The message carries the form and that optional
name, rather than asking the TUI to infer scope later.

The form's controls, imports, and the conversation history view are available
in the complete module:

{% include source_file.html name="conversation_widgets.py" source="tui_example/conversation_widgets.py" language="python" id="complete-conversation-widgets-py" %}

We connect its message to [`RavenDemo`](#complete-app-py "app.py") in [`app.py`](#complete-app-py "app.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    @on(CreateConversationForm.Submitted)
    def create_conversation(self, event: CreateConversationForm.Submitted) -> None:
        event.stop()
        if not self.backend_ready or self._backend_busy:
            event.form.show_error("Please wait for RAVEN to finish its current action.")
            return
        self._backend_busy = True
        event.form.set_busy(True)
        event.form.show_error("")
        self.query_one("#status", Static).update("Creating conversation…")
        self.run_worker(self.save_conversation(event), name="Create conversation", group="backend")


    async def save_conversation(self, event: CreateConversationForm.Submitted) -> None:
        assert self.client is not None
        try:
            metadata = await self.client.create_conversation(event.knowledge_name)
        except Exception as error:
            event.form.show_error(str(error))
            self.query_one("#status", Static).update("Conversation creation failed · Try again or cancel")
        else:
            await event.form.remove()
            try:
                await self.display_conversation(metadata["conversation_id"])
                self.query_one("#status", Static).update("Conversation created and opened · Configure models, then ask a question")
            except Exception as error:
                await self.query_one("#chat", VerticalScroll).mount(ChatMessage(
                    "Open error", f"Conversation {metadata['conversation_id']} was created, "
                    f"but could not be opened: {error}. Use /conversations to try again.",
                ))
                self.query_one("#status", Static).update("Conversation saved · Opening failed; previous selection is unchanged")
        finally:
            self._backend_busy = False
            if event.form.is_attached:
                event.form.set_busy(False)
            if self.is_running:
                self.query_one("#chat", VerticalScroll).scroll_end(animate=False)
```

</figure>

The handler checks readiness and schedules [`RavenDemo.save_conversation()`](#complete-app-py "app.py")
with the submitted message. Its adapter call is:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
class RavenClient:

    async def create_conversation(self, knowledge_name: str | None = None) -> dict[str, Any]:
        assert self.raven is not None
        task = await self.raven.create_conversation(knowledge_name)
        try:
            conversation = await task.result()
            return conversation.to_dict()
        except BaseException:
            if not task.is_finished:
                await task.cancel()
            raise
```

</figure>

`Raven.create_conversation()` supplies an operation task. We await its result
and return metadata from the created conversation. The UI then opens that
conversation using the returned ID.

Creation and opening are separate outcomes. If creation succeeds but reading
the new conversation fails, [`RavenDemo.save_conversation()`](#complete-app-py "app.py") reports that it
was saved and keeps the previous selection. It does not encourage creating
another conversation merely because the view could not be opened.

### 7.2 Opening canonical history, not the model's compacted context

We make saved conversations discoverable through `/conversations`.
[`ConversationList`](#complete-conversation-widgets-py "conversation_widgets.py") displays each title, scope, and a shortened ID, while its
selectable option carries the full `/conversations/<id>` path.
[`RavenDemo.open_conversation()`](#complete-app-py "app.py") sends a selected path through the same router
as a typed command.

In the adapter, listing is a metadata read. Opening also retrieves the
conversation object and its saved messages:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
class RavenClient:

    def list_conversations(self) -> list[dict[str, Any]]:
        assert self.raven is not None
        return sorted(
            self.raven.list_conversations(),
            key=lambda item: (item["created_at"], item["conversation_id"]),
            reverse=True,
        )


    async def open_conversation(self, conversation_id: str) -> dict[str, Any]:
        assert self.raven is not None
        conversation = self.raven.get_conversation(conversation_id)
        records = []
        cursor = 0
        while True:
            page = await self.raven.get_conversation_messages_page(
                conversation_id, limit=100, after_message_id=cursor,
            )
            records.extend(page)
            if len(page) < 100:
                break
            cursor = page[-1]["message_id"]
        turns: dict[str, dict[str, Any]] = {}
        for record in records:
            turn = turns.setdefault(record["turn_id"], {
                "turn_id": record["turn_id"], "messages": [], "has_sources": False,
            })
            turn["messages"].append(record["message"])
            turn["has_sources"] |= message_has_sources(record["message"])
        return {
            "conversation": conversation,
            "metadata": conversation.to_dict(),
            "messages": [record["message"] for record in records],
            "turns": list(turns.values()),
        }
```

</figure>

The message API returns bounded pages of **canonical, uncompacted history**.
We request 100 records at a time and advance `after_message_id` to the last
record read. We continue until a shorter page is returned, so a conversation
with more than 100 messages does not silently lose its older or newer turns
from the interface.

We group those records by `turn_id`. This keeps a user message, tool exchange,
assistant answer, and any source action associated with the same turn.
[`message_has_sources()`](#complete-raven-client-py "raven_client.py") checks saved tool evidence for the source controls;
Section 8 will explain that check.

[`ConversationHistory`](#complete-conversation-widgets-py "conversation_widgets.py") renders the user and assistant messages. Saved tool
calls and results appear as compact activity labels rather than dumping full
tool payloads into the transcript. The model-facing compacted summary is not
substituted for the messages the reader originally saw. RAVEN manages that
summary and vector memory separately when preparing a turn.

We attach the loaded data to the active UI state only after the read succeeds:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def display_conversation(self, conversation_id: str) -> None:
        """Read first; only replace the active view when loading succeeds."""
        assert self.client is not None
        opened = await self.client.open_conversation(conversation_id)
        chat = self.query_one("#chat", VerticalScroll)
        await chat.query(".chat-message").remove()
        await chat.mount(ConversationHistory(opened["metadata"], opened["messages"], opened.get("turns")))
        self.active_conversation = opened["conversation"]
        self.conversation_knowledge = opened["metadata"]["knowledge_name"]
        self.update_conversation_header(opened["metadata"])
        self.query_one("#message", PromptEditor).focus()
```

</figure>

[`RavenDemo.display_conversation()`](#complete-app-py "app.py") first awaits the adapter, then replaces
the current transcript and updates the active conversation object and scope.
A failed backend read therefore does not first discard the previous view.

The selected conversation supplies the scope for later chat and Attach
actions. Browsing another knowledge does not change it, and reopening a
conversation does not run the LLM.

### 7.3 Accepting a question without losing a rejected draft

For ordinary input, [`RavenDemo.submit_text()`](#complete-app-py "app.py") calls
[`RavenDemo.request_response()`](#complete-app-py "app.py") rather than routing a slash command.
We check the conditions for accepting the turn before clearing the editor:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def request_response(self, query: str) -> None:
        assert self.client is not None
        reason = None
        if not self.backend_ready:
            reason = "RAVEN is not ready yet. Check the startup status."
        elif self._backend_busy:
            reason = "A backend action is still running. Wait or stop the current response first."
        elif self.active_conversation is None:
            reason = "Open a conversation with /conversations or create one with /conversations/create."
        elif not self.client.models_ready:
            reason = "Configure both models with /models/configure before chatting."
        chat = self.query_one("#chat", VerticalScroll)
        self.query_one("#welcome").display = False
        if reason is not None:
            await chat.mount(ChatMessage("Cannot send", reason))
            self.query_one("#status", Static).update("Message not sent · Your draft is unchanged")
            chat.scroll_end(animate=False)
            return

        self._backend_busy = True
        self.dismiss_command_menu()
        self.query_one("#message", PromptEditor).load_text("")
        for notice in self.query(".empty-history"):
            notice.display = False
        await chat.mount(ChatMessage("You", query))
        response = StreamingResponse()
        await chat.mount(response)
        self._chat_worker = self.run_worker(self.generate_response(query, response), name="Chat turn", group="backend")
        self._chat_stopping = False
        button = self.query_one("#send", Button)
        button.label = "Stop"
        button.tooltip = "Stop the active response"
        button.disabled = False
        self.query_one("#ingest", Button).disabled = True
        self.query_one("#status", Static).update("Preparing conversation… · Retrieval: auto")
        chat.scroll_end(animate=False)
```

</figure>

If startup is incomplete, another action is running, no conversation is open,
or the model pair is missing, the request is rejected with an explanation.
The draft remains in the editor.

Once accepted, we echo the user text, mount a [`StreamingResponse`](#complete-chat-widgets-py "chat_widgets.py"), and start
[`RavenDemo.generate_response()`](#complete-app-py "app.py") as a managed worker. We also change Send to
Stop and disable Attach until the action ends.

This is the demo's one-action policy, not a limitation on RAVEN's ability to
serve independent conversations. It prevents a conversation switch or another
backend action from making this single transcript ambiguous while a turn runs.

### 7.4 Running a temporary session over the selected conversation

The worker passes the active conversation and query to
[`RavenClient.generate_response()`](#complete-raven-client-py "raven_client.py"). The adapter creates a temporary session
through the same `Raven` instance used elsewhere:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
class RavenClient:

    async def generate_response(
        self,
        conversation: Conversation,
        query: str,
        on_event: Callable[[Event], Awaitable[None]],
    ) -> AgentRunResult:
        """Use a temporary session; RAVEN owns generation and turn persistence."""
        if not self.models_ready:
            raise RuntimeError("Configure both models with /models/configure before chatting.")
        assert self.raven is not None
        session = self.raven.session(conversation)
        run = None
        try:
            await session.start()
            run = await session.generate_response(query, retrieval_mode="auto")
            async for event in run.stream:
                await on_event(event)
            # chat.completed is not enough: the parent session still saves the turn.
            return await run.collect()
        except BaseException:
            if run is not None and not run.task.is_finished:
                await run.cancel()
            raise
        finally:
            await session.close()
```

</figure>

The `Conversation` and event/result types used in this signature come from
RAVEN, with imports identified in the complete adapter module.
`session.start()` opens the conversation for this interaction;
`session.generate_response()` starts a turn and returns its run handle.

We use `retrieval_mode="auto"` because the demo has no retrieval-mode selector.
RAVEN exposes the tools permitted by the conversation scope, and the model
decides whether to answer directly or use tools in sequence. Local conversations
remain restricted to their knowledge. Global conversations can use global
retrieval or target a particular knowledge through local retrieval tools.

The TUI does not implement a query classifier or a
query → retrieval → answer loop. It submits the question and consumes the
agent's events. A follow-up may use conversation memory without retrieving the
same document again; that choice belongs to the running agent.

Each event from `run.stream` is passed to an async UI callback. After the
stream, `run.collect()` awaits the complete session task and returns its
collected result. It does not start generation a second time.

The `finally` block closes the temporary session on success, failure, or
cancellation. That does not delete the conversation, close the application's
`Raven` instance, or require the next turn to start a new conversation.
The next question creates a fresh session over the same persistent object.

### 7.5 Keeping thinking, tools, and answer text separate

We project backend events into the response view through
[`StreamingResponse.apply_event()`](#complete-chat-widgets-py "chat_widgets.py") from [`chat_widgets.py`](#complete-chat-widgets-py "chat_widgets.py").
The view keeps separate state for answer text, thinking, and tool activity:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-chat-widgets-py">chat_widgets.py</a></figcaption>

```python
class StreamingResponse(Vertical):

    def __init__(self) -> None:
        super().__init__(classes="chat-message streaming-response")
        self.response = ""
        self.thinking = ""
        self.tool_activity: dict[str, str] = {}
        self.tool_names: list[str] = []
        self.event_types: list[str] = []
        self.turn_id: str | None = None
        self.has_sources = False


    def apply_event(self, event: Event) -> str | None:
        """Project only UI-relevant events; never display raw retrieval contexts."""
        kind, data = event.type.value, event.data
        if kind not in self.event_types:
            self.event_types.append(kind)
        status = None
        if kind == "chat.thinking_delta":
            self.thinking += str(data.get("delta") or "")
            self.query_one(".thinking-text", Static).update(self.thinking)
            self.query_one(".chat-thinking").display = bool(self.thinking)
            status = "Thinking…"
        elif kind == "chat.response_delta":
            self.response += str(data.get("delta") or "")
            self.query_one(".response-text", Static).update(self.response)
            status = "Responding…"
        elif kind in {"chat.tool_call", "chat.tool_result"}:
            call_id = str(data.get("call_id") or data.get("name") or "tool")
            name = str(data.get("name") or "tool")
            if kind == "chat.tool_call":
                self.tool_names.append(name)
                text = f"{data.get('step', '?')}. {name} · running"
            else:
                state = "done" if data.get("ok") is True else "failed (agent may recover)"
                text = f"{data.get('step', '?')}. {name} · {state}"
                if data.get("ok") is True and data.get("evidence"):
                    self.has_sources = True
            self.tool_activity[call_id] = text
            self.query_one(".tool-activity", Static).update("\n".join(self.tool_activity.values()))
            self.query_one(".tool-activity").display = True
            status = f"Using {name}…" if kind == "chat.tool_call" else "Tool returned · Agent continuing…"
        elif kind == "conversation.memory_compaction.started":
            status = "Compacting conversation context…"
        elif kind == "conversation.memory_compaction.completed":
            status = "Context compacted · Agent continuing…"
        elif kind == "chat.max_iterations":
            status = "Tool-step limit reached · Waiting for the final answer…"
        elif kind.startswith("reconstruction."):
            status = "Reconstructing sources…"
        elif kind == "chat.completed":
            status = "Answer generated · Saving turn…"
        elif kind in {"conversation.turn_commit.completed", "conversation.turn_commit.reused"}:
            self.turn_id = str(data["turn_id"])
            status = "Turn committed · Finishing memory and title updates…"
        if status is not None:
            self.query_one(".response-outcome", Static).update(status)
        return status
```

</figure>

These methods use the controls defined by the same class in the full file.
The relevant mapping is:

| RAVEN event | Visible meaning |
| --- | --- |
| `chat.thinking_delta` | Append thinking text to its own collapsible view when supplied by the model/provider. |
| `chat.response_delta` | Append answer text independently of thinking and tool activity. |
| `chat.tool_call` and `chat.tool_result` | Update a tool entry using its correlation ID, step, name, and outcome. |
| `conversation.memory_compaction.started` and `conversation.memory_compaction.completed` | Explain that RAVEN is preparing a compacted context rather than leaving an unexplained wait. |
| `chat.max_iterations` | Report the tool-step limit while waiting for RAVEN's final outcome. |
| `reconstruction.*` | Indicate that supporting material is being reconstructed. |
| `chat.completed` | The answer was generated; the session may still be saving the turn. |
| `conversation.turn_commit.completed` or `conversation.turn_commit.reused` | Capture the durable turn ID while the session finishes its remaining work. |

Not every turn emits every event. Thinking depends on the model and provider,
casual questions may use no tools, and compaction occurs only when needed.

We correlate a tool result with its call rather than presenting it as unrelated
text. A failed tool result is shown as a failure the agent may recover from;
it is not automatically treated as failure of the entire turn. The view does
not print raw retrieved contexts, even though the backend supplies the model
with the material needed to answer.

{% include source_file.html name="chat_widgets.py" source="tui_example/chat_widgets.py" language="python" id="complete-chat-widgets-py" %}

### 7.6 Confirming the saved turn before showing success

The application worker connects that event renderer to the adapter:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    async def generate_response(self, query: str, response: StreamingResponse) -> None:
        assert self.client is not None and self.active_conversation is not None
        conversation = self.active_conversation
        chat = self.query_one("#chat", VerticalScroll)
        try:
            async def show_event(event: Event) -> None:
                follow = chat.is_vertical_scroll_end
                status = response.apply_event(event)
                if status is not None:
                    self.query_one("#status", Static).update(status)
                if follow:
                    chat.scroll_end(animate=False)

            result = await self.client.generate_response(conversation, query, show_event)
            response.finish("Saved to conversation", response=result.response)
            cached_files = getattr(result, "reconstructed_sources", None)
            if response.turn_id and (response.has_sources or getattr(result, "evidence", None) or cached_files):
                await response.mount(SourceButton(conversation.conversation_id, response.turn_id, cached_files))
            self.query_one("#status", Static).update("Ready · Turn saved · Ask a follow-up")
        except asyncio.CancelledError:
            if self.is_running and response.is_attached:
                response.finish("Stopped · Displayed text may be incomplete. Reopen the conversation to inspect saved turns.")
                self.query_one("#status", Static).update("Response stopped · You can send another message")
            raise
        except Exception as error:
            if self.is_running and response.is_attached:
                response.finish(f"Failed: {error}\nDisplayed text may be incomplete; reopen the conversation to inspect saved turns.")
                self.query_one("#status", Static).update("Chat failed · You can try again")
        finally:
            self._backend_busy = False
            self._chat_worker = None
            self._chat_stopping = False
            if self.is_running:
                self.update_conversation_header(conversation.to_dict())
                button = self.query_one("#send", Button)
                button.label = "Send"
                button.tooltip = "Send message"
                button.disabled = not self.query_one("#message", PromptEditor).text.strip()
                self.query_one("#ingest", Button).disabled = False
```

</figure>

The nested [`show_event()`](#complete-app-py "app.py") callback sends events to
[`StreamingResponse.apply_event()`](#complete-chat-widgets-py "chat_widgets.py") and updates the status line when that method
returns a status. It follows the transcript's end only when the reader was
already at the end, rather than forcing the view downward while earlier text
is being inspected.

There are two completion boundaries:

1. **Answer generation.** `chat.completed` means that the agent has produced
   its result. The response view still says that the turn is being saved.
2. **The complete session result.** The adapter returns only after
   `run.collect()` succeeds and its temporary session is closed. We then call
   [`StreamingResponse.finish()`](#complete-chat-widgets-py "chat_widgets.py") with the confirmed answer and display
   **Saved to conversation**.

RAVEN's session, not the TUI callback, persists the turn's messages and result.
This includes the tool exchange needed for future context. We do not append a
second copy of the user and assistant messages from the UI.

The backend also handles the initial title-generation attempt. After the
worker ends, [`RavenDemo.update_conversation_header()`](#complete-app-py "app.py") reads the conversation's
current metadata so an updated title can replace the initial label. Title
generation can fail independently of an already saved turn; the TUI does not
generate its own competing title.

When a saved turn has source evidence or a cached reconstruction, the worker
can mount a [`SourceButton`](#complete-source-widgets-py "source_widgets.py") with its conversation and turn IDs. A casual answer
without section evidence does not need one. The complete source-control file
is available below; Section 8 will trace the inspection and rebuilding paths.

{% include source_file.html name="source_widgets.py" source="tui_example/source_widgets.py" language="python" id="complete-source-widgets-py" %}

### 7.7 Stopping a response without claiming its draft was saved

Send becomes Stop while `self._chat_worker` refers to an active turn.
[`RavenDemo.send_or_stop()`](#complete-app-py "app.py") uses that state to decide which action applies:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    @on(Button.Pressed, "#send")
    async def send_or_stop(self) -> None:
        if self._chat_worker is not None:
            if not self._chat_stopping:
                self._chat_stopping = True
                self.query_one("#send", Button).disabled = True
                self.query_one("#status", Static).update("Stopping response… · Waiting for safe cleanup")
                self._chat_worker.cancel()
        else:
            await self.show_message()
```

</figure>

Stop disables further Stop clicks and requests cancellation of the managed
worker. In [`RavenClient.generate_response()`](#complete-raven-client-py "raven_client.py"), cancellation propagates through
`run.cancel()` for an unfinished session task, and the session is closed in
the `finally` block.

The cancellation handler in [`RavenDemo.generate_response()`](#complete-app-py "app.py") leaves the
displayed partial text visible but labels it **Stopped**, not **Saved to
conversation**. Ordinary failures similarly mark the output as failed.
A turn may already have committed before a late cancellation or failure, so
the interface directs the reader to reopen the conversation to inspect the
canonical saved state rather than guessing from the partial display.

The worker's final cleanup restores Send, re-enables Attach, clears the busy
flag, and refreshes the conversation header. Any new draft typed while the
previous response was running remains in the editor.

We now have the complete chat connection:

**Selected conversation → accepted question → temporary session → agent
events → separate live views → confirmed session result → saved conversation.**

A follow-up uses that same conversation and its backend-managed memory,
not a permanently running UI session. Next, we will connect each answer's
source action to the right saved turn and inspect its reconstructed files.

## 8. Attaching sources to the right answer

An answer and its sources need a more durable connection than “the latest
retrieval.” Another question may already have been asked when an earlier
answer is inspected. The application may also have been closed and reopened.
We therefore identify the sources by **conversation ID and turn ID**, rather
than by the currently selected knowledge or the last tool call on screen.

RAVEN provides the persisted turn and reconstruction machinery. The TUI adds
the per-answer action, chooses between a saved snapshot and a rebuild, and
renders the returned sections.

### 8.1 Keeping a reference with each saved answer

In Section 7, we waited for `run.collect()` before marking an answer saved.
At that point, [`RavenDemo.generate_response()`](#complete-app-py "app.py")
can attach a [`SourceButton`](#complete-source-widgets-py "source_widgets.py")
to that answer. The button carries the two identifiers and, when available,
the reconstruction already returned with the completed run:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-source-widgets-py">source_widgets.py</a></figcaption>

```python
class SourceButton(Button):

    def __init__(
        self,
        conversation_id: str,
        turn_id: str,
        cached_files: list[dict[str, Any]] | None = None,
    ) -> None:
        super().__init__("View sources", classes="source-button", compact=True)
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.cached_files = cached_files
```

</figure>

The cached files avoid another read for a newly completed answer. They are
not the only copy of the source information: reopening a conversation uses
its persisted messages and turn results.

When restoring history, [`RavenClient.open_conversation()`](#complete-raven-client-py "raven_client.py")
groups canonical messages by turn and checks each tool result with
[`message_has_sources()`](#complete-raven-client-py "raven_client.py"). We
check the evidence itself, not whether a familiar retrieval tool name appears:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
def message_has_sources(message: Any) -> bool:
    """A successful tool must supply actual section references, not just a name."""
    if message.role.value != "tool" or not isinstance(message.content, str):
        return False
    try:
        payload = json.loads(message.content)
    except ValueError:
        return False
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        return False
    evidence = payload.get("evidence")
    return isinstance(evidence, list) and any(
        isinstance(item, dict) and all(
            isinstance(item.get(key), str) and item[key]
            for key in ("knowledge_name", "file_name", "section_id")
        ) for item in evidence
    )
```

</figure>

A successful content-reading navigation tool can also provide section
evidence. A tool name alone, a failed call, or an empty evidence list cannot.
[`ConversationHistory`](#complete-conversation-widgets-py "conversation_widgets.py")
adds the source action to a restored turn only when it has source evidence
and nonempty assistant text. Casual answers and answers based only on
conversation memory normally have no new source action.

### 8.2 Passing that reference to the source viewer

We route a source click through the application, retaining the reference
stored on the clicked button:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-app-py">app.py</a></figcaption>

```python
class RavenDemo(App[None]):

    @on(Button.Pressed, ".source-button")
    async def view_response_sources(self, event: Button.Pressed) -> None:
        event.stop()
        if isinstance(event.button, SourceButton):
            await self.show_sources(event.button)


    async def show_sources(self, reference: SourceButton) -> None:
        if self.client is None:
            return
        if self._backend_busy:
            self.notify("Wait for the current backend action to finish before opening sources.")
            return
        await self.push_screen(SourceViewer(self.client, reference))
```

</figure>

An earlier answer therefore opens its own turn, even after later answers
have appeared. The `/sources` command is a convenience route to the last
source button in the **current conversation view**; it is not a separate
global source history.

[`SourceViewer`](#complete-source-widgets-py "source_widgets.py") is a modal
screen. The busy check keeps it from opening during another main backend
action. We do not check whether models are configured here: reading source
material is not a new inference request.

### 8.3 Reading the snapshot before rebuilding

We use three levels of access:

| Path | Where the reconstructed files come from | What is required |
| --- | --- | --- |
| Current answer's cache | The completed run's files, held on its source button | The current UI view |
| Persisted snapshot | The saved turn's `result.reconstructed_sources` | The saved conversation turn |
| Rebuild | Saved tool evidence resolved against current knowledge data | The saved tool messages and referenced knowledge sections |

The first path is handled inside the viewer. The remaining two belong to
[`RavenClient.turn_sources()`](#complete-raven-client-py "raven_client.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-raven-client-py">raven_client.py</a></figcaption>

```python
class RavenClient:

    async def turn_sources(
        self,
        conversation_id: str,
        turn_id: str,
        *,
        rebuild: bool,
        on_progress: Callable[[str], Awaitable[None]],
    ) -> dict[str, Any]:
        assert self.raven is not None
        if not rebuild:
            turn = await self.raven.get_conversation_turn(conversation_id, turn_id)
            result = turn.get("result") or {}
            files = result.get("reconstructed_sources")
            if isinstance(files, list) and files:
                return {"files": files, "snapshot": True}
        await on_progress("Reconstructing from this turn's saved evidence…")
        task = await self.raven.reconstruct_from_turn(conversation_id, turn_id)
        try:
            async for event in task.events():
                if event.type.value == "reconstruction.file":
                    await on_progress(f"Reconstructed {event.data.get('file_name', 'file')}")
            return {"files": await task.result(), "snapshot": False}
        except BaseException:
            if not task.is_finished:
                await task.cancel()
            raise
```

</figure>

With `rebuild=False`, a nonempty saved snapshot is returned immediately. If
none exists, the adapter falls back to `Raven.reconstruct_from_turn()`.
Selecting **Rebuild from saved evidence** passes `rebuild=True`, bypassing
the snapshot deliberately.

Rebuilding reads the successful section references in the turn's persisted
tool messages. It does **not** ask the agent the question again, repeat
retrieval, or require an LLM or embedding model. The returned task still has
ordinary operation events and a result, so progress and cancellation use
the same backend contract as other long-running actions.

There is an important historical distinction. A snapshot contains the
reconstructed text saved with the turn and remains readable without the
original document or its knowledge data. A rebuild reads the **current**
stored knowledge sections; missing references can fail, and changed data
can produce different material. Rebuilding does not overwrite the saved
turn result. We therefore label the two outcomes differently in the viewer.

### 8.4 Bringing files and provenance into the view

When [`SourceViewer`](#complete-source-widgets-py "source_widgets.py") mounts,
[`start_loading()`](#complete-source-widgets-py "source_widgets.py") launches
a managed worker and disables the file picker and rebuild button during
the read. The worker runs
[`load_sources()`](#complete-source-widgets-py "source_widgets.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-source-widgets-py">source_widgets.py</a></figcaption>

```python
class SourceViewer(ModalScreen[None]):

    async def load_sources(self, *, rebuild: bool) -> None:
        try:
            async def progress(text: str) -> None:
                self.query_one("#source-status", Static).update(text)

            if not rebuild and self.reference.cached_files:
                loaded = {"files": self.reference.cached_files, "snapshot": True}
            else:
                loaded = await self.client.turn_sources(
                    self.reference.conversation_id, self.reference.turn_id,
                    rebuild=rebuild, on_progress=progress,
                )
            self.files = loaded["files"]
            self._shown_index = None
            selector = self.query_one(Select)
            selector.display = bool(self.files)
            selector.set_options([
                (Text(f"{file['knowledge_name']} / {file['file_name']}"), index)
                for index, file in enumerate(self.files)
            ])
            self.query_one("#source-provenance", Static).update(
                "Saved snapshot from this turn. Rebuild reads the current knowledge contents."
                if loaded["snapshot"] else
                "Rebuilt from saved evidence using current knowledge contents; not a new model response."
            )
            if self.files:
                selector.value = 0
                await self.show_file(0)
            else:
                document = self.query_one("#source-document", VerticalScroll)
                await document.remove_children()
                await document.mount(Static("This turn has no reconstructable section evidence."))
            self.query_one("#source-status", Static).update(f"{len(self.files)} reconstructed file(s) · Esc to close")
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if self.is_attached:
                self.query_one("#source-status", Static).update(f"Could not load/rebuild sources: {error}")
                if not self.files:
                    document = self.query_one("#source-document", VerticalScroll)
                    await document.remove_children()
                    await document.mount(Static("No sources loaded. You can retry rebuilding, or close this view."))
        finally:
            if self.is_attached:
                self.query_one("#source-spinner").display = False
                self.query_one("#rebuild-sources", Button).disabled = False
                self.query_one(Select).disabled = not bool(self.files)
```

</figure>

Each picker option displays both knowledge and file name, so identically
named files in different knowledges remain distinguishable. The first file
opens automatically. The provenance line states whether the material is a
saved snapshot or a reconstruction from current knowledge; the status line
receives reconstruction progress and the final file count.

If a rebuild fails before returning files, the existing snapshot stays on
screen and the failure appears in the status line. If nothing has loaded,
the document area instead explains that no sources are available. An empty
successful result is also described explicitly, rather than presented as
an answer with supporting evidence.

### 8.5 Rendering sections without losing their context

RAVEN returns a list of reconstructed files. Each file identifies its
knowledge, file name, navigation type, and ordered sections. Each section
contains its ID, raw content, source range, and a `highlighted` flag.

We keep that section order when rendering a selected file:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-source-widgets-py">source_widgets.py</a></figcaption>

```python
class SourceViewer(ModalScreen[None]):

    async def show_file(self, index: int) -> None:
        if self._shown_index == index:
            return
        self._shown_index = index
        file = self.files[index]
        document = self.query_one("#source-document", VerticalScroll)
        await document.remove_children()
        await document.mount(*(
            SourceSection(section, file.get("navigation_type", "none"))
            for section in file["sections"]
        ))
        document.scroll_home(animate=False)
```

</figure>

The presentation of one section belongs to
[`SourceSection`](#complete-source-widgets-py "source_widgets.py"). Its
styling distinguishes supplied material from surrounding context, while
the text and location remain backend data:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-source-widgets-py">source_widgets.py</a></figcaption>

```python
class SourceSection(Vertical):

    def __init__(self, section: dict[str, Any], navigation_type: str) -> None:
        super().__init__(classes="source-section source-hit" if section["highlighted"] else "source-section")
        self.section = section
        self.navigation_type = navigation_type


    def compose(self) -> ComposeResult:
        label = "SUPPLIED TO MODEL" if self.section["highlighted"] else "Surrounding context"
        location = self.section.get("source_range")
        if location is not None:
            label += f" · {self.navigation_type}: {location[0]}–{location[1]}"
        yield Static(label, classes="source-section-label", markup=False)
        yield Static(self.section["section_id"], classes="list-hint", markup=False)
        yield Static(self.section.get("raw_content") or "(No text content)", markup=False)
```

</figure>

The `markup=False` arguments keep document text literal rather than treating
it as interface markup. Ranges are displayed using the file's navigation
type when a section has a location. They are useful provenance, but this
demo does not implement a jump into the original document.

![The source viewer displaying a saved reconstruction of Reaction_wheel.txt, with supplied material highlighted and surrounding context retained.]({{ '/assets/images/in-action-sources.svg' | relative_url }})

*The same source view introduced in Section 2. Its content now has a traceable
path from a saved turn through RAVEN to the highlighted sections.*

This is a readable reconstruction of parsed content, not a reproduction of
the original PDF or DOCX layout. A highlight means that a section was supplied
to the model. It does not establish that every claim in the answer follows
from that section; source inspection enables that check rather than replacing it.

### 8.6 Closing the view without leaving work running

The viewer owns its loading worker. Close and Escape both use
[`SourceViewer.action_close()`](#complete-source-widgets-py "source_widgets.py"),
which waits for that worker's cancellation before dismissing the screen:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-source-widgets-py">source_widgets.py</a></figcaption>

```python
class SourceViewer(ModalScreen[None]):

    @on(Button.Pressed, "#close-sources")
    async def action_close(self) -> None:
        await self.stop_loading()
        self.dismiss()


    async def stop_loading(self) -> None:
        if self._load_worker is not None and not self._load_worker.is_finished:
            self._load_worker.cancel()
            try:
                await self._load_worker.wait()
            except WorkerCancelled:
                pass


    async def on_unmount(self) -> None:
        await self.stop_loading()
```

</figure>

If the adapter is awaiting an unfinished reconstruction task, its exception
handler requests native task cancellation before propagating the cancellation.
The unmount hook applies the same cleanup when the screen is removed through
another route. Neither viewing nor rebuilding changes the chat history.

The complete source path is now:

**Saved answer → conversation and turn reference → cached or persisted
snapshot → optional rebuild from saved evidence → ordered file sections.**

The full implementations remain available in the expandable
[`source_widgets.py`](#complete-source-widgets-py "source_widgets.py"),
[`raven_client.py`](#complete-raven-client-py "raven_client.py"), and
[`app.py`](#complete-app-py "app.py") references above.

## 9. Reopening and checking the complete workflow

We have connected the visible workflow without moving RAVEN's document,
conversation, or agent logic into the interface. The final check is not
another model response. It is whether the workspace still makes sense after
the interface that created it has gone away.

### 9.1 Separating saved data from a running interface

The demo always uses `raven_home/` beside
[`app.py`](#complete-app-py "app.py"). RAVEN places user data beneath a
UUID-named directory inside that home. The demo uses RAVEN's default user
identity; it does not implement accounts or switch between users.

The main persisted parts are:

| Location beneath `raven_home/<user_id>/` | What it retains |
| --- | --- |
| `data/knowledge_base/<knowledge_name>/` | Knowledge metadata, the `file.sqlite3` file/section store, and Qdrant vectors |
| `data/conversations/<conversation_id>/` | Conversation metadata, canonical messages and saved turn results in `messages.sqlite3`, plus conversation memory and preferences |
| `operations/operations.sqlite3` | Operation/task records and replayable event history |
| `runtime_settings/runtime_settings.json` | Saved per-user runtime settings; the demo does not provide a settings editor |

These are backend-owned stores, not files for the interface to edit directly.
In particular, the source snapshot belongs to the saved turn result; it is
not a screenshot or an independently maintained TUI source database.

The active conversation selection, text draft, visible widgets, loading
workers, temporary sessions, and configured model handles belong to the
running application. This demo does not restore them as live objects after
a restart. Environment-based API keys likewise remain outside the saved
conversation and must be available to a new process when configuring a cloud
model again.

### 9.2 Checking continuity across a restart

We can trace the following sequence using the commands and controls from
Section 2. The observations matter more than the exact wording of a model's
answer:

1. **Create and ingest.** We create a knowledge, ingest the sample document,
   and wait for the confirmed result. Reopening that knowledge shows the
   stored file; a progress message alone is not the completion check.
2. **Ask and follow up.** We open a conversation and ask a document question.
   A successful source-backed turn shows tool activity, a saved answer, and
   a source action. A follow-up stays attached to the same conversation,
   even though a new temporary session carries out the turn.
3. **Inspect an earlier answer.** We open its source button, check the file
   and highlighted passage, and return to chat. The earlier button continues
   to identify that turn rather than the most recent response.
4. **Close and reopen.** After the work has finished, we exit gracefully
   with Ctrl+C and run the same launch command again, keeping the same
   home directory. `/conversations` lists the saved conversation by title;
   opening it restores canonical user/assistant messages and tool activity.
5. **Read the saved sources before configuring models.** The restored
   answer's source action can load its persisted reconstruction without
   inference. This distinguishes saved evidence from a fresh answer that
   merely happens to look similar.
6. **Resume model-dependent work.** We configure the model pair again before
   new chat or ingestion. The embedding model must remain compatible with
   the vectors already stored. Continuing the conversation then uses its
   backend-managed context and memory, not reconstructed UI text.

> **Keep the original embedding model.** Existing knowledge and conversation
> vectors cannot be used interchangeably with embeddings from another model.
> Equal vector dimensions do not establish compatibility. A different model
> requires rebuilding the affected embeddings; this demo does not provide
> that migration workflow.

When a turn was stopped or failed, reopening canonical history is also how
we check what actually committed. Partial text left on screen is not a
durability guarantee. After a hard process interruption, RAVEN's recovery
machinery detects interrupted work; reopening the application does not
automatically resume the agent loop or retry every failed task.

### 9.3 Keeping the application boundary small

The resulting division of work is deliberate:

| The demo implements | RAVEN supplies |
| --- | --- |
| Commands, forms, clickable lists, and the selected conversation | Knowledge and conversation objects, discovery, and persistence |
| Collecting model specifications and referencing environment credentials | Provider adapters and model configuration |
| Progress labels, separate thinking/tool/answer views, and Stop controls | Managed operation tasks, events, cancellation, and agent execution |
| Per-answer source references, a file picker, and section presentation | Saved turn results, evidence references, and reconstruction |
| Application startup and shutdown hooks | Backend resource lifecycle and recovery behavior |

This is still a focused demonstration: one main backend action at a time,
local file paths, automatic retrieval-mode selection, and no HTTP connection.
It does not expose the server's authentication boundary, a retry dashboard,
all runtime settings, or full knowledge/conversation administration. The
terminal instructions also remain intentional: the multiline-input workflow
is intended for the VS Code terminal, rather than promising identical key
handling in every Windows console.

Two lifecycle boundaries remain outside the demo. Exiting closes its RAVEN
resources, but **does not stop the external Ollama service or promise to
unload its resident models**. Also, RAVEN provides operation-cleanup
machinery, but this TUI does **not** schedule automatic history cleanup.
An application bridge or hosting backend decides and runs that retention
policy; saved event history should not be assumed to disappear on its own.

The [complete example archive]({{ '/assets/downloads/tui_example.zip' | relative_url }}){: download="tui_example.zip"}
and the [sample document]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }}){: download="Reaction_wheel.txt"}
are the same artifacts introduced in Section 2. The expandable source
references in this guide show how that application is connected, rather
than defining another implementation. A redistributable copy of the demo
should contain its source and instructions, not a populated `raven_home/`,
environment secrets, or machine-specific caches.

## 10. Conclusion

We began with a document and built a complete path from ingestion to a
conversation-backed answer and its inspectable sources. The resulting
application can preserve that interaction beyond a single run: a saved
conversation retains its turns, and an earlier answer remains connected
to the source material recorded with it. Reconstruction makes that material
available for checking, without treating the model's answer as its own proof.

The central design decision was to keep interaction separate from backend
execution and state. The TUI collects inputs, presents events, and waits for
confirmed results. RAVEN owns document processing, agent execution,
conversation memory, and persistence. This boundary lets us explain each
handoff without rebuilding the backend inside the interface.

The terminal is therefore one presentation of the workflow, not the workflow
itself. A different interface can retain the same pattern: submit a request,
show its progress, confirm its outcome, and keep the identifiers needed to
return to saved work. That is the reusable part of this example—a small
application layer that makes RAVEN's execution and stored evidence accessible
to the reader.
