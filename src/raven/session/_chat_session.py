from dataclasses import dataclass
from typing import Any, Optional

from raven.session._conversation_manager import Conversation
from raven.pipelines._pipeline import EmbeddedRetrievalPipeline, HierarchicalRetrievalPipeline, AgreementBasedRetrievalPipeline, VectorConditionedRetrievalPipeline, MemoryPipeline
from raven.core._knowledge_base import KnowledgeBase
from raven.status._status import Status

import json

import logging
import re

logger = logging.getLogger(__name__)


@dataclass
class ChatResult:
    response: Optional[str] = None
    think: Optional[str] = None
    tool_result: Any = None
    tool_name: Optional[str] = None


class ChatSession:

    def __init__(self, conversation: Conversation, knowledge_base: KnowledgeBase, base_model, embedding_model, s: Status|None=None):
        self.conversation = conversation
        self.knowledge_base = knowledge_base
        self.base_model = base_model
        self.embedding_model = embedding_model
        self.s = s

        self.base_model.reset()

        self.embedded_pipeline = EmbeddedRetrievalPipeline(knowledge_base, embedding_model)
        self.hierarchical_pipeline = HierarchicalRetrievalPipeline(knowledge_base, base_model)
        self.agreement_pipeline = AgreementBasedRetrievalPipeline(knowledge_base, self.embedded_pipeline, self.hierarchical_pipeline)
        self.vector_conditioned_pipeline = VectorConditionedRetrievalPipeline(knowledge_base, self.embedded_pipeline, self.hierarchical_pipeline)
        self.memory_pipeline = MemoryPipeline(embedding_model)

        self.messages = self.conversation.load_messages()

        self.system_prompt = self._build_system_prompt()

        if not self.messages:
            self.conversation.append_message(
                role="system",
                content=self.system_prompt
            )
            self.messages = self.conversation.load_messages()

        self.total_tokens = sum(
            len(self.base_model.tokenize(m["content"].encode("utf-8")))
            for m in self.messages
        )

        self.title_generated = self.conversation.title != "New Conversation"

        logger.info(f"ChatSession started for conversation: {conversation.conversation_id}")


    def _build_system_prompt(self):
        preferences = self.conversation.get_preference()
        preferences_block = f"\n\n[PREFERENCES]\n{preferences}\n[/PREFERENCES]" if preferences else ""

        conversation_block = f"Current Conversation type: {self.conversation.type}"
        knowledge_block = f"Knowledge name: {self.conversation.knowledge_name}" if self.conversation.knowledge_name else ""

        return f"""<|think|>
        You are Raven, part of a RAG (Retrieval Augmented Generation) framework called RA\u2200EN (Retrieval Augmented Adaptive Epistemic Navigation),
        An intelligent assistant whos primary function is to help the users navigate the knowledge base using a wide range of retrieval tools. You're strictly prohibited to use your training data to answer questions.
        You have access to a knowledge base containing multiple knowledges. Each knowledge is a separate database containing multiple user uploaded files.
        Users can create 'local' or 'global' conversations to interact with you. In 'local' conversations you only have access to local tools that can only navigate a single specified knowledge. In 'global' conversations you have access to both local and global tools unless the the user constrains you to use only one tool.
        Prohibitions: 
            1. You're strictly prohibited to use your training data to answer questions. 
            2. You're strictly prohibited to use 'local' tools without passing a 'knowledge_name' argument. You're prohibited to reuse 'knowledge_name' from a previous message.
            3. You're strictly prohibited to invoke tools that you don't have access to right now.
        
        {conversation_block}
        {knowledge_block}

        Constrained mode check:
            1. Check the number of available tools.
            2. How many of them are retrieval tools (ignoring the memory and preference tools)
            3. If the answer is 1, that means this query is in constrained mode and you must use the retrieval tool that is available.    
        
        When you get a query from user you'll follow the following steps and you must follow these protocols step by step:
        0. Read the prohibitions right now. You must not violate them in any circumstances.
        1. Identify if the query is casual or does it requires a tool call.
        2. Do not invoke tool calls for casual conversations.
        3. You first priority must always be retrieval given the user query needs it. Using your training data/general knowledge to answer questions is strictly forbidded.
        4. Critical: Perform the Constrained mode check right now.
        5. Critical: Identify if you're about to use a local tool or not. You must ask the user to provide a 'knowledge_name' for local tools if none is provided. Do not invoke a local tool without a knowledge name.
        6. You must act as a presenter who is presenting the retrieved into explaining every detail.

        {preferences_block}
"""

    def _refresh_system_prompt(self):
        self.system_prompt = self._build_system_prompt()
        self.messages[0] = {"role": "system", "content": self.system_prompt}
        self.conversation.update_system_prompt(self.system_prompt)
        logger.info(f"System prompt refreshed for conversation: {self.conversation.conversation_id}")


    @property
    def tools(self):
        memory_tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_memory",
                    "description": "Retrieves relevant past conversation context. Use when the user references something no longer in the current context window.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "What you are trying to recall from past conversation"
                            }
                        },
                        "required": ["query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "save_preference",
                    "description": "Saves a user preference or specification for this conversation. Call this when the user explicitly states a preference, instruction, or specification they want remembered.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "preference": {
                                "type": "string",
                                "description": "The preference or specification to save"
                            }
                        },
                        "required": ["preference"]
                    }
                }
            }
        ]

        local_tools = [
            {
                "type": "function",
                "function": {
                    "name": "local_embedded_retrieval",
                    "description": "Retrieves relevant context from a specific knowledge using vector similarity search. Use for precise queries — scientific, engineering, medical or technical data.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "knowledge_name": {
                                "type": "string",
                                "description": "The name of the knowledge to search in. Not required for local conversations."
                            },
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            }
                        },
                        "required": ["user_query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "local_hierarchical_retrieval",
                    "description": "Retrieves relevant context from a specific knowledge using reasoning over metadata. Use for narrative queries — stories, events, characters.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "knowledge_name": {
                                "type": "string",
                                "description": "The name of the knowledge to search in."
                            },
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            }
                        },
                        "required": ["knowledge_name", "user_query"]
                    }
                }
            },
        {
                "type": "function",
                "function": {
                    "name": "local_agreement_retrieval",
                    "description": "Retrieves context from a specific knowledge using both vector and hierarchical retrieval and checks agreement between them. Use when high confidence is required.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "knowledge_name": {
                                "type": "string",
                                "description": "The name of the knowledge to search in. Not required for local conversations."
                            },
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            }
                        },
                        "required": ["user_query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "local_vector_conditioned_retrieval",
                    "description": "Retrieves context from a specific knowledge by first narrowing search to most relevant files then applying hierarchical reasoning. Use when you want targeted retrieval within a knowledge.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "knowledge_name": {
                                "type": "string",
                                "description": "The name of the knowledge to search in. Not required for local conversations."
                            },
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            }
                        },
                        "required": ["user_query"]
                    }
                }
            }
        ]

        global_tools = [
            {
                "type": "function",
                "function": {
                    "name": "global_embedded_retrieval",
                    "description": "Retrieves relevant context across all knowledges using vector similarity search. Use when the user wants to search broadly without specifying a knowledge.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            }
                        },
                        "required": ["user_query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "global_hierarchical_retrieval",
                    "description": "Retrieves relevant context across all knowledges using reasoning over metadata. Use for narrative queries across multiple knowledges. Set full_retrieval to true to search all knowledges, false to let the model select the most relevant knowledges first.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            },
                            "full_retrieval": {
                                "type": "boolean",
                                "description": "If true, searches all knowledges. If false, model selects most relevant knowledges first. Default false."
                            }
                        },
                        "required": ["user_query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "global_agreement_retrieval",
                    "description": "Retrieves context across all knowledges using both vector and hierarchical retrieval and checks agreement. Use when high confidence is needed across multiple knowledges.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            },
                            "full_retrieval": {
                                "type": "boolean",
                                "description": "If true, searches all knowledges. If false, model selects most relevant knowledges first. Default false."
                            }
                        },
                        "required": ["user_query"]
                    }
                }
            },
            {
                "type": "function",
                "function": {
                    "name": "global_vector_conditioned_retrieval",
                    "description": "Retrieves context across all knowledges by first narrowing search using vector retrieval then applying hierarchical reasoning on the most relevant files.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "user_query": {
                                "type": "string",
                                "description": "The user's question to search for"
                            }
                        },
                        "required": ["user_query"]
                    }
                }
            }
        ]

        if self.conversation.type == "local":
            return local_tools + memory_tools
        else:
            return local_tools + global_tools + memory_tools

    def get_active_tools(self, retrieval_mode= "auto") -> list:
        if retrieval_mode == "auto" or not retrieval_mode:
            return self.tools

        non_retrieval = [t for t in self.tools if t["function"]["name"] in ["get_memory", "save_preference"]]
        
        retrieval = [t for t in self.tools if t["function"]["name"] == retrieval_mode]
        
        return non_retrieval + retrieval


    def execute_tool(self, tool_name: str, tool_args: dict):
        active_tools = self.get_active_tools()
        if not tool_name in [t["function"]["name"] for t in active_tools]:
            return f"{tool_name} currently unavailable. The user has constrained you to use {active_tools} only"

        try:
            if tool_name == "get_memory":
                return self.memory_pipeline.get_memory(
                    query=tool_args["query"],
                    conversation=self.conversation
                )
            
            if tool_name == "save_preference":
                self.conversation.save_preference(tool_args["preference"])
                self._refresh_system_prompt()
                return "Preference saved successfully."

            if tool_name == "local_embedded_retrieval":
                if not tool_args.get("knowledge_name"):
                    logger.error(f"Local Embedded Retrieval failed: knowledge_name not provided")
                    return f"Local tools need a 'knowledge_name'. None provided. Ask the user to provide 'knowledge_name' immediately"
                
                return self.embedded_pipeline.retrieve_local_context(
                    knowledge_name=self.conversation.knowledge_name or tool_args.get("knowledge_name"),             #type: ignore
                    user_query=tool_args["user_query"]
                )

            if tool_name == "local_hierarchical_retrieval":
                if not tool_args.get("knowledge_name"):
                    logger.error(f"Local Embedded Hierarchical failed: knowledge_name not provided")
                    return f"Local tools need a 'knowledge_name'. None provided. Ask the user to provide 'knowledge_name' immediately"
                
                return self.hierarchical_pipeline.retrieve_local_context(
                    knowledge_name=self.conversation.knowledge_name or tool_args.get("knowledge_name"),             #type: ignore
                    user_query=tool_args["user_query"]
                )

            if tool_name == "local_agreement_retrieval":
                if not tool_args.get("knowledge_name"):
                    logger.error(f"Local Agreement-based Retrieval failed: knowledge_name not provided")
                    return f"Local tools need a 'knowledge_name'. None provided. Ask the user to provide 'knowledge_name' immediately"
                
                return self.agreement_pipeline.retrieve_local_context(
                    knowledge_name=self.conversation.knowledge_name or tool_args.get("knowledge_name"),             #type: ignore
                    user_query=tool_args["user_query"]
                )

            if tool_name == "local_vector_conditioned_retrieval":
                if not tool_args.get("knowledge_name"):
                    logger.error(f"Local Vector-conditioned Retrieval failed: knowledge_name not provided.")
                    return f"Local tools need a 'knowledge_name'. None provided. Ask the user to provide 'knowledge_name' immediately"
                
                return self.vector_conditioned_pipeline.retrieve_local_context(
                    knowledge_name=self.conversation.knowledge_name or tool_args.get("knowledge_name"),             #type: ignore
                    user_query=tool_args["user_query"]
                )

            if tool_name == "global_embedded_retrieval":
                return self.embedded_pipeline.retrieve_global_context(
                    user_query=tool_args["user_query"]
                )

            if tool_name == "global_hierarchical_retrieval":
                full_retrieval = True if tool_args.get("full_retrieval", "false") == "true" else False
                return self.hierarchical_pipeline.retrieve_global_context(
                    user_query=tool_args["user_query"],
                    full_retrieval=full_retrieval
                )

            if tool_name == "global_agreement_retrieval":
                full_retrieval = True if tool_args.get("full_retrieval", "false") == "true" else False
                return self.agreement_pipeline.retrieve_global_context(
                    user_query=tool_args["user_query"],
                    full_retrieval=full_retrieval
                )

            if tool_name == "global_vector_conditioned_retrieval":
                return self.vector_conditioned_pipeline.retrieve_global_context(
                    user_query=tool_args["user_query"]
                )

            logger.warning(f"Unknown tool called: {tool_name}")
            return None

        except Exception as e:
            logger.error(f"Tool execution failed — {tool_name}: {e}")
            raise


    def generate_response(self, user_text: str, retrieval_mode = "auto") -> ChatResult:                     #type: ignore
        try:
            if not self.title_generated:
                self.set_status("generating_title")                                                          #type: ignore
                title = self._generate_title(user_text)

                if title:
                    self.conversation.update_title(title)
                    self.set_status("title_generated")                                                       #type: ignore
                else:
                    logger.error(f"Invalid title: {title}")
                    raise ValueError(f"Invalid title: {title}")

                self.title_generated = True

                      
            self.messages.append({"role": "user", "content": user_text})
            self.conversation.append_message("user", user_text)

            self.total_tokens += len(self.base_model.tokenize(user_text.encode("utf-8")))

            self._trim_messages()

            active_tools = self.get_active_tools(retrieval_mode)

            self.set_status("generating_response")                                                          #type: ignore 
            initial_response = self.base_model.create_chat_completion(
                messages=self.messages,
                tools=active_tools,
                tool_choice="auto",
                temperature=0.6,
                top_p=0.95,
                top_k=64,
                max_tokens=4096,
                stream=False,
                repeat_penalty=1.2,
                stop=[
                    "<|end|>",
                    "<end>",
                    "<unused49>"
                ]
            )

            content = initial_response["choices"][0]["message"]["content"]

            thinking_block, assistant_response = self.strip_thinking(content)

            if "<|tool_call>" in content:
                match = re.search(r'<\|tool_call>call:(\w+)\{(.+?)\}<tool_call\|>', content, re.DOTALL)
                
                if match:
                    tool_name = match.group(1)
                    raw_args = match.group(2)
                    pattern = r'(\w+):(?:<\|"\|>(.*?)<\|"\|>|([^,]+))'
                    args_match = re.findall(pattern, raw_args, re.DOTALL)

                    tool_args = {}
                    for key, tagged_val, untagged_val in args_match:
                        value = tagged_val if tagged_val else untagged_val
                        tool_args[key] = value.strip()
                    
                    logger.info(f"Tool called: {tool_name} — args: {tool_args}")

                    tool_result = self.execute_tool(tool_name, tool_args)

                    self.total_tokens += len(self.base_model.tokenize(f"tool_name: {tool_name}, tool_result: {tool_result}".encode("utf-8")))
                    self.messages.append({"role": "tool_result", "content": f"tool_name: {tool_name}, tool_result: {tool_result}"})
                    self.conversation.append_message("tool_result", f"tool_name: {tool_name}, tool_result: {tool_result}")

                    if not tool_name in ['get_memory', 'save_preference']:
                        self.set_status("retrieved_sections")                                                 #type: ignore
                
                    final_response = self.base_model.create_chat_completion(
                        messages=self.messages,
                        temperature=0.6,
                        top_p=0.95,
                        top_k=64,
                        max_tokens=4096,
                        stream=False,
                        repeat_penalty=1.2,
                        stop=[
                            "<|end|>",
                            "<end>",
                            "<unused49>"
                        ]
                    )

                    assistant_response = final_response["choices"][0]["message"]["content"]
                    final_thinking_block, response = self.strip_thinking(assistant_response)

                    if thinking_block and final_thinking_block:
                        thinking_block += final_thinking_block

                    self.total_tokens += len(self.base_model.tokenize(response.encode("utf-8")))

                    self.messages.append({"role": "assistant", "content": response})
                    message_id = self.conversation.append_message("assistant", response)

                    if tool_name not in ["get_memory", "save_preference"]:
                        self.conversation.save_retrieved_sections(message_id, tool_result)                  #type: ignore

                    self.set_status("response_complete")                                                    #type: ignore
                    logger.info(f"Response generated for conversation: {self.conversation.conversation_id}")
                    return ChatResult(response=response, think=thinking_block, tool_result=tool_result, tool_name=tool_name)

            else:
                thinking_block, response = self.strip_thinking(content)
                self.messages.append({"role": "assistant", "content": response})
                self.conversation.append_message("assistant", response)

                self.total_tokens += len(self.base_model.tokenize(response.encode("utf-8")))

                self.set_status("response_complete")                                                        #type: ignore
                logger.info(f"Response generated for conversation: {self.conversation.conversation_id}")
                return ChatResult(response=response, think= thinking_block)

        except Exception as e:
            logger.error(f"generate_response failed: {e}")
            raise


    def generate_response_stream(self, user_text: str, retrieval_mode= "auto"):
        try:
            if not self.title_generated:
                self.set_status("generating_title")                                                          #type: ignore
                title = self._generate_title(user_text)

                if title:
                    self.conversation.update_title(title)
                    self.set_status("title_generated")                                                       #type: ignore
                else:
                    logger.error(f"Invalid title: {title}")
                    raise ValueError(f"Invalid title: {title}")

                self.title_generated = True

            self.messages.append({"role": "user", "content": user_text})
            self.conversation.append_message("user", user_text)
            self.total_tokens += len(self.base_model.tokenize(user_text.encode("utf-8")))

            self._trim_messages()

            self.set_status("generating_response")                                                       #type: ignore
            initial_stream = self.base_model.create_chat_completion(
                messages=self.messages,
                tools=self.get_active_tools(retrieval_mode),
                tool_choice="auto",
                temperature=0.6,
                top_p=0.95,
                top_k=64,
                max_tokens=4096,
                stream=True,
                repeat_penalty=1.2,
                stop=[
                    "<|end|>",
                    "<end>",
                    "<unused49>"
                ]
            )

            accumulated = ""
            in_thinking = False
            full_thinking = ""
            full_response = ""
            tool_call_detected = False

            tool_name = None
            tool_result = None

            for chunk in initial_stream:
                delta = chunk["choices"][0]["delta"].get("content", "")
                if not delta:
                    continue

                accumulated += delta

                if "<|channel>" in delta:
                    in_thinking = True
                    self.set_status("thinking_start")                                                    #type: ignore
                    continue

                if "<channel|>" in delta:
                    in_thinking = False
                    self.set_status("thinking_end")                                                      #type: ignore
                    continue

                if in_thinking:
                    yield ChatResult(think=delta)
                    full_thinking += delta

                else:
                    if "<|tool_call>" in delta or "<tool_call>" in delta:
                        tool_call_detected = True

                    if tool_call_detected:
                        pass
                    else:
                        full_response += delta
                        yield ChatResult(response=delta)

            if tool_call_detected:
                self.set_status("tool_call_detected")                                                            #type: ignore
                tool_name, tool_result = self.parse_tool_call(accumulated)
                
                self.set_status("retrieved_sections")                                                            #type: ignore

                self.total_tokens += len(self.base_model.tokenize(f"tool_name:{tool_name}, tool_result: {tool_result}".encode("utf-8")))
                self.messages.append({"role": "tool_result", "content": f"tool_name:{tool_name}, tool_result: {tool_result}"})
                self.conversation.append_message("tool_result", f"tool_name:{tool_name}, tool_result: {tool_result}")

                final_stream = self.base_model.create_chat_completion(
                    messages=self.messages,
                    temperature=0.6,
                    top_p=0.95,
                    top_k=64,
                    max_tokens=4096,
                    stream=True,
                    repeat_penalty=1.2,
                    stop=[
                        "<|end|>",
                        "<end>",
                        "<unused49>"
                    ]
                )

                in_thinking = False
                full_response = ""

                for chunk in final_stream:
                    delta = chunk["choices"][0]["delta"].get("content", "")
                    if not delta:
                        continue

                    if "<|channel>" in delta:
                        in_thinking = True
                        self.set_status("thinking_start")                                                #type: ignore
                        continue

                    if "<channel|>" in delta:
                        in_thinking = False
                        self.set_status("thinking_end")                                                  #type: ignore
                        continue

                    if in_thinking:
                        yield ChatResult(think= delta)
                        full_thinking += delta
                    else:
                        full_response += delta
                        yield ChatResult(response=delta)

            self.messages.append({"role": "assistant", "content": full_response})
            message_id = self.conversation.append_message("assistant", full_response)
            self.total_tokens += len(self.base_model.tokenize(full_response.encode("utf-8")))

            if tool_call_detected and tool_name not in ["get_memory", "save_preference"] and tool_result:
                self.conversation.save_retrieved_sections(message_id, tool_result)                          #type: ignore

            self.set_status("response_complete")                                                             #type: ignore
            logger.info(f"Streaming response complete for conversation: {self.conversation.conversation_id}")
            yield ChatResult(tool_result=tool_result, tool_name=tool_name)

        except Exception as e:
            logger.error(f"generate_response_stream failed: {e}")
            raise


    def parse_tool_call(self, accumulated):
        metadata = getattr(self.base_model, 'metadata', {})
        model_name = metadata.get('general.name', 'Unknown')

        name_lower = model_name.lower()
        if "gemma" in name_lower:
            base_model_type = "gemma"
        elif "qwen" in name_lower:
            base_model_type = "qwen"
        else:
            base_model_type = "unknown"

        logger.info(f"Base Model type detected: {base_model_type}")

        if base_model_type == "gemma":
            match = re.search(r'<\|tool_call>call:(\w+)\{(.+?)\}<tool_call\|>', accumulated, re.DOTALL)
            if match:
                tool_name = match.group(1)
                raw_args = match.group(2)
                pattern = r'(\w+):(?:<\|"\|>(.*?)<\|"\|>|([^,]+))'
                args_match = re.findall(pattern, raw_args, re.DOTALL)
                tool_args = {}
                for key, tagged_val, untagged_val in args_match:
                    value = tagged_val if tagged_val else untagged_val
                    tool_args[key] = value.strip()
                
                self.total_tokens += len(self.base_model.tokenize(f"tool_name:{tool_name}, tool_args: {tool_args}".encode("utf-8")))
                self.messages.append({"role": "tool_result", "content": f"tool_name:{tool_name}, tool_args: {tool_args}"})
                self.conversation.append_message("tool_result", f"tool_name:{tool_name}, tool_args: {tool_args}")

                tool_result = self.execute_tool(tool_name, tool_args)

                self.total_tokens += len(self.base_model.tokenize(f"tool_name:{tool_name}, tool_result: {tool_result}".encode("utf-8")))
                self.messages.append({"role": "tool_result", "content": f"tool_name:{tool_name}, tool_result: {tool_result}"})
                self.conversation.append_message("tool_result", f"tool_name:{tool_name}, tool_result: {tool_result}")

                return tool_name, tool_result

            else:
                tool_name = None
                tool_args = None
                tool_result = None

        elif base_model_type == "qwen":
            match = re.search(r'\u51c6\u6837\s*(\{.*?\})\s*\u51c6\u6837', accumulated, re.DOTALL)
            if match:
                tool_json = json.loads(match.group(1))
                tool_name = tool_json["name"]
                tool_args = tool_json["arguments"]

                self.total_tokens += len(self.base_model.tokenize(f"tool_name:{tool_name}, tool_args: {tool_args}".encode("utf-8")))
                self.messages.append({"role": "tool_result", "content": f"tool_name:{tool_name}, tool_args: {tool_args}"})
                self.conversation.append_message("tool_result", f"tool_name:{tool_name}, tool_args: {tool_args}")

                tool_result = self.execute_tool(tool_name, tool_args)

                self.total_tokens += len(self.base_model.tokenize(f"tool_name:{tool_name}, tool_result: {tool_result}".encode("utf-8")))
                self.messages.append({"role": "tool_result", "content": f"tool_name:{tool_name}, tool_result: {tool_result}"})
                self.conversation.append_message("tool_result", f"tool_name:{tool_name}, tool_result: {tool_result}")

                return tool_name, tool_result
            else:
                tool_name = None
                tool_args = None
                tool_result = None

        else:
            return None, None
        
        return tool_name, tool_result
    

    def strip_thinking(self, content: str):
        match = re.findall(r'<\|channel>.*?<channel\|>', content, flags=re.DOTALL)
        if not match:
            think = None
        else:
            think = match[0]

        return think, re.sub(r'<\|channel>.*?<channel\|>', '', content, flags=re.DOTALL).strip()


    def _generate_title(self, first_message: str):
        try:
            response = self.base_model.create_chat_completion(
                messages=[
                    {"role": "system", "content": "Generate a short 4-6 word title for a conversation that starts with the following message. Output only the title, nothing else."},
                    {"role": "user", "content": first_message}
                ],
                temperature=0.6,
                max_tokens=64,
                stream=False
            )
            title = response["choices"][0]["message"]["content"].strip()
            thinking_block, title = self.strip_thinking(title)
            logger.info(f"Auto-generated title: {title}")
            return title
        except Exception as e:
            logger.warning(f"Failed to auto-generate title: {e}")

    
    def _trim_messages(self):
        try:
            trim_start = self.base_model.n_ctx() - 2048 if self.base_model.n_ctx() > 2048 else self.base_model.n_ctx()
            while self.total_tokens > trim_start:
                if len(self.messages) <= 2:
                    break

                i = 1
                while i < len(self.messages):
                    if self.messages[i]["role"] == "user":
                        j = i + 1
                        while j < len(self.messages) and self.messages[j]["role"] != "user":
                            j += 1

                        cycle = self.messages[i:j]
                        del self.messages[i:j]

                        for msg in cycle:
                            self.total_tokens -= len(self.base_model.tokenize(msg["content"].encode("utf-8")))

                        logger.info(f"Trimmed one conversation cycle — remaining tokens: {self.total_tokens}")
                        break
                    i += 1
                else:
                    break

        except Exception as e:
            logger.error(f"Failed to trim messages: {e}")
            raise

    
    def set_status(self, status):
        if self.s:
            self.s.status =status
        else:
            return
