from dataclasses import dataclass, fields

@dataclass
class StatusRegistry:
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


    def list_all(self) -> dict[str, str]:
        return {f.name: getattr(self, f.name) for f in fields(self)}


class Status:
    def __init__(self, initial_status=None, on_change=None, registry: StatusRegistry|None=None):
        self._status = initial_status
        self._listeners = {}
        self.on_change = on_change

        if registry is None:
            self.registry = StatusRegistry()
        else:
            if not isinstance(registry, StatusRegistry):
                raise RuntimeError("registry must be an instance of StatusRegistry")
            self.registry = registry

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        if not self.status == value:
            self._status = value

            if self.on_change:
                self.on_change(status=self._status)

            listeners = self._listeners.get(self.status, [])[:]
            for callback, run_once in listeners:
                if callback:
                    callback(status=self.status)

                    if run_once:
                        self._listeners[self.status].remove((callback, run_once))

    
    def on_status(self, target_status, callback, run_once=False):
        if not target_status in self._listeners:
            self._listeners[target_status] = []
        self._listeners[target_status].append((callback, run_once))
