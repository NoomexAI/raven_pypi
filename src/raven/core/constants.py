import os
from importlib.resources import files
from platformdirs import PlatformDirs
from dataclasses import dataclass, fields

RAVEN_HOME = os.environ.get("RAVEN_HOME")

if RAVEN_HOME:
    _USER_DATA_DIR = RAVEN_HOME
else:
    _dirs = PlatformDirs(appname="raven", appauthor="NoomexAI", version="0.1.0")
    _USER_DATA_DIR = _dirs.user_data_dir


ROOT_DIR = _USER_DATA_DIR

BASE_MODEL_DIR = os.path.join(ROOT_DIR, "models", "base_model")
EMBEDDING_MODEL_DIR = os.path.join(ROOT_DIR, "models", "embedding_model")
SBD_MODEL_DIR = os.path.join(ROOT_DIR, "models", "sbd_model")
KNOWLEDGE_BASE_DIR = os.path.join(ROOT_DIR, "data", "KnowledgeBase")
CONVERSATION_DIR = os.path.join(ROOT_DIR, "data", "Conversations")
SETTINGS_PATH = os.path.join(ROOT_DIR, "settings.json")

GRAMMAR_DIR = files("raven.grammar")


CHUNK_SIZE = 150
CHUNK_OVERLAP = 20
SUBFILE_SIZE = 3000
SENTENCES_PER_CHUNK = 4

DEFAULT_THEME_MODE = "system"
DEFAULT_SELECTED_MODEL = None
DEFAULT_MODEL_PARAMETERS = {
    "n_ctx": 32768,
    "n_gpu_layers": -1,
    "n_threads": 6,
}


