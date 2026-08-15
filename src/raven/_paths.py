"""Filesystem roots for the v2 core.

All three consumers (knowledge, conversations, ollama bundle) default to the
PROJECT root of this repository, not to the package dir — moving the package
under src/raven/ must not relocate user data or the Ollama bundle.
"""

import os
from pathlib import Path

# src/raven/_paths.py -> project root (repo top level)
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def data_root() -> Path:
    for var in ("RAVEN_DATA_DIR", "RAVEN_HOME"):
        val = os.environ.get(var)
        if val:
            return Path(val).resolve()
    return PROJECT_ROOT / "data"


def ollama_root() -> Path:
    return Path(os.environ.get("RAVEN_HOME", PROJECT_ROOT)).resolve()
