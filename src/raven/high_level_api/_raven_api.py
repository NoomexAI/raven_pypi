from raven.core._knowledge_base import KnowledgeBase
from raven.core._model_manager import ModelManager
from raven.session._conversation_manager import ConversationManager
from raven.session._chat_session import ChatSession
from raven.pipelines._pipeline import IngestionPipeline, RetrievalPipeline
from raven.reconstructor._reconstructor import Reconstructor
from raven.status._status import Status

from llama_cpp import LLAMA_POOLING_TYPE_MEAN


class SessionCache:
    def __init__(self):
        self.conversation_id: str | None = None
        self.last_cached_session: ChatSession | None = None

    def clear(self):
        self.conversation_id = None
        self.last_cached_session = None


class Raven:
    def __init__(
            self,
            base_model_name: str | None = None,
            base_model_parameters: dict = {
                "n_ctx": 32768,
                "n_threads": 6,
                "n_gpu_layers": -1,
                "verbose": False
            },
            embedding_model_parameters: dict = {
                "n_ctx": 0,
                "n_gpu_layers": -1,
                "n_batch": 512,
                "n_ubatch": 512,
                "embedding": True,
                "pooling_type": LLAMA_POOLING_TYPE_MEAN,
                "verbose": False
            },
            s: Status | None = None,
            global_cached_session: SessionCache | None = None
        ):
        self.s = s
        self._knowledge_base = KnowledgeBase()
        self._model_manager = ModelManager(s=self.s)

        self._base_model = self._model_manager.initiate_base_model(base_model_name, **base_model_parameters)                               #type: ignore
        self._embedding_model = self._model_manager.initiate_embedding_model(**embedding_model_parameters)
        self._sbd_model = self._model_manager.initiate_sbd_model()

        self._ingestion_pipeline = IngestionPipeline(
            knowledge_base = self._knowledge_base,
            base_model = self._base_model,
            embedding_model = self._embedding_model,
            sbd_model = self._sbd_model,
            s=self.s
        )

        self._navigation = RetrievalPipeline(self._knowledge_base)

        self._conversation_manager = ConversationManager(
            knowledge_base = self._knowledge_base,
            base_model = self._base_model,
            embedding_model = self._embedding_model
        )

        self._reconstructor = Reconstructor(self._knowledge_base)

        if not global_cached_session:
            self.cached_session = SessionCache()
        else:
            self.cached_session = global_cached_session

    
    def list_knowledges(self):
        self._navigation.list_knowledges()
    
    def list_files(self, knowledge_name: str):
        self._navigation.list_files(knowledge_name)
    
    def list_sections(self, knowledge_name: str, file_name: str):
        self._navigation.list_sections(knowledge_name, file_name)

    
    def list_conversations(self):
        self._conversation_manager.list_conversations()


    def create_knowledge(self, knowledge_name: str, user_summary: str= ""):
        """ - Global Hierarchical Retrieval tool uses the **user_summary** parameter to narrow down potential candidate for retrieval to boost perfromence"""

        self._knowledge_base.create_knowledge(knowledge_name, user_summary)
    
    def delete_knowledge(self, knowledge_name: str):
        self._knowledge_base.delete_knowledge(knowledge_name)

    def delete_conversation(self, conversation_id):
        self._conversation_manager.delete_conversation(conversation_id)


    def ingest(self, knowledge_name: str, file_path: str, force_stabilize: bool= False):
        """ - **force_stabilize** parameter enforced grammar stabilization at the cost of ingestion speed"""

        self._ingestion_pipeline.ingest(
            knowledge_name,
            file_path,
            force_stabilize
        )

    def session(self, conversation_id: str|None=None, knowledge_name: str|None=None) -> ChatSession:
        """
             - Use **conversation_id = None** to create a new conversation in you Conversation directory. If a valid **conversation_id** is provided, it'll load that conversation.
             - Use **knowledge_name** parameter to create a local conversation where retrieval tools will only have access to that specific knowledge.
        """
        if not conversation_id:
            conversation_id = self._conversation_manager.create_conversation(knowledge_name)                    #type: ignore

        self.conversation = self._conversation_manager.get_conversation(conversation_id)                        #type: ignore
        
        if not conversation_id == self.cached_session.conversation_id:
            self.chat_session = ChatSession(
                conversation = self.conversation,
                knowledge_base = self._knowledge_base,
                base_model = self._base_model,
                embedding_model = self._embedding_model,
                s= self.s
            )
            self.cached_session.conversation_id = conversation_id
            self.cached_session.last_cached_session = self.chat_session
        else:
            self.chat_session = self.cached_session.last_cached_session

        return self.chat_session                                                                                #type: ignore


    def reconstruct(self, tool_result, tool_name):
        return self._reconstructor.reconstruct(
            retrieval_result = tool_result,
            tool_name = tool_name
        )