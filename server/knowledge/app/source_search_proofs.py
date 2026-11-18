"""Text-free search proofs over admitted document identities and live chunk keys."""

from __future__ import annotations

import asyncio
from dataclasses import InitVar, dataclass, field

from server.core.ch import sequential_reads, set_query_tag
from server.core.diagnostics import private_work
from server.knowledge.app.source_access import Admitted, ReadPlan
from server.knowledge.app.source_corpus import LostCorpus, Proofs
from server.knowledge.app.source_search import _chunk_key
from server.knowledge.store import source_candidates as store


def _served(item: Admitted) -> bool:
    """The initial chunk admission must bind the key to its captured serving state."""
    revision = int(item.candidate["revision"])
    if item.serving is None:
        return bool(revision == item.document["revision"])
    state = item.serving
    return (
        not state.deleted
        and state.revision == item.document["revision"] == revision == state.chunks_revision
        and state.chunks_version is not None
        and int(item.candidate["doc_version"]) == state.chunks_version
    )


@dataclass
class SearchProofs:
    plan: ReadPlan
    model: str
    items: InitVar[tuple[Admitted, ...]]
    embedded_only: bool = True
    invalid: bool = field(default=False, init=False)
    _documents: Proofs = field(init=False, repr=False)
    _keys: tuple[store.ChunkKey, ...] = field(default=(), init=False, repr=False)
    _checked: bool = field(default=False, init=False, repr=False)
    _checking: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self, items: tuple[Admitted, ...]) -> None:
        self._documents = Proofs(self.plan, items)
        self._keys = tuple(sorted({_chunk_key(item.candidate) for item in items}))
        self.invalid = (
            self._documents.invalid
            or (bool(items) and not self.model)
            or not all(_served(item) for item in items)
        )

    @property
    def mirrored(self) -> bool:
        return bool(self._keys)

    def fresh(self) -> bool:
        if self.invalid:
            return False
        if not self._keys:
            return True
        self.invalid = not self._checked or not self._documents.fresh()
        return not self.invalid

    async def check(self) -> None:
        """Renew documents before and after every bounded exact-key probe sequence."""
        async with self._checking:
            if self.invalid:
                raise LostCorpus()
            if not self._keys:
                return
            set_query_tag("knowledge.mirror.search_proof")
            try:
                with private_work(), sequential_reads():
                    await self._documents.check()
                    for start in range(0, len(self._keys), 64):
                        keys = list(self._keys[start : start + 64])
                        current = await store.chunk_keys(
                            self.plan.org, self.model, keys, embedded_only=self.embedded_only
                        )
                        if len(current) != len(keys) or set(current) != set(keys):
                            raise LostCorpus()
                    await self._documents.check()
                    if self.invalid:
                        raise LostCorpus()
                    self._checked = True
            except LostCorpus:
                self.invalid = True
                raise
            except Exception:  # noqa: BLE001 — conditional proof failures disclose no backend details
                self.invalid = self.plan.failed = True
                raise LostCorpus() from None
