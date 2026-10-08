"""A namespace's `loom.code_index_state` rows, keyed by `state_key`."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict


class StateEntry(TypedDict, total=False):
    """One state key's value, its free-form detail and when it was written.
    Readers `.get` each field: a missing key reads as unset."""

    value: str
    detail: str
    updated_at: datetime


# state_key -> entry: what `CodeRepo.all_state` reads for one namespace.
IndexState = dict[str, StateEntry]
