---
layout: default
title: Integration guides
description: "Integrate RAVEN into applications and external tools through its Python API, HTTP server, and focused integration layers."
scripts:
  - /assets/js/source-links.js
---

# Integrating RAVEN into other systems

## 1. RAVEN as part of a larger system

RAVEN can provide the document and conversation backend of an application
without defining how that application looks or how its users interact with it.
A research workspace might present documents beside a chat interface. An
existing service might add questions over its reference material. An AI tool
might delegate document questions to RAVEN while continuing to handle other
tasks itself. Each uses the same backend capabilities through a different
interaction layer.

Integration is the connection between those systems: translating an action in
the surrounding application into a RAVEN request, then making its progress,
result, or failure useful to that application. A file upload can become an
ingestion request; a question can become a turn in a persistent conversation.
The surrounding system does not need to reproduce document parsing, retrieval,
conversation memory, or source reconstruction to offer those capabilities.

### Choosing the connection

RAVEN offers two entry points. Python applications can call its library API
directly. Applications running in another process or language can use its
optional HTTP server, including Server-Sent Events for observing long-running
work. The server exposes the same backend rather than a separate implementation
of ingestion or chat.

| Connection | Where it fits |
| --- | --- |
| **Python API** | A Python application creates a `Raven` instance and calls it within its own process. The [In Action walkthrough]({{ '/demo/' | relative_url }}) uses this approach for a terminal document assistant. |
| **HTTP API** | A desktop application, host backend, or external service sends authenticated requests to a separately running RAVEN server. The client does not need to import RAVEN's Python components or open its databases. |

If an external tool expects a different protocol, a small bridge can sit above
either entry point. Its job is to translate that tool's inputs into RAVEN calls
and shape the returned information for its client. Such a bridge is application
code, not a requirement imposed on every RAVEN integration.

### Keeping the responsibilities separate

RAVEN owns the document and conversation machinery: preparing documents,
retrieving source sections, running its agent, managing conversation memory,
and persisting the associated records. Operations and events make that work
observable and provide mechanisms for cancellation and eligible retries.

The surrounding application decides how those capabilities are used:

- **Interaction:** which actions become buttons, commands, or tools, and which
  knowledge or conversation each request targets.
- **Presentation:** whether results appear as streamed text, a completed answer,
  source material, or structured data passed to another system.
- **Deployment:** how the server and model services are started, how credentials
  are supplied, and, in a hosted application, how authenticated users are mapped
  to their RAVEN user identities.
- **Application policy:** which features are exposed, how failures and retry
  actions are presented, and when retained operation history is cleaned up.

These decisions need not expose every backend feature. A focused integration
can provide only ingestion and conversation tools, while another application
also exposes direct retrieval, source inspection, or operation management.
The important boundary is that **the integration layer connects the systems;
RAVEN remains responsible for performing the backend work.**

The following sections work through one concrete integration, from running it
to tracing its requests through the implementation. It illustrates how an
external tool can use RAVEN's HTTP interface; it is not the only way to integrate
RAVEN. The [architecture page]({{ '/architecture/' | relative_url }}) provides
the deeper explanation of the backend behind those calls.

## 2. An MCP bridge: prepare and launch the example

