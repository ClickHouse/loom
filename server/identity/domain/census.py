"""Typed rows of the namespace registry census."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any


@dataclass(frozen=True, slots=True)
class UnregisteredNamespace:
    """An (org, namespace) holding live memory with no registry row."""

    org: str
    namespace: str
    live_rows: int
    last_write: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> UnregisteredNamespace:
        return cls(
            org=str(row["org"]),
            namespace=str(row["namespace"]),
            live_rows=int(row.get("live_rows") or 0),
            last_write=row.get("last_write"),
        )


@dataclass(frozen=True, slots=True)
class BenchGcCandidate:
    """A registered bench namespace idle past the window, still holding live memory."""

    org: str
    namespace: str
    owner_email: str
    live_rows: int
    last_write: datetime | None

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> BenchGcCandidate:
        return cls(
            org=str(row["org"]),
            namespace=str(row["namespace"]),
            owner_email=str(row.get("owner_email") or ""),
            live_rows=int(row.get("live_rows") or 0),
            last_write=row.get("last_write"),
        )


__all__ = ["BenchGcCandidate", "UnregisteredNamespace"]
