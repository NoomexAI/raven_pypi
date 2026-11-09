"""Document selections shared by retrieval callers."""

from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, StringConstraints


_NonBlankString = Annotated[str, StringConstraints(pattern=r"\S")]


class RetrievalSelection(BaseModel):
    """Select a whole knowledge or a file by ID, name, or both."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    knowledge_name: _NonBlankString
    file_id: _NonBlankString | None = None
    file_name: _NonBlankString | None = None




class RetrievalScope(BaseModel):
    """Select documents across knowledges. An empty scope selects the whole base."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    selections: tuple[RetrievalSelection, ...] = ()
