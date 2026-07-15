import logging
import os
from raven.core._constants import ROOT_DIR

def setup_logging():
    os.makedirs(ROOT_DIR, exist_ok=True)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(os.path.join(ROOT_DIR, "raven.log"), encoding="utf-8")
        ]
    )