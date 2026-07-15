# RAVEN

**Retrieval Augmented Adaptive Epistemic Navigation** — Privacy-first, fully offline RAG backend.

## Installation

```bash
pip install raven
```

**Required: `llama-cpp-python`** — see [PyPI page](https://pypi.org/project/llama-cpp-python/) for platform-specific installation.

## Quick Start

```python
import os
os.environ["RAVEN_HOME"] = r".\RAVEN"

from raven.core import KnowledgeBase, ModelManager, setup_logging
from raven.pipelines import IngestionPipeline
from raven.session import ConversationManager, ChatSession
from raven.reconstructor import Reconstructor

setup_logging()

knowledge_base = KnowledgeBase()
model_manager = ModelManager()

print("Initializing models...\n")
base_model = model_manager.initiate_base_model()
embedding_model = model_manager.initiate_embedding_model()
sbd_model = model_manager.initiate_sbd_model()
print("Models initialised\n")


knowledge_base.create_knowledge("engineering", user_summary="contains engineering files")

ingestion = IngestionPipeline(
    knowledge_base= knowledge_base,
    base_model= base_model,
    embedding_model= embedding_model,
    sbd_model= sbd_model
)

print("Ingesting...\n")
ingestion.ingest(
    knowledge_name= "engineering",
    file_path= r"Reaction_wheel.txt"
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

while True:
    user_query = input("User:")
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

## Architecture

- **KnowledgeBase**: SQLite + sqlite-vec for vector storage
- **IngestionPipeline**: Semantic sectioning via LLM + BGE-M3 embeddings
- **Retrieval Pipelines**: Embedded, Hierarchical, Agreement-based, Vector-conditioned
- **ChatSession**: Tool-calling chat with memory, preferences, streaming

## Requirements

- Python 3.11+
- Vulkan-compatible GPU (recommended for llama-cpp-python)
- 6 GB+ VRAM

## License

MIT — see [LICENSE.txt](LICENSE.txt)