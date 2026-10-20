"""Whether the op being served is a read that may make no LLM call.

`LOOM_READS_LLM_FREE` makes every read op (`Operation.read_only`) LLM-free. The
transports mark the request while they run its handler, and every read path
that could call a model reads the mark: memory.search through `_is_llm_free`,
the other planes through `reads_llm_free()`. An op whose purpose is a model's
answer (an ask's synthesis) refuses with 409 `READS_LLM_FREE` instead of
answering without one.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from server.core.errors import ReadsLlmFreeError
from server.settings import settings

_read: ContextVar[bool] = ContextVar("llm_free_read", default=False)


def reads_llm_free() -> bool:
    """True while serving a read op under `LOOM_READS_LLM_FREE`."""
    return _read.get()


@contextmanager
def serving(op: Any) -> Iterator[None]:
    """Marks the handler's run of `op`: a read op is LLM-free under the setting."""
    token = _read.set(bool(settings.reads_llm_free and op.read_only))
    try:
        yield
    finally:
        _read.reset(token)


def refuse_synthesis(op_name: str, retrieval_only: str) -> None:
    """409 for a read whose answer a model writes, while reads are LLM-free."""
    if reads_llm_free():
        raise ReadsLlmFreeError(
            f"{op_name} answers with an LLM, and LOOM_READS_LLM_FREE makes every read "
            f"LLM-free: ask with {retrieval_only} for the recalled evidence"
        )