# Retrieval Modes
@dataclass
class RetrievalModes:
    AUTO: str = "auto"

    LOCAL_EMBEDDED_RETRIEVAL: str = "local_embedded_retrieval"
    LOCAL_HIERARCHICAL_RETRIEVAL: str = "local_hierarchical_retrieval"
    LOCAL_AGREEMENT_BASED_RETRIEVAL: str = "local_agreement_retrieval"
    LOCAL_VECTOR_CONDITIONED_RETRIEVAL: str = "local_vector_conditioned_retrieval"

    GLOBAL_EMBEDDED_RETRIEVAL: str = "global_embedded_retrieval"
    GLOBAL_HIERARCHICAL_RETRIEVAL: str = "global_hierarchical_retrieval"
    GLOBAL_AGREEMENT_BASED_RETRIEVAL: str = "global_agreement_retrieval"
    GLOBAL_VECTOR_CONDITIONED_RETRIEVAL: str = "global_vector_conditioned_retrieval"

    def list_all(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# Base Model Catalog
MODEL_CATALOG = [
    {
        "display_name": "Gemma 4 E2B-it",
        "file_name": "gemma-4-E2B-it-Q4_K_M.gguf",
        "repo_id": "unsloth/gemma-4-E2B-it-GGUF",
        "model_family": "gemma",
        "size_gb": 3.1,
    },
    {
        "display_name": "Gemma 4 E4B-it",
        "file_name": "gemma-4-E4B-it-Q4_K_M.gguf",
        "repo_id": "unsloth/gemma-4-E4B-it-GGUF",
        "model_family": "gemma",
        "size_gb": 5.0,
    },
    {
        "display_name": "Qwen2.5 7B Instruct",
        "file_name": "Qwen2.5-7B-Instruct-Q4_K_M.gguf",
        "repo_id": "bartowski/Qwen2.5-7B-Instruct-GGUF",
        "model_family": "qwen",
        "size_gb": 4.7,
    },
]

# Prompts
INGESTION_SYSTEM_PROMPT = """<|think|>
    You are a precise data extraction agent. Your task is to process unstructured text into a strict, machine-readable JSON object.
    You'll be provided with indexed text chunks of a file.
    1. Read the chunks in order and split the file into multiple sections based on semantic shifts in context. Do not over-grain the sections. Each section must capture a broad semantic context (like events in narrative files)
    2. Each section should contain a interger number of chunks.
    3. The chunk orders are rigid and must not be changed.

    ### CONSTRAINTS:
    1. OUTPUT: You must ONLY output valid JSON. No conversational text, no markdown wrappers (like ```json), no explanations, and no polite greetings.
    2. STRUCTURE: Every section must follow this exact schema:
    {
        "sections": {
            "type": "array",
            "description": "An ordered list of semantically isolated segments extracted from the text.",
            "items": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "A concise, summary explaining the main concept of this section. Can be multi sentence if necessary."
                    },
                    "keywords": {
                        "type": "array", 
                        "items": {"type": "string"},
                        "description": "A list of relevant tags, identifiers found in this text segment."
                    },
                    "conditions": {
                        "type": "array", 
                        "items": {"type": "string"},
                        "description": "An explicit list of conditional states, numeric thresholds (e.g., temperatures, voltages, chemical values, or conditional events), or functional constraints that trigger actions. Leave empty if none."
                    },
                    "definitions":{
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "The answers to the 'What' and 'Who' questions that can be answered from this section."
                    },
                    "end_chunk_id": {
                        "type": "integer",
                        "description": "The ending chunk id of each section"
                    }
                },
                "required": ["summary", "keywords", "conditions", "definitions", "end_chunk_id"]
            }
        }
    }

    3. You must not hallucinate the end_chunk_id.
    4. definitions: definitions are the answer to the 'who' and 'what' questions that can be answered from a section. Do not leave the definitions empty unless there is absolutely nothing to define at all.
    5. LANGUAGE: Always respond in the language used in the input text.
"""

HIERARCHICAL_RETRIEVAL_SYSTEM_PROMPT = lambda top_k=3: f"""<|think|>
    step-1: Read the json. locate the blocks between <startk><endk> pairs. Each block corresponds to a separate Knowledge. The knowledge_name is mentioned at the first of each block (just after startk).
    step-2: Locate the all the file_metadata from each Knowledge. Knowledge can contain single or multiple file_metadata. Correctly map which file_metadata belongs to which knowledge (inside the same <startk><endk> pairs)
    step-3: Read the contents of the file_metadata. Each one contains single or multiple data about multiple sections. Each with a section_id. You must read the full metadata and you must list the section_ids belonging to each knowledge.
    step-4: Based on the user_query, you'll select the best {top_k} section_ids from all the file_metadata and sort them according to the relevance to the query. Notice their corresponding knowledge_name from the file_metadata mapping from step-2.
    step-5: Finally return a json of the following format.
    {{
        "section_id_list": [
            [section_id1, knowledge_name1 (the name of the knowledge to which the section_id belongs)],
            [section_id2, knowledge_name2 (the name of the knowledge to which the section_id belongs)],
            .....
            .....
        ]
    }}
    *** CRITICAL: You must sort the sections by the most relevance order. The most relevant section will be at index 0.
    *** CRITICAL: You will not use any conversational texts or tags like ```json. And must not make up any section_id. You can only choose from the given section_id in the metadata.
    ***** CRITICAL: You must not hallucinate a section in a knowledge that it doesn't bolong.
    *** CRITICAL: Pay extra attentions to large file metadata as you tend to hallucinate sections when the metadata is large. Make sure that the file_metadata-knowledge mapping remains consistant
"""

HIERARCHICAL_RETRIEVAL_SUMMARY_SYSTEM_PROMPT = lambda top_k=3: f"""<|think|>
    You are a metadata interpreter for retrieval. You'll be provided with a json metadata of knowledge_name and knowledge_summary along with a user query.
    The metadata will containt many summaries of different knowledge folder with their name (knowledge_name). You have to choose the best {top_k} knowledge_names which are more relevant to user query based on their knowledge_summary.
    FInally you'll output that in the following json format:
    {{
        "knowledge_name_list": [
            knowledge_name1,
            knowledge_name2,
            .....
            .....
        ]
    }}
    ### CRITICAL: You will not use any conversational texts or tags like ```json.
"""

MEMORY_UPDATE_SYSTEM_PROMPT = f"""
    You are a metadata generator. You'll be provided with a conversation history between a chatbot and the user and an existing preferences json.
    step-1: Read the conversation history and identify the user specifications by identifying phrases like "From now on you'll do this" or "Remember this from now on." and phrases like that, that indicates that the user wants the chatbot to remember that specification.
    step-2: Read the preferences json and identify if any of the new user specifications override the existing ones. If it does then override them. If it doesn't then keep those untouched. Append the new specifications to the list.
    step-3: After performing step-1 and step-2 , you'll create a json of the following format.
    {{
        user_specifications:[
            user_specification1,
            user_specification2,
            .....
            .....
        ]
    }}
    ### CRITICAL: You will not use any conversational texts or tags like ```json.
"""