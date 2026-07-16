"""
RAVEN — Retrieval Augmented Adaptive Epistemic Navigation
Backend library for local, privacy-first RAG.
"""

from raven.high_level_api._raven_api import SessionCache, Raven
from raven import core, grammar, pipelines, reconstructor, session, status

__version__ = "0.1.0"

__all__ = [
    "SessionCache", "Raven",
    "core", "grammar", "pipelines", "reconstructor", "session", "status"
]
