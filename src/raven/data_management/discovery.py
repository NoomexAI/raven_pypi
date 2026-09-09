"""Contracts for reporting persisted resources that Raven cannot load."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..core.errors import ErrorCode, RavenError


class DiscoveryIssue(BaseModel):
    """One safely reportable problem found during registry discovery."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    resource_type: Literal["knowledge", "conversation"]
    directory_name: str = Field(min_length=1)
    code: ErrorCode
    message: str = Field(min_length=1)


    @classmethod
    def from_error(
        cls,
        resource_type: Literal["knowledge", "conversation"],
        directory_name: str,
        error: BaseException,
    ) -> "DiscoveryIssue":
        if isinstance(error, RavenError):
            code = error.code
            message = error.message
        else:
            code = ErrorCode.INVALID_METADATA
            message = (
                f"Unable to load {resource_type} metadata from "
                f"directory '{directory_name}'."
            )
        return cls(
            resource_type=resource_type,
            directory_name=directory_name,
            code=code,
            message=message,
        )


    def as_error(self) -> RavenError:
        return RavenError(
            self.code,
            self.message,
            details={
                "resource_type": self.resource_type,
                "directory_name": self.directory_name,
            },
        )
