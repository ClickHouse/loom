"""Strict evidence retained with a selected private response, without rendering it."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from server.core import request_tail
from server.core.diagnostics import private_work

if TYPE_CHECKING:
    from server.knowledge.app.source_access import ReadPlan
    from server.knowledge.app.source_documents import CheckedDocument


class PublicationProof(Protocol):
    @property
    def mirrored(self) -> bool: ...

    async def check(self) -> None: ...

    def fresh(self) -> bool: ...


def retain(proof: PublicationProof | None) -> None:
    if proof is not None and proof.mirrored:
        request_tail.retain_publication(proof, proof.check, proof.fresh)


def discard(proof: PublicationProof | None) -> None:
    if proof is not None:
        request_tail.discard_publication(proof)


@dataclass
class DocumentProof:
    plan: ReadPlan
    document: CheckedDocument
    _latest: CheckedDocument | None = field(default=None, init=False)

    @property
    def mirrored(self) -> bool:
        return True

    async def check(self) -> None:
        from server.knowledge.app.source_corpus import LostCorpus
        from server.knowledge.app.source_documents import refresh

        with private_work():
            self._latest = await refresh(self.plan, self.document)
            if self._latest is None:
                raise LostCorpus()

    def fresh(self) -> bool:
        return self._latest is not None and self.plan.current(self._latest.admitted)


def retain_document(plan: ReadPlan, document: CheckedDocument) -> DocumentProof:
    proof = DocumentProof(plan, document)
    retain(proof)
    return proof
