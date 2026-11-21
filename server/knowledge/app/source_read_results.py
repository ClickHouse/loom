"""A final shared permission barrier for composed document reads."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import nullcontext

from server.core.background import gather_or_cancel
from server.core.ch import set_query_tag
from server.core.diagnostics import private_work
from server.knowledge.app.document_reads import CheckedList
from server.knowledge.app.knowledge_search import CheckedSearch
from server.knowledge.app.source_corpus import LostCorpus


async def settle(results: Sequence[CheckedSearch | CheckedList]) -> None:
    """Renew every child, or publish their complete cached ordinary fallbacks."""
    children = tuple(results)
    proofs = tuple(child.proofs for child in children if child.proofs is not None)
    with private_work() if any(child.marked for child in children) else nullcontext():
        set_query_tag("knowledge.mirror.composed_read")
        try:
            if any(child.lost for child in children):
                raise LostCorpus()
            for proof in proofs:
                await proof.check()
            if any(child.lost for child in children) or not all(proof.fresh() for proof in proofs):
                raise LostCorpus()
        except LostCorpus:
            await gather_or_cancel(*(child.ordinary() for child in children))
