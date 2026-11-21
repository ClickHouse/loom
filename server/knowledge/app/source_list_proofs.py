"""Text-free list proofs over selected copies and their ordinary shadows."""

from __future__ import annotations

import asyncio
from dataclasses import InitVar, dataclass, field

from server.core.ch import sequential_reads, set_query_tag
from server.core.diagnostics import private_work
from server.knowledge.app.source_access import Admitted, ReadPlan
from server.knowledge.app.source_corpus import LostCorpus, Proofs
from server.knowledge.store import source_documents as store


@dataclass
class ListProofs:
    plan: ReadPlan
    include_superseded: bool
    items: InitVar[tuple[Admitted, ...]]
    source_id: str | None = None
    invalid: bool = field(default=False, init=False)
    _documents: Proofs = field(init=False, repr=False)
    _families: tuple[tuple[str, str], ...] = field(default=(), init=False, repr=False)
    _checked: bool = field(default=False, init=False, repr=False)
    _items: tuple[Admitted, ...] = field(default=(), init=False, repr=False)
    _checking: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self, items: tuple[Admitted, ...]) -> None:
        self._items = items
        self._documents = Proofs(self.plan, items)
        self._families = tuple(
            (item.namespace, item.candidate["source_uri"].rsplit("#", 1)[0]) for item in items
        )
        self.invalid = self._documents.invalid or len(set(self._families)) != len(items)

    def selected(self, families: set[tuple[str, str]]) -> ListProofs:
        """Keep the physical owners of the final page and its cursor lookahead."""
        return ListProofs(
            self.plan,
            self.include_superseded,
            tuple(
                item
                for family, item in zip(self._families, self._items, strict=True)
                if family in families
            ),
            source_id=self.source_id,
        )

    @property
    def mirrored(self) -> bool:
        return bool(self._families)

    def fresh(self) -> bool:
        if self.invalid:
            return False
        if not self._families:
            return True
        self.invalid = not self._checked or not self._documents.fresh()
        return not self.invalid

    async def check(self) -> None:
        """Renew every selected family, including the page's lookahead, as one list."""
        async with self._checking:
            if self.invalid:
                raise LostCorpus()
            if not self._families:
                return
            set_query_tag("knowledge.mirror.list_proof")
            try:
                with private_work(), sequential_reads():
                    await self._documents.check()
                    for start in range(0, len(self._families) if self.source_id is None else 0, 64):
                        if await store.ordinary_families(
                            self.plan.org,
                            list(self._families[start : start + 64]),
                            include_superseded=self.include_superseded,
                        ):
                            raise LostCorpus()
                    await self._documents.check()
                    if self.invalid:
                        raise LostCorpus()
                    self._checked = True
            except LostCorpus:
                self.invalid = True
                raise
            except Exception:  # noqa: BLE001 — conditional shadow failures disclose no backend details
                self.invalid = self.plan.failed = True
                raise LostCorpus() from None
