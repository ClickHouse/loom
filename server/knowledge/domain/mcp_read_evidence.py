"""Content-free successful-read proof for one exact selected MCP document."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.knowledge.domain.document_access import fresh


@dataclass(frozen=True)
class ReadProof:
    uri: str
    scope_sha256: str
    read_at: datetime

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ReadProof | None:
        digest, observed = row.get("mcp_scope_sha256"), row.get("mcp_read_at")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(c not in "0123456789abcdef" for c in digest)
            or not isinstance(observed, str)
            or len(observed) > 64
        ):
            return None
        try:
            at = datetime.fromisoformat(observed)
        except ValueError:
            return None
        uri = row.get("source_uri")
        if at.tzinfo is None or not isinstance(uri, str):
            return None
        return cls(uri, digest, at)


@dataclass(frozen=True)
class ReadScope:
    scope_sha256: str
    selected_uris: frozenset[str]

    def admits(self, proof: ReadProof | None, *, at: datetime, stale_seconds: int) -> bool:
        return (
            proof is not None
            and proof.scope_sha256 == self.scope_sha256
            and proof.uri in self.selected_uris
            and fresh(proof.read_at, at, stale_seconds)
        )
