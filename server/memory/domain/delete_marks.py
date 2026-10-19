"""The `source_trace` memory.delete_version stamps on its tombstone: the version it removed.

A write the version check could not see lands above such a tombstone whether it came
before or after the delete (server/memory/app/write_fence.py), so the write fence tells
this tombstone from a plain memory.delete's by it, and no caller may write the prefix.
"""

from __future__ import annotations

DELETE_VERSION_TRACE = "memory.delete_version:"


def delete_version_trace(version: int) -> str:
    """The trace of the tombstone memory.delete_version writes over `version`."""
    return f"{DELETE_VERSION_TRACE}{int(version)}"


def caller_trace(trace: str) -> str:
    """`trace` when a caller may store it as a `source_trace`; raises otherwise."""
    if trace.startswith(DELETE_VERSION_TRACE):
        raise ValueError(f"`source_trace` may not start with {DELETE_VERSION_TRACE!r}")
    return trace
