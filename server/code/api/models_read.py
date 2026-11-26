"""Read exact UTF-8 source ranges from indexed Git files."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field

from server.code.api.models_common import _CodeIdentity, _LessonsOut, _ResultLessons

# not a setting: the request schema publishes this string length.
CODE_READ_RANGE_TEXT_MAX_CHARS = 1024
# not a setting: the request schema publishes this batch size.
CODE_READ_MAX_RANGES = 32

_FilePath = Annotated[str, Field(min_length=1, max_length=1024)]
_SourceLine = Annotated[int, Field(ge=1, lt=2**32)]
_RangeText = Annotated[str, Field(max_length=CODE_READ_RANGE_TEXT_MAX_CHARS)]


class CodeReadRangeInput(BaseModel):
    path: _FilePath
    start_line: _SourceLine
    end_line: _SourceLine | None = None


class CodeReadInput(_CodeIdentity, _ResultLessons):
    # Batch of inclusive one-based ranges as path:start-end strings or objects
    # with path, start_line, and optional end_line.
    ranges: (
        Annotated[list[_RangeText], Field(max_length=CODE_READ_MAX_RANGES)]
        | Annotated[list[CodeReadRangeInput | _RangeText], Field(max_length=CODE_READ_MAX_RANGES)]
    ) = Field(default_factory=list)
    # Repository-relative file for a single range.
    path: _FilePath | None = None
    # First line of a single range, one-based.
    start_line: _SourceLine | None = None
    # Inclusive last line, defaulting to start_line.
    end_line: _SourceLine | None = None


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


class CodeReadOutput(_LessonsOut):
    results: list[CodeReadRange]
    as_of: dict[str, Any]
    # Joins this read to the code trace and request ledger.
    trace_run_id: str = ""
