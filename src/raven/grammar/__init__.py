"""Grammar schemas for LLM output stabilization (JSON schema)."""

from importlib.resources import files

GRAMMAR_DIR = files("raven.grammar")

__all__ = ["GRAMMAR_DIR"]