One way to connect RAVEN to an external tool is through the **Model Context
Protocol (MCP)**. An MCP server describes the tools it offers and the arguments
they accept; a compatible client can discover and invoke them. For this
example, we use the official [MCP Python SDK](https://pypi.org/project/mcp/)
to build **RavenMCP**, a small server
that translates tool calls into requests to RAVEN's HTTP API.

We begin with [MCP Inspector](https://modelcontextprotocol.io/docs/tools/inspector),
a browser-based interface for discovering and calling MCP tools. Inspector lets
us submit each call directly and inspect its result before involving another
AI agent. A later section connects the same bridge to Codex; no changes to the
Python bridge are needed for that route.

### What the bridge exposes

RavenMCP provides seven tools for one document-questioning workflow:

| Tool | Purpose |
| --- | --- |
| `configure_models` | Configure an installed Ollama LLM and embedding model for RAVEN. |
| `create_knowledge` | Create a named knowledge for related documents. |
| `list_knowledges` | Discover existing knowledges and their summaries. |
| `ingest_file` | Upload a local document into an existing knowledge and wait for ingestion. |
| `create_conversation` | Create a local conversation bound to a knowledge, or a global conversation without that binding. |
| `list_conversations` | Find saved conversations by title and reuse their IDs. |
| `ask_raven` | Submit a question in a conversation and return the completed answer and available evidence references. |

The connection has two parts: **Inspector communicates with RavenMCP over
stdio; RavenMCP communicates with RAVEN through authenticated HTTP requests.**
Stdio here means the bridge process's standard input and output, not an
additional HTTP endpoint. Inspector starts the bridge process; the RAVEN server
runs separately and owns model configuration, document processing, and conversation data.

Calling `ask_raven` asks RAVEN's own agent to handle the question. The bridge
does not implement another retrieval pipeline or generate the answer itself.
This example uses Ollama, automatic retrieval selection, and completed MCP
results rather than streamed tokens. It leaves out cloud-provider controls,
resource deletion, retry controls, and a reconstruction viewer to keep the
integration focused.

### Prerequisites

> **Tested on Windows.** The commands below use PowerShell, and the supplied
> `start.ps1` launcher uses Windows-specific process handling. Other operating
> systems require different launch commands; installing PowerShell alone does
> not make this launcher portable.

Install the following before continuing, then open a PowerShell terminal:

| Requirement | Purpose |
| --- | --- |
| Python 3.11 or newer, available as `python` | Runs the bridge and the RAVEN server. |
| [Node.js](https://nodejs.org/en/download) 22.19.0 or newer, with npm | Runs Inspector through `npx`. Node.js is needed for this Inspector route, not by RAVEN itself. |
| [Ollama](https://ollama.com/download) | Runs the local models. This walkthrough follows RAVEN v0.2.0's Ollama 0.34.2 setup. |

RAVEN and the bridge do not install or manage the Ollama service. Open Ollama
and leave it running at `http://127.0.0.1:11434`. If it is not already running,
start it in a separate terminal:

```powershell
ollama serve
```

Do not start a second instance if the service is already active. In the terminal
used for the walkthrough, download the example's models:

```powershell
ollama pull gemma4:e2b
ollama pull bge-m3:latest
ollama list
```

`gemma4:e2b` supplies generation and agent tool calls; `bge-m3:latest` supplies
embeddings. These downloads may take time and require sufficient disk space
and memory. Another installed tool-calling LLM can be selected later if needed.
Downloading the models does not configure them inside RAVEN; that is a tool
call in the next section.

### Download and extract the example

Download these files into a working directory:

- [RavenMCP source — mcp_example.zip]({{ '/assets/downloads/mcp_example.zip' | relative_url }}){: download="mcp_example.zip"}
- [Reaction_wheel.txt]({{ '/assets/downloads/test_files/Reaction_wheel.txt' | relative_url }}){: download="Reaction_wheel.txt"}
- [EPS.txt]({{ '/assets/downloads/test_files/EPS.txt' | relative_url }}){: download="EPS.txt"}
- [Clinical.txt]({{ '/assets/downloads/test_files/Clinical.txt' | relative_url }}){: download="Clinical.txt"}

The walkthrough uses `Reaction_wheel.txt`; the other files provide additional
documents to explore. They are illustrative inputs, not validated engineering
or medical guidance. If a text link opens in the browser instead of downloading,
save it as plain text with the stated filename.

Open PowerShell in that working directory and extract the archive:

```powershell
Expand-Archive -LiteralPath .\mcp_example.zip -DestinationPath .
```

Keep the five files together in the resulting `mcp_example/` folder:

```text
raven-demo/
├── mcp_example/
│   ├── README.md
│   ├── requirements.txt
│   ├── server.py
│   ├── client.py
│   └── start.ps1
├── Reaction_wheel.txt
├── EPS.txt
└── Clinical.txt
```

The working directory can have any name. Run the remaining commands from the
**parent containing `mcp_example/`**, not from inside that folder. No clone of
the RAVEN repository is required; the dependencies install RAVEN from PyPI.

### Install the Python dependencies

Create and activate a virtual environment, then install the supplied requirements:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r .\mcp_example\requirements.txt
```

The requirements install `noomexai-raven[server]==0.2.0`, `mcp[cli]==2.3.0`,
`httpx2==2.12.0`, and `anyio>=4.9,<5`, along with their dependencies. The server
extra is needed here because the bridge calls RAVEN over HTTP. Keep this
environment activated for the launcher.

If PowerShell blocks environment activation or the launcher, allow scripts in
the current terminal process, then repeat the blocked command:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

This does not change the machine-wide execution policy. An organization-managed
policy may still prevent script execution.

### Launch RAVEN and Inspector

From the same activated terminal, run:

```powershell
.\mcp_example\start.ps1
```

The launcher starts RAVEN, waits for an authenticated runtime response, and
launches Inspector. If RAVEN is already reachable for the selected home, it
reuses that server. Inspector 2.12.0 is downloaded through `npx` on first use;
allow the package download if npm prompts for confirmation. A separate global
Inspector installation is not required.

Open the browser address printed by Inspector if it does not open automatically.
Use that address as printed, including its Inspector access token. Connect to
the supplied stdio server, then open its tool list. The seven RavenMCP tools
listed above should appear. No document question is sent and no models are
configured merely by opening this connection.

The launcher supplies RAVEN's bearer token automatically. **Do not paste it into
Inspector or publish it.** Inspector's own access token protects its interface;
it is separate from the service token used in the bridge's HTTP requests.

### Storage, startup problems, and shutdown

RAVEN's default address is `http://127.0.0.1:8765`. This is the backend address,
not the browser address of Inspector. The launcher uses `raven_home/` beside
`start.ps1` for persistent data, creating it on first startup. A fresh home
contains no ingested knowledge or conversations; an existing home keeps its
saved data. Model configuration must be repeated after restarting RAVEN.

If another RAVEN server owns port 8765 with a different home, stop it before
starting this example. If startup fails, inspect `mcp_example/.tmp/raven.stderr.log`.
The launcher's host and port follow the selected home's
`system_settings/system_settings.json` when that file exists.

For tool discovery without opening the browser, run the following instead of
the normal launcher command:

```powershell
.\mcp_example\start.ps1 -Check
```

Keep the launcher terminal open during the walkthrough. Finish active work,
then press **Ctrl+C in that terminal** to stop Inspector. Closing its browser
tab alone does not stop it. On normal exit, the launcher terminates a RAVEN
process it started; a server it reused remains running. This termination is not
a guaranteed graceful drain, so do not use it as a way to finish an active
ingestion or chat turn. Ollama remains running independently.

At this point, the connection is ready for tool calls. The next section
configures RAVEN's model pair and follows a document through ingestion, a
conversation, and a follow-up question.

## 3. Complete the workflow in Inspector

Keep the launcher running and open RavenMCP's **Tools** view in Inspector.
Selecting a tool displays its description and argument form. Fill the fields,
then select **Execute Tool**. The JSON blocks below show
the exact **tool arguments**, not an HTTP request or a complete MCP protocol
message. Enter the corresponding values in Inspector's form.

The screenshots show the input forms before submission. Paths and conversation
IDs shown as examples must be replaced with the values from the current workflow.

Use the same RAVEN home throughout this workflow. Each successful call changes
or reads that home's state; Inspector is not displaying a separate sample
database.

### Step 1 — Configure the model pair

Select `configure_models` and supply the names downloaded in section 2:

```json
{
  "llm_model": "gemma4:e2b",
  "embedding_model": "bge-m3:latest"
}
```

![Inspector's configure_models form with gemma4:e2b as the LLM and bge-m3:latest as the embedding model.]({{ '/assets/images/integration-models.jpg' | relative_url }})

*Model configuration: both roles are required. The tool list remains available
on the left.*

Both fields are required. Use the exact names shown by `ollama list` if a
different installed pair is selected. No provider name or API key is needed:
this bridge always builds Ollama specifications for the two roles.

Wait for the call to return successfully before continuing. RAVEN prepares its
model adapters and preloads the selected models. The result contains
`operation_id`, `task_id`, `status: "completed"`, and `models`, identifying the
submitted pair. Reconfiguring may unload replaced Ollama models; it is not
merely a change to the labels shown in Inspector.

> **Configuration is not transactional.** RAVEN installs the new model adapters
> before preloading them. If preloading fails, the operation reports failure,
> but the newly configured pair can remain installed; the previous configuration
> is not automatically restored. Inspect the operation and server logs before
> continuing. `GET /api/v1/provider/models/configured` shows the configured pair,
> not proof that preloading succeeded. Resolve the failure and deliberately
> configure the intended pair again before ingestion or chat.

> **Reconfiguration may also be needed without a server restart.** The server
> can evict an idle user's runtime after 30 minutes by default
> (`runtime_idle_seconds`), or evict an unused runtime when space is needed for
> another user. Its replacement has no configured models. An open MCP connection
> alone does not prevent eviction. If this happens, call `configure_models`
> again with the intended pair; saved knowledge and conversations remain on disk.

Configuration, ingestion, and chat return their final result after waiting for
the backend operation. There is no live token or progress stream in this MCP
example. The bridge waits up to ten minutes; Inspector can impose its own
shorter timeout. For slow operations, increase the request timeout in the
connected server's settings before submitting the call. Allowing 660 seconds
(660000 milliseconds if that field uses milliseconds) accommodates the bridge's
wait and follow-up requests.

> **Choose the embedding model before ingestion and keep it fixed.** Stored
> vectors must be searched with the embedding model that generated them.
> Changing the LLM is a separate decision; replacing the embedding model
> requires rebuilding the affected embeddings.

### Step 2 — Find or create a knowledge

Select `list_knowledges` and call it with:

```json
{
  "limit": 10
}
```

Both arguments are optional: `limit` defaults to 10 and `after_name` defaults
to null. With a fresh home, the result is:

```json
{
  "items": [],
  "next_cursor": null
}
```

If `engineering` is already listed, reuse it. Otherwise select
`create_knowledge` and submit:

```json
{
  "name": "engineering",
  "user_summary": "Engineering reference documents"
}
```

![Inspector's create_knowledge form with the name engineering and an optional summary.]({{ '/assets/images/integration-create-knowledge.jpg' | relative_url }})

*Create a knowledge by name, with an optional description of its contents.*

`name` is required. `user_summary` is optional and defaults to an empty string.
The result contains `schema_version`, `name`, `created_at`, and `user_summary`.
This creates the persistent knowledge, but does not ingest a file or select it
as an implicit target for later tools. Those calls still need its name.

An existing name produces a conflict rather than overwriting the knowledge.
Call `list_knowledges` again to confirm that `engineering` is available. Its
items contain `name`, `user_summary`, `created_at`, and `count`; `count` is the
backend's vector count, not a file count.

### Step 3 — Ingest the sample document

The `ingest_file` tool requires an **absolute path on the machine running the
MCP bridge**. It reads that local file and uploads its contents to RAVEN. It
does not ask the RAVEN server to open the supplied path.

In the PowerShell working directory where the sample was saved, obtain its
absolute path:

```powershell
(Resolve-Path -LiteralPath .\Reaction_wheel.txt).Path
```

Select `ingest_file`. Both fields are required. The following example assumes
the working directory is `E:/raven-demo`; replace `file_path` with the actual
path returned by PowerShell:

```json
{
  "knowledge_name": "engineering",
  "file_path": "E:/raven-demo/Reaction_wheel.txt"
}
```

![Inspector's ingest_file form with engineering as the target knowledge and an illustrative absolute path to Reaction_wheel.txt.]({{ '/assets/images/integration-ingest.jpg' | relative_url }})

*Ingestion takes the target knowledge and an absolute file path. Replace this
example path with the location of the downloaded document.*

Forward slashes are valid in this Windows path. If a JSON object is entered
using backslashes instead, escape each one as `\\`; Inspector's ordinary path
field accepts the path itself without JSON escaping.

Wait for the tool to finish. A successful result includes `operation_id`,
`task_id`, `status: "completed"`, `knowledge_name`, and `file_name`. The filename
should be `Reaction_wheel.txt`. This means ingestion completed, not just that
an upload was accepted.

The bridge accepts `.txt`, `.md`, `.pdf`, and `.docx` files up to 64 MiB; RAVEN
may enforce a lower upload limit. Missing paths, empty files, unsupported
formats, and duplicate filenames produce errors. Do not ingest the same file
again simply to continue a conversation. When repeating this walkthrough with
an existing home, skip this step if the sample has already been ingested.

### Step 4 — Create a conversation

Select `create_conversation` and bind the discussion to the ingested knowledge:

```json
{
  "knowledge_name": "engineering"
}
```

![Inspector's create_conversation form with the optional knowledge name set to engineering for a local conversation.]({{ '/assets/images/integration-create-conversation.jpg' | relative_url }})

*Supplying a knowledge name binds the new conversation to that knowledge.*

The result contains a generated `conversation_id`, `type: "local"`, and
`knowledge_name: "engineering"`, together with the conversation's title,
creation time, and other metadata. A new conversation starts with the title
`New Conversation`. Creating it does not send a message to the model.

**Copy the returned `conversation_id`.** It identifies the persistent
discussion and is required for every `ask_raven` call. Do not create another
conversation for each question.

The knowledge binding is optional. To create a **global** conversation instead,
omit the field:

```json
{}
```

This returns `type: "global"` and `knowledge_name: null`. A local conversation
is restricted to its knowledge; a global conversation can work across the
knowledge base. Choose one scope for the walkthrough rather than submitting
both creation examples unless two separate conversations are wanted. An empty
string is not a substitute for omitting the knowledge name.

### Step 5 — Ask a document question

Select `ask_raven`. Both `conversation_id` and a nonblank `user_query` are
required; the query may contain up to 32768 characters. Replace the illustrative
ID below with the one returned by the preceding creation call:

```json
{
  "conversation_id": "a7c92f4b1d60",
  "user_query": "Use retrieval to search engineering. According to Reaction_wheel.txt, what is the maximum time allowed to terminate calibration and enter magnetic-torquer-only safe-hold after the specified reaction-wheel anomaly? Give the limit in milliseconds and name the file."
}
```

![Inspector's ask_raven form with an illustrative conversation ID and a question about the reaction-wheel safe-hold execution window.]({{ '/assets/images/integration-ask.jpg' | relative_url }})

*Send a question using the conversation ID returned by RAVEN. The full query
is shown in the JSON example above; the form's text field scrolls horizontally.*

The bridge submits a conversation turn with retrieval set to `auto`. RAVEN's
agent chooses from the tools allowed by that conversation's scope, generates
an answer, and saves the completed turn. The bridge then reads that saved turn
and returns a focused result:

| Field | Meaning |
| --- | --- |
| `conversation_id` | The discussion used for the question. |
| `turn_id` | The saved interaction within that conversation. |
| `operation_id`, `task_id` | The backend execution that performed the work. |
| `status` | `completed` when the tool successfully obtains the saved answer. |
| `response` | The model's answer text. |
| `evidence` | Source references, each containing `knowledge_name`, `file_name`, and `section_id`, when obtained during the run. |
| `iteration_limit_reached` | Whether the agent reached its iteration bound. Inspect the answer if this is true. |

Check the answer against the sample document. A source-backed result can identify sections from
`engineering` / `Reaction_wheel.txt`; section IDs depend on that ingestion.
The bridge does not return full section contents, thinking, internal tool
traces, or reconstructed files.

An operation completing successfully does not guarantee an accurate answer.
Small models can choose tools imperfectly or omit conditions from the source.
The explicit retrieval instruction makes the intended action clear, but the
answer still needs to be checked against the document. Evidence identifies
material supplied to the model, not proof that every generated claim is correct.

### Step 6 — Continue the same conversation

Keep the same conversation ID and submit another `ask_raven` call:

```json
{
  "conversation_id": "a7c92f4b1d60",
  "user_query": "What battery state-of-charge condition must be satisfied before that safe-hold sequence is permitted? Use the engineering source and preserve the threshold's qualifier."
}
```

Again, replace the illustrative ID with the real one. The sample states that
battery state of charge must be **above 14%**; this is not the same as saying
that 14% itself is sufficient.

RAVEN retains the conversation's messages and tool exchanges, so the follow-up
can refer to the preceding question. The bridge does not send its own copy of
the chat history. A follow-up may use context already available in conversation
memory instead of calling retrieval again; an empty `evidence` list does not
by itself mean that the turn failed or that earlier source context was absent.

Wait for one turn to finish before starting another in the same conversation.
RAVEN rejects competing turns rather than allowing overlapping writes to that
discussion.

### Step 7 — Find the discussion again

Select `list_conversations` and submit:

```json
{
  "limit": 10
}
```

The result contains `items` and `next_cursor`. Conversations are listed newest
first, with their IDs, titles, local/global types, knowledge bindings, pin
state, and creation metadata—not their full messages. After the first completed
turn, RAVEN normally generates a title that helps identify the discussion.

Choose the desired entry and reuse its `conversation_id` in `ask_raven`.
The list does not need configured models. After restarting RAVEN, configure
the models again before sending another question; the saved conversations and
knowledge remain available in the same home.

### Reading subsequent pages

Both list tools default to 10 items. They accept `limit` values from 1 to 100,
subject to the server's configured maximum. A null `next_cursor` means there
is no next page. Otherwise, copy the exact returned cursor into the matching
argument for the next call:

| Tool | First page | Next page |
| --- | --- | --- |
| `list_knowledges` | Omit `after_name`, or set it to null. | Set `after_name` to the preceding result's `next_cursor`. |
| `list_conversations` | Omit `after_conversation_id`, or set it to null. | Set `after_conversation_id` to the preceding result's `next_cursor`. |

For example, if a knowledge page returns `next_cursor: "engineering"`, the
next `list_knowledges` call is:

```json
{
  "limit": 10,
  "after_name": "engineering"
}
```

For `list_conversations`, an illustrative next-page call is:

```json
{
  "limit": 10,
  "after_conversation_id": "a7c92f4b1d60"
}
```

Replace that ID with the returned cursor, not an arbitrary conversation ID.
Omitting both arguments starts either list at its first page with the default
size.

### If a call fails

An argument or backend failure appears as a tool error rather than a successful
result. Correct a rejected input before trying again. If a long-running call
times out or the connection drops, retain any returned operation and turn IDs:
the backend may already have accepted or saved the work. Do not immediately
repeat a creation or ingestion call and assume nothing happened.

Use `list_knowledges` and `list_conversations` to rediscover resources. For an
uncertain operation, the RAVEN HTTP API exposes its status through
`GET /api/v1/operations/{operation_id}`; this small bridge has no separate
operation-status or retry tool. Cancelling a call requests cancellation of a
known backend operation, but does not promise rollback of committed data.

The complete workflow now passes through the external interface: tools create
and discover resources, a file reaches RAVEN through an upload, and a saved
conversation supports more than one question. The next section uses the same
tools from Codex instead of invoking them manually in Inspector.

## 4. Connect the bridge to Codex

Inspector makes each tool call explicit: select a tool, fill its arguments,
and execute it. An AI client can use the same definitions to choose and invoke
tools from a request written in natural language. Here, we connect RavenMCP
to Codex without changing the bridge's Python files.

Keep the extracted example, installed Python dependencies, and running Ollama
service from section 2. Install and sign in to the Codex desktop app if needed.
**Inspector and Node.js are not required for this route.** Codex communicates
with the bridge over stdio; the bridge continues to call RAVEN over HTTP.
The commands below retain the Windows/PowerShell setup used earlier.

### Step 1 — Start RAVEN separately

Finish any active Inspector calls before stopping its launcher. If that launcher
started RAVEN, its normal exit also terminates that server. A server it reused
is left running; do not start another instance for the same home or port.

If RAVEN is not already running, start it from the parent of `mcp_example/`,
with the virtual environment activated:

```powershell
nraven serve --home .\mcp_example\raven_home
```

Keep this terminal open. Use the same home as the Inspector walkthrough to
retain its knowledge and conversations. Starting the server does not configure
the models; configure them again after a server restart before ingestion or chat.

### Step 2 — Add project-local MCP configuration

Open the working directory containing `mcp_example/` as a project in Codex.
Create a `.codex/` folder in that directory, then create `config.toml` inside it:

```text
raven-demo/
├── .codex/
│   └── config.toml
├── .venv/
└── mcp_example/
```

This configuration belongs to the chosen project, not inside `mcp_example/`
or RAVEN's home. If a configuration file already exists, add the RavenMCP entry
without replacing unrelated settings. Codex loads project-local configuration
for trusted projects; the supported connection options are documented in the
[official Codex MCP documentation](https://developers.openai.com/codex/mcp).

From the activated PowerShell terminal, find the environment's Python executable:

```powershell
python -c "import sys; print(sys.executable)"
```

Add the following to `.codex/config.toml`. This example assumes the working
directory is `E:/raven-demo`; **replace all paths with the actual locations**,
including the Python path printed above:

```toml
[mcp_servers.RavenMCP]
command = "powershell.exe"
args = [
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", "E:/raven-demo/mcp_example/start.ps1",
    "-RavenHome", "E:/raven-demo/mcp_example/raven_home",
    "-McpServer",
    "-PythonExecutable", "E:/raven-demo/.venv/Scripts/python.exe",
]
cwd = "E:/raven-demo"
startup_timeout_sec = 30
tool_timeout_sec = 660
```

Each argument is a separate array entry, so paths containing spaces remain
single arguments. Forward slashes avoid TOML's backslash-escaping requirements
for Windows paths.

| Setting or argument | Purpose |
| --- | --- |
| `command` and `-File` | Start PowerShell and run the supplied launcher. The execution-policy override applies to this PowerShell process, not the machine-wide policy. |
| `-RavenHome` | Identify the same home used by the running RAVEN server. |
| `-McpServer` | Select bridge-only mode, without starting RAVEN or Inspector. |
| `-PythonExecutable` | Select the environment containing the example's dependencies; Codex does not need to inherit terminal activation. |
| `cwd` | Set the launch working directory. |
| `startup_timeout_sec` | Allow 30 seconds for the MCP bridge to initialize, not for model configuration or ingestion. |
| `tool_timeout_sec` | Allow 660 seconds for a tool call, accommodating the bridge's ten-minute operation wait and follow-up requests. It does not extend the bridge's own wait limit. |

**Keep `-McpServer` in the configuration.** Normal launcher mode opens Inspector
and prints launcher messages; it is not the stdio MCP server Codex should run.
Bridge-only mode starts `server.py` and reserves stdout for MCP messages.

No bearer token belongs in the TOML file. The launcher reads the running
server's token from the selected home's `temp/server_auth/bearer_token`, then
supplies it to the bridge process. That token authenticates the bridge's HTTP
requests; it is not an Ollama API key or a Codex account credential.

### Step 3 — Load and check the connection

Save the configuration, trust the project when appropriate, and restart Codex
with that project open. Ask it to make a read-only call first:

> Use RavenMCP's `list_knowledges` tool and show the available knowledge names
> and summaries. Use the MCP tool rather than inspecting local database files.

Codex should invoke the tool and report its result. An empty `items` list is a
valid result for a fresh home, not a connection failure. If the tool is
unavailable, check the project location, trust status, configuration paths,
and whether Codex was restarted. A missing-token error usually means RAVEN
was not started first or `-RavenHome` points to a different home.

Tool calls can read or change RAVEN state. Review any approval request shown
by the client, especially for model configuration, resource creation, or file
ingestion. The bridge's tool descriptions do not replace the client's access
and approval controls.

### Step 4 — Use the document workflow through the agent

The tool inputs and results are the same as in section 3. The difference is
that requests now describe the intended actions to Codex rather than entering
each argument in Inspector. Keep the requests specific and let each operation
finish before submitting the next one.

Configure RAVEN's installed model pair:

> Use RavenMCP's `configure_models` with `llm_model` set to `gemma4:e2b` and
> `embedding_model` set to `bge-m3:latest`. Wait for completion before continuing.

Keep the embedding model used for any existing knowledge. This call configures
**RAVEN's models**, not the model running Codex.

Discover or create the target knowledge:

> Use RavenMCP to find the knowledge named `engineering`. Reuse it if it exists;
> otherwise create it with the summary “Engineering reference documents”.

If the sample was already ingested in Inspector, reuse it and skip the upload.
For a fresh knowledge, use the following request, replacing the example path
with the sample's actual absolute path:

> Use RavenMCP's `ingest_file` to add `E:/raven-demo/Reaction_wheel.txt` to
> `engineering`. Wait for ingestion to complete.

The path is read on the bridge's machine and the file is uploaded to RAVEN.
Only grant this local-file tool to trusted callers. The example does not expose
a file-listing tool, so do not assume the client can check for an existing
filename through `list_knowledges` alone.

For a new discussion, request a local conversation:

> Use RavenMCP's `create_conversation` with `knowledge_name` set to `engineering`.
> Show the returned conversation ID and use it for the following questions.

To continue a discussion created earlier instead, ask for `list_conversations`,
choose its entry by title, and specify that ID. Creating another conversation
is not required to reopen one. A global conversation is also possible by
omitting `knowledge_name`, as in the Inspector workflow.

Then ask a source question:

> Use RavenMCP's `ask_raven` with that conversation ID. Ask RAVEN: “Use retrieval
> to search engineering. According to Reaction_wheel.txt, what is the maximum
> time allowed to enter magnetic-torquer-only safe-hold after the specified
> reaction-wheel anomaly? Give the limit in milliseconds and name the file.”
> Show RAVEN's response and any returned evidence references.

Continue with the same ID:

> Use `ask_raven` again in the same RAVEN conversation. Ask: “What battery
> state-of-charge condition must be satisfied before that sequence is permitted?
> Preserve the threshold's qualifier.”

The Codex chat and the RAVEN conversation are separate. Codex selects MCP tools;
RAVEN's agent selects its own internal tools and produces the saved answer.
The RAVEN conversation ID preserves that backend discussion across calls, even
when a different client is used later. Inspect the actual tool results and
source material rather than treating a natural-language request as a guarantee
of a particular tool sequence or a correct answer.

### Reconnect, stop, or remove the connection

- **After restarting RAVEN:** keep the same home, reconnect RavenMCP so its
  process reads the new service token, and configure RAVEN's models again.
  Saved knowledge and conversations remain on disk.
- **After moving the example or its environment:** update the absolute paths in
  the project configuration and restart the MCP connection.
- **When finished:** wait for active calls to finish, then stop the separately
  running RAVEN server with Ctrl+C in its terminal. Closing Codex does not stop
  that server, and neither action stops the external Ollama service.
- **To remove the integration:** remove only the `[mcp_servers.RavenMCP]` entry
  and its settings, then restart Codex. If the file contains nothing else, it
  can be deleted. Keep local configuration and service credentials out of Git.

The same bridge now serves two kinds of client: a manual interface for
inspecting individual calls and an agent that can invoke those calls as part
of a broader task. The next section turns to the implementation, starting with
the authenticated HTTP connection shared by the tools.

## 5. Building the authenticated HTTP connection

The preceding sections use the finished example. From here, we retrace its
construction: which responsibilities belong in the bridge, which requests cross
the HTTP boundary, and which work stays inside RAVEN. These are explanations of
the downloaded files, not additional setup steps or a second application to run.

We start with one connection shared by the tools. In the
[In Action example]({{ '/demo/' | relative_url }}),
the application constructs a `Raven` object in its own process. Here, RAVEN is
already running in a separate process, so the bridge needs an HTTP client instead
of another backend instance. It does not open knowledge or conversation databases.

The implementation has two Python files:

| File | Responsibility |
| --- | --- |
| [`server.py`](#complete-mcp-server-py "server.py") | Define the MCP server, establish the shared connection during its lifespan, and register the tool-facing functions. |
| [`client.py`](#complete-mcp-client-py "client.py") | Translate those functions into authenticated RAVEN requests, wait for operations, and prepare their results. |

The snippets below are excerpts from those files. Imports and helper methods
are supplied by the expandable complete-file panels; repeated class headers
identify where a method belongs, not separate classes to add repeatedly. The
full files also contain the operation and answer-handling code discussed later.

### 5.1 Establishing the connection once

We define [`lifespan()`](#complete-mcp-server-py "server.py") in
[`server.py`](#complete-mcp-server-py "server.py"). It reads the bridge's
connection settings, validates them, and keeps one asynchronous HTTP client
open while the MCP server runs:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-server-py">server.py</a></figcaption>

```python
@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncGenerator[RavenClient, None]:
    token = os.environ.get("RAVEN_TOKEN", "").strip()
    if not token or any(character.isspace() for character in token):
        raise ValueError("Set RAVEN_TOKEN to the current RAVEN service bearer token.")

    base_url = os.environ.get("RAVEN_URL", "http://127.0.0.1:8765").strip()
    try:
        url = urlsplit(base_url)
        valid_url = (
            url.scheme in ("http", "https")
            and bool(url.hostname)
            and url.username is None
            and url.password is None
            and url.path in ("", "/")
            and not url.query
            and not url.fragment
        )
        _ = url.port
    except ValueError:
        valid_url = False
    if not valid_url:
        raise ValueError("RAVEN_URL must be an HTTP(S) origin without credentials or a path.")

    headers = {"Authorization": f"Bearer {token}"}
    user_id = os.environ.get("RAVEN_USER_ID", "").strip()
    if user_id:
        try:
            headers["X-Raven-User-ID"] = str(UUID(user_id))
        except ValueError:
            raise ValueError("RAVEN_USER_ID must be a valid UUID when supplied.") from None

    async with httpx.AsyncClient(
        base_url=base_url,
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=5.0),
        follow_redirects=False,
        trust_env=False,
    ) as http:
        yield RavenClient(http)
```

</figure>

The file imports `httpx2 as httpx`, `os`, `urlsplit` from `urllib.parse`,
`UUID` from `uuid`, `asynccontextmanager` from `contextlib`, and `AsyncGenerator`
from `collections.abc`. `MCPServer` comes from the installed MCP SDK, while
[`RavenClient`](#complete-mcp-client-py "client.py") is our own class.

There are three settings at this boundary:

- **`RAVEN_URL`** identifies the RAVEN HTTP origin, such as
  `http://127.0.0.1:8765`. Endpoint paths are added by the client methods.
  Credentials, a non-root path, a query, or a fragment are not accepted here.
- **`RAVEN_TOKEN`** supplies the service bearer token. Missing or whitespace-
  containing values stop bridge initialization; the token is not a tool argument.
- **`RAVEN_USER_ID`** optionally supplies a UUID for the trusted user-context
  header. The local walkthrough leaves it unset and uses RAVEN's default user.
  A UUID is not proof of user identity: a hosted application must authenticate
  and authorize that identity before forwarding it. This fixed-context bridge
  is not a multi-user authentication service.

The HTTP client attaches the headers to each request. Its 30-second timeout,
with a five-second connection timeout, concerns individual HTTP exchanges—not
the full duration of ingestion or a model response. Long-running work is
submitted and observed through separate requests, as section 7 will explain.

We also disable redirects and environment-derived proxy configuration. The
connection therefore uses the explicitly selected origin rather than following
a redirect or inheriting a proxy from the launching shell. This does not replace
deployment security: use a trusted local service for this example, and protect
credentials and transport when adapting it to another environment.

### 5.2 Passing the shared connection to tools

The yielded object is a small application-side client:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:
    OPERATION_TIMEOUT_SECONDS = 600.0
    OPERATION_POLL_SECONDS = 1.0
    MAX_UPLOAD_BYTES = 64 * 1024 * 1024

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
```

</figure>

[`RavenClient`](#complete-mcp-client-py "client.py") holds the connection, not
a second copy of RAVEN's state. Its methods use
[`_request()`](#complete-mcp-client-py "client.py") to send requests, check
HTTP status and JSON shape, and turn expected failures into MCP tool errors.
That helper does not forward arbitrary backend response bodies or exception
strings to the caller.

We then attach [`lifespan()`](#complete-mcp-server-py "server.py") to the MCP
server. This registration comes after the function definition in
[`server.py`](#complete-mcp-server-py "server.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-server-py">server.py</a></figcaption>

```python
mcp = MCPServer(
    "RavenMCP",
    instructions=(
        "Configure installed Ollama models, create knowledge, and discover it with list_knowledges. "
        "Upload documents with ingest_file, then create a conversation and ask_raven. "
        "A knowledge_name creates a local conversation; omitting it creates a global one. "
        "Find existing conversations with list_conversations and reuse their IDs for follow-ups."
    ),
    lifespan=lifespan,
    log_level="WARNING",
)
```

</figure>

Tool functions receive an SDK `Context` and obtain the yielded
[`RavenClient`](#complete-mcp-client-py "client.py") through
`ctx.request_context.lifespan_context`. No new HTTP client is constructed for
each call. When the lifespan exits, the `async with` block closes its connection
pool; it does not shut down the separately running RAVEN server or Ollama.

### 5.3 Supplying credentials without exposing them as arguments

The Python bridge expects environment variables; it does not search for token
files itself. The Windows launcher,
`start.ps1`, supplies them in its `-McpServer` branch immediately before
starting [`server.py`](#complete-mcp-server-py "server.py"):

1. Resolve the selected home and the server origin.
2. Read the current token from `temp/server_auth/bearer_token` beneath that home.
3. Set `RAVEN_URL` and `RAVEN_TOKEN` in the child process's environment.
4. Run the Python bridge using the selected interpreter.

This keeps the token out of MCP tool inputs and process command-line arguments.
It also avoids relying on Inspector to forward arbitrary environment variables
from the original terminal. Both Inspector and the Codex configuration use that
same bridge-only branch. After a RAVEN restart rotates the token, reconnect the
bridge so it reads the new value.

Finally, stdio is the MCP transport, so stdout must contain protocol messages,
not application diagnostics. We do not add `print()` calls to the bridge's
request path. RAVEN's HTTP server has its own process and logs.

The complete MCP-facing file is available here. Its tool registrations are
the subject of the next section:

{% include source_file.html name="server.py" source="mcp_example/server.py" language="python" id="complete-mcp-server-py" %}

## 6. Registering tools and translating requests

With the connection in place, we define what an external caller can ask RAVEN
to do. We do not expose every HTTP route as an MCP tool. The example needs a
small document workflow, so its contract consists of seven actions with explicit
inputs and bounded responsibilities.

Each call crosses two interfaces:

1. A function in [`server.py`](#complete-mcp-server-py "server.py") defines
   the MCP tool's arguments and description, then delegates to the shared client.
2. A method in [`RavenClient`](#complete-mcp-client-py "client.py") selects
   the HTTP method, endpoint, and request data that RAVEN expects.

This separation lets us change a tool's presentation without duplicating
RAVEN's ingestion, conversation, or agent implementation.

### 6.1 Starting with a simple creation tool

The [`create_knowledge()`](#complete-mcp-server-py "server.py") tool is defined
at module level, not inside a class. Its registration contains both the input
constraints and the usage guidance exposed to the client:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-server-py">server.py</a></figcaption>

```python
@mcp.tool(annotations=ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
))
async def create_knowledge(
    ctx: Context[RavenClient, Any],
    name: Annotated[str, Field(
        min_length=1, max_length=255,
        description="Required. Unique name for the new knowledge, reused when ingesting files or creating local conversations; no default.",
    )],
    user_summary: Annotated[str, Field(
        max_length=32768,
        description="Optional. Description of the knowledge's intended contents; defaults to an empty string.",
    )] = "",
) -> dict[str, Any]:
    """Create an empty knowledge before adding documents to it.

    Args:
        name (required): A unique knowledge name to use in later ingestion and
            local-conversation calls. There is no default.
        user_summary (optional, default empty string): A description of the
            knowledge's subject or intended contents.

    Supply a new knowledge name and an optional summary of its contents.
    Models are not required for creation. Returns the saved metadata. An
    existing name produces a conflict; use list_knowledges to discover and
    reuse existing knowledge rather than creating it again.
    """
    return await ctx.request_context.lifespan_context.create_knowledge(name, user_summary)
```

</figure>

The module imports `Annotated` and `Any` from `typing`, `Field` from Pydantic,
`Context` from `mcp.server.mcpserver`, and `ToolAnnotations` from `mcp.types`.
The SDK uses the function's signature, annotations, and docstring to describe
the tool. `ctx` is supplied by the SDK; the caller supplies only `name` and,
optionally, `user_summary`.

The description answers both **what the tool does** and **when to use it**:
create an empty knowledge before ingestion, but discover and reuse an existing
one instead of recreating it. The annotation hints describe its intended
behavior; they are not authorization rules or a substitute for validation.

Its client-side counterpart is deliberately small:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def create_knowledge(self, name: str, user_summary: str = "") -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/knowledges",
            json={"name": name, "user_summary": user_summary},
        )
```

</figure>

For the MCP input `{"name":"engineering","user_summary":"Engineering reference documents"}`,
the HTTP request is `POST /api/v1/knowledges`, with this JSON body:

```json
{
  "name": "engineering",
  "user_summary": "Engineering reference documents"
}
```

The shared client supplies the bearer header and encodes the JSON. RAVEN
validates the request, creates the knowledge, and returns saved metadata with
HTTP `201`. A duplicate name is a conflict, not a request to overwrite the
existing knowledge. The bridge does not create a directory or metadata file itself.

### 6.2 Keeping discovery read-only and paginated

The [`list_knowledges()`](#complete-mcp-server-py "server.py") tool uses
`read_only_hint=True`. Its inputs become query parameters rather than a JSON
body:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def list_knowledges(
        self,
        limit: int = 10,
        after_name: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if after_name is not None:
            params["after_name"] = after_name
        return await self._request("GET", "/api/v1/knowledges", params=params)
```

</figure>

Thus `{"limit":10}` becomes `GET /api/v1/knowledges?limit=10`, with no request
body. The result's `items` and `next_cursor` pass back to the MCP caller. The
bridge does not fetch every page automatically, which keeps a discovery call
bounded rather than returning the entire knowledge base at once.

[`list_conversations()`](#complete-mcp-client-py "client.py") follows the same
pattern with `/api/v1/conversations` and `after_conversation_id`. Both tool
schemas accept page sizes from 1 to 100, defaulting to 10; RAVEN may impose a
lower maximum. The cursor mappings and exact call formats are shown in section 3.

### 6.3 Expanding model names into provider specifications

The MCP caller supplies two installed Ollama model names. RAVEN's configuration
endpoint expects a model specification for each role. We make that translation
inside [`RavenClient.configure_models()`](#complete-mcp-client-py "client.py"):

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def configure_models(self, llm_model: str, embedding_model: str) -> dict[str, Any]:
        models = {
            "llm": {"provider": "ollama", "model": llm_model, "role": "llm"},
            "embedding": {"provider": "ollama", "model": embedding_model, "role": "embedding"},
        }
        reference = await self._request(
            "POST", "/api/v1/provider/models/configure", json=models,
        )
        operation_id, task_id = self._operation_ids(reference)
        await self._wait_for_operation(operation_id)
        return {
            "operation_id": operation_id,
            "task_id": task_id,
            "status": "completed",
            "models": models,
        }
```

</figure>

For the model pair used earlier, `POST /api/v1/provider/models/configure`
receives:

```json
{
  "llm": {
    "provider": "ollama",
    "model": "gemma4:e2b",
    "role": "llm"
  },
  "embedding": {
    "provider": "ollama",
    "model": "bge-m3:latest",
    "role": "embedding"
  }
}
```

Fixing `provider` and `role` keeps this tool focused. RAVEN still supports a
broader provider contract; this example simply does not expose it. It also does
not pull missing models or start Ollama. RAVEN prepares the adapters and preloads
the selected models through its own configuration operation.

Unlike creation of an empty knowledge, this request returns HTTP `202` with
operation identifiers. [`_operation_ids()`](#complete-mcp-client-py "client.py")
validates those identifiers, and
[`_wait_for_operation()`](#complete-mcp-client-py "client.py") waits before
the tool reports `status: "completed"`. An accepted request alone is not success.
Adapter installation precedes preloading, so a failed configuration operation
does not guarantee that the previous pair remains configured; the recovery
caveat in section 3 applies here as well.

### 6.4 Uploading bytes instead of forwarding a filesystem path

Ingestion introduces a different boundary. `file_path` belongs to the bridge's
machine, so we cannot send that string to RAVEN and assume its server can open
the same file. We read the selected source locally and upload its bytes:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def ingest_file(self, knowledge_name: str, file_path: str) -> dict[str, Any]:
        file_name, content, content_type = await asyncio.to_thread(self._read_upload, file_path)
        reference = await self._request(
            "POST",
            f"/api/v1/knowledges/{quote(knowledge_name, safe='')}/files",
            files={"file": (file_name, content, content_type)},
        )
        operation_id, task_id = self._operation_ids(reference)
        await self._wait_for_operation(operation_id)
        return {
            "operation_id": operation_id,
            "task_id": task_id,
            "status": "completed",
            "knowledge_name": knowledge_name,
            "file_name": file_name,
        }
```

</figure>

The file imports `asyncio` and `quote` from `urllib.parse`.
[`_read_upload()`](#complete-mcp-client-py "client.py") checks for an absolute
path, an accepted extension, nonempty readable content, and the 64 MiB bridge
limit. Running it through `asyncio.to_thread()` keeps that synchronous file read
off the event loop. The complete source below includes the helper and its
content-type mapping.

For `knowledge_name: "engineering"`, this is
`POST /api/v1/knowledges/engineering/files`. **Its body is multipart, not JSON**:
the part is named `file`, its filename is `Reaction_wheel.txt`, and its content
is the source bytes. `files={"file": (...)}` lets the HTTP library construct
the multipart boundary and headers. The absolute local path is not a body field.
`quote(..., safe='')` encodes the knowledge name as a path component.

This is a bounded, in-memory upload, not a streaming file reader. RAVEN applies
its own upload validation and retention policy, then returns an ingestion
operation reference. Parsing, semantic splitting, embeddings, and persistence
remain backend responsibilities. Exposing this tool grants access to selected
local files, so it is intended for trusted callers.

### 6.5 Creating a conversation and submitting a turn

Conversation creation also maps directly to JSON:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def create_conversation(
        self,
        knowledge_name: str | None = None,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/api/v1/conversations",
            json={"knowledge_name": knowledge_name},
        )
```

</figure>

`POST /api/v1/conversations` receives `{"knowledge_name":"engineering"}`
for a local conversation or `{"knowledge_name":null}` for a global one. An
omitted MCP argument becomes `None` in Python and `null` in the HTTP body.
RAVEN determines the type and returns the saved metadata with HTTP `201`;
the bridge does not calculate scope or invent a conversation ID.

The caller then passes that ID to
[`ask_raven()`](#complete-mcp-server-py "server.py"). Its client method submits
`POST /api/v1/conversations/{conversation_id}/turns` with this body:

```json
{
  "user_query": "Use retrieval to explain the reaction-wheel safe-hold conditions.",
  "retrieval_mode": "auto"
}
```

Here `{conversation_id}` is replaced with the actual returned ID and encoded
as a path component. The MCP tool exposes `conversation_id` and `user_query`,
not `retrieval_mode`: the bridge fixes that field to `"auto"`. RAVEN's agent
decides which internal tools to use within the conversation's scope. An outer
MCP client is requesting an answer from that agent, not calling a retrieval
pipeline directly.

This final request returns HTTP `202`, including operation, task, and turn
identifiers. The tool waits, then reads the saved turn before returning an
answer. Those two responsibilities deserve separate treatment: section 7
explains operation observation and interruption, and section 8 follows the
saved conversation result back to the MCP caller.

The complete HTTP client ties these request mappings to their shared helpers:

{% include source_file.html name="client.py" source="mcp_example/client.py" language="python" id="complete-mcp-client-py" %}

## 7. Waiting for operations and handling interruption

An HTTP response can confirm that work was accepted without confirming that
the work finished. Model configuration, ingestion, and chat use that distinction:
RAVEN returns an operation reference, and the bridge observes the operation
before reporting a completed tool result.

We keep this observation logic in
[`RavenClient`](#complete-mcp-client-py "client.py"), so each tool does not
need its own polling loop or cancellation policy.

### 7.1 Separating submission from completion

The `reference` used in the preceding examples is the JSON object returned by
an accepted request. For example, an illustrative chat submission returns HTTP
`202` with this shape:

```json
{
  "conversation_id": "a7c92f4b1d60",
  "turn_id": "a027efae-3806-45fd-a6f4-aa9af928f23f",
  "operation_id": "cc9dce11-16ca-42be-b418-18588d03a571",
  "task_id": "8f708231-4a27-40cb-a51e-477339c167ad",
  "status": "queued",
  "events_url": "/api/v1/operations/cc9dce11-16ca-42be-b418-18588d03a571/events"
}
```

These IDs are illustrative; actual values come from the server. The initial
status may already be `running`. The identifiers serve different purposes:

| Identifier | What it identifies |
| --- | --- |
| `operation_id` | The backend operation whose status we observe or whose cancellation we request. |
| `task_id` | The submitted task within that operation. The bridge preserves it in the completed result, but polls operation status. |
| `conversation_id` | The persistent discussion to continue across chat calls. |
| `turn_id` | This particular turn, used to retrieve its saved answer. |

Configuration and ingestion return operation/task references without the chat
identifiers. We validate the common identifiers before constructing subsequent
requests:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    @staticmethod
    def _operation_ids(reference: dict[str, Any]) -> tuple[str, str]:
        try:
            return str(UUID(reference["operation_id"])), str(UUID(reference["task_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ToolError("RAVEN returned an invalid operation reference.") from None
```

</figure>

[`_operation_ids()`](#complete-mcp-client-py "client.py") parses UUIDs supplied
by RAVEN; it does not generate new ones. Generating another ID here would lose
the connection to the accepted work.

### 7.2 Observing the existing operation

The shared waiter requests
`GET /api/v1/operations/{operation_id}` until it sees a terminal state:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def _wait_for_operation(self, operation_id: str) -> None:
        try:
            async with asyncio.timeout(self.OPERATION_TIMEOUT_SECONDS):
                while True:
                    record = await self._request("GET", f"/api/v1/operations/{operation_id}")
                    status = record.get("status")
                    if status == "completed":
                        return
                    if status in ("failed", "cancelled"):
                        raise ToolError(
                            f"RAVEN operation {operation_id} {status}. Check its status "
                            "and server logs. Work may have committed changes before "
                            "failing or stopping; do not assume rollback or resubmit blindly."
                        )
                    if status not in ("queued", "running"):
                        raise ToolError(f"RAVEN operation {operation_id} returned an invalid status.")
                    await asyncio.sleep(self.OPERATION_POLL_SECONDS)
        except TimeoutError:
            cancelled = await self._cancel_operation(operation_id)
            action = "Cancellation was requested" if cancelled else "Cancellation could not be confirmed"
            raise ToolError(
                f"Stopped waiting for RAVEN operation {operation_id}. {action}; "
                "check the operation before submitting again."
            ) from None
        except asyncio.CancelledError:
            await self._cancel_operation(operation_id)
            raise
        except ToolError as error:
            # Keep the accepted operation recoverable even if observing it fails.
            raise ToolError(f"Operation {operation_id}: {error}") from None
```

</figure>

The constants defined earlier give this loop a 600-second wait limit and a
one-second pause between nonterminal observations. `await asyncio.sleep()`
yields control; it does not block the bridge's event loop. The next request
follows that pause, so the interval also includes the HTTP request's duration.

The status determines what the tool does next:

| Observed state | Bridge behavior |
| --- | --- |
| `queued` or `running` | Keep observing the same operation. |
| `completed` | Return from the waiter; the calling method prepares its result. |
| `failed` or `cancelled` | Raise a tool error containing the operation ID. |
| Missing or unrecognized status | Reject the response rather than assume success or wait indefinitely. |

Notice that the waiter returns `None`, not the answer. Configuration and
ingestion prepare their completion summaries after this check. Chat makes
another request for the saved turn, covered in section 8.

### 7.3 Requesting cancellation without assuming rollback

If the wait limit expires, or cancellation reaches the waiting coroutine, we
attempt to notify RAVEN before leaving the call:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def _cancel_operation(self, operation_id: str) -> bool:
        # MCP uses AnyIO cancellation scopes. Shield this short cleanup request
        # so cancelling the tool still gives RAVEN a chance to stop its work.
        with anyio.CancelScope(shield=True):
            with anyio.move_on_after(5):
                try:
                    await self._request("POST", f"/api/v1/operations/{operation_id}/cancel")
                    return True
                except ToolError:
                    pass
        return False
```

</figure>

The module imports `anyio` for these scopes. The shield allows the short
cleanup request to proceed despite the enclosing AnyIO scope's cancellation;
the inner scope limits that attempt to five seconds. The request has no body.
We re-raise the original coroutine cancellation afterward rather than turn it
into a successful tool result.

The returned Boolean means the cancellation request received an acceptable
response—not that every backend task has already stopped. Cancellation is
cooperative, and committed changes are not automatically undone. Recheck the
operation or saved resource before deciding what to do next.

This mechanism also requires a live bridge and a known operation ID. Killing
the bridge process cannot run its cleanup code. A connection lost during
submission may leave accepted work whose ID never reached the bridge. Likewise,
a client-side timeout is not proof that a cancellation notification was delivered.

There are therefore three separate timing boundaries: the HTTP request timeout,
the bridge's operation wait, and the MCP client's tool-call timeout. Section 3
explains the Inspector setting; section 4 configures Codex's tool timeout to
660 seconds. Extending either client's limit does not extend the bridge's
600-second operation wait.

### 7.4 Reporting errors without silently repeating writes

[`_request()`](#complete-mcp-client-py "client.py") converts connection errors,
HTTP failures, and malformed responses into `ToolError` messages. It uses
status-specific hints instead of forwarding arbitrary backend response bodies,
which could contain deployment details. When observation fails after acceptance,
[`_wait_for_operation()`](#complete-mcp-client-py "client.py") adds the known
operation ID to that error.

An observation failure does not automatically cancel or resubmit the operation.
For example, a failed status request says that the bridge could not observe the
work, not that ingestion failed. Repeating a write at that point might duplicate
an action that already committed. The bridge has no automatic retry loop,
retry tool, or persisted observation cursor. RAVEN's operation-status and
eligible-task retry endpoints remain separate backend capabilities.

Finally, polling is an intentional simplification of this example. RAVEN also
provides replayable SSE events through the returned `events_url`, but this bridge
does not consume that stream, forward token deltas, or publish MCP progress
notifications. The MCP call stays pending until it can return a completed
result or a tool error. A successful call demonstrates the integration, not a
streaming interface.

## 8. Preserving conversations and returning focused answers

Operation completion answers whether the backend work finished. A chat tool
must also return the right answer from the right conversation and turn. We
therefore keep conversation identity across calls and read the result that
RAVEN persisted, rather than maintaining another chat history inside the bridge.

### 8.1 Following a turn from submission to its saved result

[`RavenClient.ask_raven()`](#complete-mcp-client-py "client.py") brings together
the request mapping from section 6 and the operation waiter from section 7:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def ask_raven(self, conversation_id: str, user_query: str) -> dict[str, Any]:
        path = f"/api/v1/conversations/{quote(conversation_id, safe='')}/turns"
        reference = await self._request(
            "POST", path, json={"user_query": user_query, "retrieval_mode": "auto"},
        )
        operation_id, task_id = self._operation_ids(reference)
        try:
            turn_id = str(UUID(reference["turn_id"]))
        except (KeyError, TypeError, ValueError, AttributeError):
            raise ToolError(f"RAVEN operation {operation_id} returned an invalid turn reference.") from None

        try:
            await self._wait_for_operation(operation_id)
            turn = await self._request("GET", f"{path}/{turn_id}")
        except ToolError as error:
            raise ToolError(f"Conversation {conversation_id}, turn {turn_id}: {error}") from None
        result = turn.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("response"), str):
            raise ToolError(
                f"RAVEN operation {operation_id} finished, but turn {turn_id} "
                "has no usable saved answer. Check the turn before submitting again."
            )
        return {
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "operation_id": operation_id,
            "task_id": task_id,
            "status": "completed",
            "response": result["response"],
            "evidence": result.get("evidence", []),
            "iteration_limit_reached": result.get("iteration_limit_reached", False),
        }
```

</figure>

The method performs three HTTP stages:

1. **Submit:** post the query to the selected conversation's `/turns` endpoint.
   Keep the returned operation, task, and turn identifiers.
2. **Observe:** wait for that operation to complete. Failed or cancelled work
   does not proceed to the successful-answer path.
3. **Read:** request
   `GET /api/v1/conversations/{conversation_id}/turns/{turn_id}`, with no body,
   and extract the saved `result`.

We do not take an answer from the `202` reference: it contains identifiers,
not the generated response. Nor do we assume an operation marked completed
necessarily yielded a usable answer document. The explicit result check rejects
a missing result or a non-string `response`. If fetching the saved turn fails,
the error retains its conversation, turn, and operation context; that failure
is not a reason to generate the same answer again automatically.

### 8.2 Reusing identity instead of recreating history

The persistent connection between turns is `conversation_id`, not a Python
object in the bridge and not the MCP client's own chat ID. A follow-up calls
[`ask_raven()`](#complete-mcp-server-py "server.py") with the same conversation
ID and a new `user_query`. Each accepted turn receives its own turn and operation
identifiers, while the conversation remains the same.

When a client no longer has that ID, discovery is enough to find the discussion:

<figure class="code-snippet" markdown="1">
<figcaption class="code-snippet-filename"><a href="#complete-mcp-client-py">client.py</a></figcaption>

```python
class RavenClient:

    async def list_conversations(
        self,
        limit: int = 10,
        after_conversation_id: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, str | int] = {"limit": limit}
        if after_conversation_id is not None:
            params["after_conversation_id"] = after_conversation_id
        return await self._request("GET", "/api/v1/conversations", params=params)
```

</figure>

[`list_conversations()`](#complete-mcp-client-py "client.py") returns a
newest-first metadata page, including titles and IDs, rather than a page of
messages. A client can present those titles for selection and then reuse the
selected ID. A title is a display aid, not a unique identifier. To continue
paging, pass the exact `next_cursor` as `after_conversation_id`; `null` means
there is no next page.

Scope remains part of the stored conversation:

- A **local conversation** was created with a `knowledge_name` and remains
  restricted to that knowledge.
- A **global conversation** was created without one and can work across the
  knowledge base, including a named knowledge requested in a particular query.

The bridge submits `retrieval_mode: "auto"` in either case. It does not infer
scope from the latest question or change a conversation's scope while sending
a turn. RAVEN resolves the allowed internal tools. A competing turn in the same
conversation is rejected with HTTP `409`; the bridge does not queue it or create
another conversation implicitly. Different conversations can operate concurrently,
subject to the backend's configured limits.

### 8.3 Returning an answer, not the whole execution record

The saved turn contains more information than this MCP tool needs to return.
We select a smaller result so the external client receives the answer and its
references without an entire internal transcript or reconstructed document:

| Returned field | Purpose |
| --- | --- |
| `conversation_id` | Continue the same discussion. |
| `turn_id` | Identify this saved turn. |
| `operation_id` and `task_id` | Identify the backend work that produced the result. |
| `status` | `"completed"` after operation observation and saved-answer validation. |
| `response` | The saved answer text. |
| `evidence` | Section references containing `knowledge_name`, `file_name`, and `section_id`. |
| `iteration_limit_reached` | Preserve RAVEN's indication that its agent reached the execution limit. Completion alone does not imply an unrestricted or exhaustive investigation. |

For a casual turn, `evidence` may be an empty list. A follow-up may also use
information already available in conversation memory rather than obtain fresh
section references. An empty list is therefore not automatically a tool failure,
but it does not establish fresh retrieval either. Evidence identifies source
material supplied to the model; it is not proof that every claim in the answer
is correct.

The projection deliberately excludes thinking text, internal tool-call records,
and full reconstructed files. RAVEN can save these as part of its turn result;
their omission here is a choice of the bridge's external contract, not a loss
of backend functionality. Returning complete source files on every question
would also consume the outer client's context unnecessarily.

For an application that needs source inspection, the HTTP API exposes
`POST /api/v1/conversations/{conversation_id}/turns/{turn_id}/reconstruction`
with no request body. That request starts a separate reconstruction operation.
This example does **not** register a corresponding MCP tool or viewer. The
[source reconstruction architecture]({{ '/architecture/' | relative_url }}#9-source-reconstruction)
explains the evidence-to-source relationship, while the
[In Action example]({{ '/demo/' | relative_url }}) demonstrates a source viewer.

### 8.4 Leaving memory ownership with RAVEN

Nothing in [`RavenClient`](#complete-mcp-client-py "client.py") appends user,
assistant, or tool messages to a local history. The backend conducts the turn
through its session and harness, then persists the canonical interaction and
run result in the conversation. It also owns context compaction, vector memory,
conversation-local preferences, and title generation. The bridge neither
implements those mechanisms again nor transfers the MCP client's whole chat
into RAVEN.

This is why reconnecting an MCP client does not require replaying all previous
questions into a new conversation. With the same RAVEN home and user context,
the saved conversation can be found and continued by ID. Restarting RAVEN still
requires model configuration again, as does recreating a user runtime after
idle or capacity-driven eviction. Durable discussion history and runtime model
configuration are different kinds of state.

The [conversation and memory architecture]({{ '/architecture/' | relative_url }}#7-conversations-sessions-and-memory)
describes the backend's persistence and derived context in more detail. At the
integration boundary, the reusable rule is smaller: preserve resource identity,
submit one new turn, and return the saved result without creating a second
source of conversation truth.

## 9. Limits and adapting the pattern

The example demonstrates a complete document workflow through a deliberately
small interface. It is not a complete MCP product or a hosted deployment
template. The useful part to carry into another system is the boundary: expose
the actions that system needs, translate them into RAVEN requests, and preserve
enough identity and outcome information to continue or inspect the work.

### 9.1 Choosing what the external interface should expose

We selected seven tools rather than mirror the entire HTTP API. That decision
keeps the external caller's choices understandable, but it also determines
which backend capabilities are accessible through this particular bridge:

| Example decision | What it means when adapting the bridge |
| --- | --- |
| Fix model configuration to Ollama. | Cloud-provider selection would require an expanded configuration contract and application-owned credential handling. RAVEN already supports those providers; this tool does not expose them. |
| Submit chat with automatic retrieval selection. | Explicit retrieval-mode controls could be added to the tool input, with validation against RAVEN's supported modes and conversation scope. They are not selectable in the supplied example. |
| Poll operations and return a final result. | A live interface would need to consume RAVEN's event stream and decide how to present progress, replay, and cancellation. The current bridge does not forward these as streaming output or MCP progress notifications. |
| Return answer text and evidence references. | A source-inspection interface would need to expose and render the relevant saved source or reconstruction data. There is no reconstruction tool in this bridge. |
| Buffer a bounded upload in memory. | Larger-document workflows need a deliberate memory and upload strategy, rather than merely increasing the 64 MiB bridge limit. RAVEN's own request limits still apply. |
| Leave administration and retry tools out. | Adding deletion, model administration, or eligible-task retry would require explicit input contracts and access policy, not just another endpoint mapping. |

These are possible extensions, not unfinished steps required to run the example.
The [RAVEN README]({{ site.repository_url }}/blob/main/README.md) describes the
larger API contract. A new tool should have a concrete purpose in the surrounding
application rather than exist simply because a backend route is available.

### 9.2 Keeping the trust boundary explicit

This walkthrough uses a trusted local client and a separately running local
RAVEN service. In particular,
[`ingest_file()`](#complete-mcp-server-py "server.py") can read a selected
accessible file on the bridge's machine and upload its contents. An absolute
path, accepted extension, and size check do not establish that a caller is
authorized to read that file. The tool must not become an unrestricted file
gateway for untrusted remote users.

Similarly, the RAVEN bearer token grants access to the internal service; it
does not authenticate an application's public user account. The optional
`RAVEN_USER_ID` is one trusted user context configured for this bridge process,
not a per-call identity selected by an end user. Adding a UUID field to every
tool would not turn the example into a secure multi-user service.

A hosted adaptation needs an application layer that authenticates users,
authorizes their internal identities and permitted actions, protects service
credentials, and supervises the deployment. RAVEN validates forwarded context
and isolates its user runtimes, but the host must decide which identity a
request is allowed to use. The
[HTTP and hosted-runtime architecture]({{ '/architecture/' | relative_url }}#102-the-fastapi-boundary-and-hosted-runtime)
explains that division of responsibility. The supplied Windows launcher remains
a local convenience, not a substitute for hosted process supervision.

### 9.3 Owning startup, shutdown, and retention

Several operational responsibilities intentionally remain outside the Python
bridge:

- **Ollama lifecycle:** the application or operator starts and stops Ollama,
  installs the models, and manages its resources. Neither the bridge nor RAVEN
  owns that process. Closing the MCP client does not imply that Ollama's loaded
  models have been unloaded.
- **RAVEN lifecycle:** normal Inspector launcher mode starts or reuses a server;
  on exit it terminates only a server it started. That is not a graceful drain
  of active work. In the direct-client setup, RAVEN is started and stopped
  separately. A longer-lived application should own an appropriate shutdown
  sequence and finish or cancel active work deliberately.
- **Runtime configuration:** the model pair must be configured after a RAVEN
  restart or user-runtime eviction. Idle eviction defaults to 30 minutes;
  capacity-driven eviction can also replace an unused runtime. Persisted
  knowledge and conversations survive, but the configured model pair and its
  adapters are in-memory state. Reconnecting the bridge does not restore them;
  persisted runtime settings are separate. A configuration failure during
  preloading can also leave the new pair installed rather than restore the old
  one, so confirm and deliberately reconfigure the intended pair after resolving
  the failure.
- **Operation-history retention:** RAVEN supplies `OperationCleanupService`,
  but neither its bundled server nor this example schedules automatic operation
  cleanup. An application bridge or hosting backend must arrange the cleanup
  schedule and its lifecycle for the relevant user runtimes. Retention settings
  alone do not delete saved operations. This is separate from the server's
  implemented staged-upload cleanup.

The bridge uses HTTP and has no operation-cleanup endpoint to call. Scheduling
the cleanup service therefore belongs in the application or host layer with
access to the owning runtime, rather than deleting database files from this
client. Cleanup also removes associated event and retry history, so its policy
should account for how long an application needs to inspect or recover work.

**Keep the embedding model fixed for existing ingested knowledge.** Switching
the generation model is a different decision from replacing the embedding
model that defines the stored vector space. A new embedding model requires an
appropriate re-ingestion or rebuilding workflow; configuring another model
name does not make existing vectors compatible with it.

### 9.4 Distinguishing integration success from answer quality

Inspector makes calls chosen by the operator. An AI client instead chooses
MCP tools from their descriptions and the task it receives. If it calls
[`ask_raven()`](#complete-mcp-server-py "server.py"), that delegates to another
agent: RAVEN's configured LLM chooses its internal retrieval, navigation, and
memory tools before producing the saved answer. These are separate decisions
and potentially separate model costs and latency.

A successful [`ask_raven()`](#complete-mcp-server-py "server.py") call shows
that the request reached RAVEN, completed, and
returned a usable saved result. It does not establish that the agent chose the
best retrieval strategy or preserved every condition in the source. Small
models can be limited in multi-tool sequences, and any model can answer from
general knowledge or overlook a qualifier. Explicit retrieval requests and
source checking remain useful even when the integration itself works correctly.

The same distinction applies to recovery: an error with a known operation ID
is inspectable work, not permission to repeat a write blindly. An application
can build a richer recovery experience around RAVEN's status and retry contracts
without making the bridge pretend that every interrupted request is safe to
resubmit.

## Conclusion

We connected an external tool interface to RAVEN without moving document
processing, agent execution, or conversation storage into that interface. The
bridge translates a small set of actions into authenticated HTTP requests,
waits for confirmed outcomes, and returns saved answers with the identifiers
needed to continue the discussion or inspect the work.

MCP is one expression of that pattern, not a requirement of RAVEN. A different
application can use the same HTTP boundary or call the Python API directly.
The important decisions remain the same: define the caller's contract, preserve
resource identity, distinguish acceptance from completion, and keep application
policy separate from backend execution and persistence.

The [In Action walkthrough]({{ '/demo/' | relative_url }}) develops the
in-process Python approach, while the
[architecture page]({{ '/architecture/' | relative_url }}) explains the machinery
behind both entry points. The
[README]({{ site.repository_url }}/blob/main/README.md) provides the broader API,
settings, and deployment reference. Together, they provide the context for
adapting this focused example to another interface without rebuilding RAVEN
inside it.
