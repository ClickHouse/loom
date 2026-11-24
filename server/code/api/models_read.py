"""Read exact UTF-8 source ranges from indexed Git files."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from server.code.api.models_common import _CodeIdentity


class CodeReadInput(_CodeIdentity):
    ranges: list[Annotated[str, Field(max_length=1024)]] = Field(
        default_factory=list,
        max_length=32,
        description="Batch of repo-relative path:start-end ranges with inclusive one-based lines.",
    )
    path: str | None = Field(
        default=None, min_length=1, max_length=1024, description="File for a single range."
    )
    start_line: int | None = Field(
        default=None, ge=1, lt=2**32, description="First line of the single range, one-based."
    )
    end_line: int | None = Field(
        default=None, ge=1, lt=2**32, description="Last line, inclusive. Defaults to start_line."
    )


class CodeReadWindow(BaseModel):
    start_line: int
    end_line: int
    text: str


class CodeReadRange(BaseModel):
    path: str
    start_line: int
    end_line: int
    status: Literal["complete", "partial", "not_indexed", "out_of_range", "unavailable"]
    indexed_commit: str = ""
    windows: list[CodeReadWindow] = Field(default_factory=list)


class CodeReadOutput(BaseModel):
    results: list[CodeReadRange]
    as_of: dict[str, Any]
