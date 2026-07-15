# RAVEN

**Retrieval Augmented Adaptive Epistemic Navigation** — Privacy-first, fully offline RAG backend.

## Installation

```bash
pip install noomexai-raven
```

**Required: `llama-cpp-python`** - not installed with the package due to os specific build dependencies. Build `llama-cpp-python` from source or use a prebuilt wheel. See official PyPI page for `llama-cpp-python`.

**Powershell (Windows):**

```bash
$env:CMAKE_ARGS = "-DGGML_VULKAN=1"
pip install llama-cpp-python --no-cache-dir --force-reinstall -v
```

 — see [PyPI page](https://pypi.org/project/llama-cpp-python/) for platform-specific installation.

## Architecture

- **KnowledgeBase**: List of **Knowledge** databases powered by SQLite + sqlite-vec for vector storage
- **IngestionPipeline**: Semantic sectioning via LLM + BGE-M3 embeddings
- **Retrieval Pipelines**: Embedded, Hierarchical, Agreement-based, Vector-conditioned
- **ChatSession**: Tool-calling chat with memory, preferences, streaming

For detailed explanation visit [RAVEN desktop app page](https://github.com/NoomexAI/RAVEN).

## Requirements

- Python 3.11+
- Vulkan-compatible GPU (recommended for llama-cpp-python)
- 6 GB+ VRAM

## Quick Start

The high level api, **Raven** can be used for quick testing but is not suitable for development environment. For development environment, it's better to use the explicit components.

```python
import os
os.environ['RAVEN_HOME'] = "./RAVEN"            # make sure this is set before importing anything from raven.

import raven

api = raven.Raven()

api.create_knowledge("<knowledge_name>", user_summary="contains files about <knowledge_name>")
api.ingest(
    knowledge_name = "<knowledge_name>",
    file_path= "/path/to/file.txt"              # Only supports .txt files for now.
)
# Once a knowledge is created it's added to the KnowledgeBase registry. You won't have to create it again unless you delete it.
# You can ingest files in a previously created knowledge the same way by passing the knowledge_name to api.ingest

session = api.session()

while True:
    user_query = input("\nUser: ")
    
    result = session.generate_response(user_query, retrieval_mode='auto')

    print("\nThinking:\n")
    print(result.think)

    print("\nResponse:\n")
    print(result.response)

    tool_name, tool_result = result.tool_name, result.tool_result

    print("\nReconstruct:\n")
    print(api.reconstruct(tool_result, tool_name))
```

## Status

 - To enable default status tracking, use the **Status** class from **raven.status**.

```python
import raven
from raven.status import Status

s = Status()
s.on_change = lambda status: print("\n\n", status)

# s.on_change runs every time s.status = "<new_status>" is defined

api = raven.Raven(s=s)
```

 - on_status method and custom status

```python
from raven.status import Status

def alert(status):
    print("Error found.")

s.on_status("Error", alert)

s.status = "Error"              # triggers the alert function and prints the message
```

This way you can define custom status and trigger functions on that status

 - Custom status fields (advanced)

There are 27 status fields defined in the **StatusRegistry** dataclass.

```python
@dataclass
class StatusRegistry:                       # exposed via raven.status
    # Model Manager
    LOADING_BASE_MODEL: str = "loading_base_model"
    BASE_MODEL_LOADED: str = "base_model_loaded"
    LOADING_EMBEDDING_MODEL: str = "loading_embedding_model"
    EMBEDDING_MODEL_LOADED: str = "embedding_model_loaded"
    LOADING_SBD_MODEL: str = "loading_sbd_model"
    SBD_MODEL_LOADED: str = "sbd_model_loaded"
    DOWNLOADING_BASE_MODEL: str = "downloading_base_model"
    BASE_MODEL_DOWNLOADED: str = "base_model_downloaded"
    DOWNLOADING_EMBEDDING_MODEL: str = "downloading_embedding_model"
    EMBEDDING_MODEL_DOWNLOADED: str = "embedding_model_downloaded"
    DOWNLOADING_SBD_MODEL: str = "downloading_sbd_model"
    SBD_MODEL_DOWNLOADED: str = "sbd_model_downloaded"

    # Ingestion Pipeline
    INGESTION_GRAMMAR_ENFORCED: str = "ingestion_grammar_enforced"
    SUBDIVIDING_FILE: str = "subdividing_file"
    FILE_SUBDIVIDED: str = "file_subdivided"
    INGESTION_INFERENCE_RUNNING: str = "ingestion_inference_running"
    INGESTION_INFERENCE_COMPLETE: str = "ingestion_inference_complete"
    INGESTING: str = "ingesting"
    INGESTION_COMPLETE: str = "ingestion_complete"

    # Chat Session
    GENERATING_TITLE: str = "generating_title"
    TITLE_GENERATED: str = "title_generated"
    GENERATING_RESPONSE: str = "generating_response"
    THINKING_START: str = "thinking_start"
    THINKING_END: str = "thinking_end"
    TOOL_CALL_DETECTED: str = "tool_call_detected"
    RETRIEVED_SECTIONS: str = "retrieved_sections"
    RESPONSE_COMPLETE: str = "response_complete"
```

You can access those status fileds using **s.registry** parameter.

```python
from raven.status import Status

s = status()
print(s.registry.LOADING_BASE_MODEL)

# To see all of the fields
print(s.registry.list_all())
```

You can define your own status fields by creating a custom dataclass that inherits from **StatusRegistry**.

```python
from raven.status import Status, StatusRegistry
from dataclasses import dataclass

@dataclass
class ExtendedStatusRegistry(StatusRegistry):
    CUSTOM_STATUS: str = "custom_status"

status_registry = ExtendedStatusRegistry()

s = Status(registry= status_registry)

# Now you can use your custom status field
print(s.registry.CUSTOM_STATUS)
```

## Logging

To setup logging and create a ./RAVEN/raven.log file use the **setup_logging** function.

```python
from raven import Raven
from raven.core import setup_logging

setup_logging()

api = Raven()
```

## Streaming

To enable streaming use the **generate_response_stream** method instead in generate_response.

```python
import os
os.environ['RAVEN_HOME'] = "./RAVEN"            # make sure this is set before importing anything from raven.

import raven

api = raven.Raven()
session = api.session()

while True:
    user_query = input("\nUser: ")
    stream = session.generate_response_stream(user_query, retrieval_mode="auto")

    # generate_response_stream returns a Generator[ChatResult] object

    tool_name = None
    tool_result = None

    print("\nRaven:")
    for result in stream:

        if result.think:
            print(result.think, end="", flush=True)
        if result.response:
            print(result.response, end="", flush=True)

        if result.tool_result and result.tool_name:
            tool_name = result.tool_name
            tool_result = result.tool_result
    
    print("\nReconstruct:\n")
    print(api.reconstruct(tool_result, tool_name))
```

## Components

For development environment it's better to use individual components explicitly to control the data flow.

```python
import os
os.environ["RAVEN_HOME"] = r"./RAVEN"

from raven.core import KnowledgeBase, ModelManager, setup_logging
from raven.pipelines import IngestionPipeline
from raven.session import ConversationManager, ChatSession
from raven.reconstructor import Reconstructor

setup_logging()

# Initialize knowledge base and model manager
knowledge_base = KnowledgeBase()
model_manager = ModelManager()

print("Initializing models...\n")
base_model = model_manager.initiate_base_model()                
# supports 3 base models: gemma-4-E2B-it, gemma-4-E4B-it, Qwen2.5-7B-instruct (experimental) in .gguf format.

embedding_model = model_manager.initiate_embedding_model()
sbd_model = model_manager.initiate_sbd_model()
print("Models initialised\n")


knowledge_base.create_knowledge("engineering", user_summary="contains engineering files")
knowledge_base.create_knowledge("medical", user_summary= "contains medical files")

ingestion = IngestionPipeline(
    knowledge_base= knowledge_base,
    base_model= base_model,
    embedding_model= embedding_model,
    sbd_model= sbd_model
)

print("Ingesting...\n")
ingestion.ingest(
    knowledge_name= "engineering",
    file_path= r"path/to/file.txt"
)
ingestion.ingest(
    knowledge_name= "medical",
    file_path= r"path/to/file.txt"
)
print("Ingestion complete\n")

conversation_manager = ConversationManager(
    knowledge_base= knowledge_base,
    base_model= base_model,
    embedding_model= embedding_model
)
conversation_id = conversation_manager.create_conversation()
conversation = conversation_manager.get_conversation(conversation_id)

session = ChatSession(
    conversation= conversation,
    knowledge_base= knowledge_base,
    base_model= base_model,
    embedding_model= embedding_model,
)

# For normal chat loop
while True:
    user_query = input("\nUser: ")
    
    result = session.generate_response(user_query, retrieval_mode='auto')

    print("\nThinking:\n")
    print(result.think)

    print("\nResponse:\n")
    print(result.response)

    tool_name, tool_result = result.tool_name, result.tool_result

    reconstructor = Reconstructor(knowledge_base)
    reconstructed = reconstructor.reconstruct(tool_result, tool_name)
    print(f"\nReconstructed:\n{reconstructed}\n")


# For streaming chat loop
while True:
    user_query = input("\nUser:")
    stream = session.generate_response_stream(user_query, retrieval_mode="auto")

    tool_name = None
    tool_result = None

    print("\nRaven:")
    for result in stream:

        if result.think:
            print(result.think, end="", flush=True)
        if result.response:
            print(result.response, end="", flush=True)

        if result.tool_result and result.tool_name:
            tool_name = result.tool_name
            tool_result = result.tool_result


    reconstructor = Reconstructor(knowledge_base)
    reconstructed = reconstructor.reconstruct(tool_result, tool_name)
    print(f"\nReconstructed:\n{reconstructed}\n")
```

## Status in components

To enable status tracking while using explicit components, you have to pass the status instance to the classes that expects it. There are three classes that expect an **s: status** argument.
 - ModelManager
 - IngestionPipeline
 - ChatSession

```python
import os
os.environ["RAVEN_HOME"] = r"./RAVEN"

from raven.core import KnowledgeBase, ModelManager, setup_logging
from raven.pipelines import IngestionPipeline
from raven.session import ConversationManager, ChatSession
from raven.reconstructor import Reconstructor
from raven.status import Status

setup_logging()

s = Status()
s.on_change = lambda status: print(status)

model_manager = ModelManager(s=s)

ingestion = IngestionPipeline(
    knowledge_base= knowledge_base,
    base_model= base_model,
    embedding_model= embedding_model,
    sbd_model= sbd_model,
    s= s
)

session = ChatSession(
    conversation= conversation,
    knowledge_base= knowledge_base,
    base_model= base_model,
    embedding_model= embedding_model,
    s= s
)
```

## License

MIT — see [LICENSE.txt](LICENSE.txt)