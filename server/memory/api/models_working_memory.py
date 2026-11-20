"""working_memory.* — small always-resident agent scratchpad."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.memory.api.models_common import _Identity

DEFAULT_CHAR_LIMIT = 4096
MAX_CHAR_LIMIT = 16384


class WorkingMemorySetInput(_Identity):
    """Overwrite one working-memory block."""

    # The block to write, within the caller's (agent, user, session, run) scope.
    block_key: str = Field(default="scratchpad", min_length=1, max_length=80)
    # The block's whole new text, replacing what it held; empty stores an empty block.
    content: str = Field(default="", max_length=MAX_CHAR_LIMIT)
    # The block's size cap in characters; `content` longer than it is refused with 422.
    char_limit: int = Field(default=DEFAULT_CHAR_LIMIT, ge=1, le=MAX_CHAR_LIMIT)


class WorkingMemoryAppendInput(_Identity):
    """Append text to a working-memory block, creating it when missing."""

    # The block to append to; a missing block is created with the default size cap.
    block_key: str = Field(default="scratchpad", min_length=1, max_length=80)
    # Text added at the end of the block.
    addition: str = Field(..., min_length=1, max_length=MAX_CHAR_LIMIT)
    # Put between the existing text and `addition`; unused when the block is new.
    separator: str = "\n"


class WorkingMemoryReplaceInput(_Identity):
    # The block to edit; a missing block is a 404.
    block_key: str = Field(default="scratchpad", min_length=1, max_length=80)
    # Text to find; every occurrence is replaced, and absent text is a 422.
    old: str = Field(..., min_length=1)
    # Replacement text; empty deletes the occurrences of `old`.
    new: str = ""


class WorkingMemoryGetInput(_Identity):
    # The block to read, within the caller's (agent, user, session, run) scope.
    block_key: str = Field(default="scratchpad", min_length=1, max_length=80)


class WorkingMemoryDeleteInput(_Identity):
    # The block to delete.
    block_key: str = Field(default="scratchpad", min_length=1, max_length=80)


class WorkingMemoryRenderInput(_Identity):
    # Only these blocks (at most 20); empty = every live block in the scope.
    block_keys: list[str] = Field(default_factory=list, max_length=20)
    # `xml` wraps each block in a `<working-memory block=...>` tag;
    # `markdown` puts it under a `## <block_key>` heading.
    format: Literal["xml", "markdown"] = "xml"
    # At most this many blocks, in `block_key` order (1-100).
    limit: int = Field(default=20, ge=1, le=100)


class WorkingMemoryBlock(BaseModel):
    block_key: str
    content: str
    char_limit: int
    version: int
    updated_at: datetime | None = None


class WorkingMemoryGetOutput(BaseModel):
    # False when the scope holds no live block under this key; the rest is then defaults.
    found: bool
    # Echo of the block's key.
    block_key: str
    # The block's text; empty when not found.
    content: str = ""
    # The block's size cap in characters; a write past it is refused with 422.
    char_limit: int = DEFAULT_CHAR_LIMIT
    # Version of the block's current row (a nanosecond timestamp); 0 when not found.
    version: int = 0
    # When the block was last written; None when not found.
    updated_at: datetime | None = None
    # len(content) / char_limit. The block refuses writes past 1.0 with a
    # 422; this is the number that lets a caller see that coming and trim
    # or summarize first.
    fill_ratio: float = 0.0


class WorkingMemoryMutationOutput(BaseModel):
    # The block that was written.
    block_key: str
    # Version of the row this write created (a nanosecond timestamp).
    version: int
    # The block's size cap in characters after the write.
    char_limit: int
    # Length of the block's content after the write, in characters.
    value_length: int
    # `value_length / char_limit`, to three places; a write that would pass 1.0 is refused.
    fill_ratio: float = 0.0


class WorkingMemoryDeleteOutput(BaseModel):
    # False when the scope held no live block under this key.
    deleted: bool
    # Echo of the input `block_key`.
    block_key: str


class WorkingMemoryRenderOutput(BaseModel):
    # The blocks as one text in the chosen `format`; empty when the scope has none.
    rendered: str
    # The rendered blocks with their size caps and versions, in `block_key` order.
    blocks: list[WorkingMemoryBlock] = Field(default_factory=list)
    # How many blocks were rendered.
    block_count: int
    # Sum of the blocks' content lengths, in characters.
    total_chars: int
