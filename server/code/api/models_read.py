"""Read exact UTF-8 source ranges from indexed Git files."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from server.code.api.models_common import _CodeIdentity


class CodeReadInput(_CodeIdentity):
    ranges: list[Annotated[str, Field(max_length=1024)]] = Field(
        min_length=1,
        max_length=32,
        description="Repo-relative path:start-end ranges with inclusive one-based line numbers.",
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
