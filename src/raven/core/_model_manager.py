from raven.core._constants import BASE_MODEL_DIR, EMBEDDING_MODEL_DIR, SBD_MODEL_DIR, MODEL_CATALOG
from raven.core._downloader import Downloader

from raven.status._status import Status

try:
    from llama_cpp import Llama, LLAMA_POOLING_TYPE_MEAN
except ImportError:
    raise ImportError(
        "llama-cpp-python is required but not installed. "
        "Install it with: pip install llama-cpp-python\n"
        "For GPU acceleration: pip install llama-cpp-python[cuda]  (or [metal], [vulkan])"
    )

from wtpsplit_lite import SaT
from huggingface_hub import constants

import os
import logging
import time

logger = logging.getLogger(__name__)


class ModelManager:
    def __init__(self,
        base_model_dir: str = BASE_MODEL_DIR,
        embedding_model_dir: str = EMBEDDING_MODEL_DIR,
        sbd_model_dir: str = SBD_MODEL_DIR,
        s: Status | None = None
    ):
        self.base_model_dir = base_model_dir
        self.embedding_model_dir = embedding_model_dir
        self.sbd_model_dir = sbd_model_dir
        self.s = s

        for dir in [self.base_model_dir, self.embedding_model_dir, self.sbd_model_dir]:
            os.makedirs(dir, exist_ok=True)

        self.base_models = [item for item in os.listdir(base_model_dir) if item.endswith(".gguf")]
        self.embedding_models = [item for item in os.listdir(embedding_model_dir) if item.endswith(".gguf")]
        self.sbd_models = os.listdir(sbd_model_dir)

        self.default_base_model_name = "gemma-4-E2B-it-Q4_K_M.gguf"
        self.valid_base_models = [entry["file_name"] for entry in MODEL_CATALOG]


    def initiate_base_model(self, model_name: str = None, **kwargs) -> Llama:                              # type: ignore
        if not self.base_models or not set(self.base_models).intersection(set(self.valid_base_models)):
            self.ensure_base_model(model_name)

        # Default to first model if no name given
        if model_name is None:
            if self.default_base_model_name in self.base_models:
                model_name = self.default_base_model_name
            else:
                model_name = [f for f in self.base_models if f in self.valid_base_models][0]

        if model_name not in self.base_models:
            logger.error(f"Model not found: {model_name}")
            raise FileNotFoundError(f"Model not found: {model_name}")

        self.base_model_path = os.path.join(self.base_model_dir, model_name)
        logger.info(f"Loading base model: {model_name}")

        parameters = {
            "n_ctx": 32768,
            "n_threads": 6,
            "n_gpu_layers": -1,
            "verbose": False
        }
        parameters.update(kwargs)

        start_time = time.time()
        self.set_status(self.s.registry.LOADING_BASE_MODEL)                                                                #type: ignore
        try:
            self.base_model = Llama(
                model_path=self.base_model_path,
                **parameters
            )
            logger.info(f"Base model loaded in: {time.time() - start_time:.2f}s")
            self.set_status(self.s.registry.BASE_MODEL_LOADED)                                                             #type: ignore
            return self.base_model
        except Exception as e:
            logger.error(f"Failed to load base model: {e}")
            raise RuntimeError(f"Failed to load base model: {e}")


    def initiate_embedding_model(self, **kwargs) -> Llama:
        if not self.embedding_models or not [item for item in self.embedding_models if item.endswith(".gguf")]:
            self.ensure_embedding_model()

        if not "bge-m3-F16.gguf" in self.embedding_models:
            raise FileNotFoundError(f"Embedding model not found: bge-m3-F16.gguf")
        
        self.embedding_model_path = os.path.join(self.embedding_model_dir, "bge-m3-F16.gguf")
        logger.info(f"Loading embedding model: bge-m3-F16.gguf")

        parameters = {
            "n_ctx": 0,
            "n_gpu_layers": -1,
            "n_batch": 512,
            "n_ubatch": 512,
            "embedding": True,
            "pooling_type": LLAMA_POOLING_TYPE_MEAN,
            "verbose": False
        }
        parameters.update(kwargs)

        start_time = time.time()
        self.set_status(self.s.registry.LOADING_EMBEDDING_MODEL)                                                           #type: ignore
        try:
            self.embedding_model = Llama(
                model_path= self.embedding_model_path,
                **parameters
            )
            logger.info(f"Embedding model loaded in: {time.time() - start_time:.2f}s")
            self.set_status(self.s.registry.EMBEDDING_MODEL_LOADED)                                                        #type: ignore
            return self.embedding_model
        except Exception as e:
            logger.error(f"Failed to load embedding model: {e}")
            raise


    def initiate_sbd_model(self):
        if not self.sbd_models:
            self.ensure_sbd_model()

        os.environ["HF_HUB_OFFLINE"] = "1"
        constants.HF_HUB_OFFLINE = True

        if not "sat-3l-sm" in self.sbd_models:
            raise FileNotFoundError(f"SBD model dir not found: sat-3l-sm")
        
        self.sbd_model_path = os.path.join(self.sbd_model_dir, "sat-3l-sm")
        logger.info(f"Loading SBD model: sat-3l-sm")

        start_time = time.time()
        self.set_status(self.s.registry.LOADING_SBD_MODEL)                                                                 #type: ignore
        try:
            self.sbd_model = SaT(self.sbd_model_path)
            logger.info(f"SBD model loaded in: {time.time() - start_time:.2f}s")

            os.environ["HF_HUB_OFFLINE"] = "0"
            constants.HF_HUB_OFFLINE = False

            self.set_status(self.s.registry.SBD_MODEL_LOADED)                                                              #type: ignore
            return self.sbd_model
        except Exception as e:
            logger.error(f"Failed to load SBD model: {e}")
            raise

    def ensure_base_model(self, model_name: str | None = None):
        entry = next((e for e in MODEL_CATALOG if e["file_name"] == self.default_base_model_name), None) if not model_name else next((e for e in MODEL_CATALOG if e["file_name"] == model_name), None)
        if not entry:
            logger.error(f"No catalog entry for model: {self.default_base_model_name}")
            return
        display_name = entry["display_name"]
        logger.info(f"Downloading default model: {display_name}")
        
        try:
            self.set_status(self.s.registry.DOWNLOADING_BASE_MODEL)                                                        #type: ignore
            Downloader.download(entry["repo_id"], entry["file_name"], self.base_model_dir)
            logger.info(f"Default model downloaded: {display_name}")
            self.base_models = os.listdir(self.base_model_dir) if os.path.exists(self.base_model_dir) else []

            self.set_status(self.s.registry.BASE_MODEL_DOWNLOADED)                                                         #type: ignore
            logger.info("Initializing models")
        except Exception as e:
            logger.error(f"Failed to download {display_name}: {e}")
            raise

    def ensure_embedding_model(self):
        """Download BGE-M3 embedding model if not present."""
        embedding_model_name = "bge-m3-F16.gguf"
        repo_id = "lm-kit/bge-m3-GGUF"
        
        logger.info(f"Downloading embedding model: {embedding_model_name}")
        
        try:
            self.set_status(self.s.registry.DOWNLOADING_EMBEDDING_MODEL)                                                   #type: ignore
            Downloader.download(repo_id, embedding_model_name, self.embedding_model_dir)
            self.embedding_models = os.listdir(self.embedding_model_dir) if os.path.exists(self.embedding_model_dir) else []

            self.set_status(self.s.registry.EMBEDDING_MODEL_DOWNLOADED)                                                    #type: ignore
            logger.info(f"Embedding model downloaded: {embedding_model_name}")
        except Exception as e:
            logger.error(f"Failed to download embedding model: {e}")
            raise

    def ensure_sbd_model(self):
        """Download SaT-3L-SM sentence boundary detection model if not present."""
        sbd_dir = os.path.join(self.sbd_model_dir, "sat-3l-sm")
        os.makedirs(sbd_dir, exist_ok=True)
        
        files_to_download = [
            ("config.json", "segment-any-text/sat-3l-sm"),
            ("model_optimized.onnx", "segment-any-text/sat-3l-sm"),
        ]
        
        self.set_status(self.s.registry.DOWNLOADING_SBD_MODEL)                                                             #type: ignore
        for file_name, repo_id in files_to_download:
            logger.info(f"Downloading SBD file: {file_name}")
            
            try:
                Downloader.download(repo_id, file_name, sbd_dir)
            except Exception as e:
                logger.error(f"Failed to download SBD file {file_name}: {e}")
                raise
        
        self.set_status(self.s.registry.SBD_MODEL_DOWNLOADED)                                                              #type: ignore
        logger.info("SBD model downloaded: sat-3l-sm")
        self.sbd_models = os.listdir(self.sbd_model_dir) if os.path.exists(self.sbd_model_dir) else []

    
    def set_status(self, status):
        if self.s:
            self.s.status = status