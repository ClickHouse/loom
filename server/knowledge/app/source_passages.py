"""Checked document passage selections with saved ordinary search passes."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, field, replace

from server.core.diagnostics import private_work
from server.core.errors import VectorCapacityError
from server.knowledge.api.models_knowledge import _SearchHit
from server.knowledge.app import knowledge_search
from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
from server.knowledge.app.source_access import ReadPlan
from server.knowledge.app.source_corpus import LostCorpus
from server.knowledge.app.source_search_proofs import SearchProofs
from server.memory.app.agent_rules import request_rule_agent, served_hits
from server.memory.domain.assistant_rules import hits_to_ask


@dataclass
class CheckedPassages:
    _first: CheckedSearch
    _wide: CheckedSearch | None
    _agent: str
    _top_k: int
    _keep: Callable[[_SearchHit], bool] | None
    hits: list[_SearchHit]
    lost: bool = False
    _used_wide: bool = field(default=False, init=False)
    _ordinary_mode: bool = field(default=False, init=False)
    _failed: bool = field(default=False, init=False)
    _ordinary_ready: bool = field(default=False, init=False)
    _checking: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    @property
    def marked(self) -> bool:
        return self._first.marked

    @property
    def mirrored(self) -> bool:
        children = (self._first,) if self._wide is None else (self._first, self._wide)
        return not self.lost and any(
            child.proofs is not None and child.proofs.mirrored for child in children
        )

    def fresh(self) -> bool:
        if self._failed:
            return False
        if self._ordinary_mode:
            return self._ordinary_ready
        children = (self._first,) if self._wide is None else (self._first, self._wide)
        return all(
            not child.lost and (child.proofs is None or child.proofs.fresh()) for child in children
        )

    def publication_proofs(self) -> tuple[SearchProofs, ...]:
        if self._ordinary_mode or self.lost or self._failed:
            return ()
        children = (self._first, self._wide) if self._used_wide else (self._first,)
        return tuple(
            child.proofs for child in children if child is not None and child.proofs is not None
        )

    async def _select(
        self, first: list[_SearchHit], wide: list[_SearchHit] | None
    ) -> list[_SearchHit]:
        used_wide = False

        async def saved(_width: int) -> list[_SearchHit]:
            nonlocal used_wide
            used_wide = True
            if wide is None:
                raise LostCorpus()
            return wide

        result = await served_hits(
            first, saved, lambda hit: hit.tags, self._agent, self._top_k, keep=self._keep
        )
        self._used_wide = used_wide
        return result

    async def _fallback(self) -> list[_SearchHit]:
        if self._failed:
            raise LostCorpus()
        if self._ordinary_mode:
            return self.hits
        self._ordinary_mode, self.lost = True, True
        try:
            first = (await self._first.ordinary()).results
            wide = None
            if self._wide is not None:
                try:
                    wide = (await self._wide.ordinary()).results
                except VectorCapacityError:
                    raise
                except Exception:  # noqa: BLE001 — the first complete ordinary pass still answers
                    self._wide = None
            self.hits = await self._select(first, wide)
            self._ordinary_ready = True
        except BaseException:
            self._failed = True
            raise
        return self.hits

    async def ordinary(self) -> list[_SearchHit]:
        async with self._checking:
            with private_work() if self.marked else nullcontext():
                return await self._fallback()

    async def current(self) -> list[_SearchHit]:
        async with self._checking:
            with private_work() if self.marked else nullcontext():
                if self._failed:
                    raise LostCorpus()
                if self._ordinary_mode:
                    return self.hits
                try:
                    first = (await self._first.current()).results
                    wide = None
                    if self._wide is not None:
                        try:
                            wide = (await self._wide.current()).results
                        except VectorCapacityError:
                            raise
                        except Exception:  # noqa: BLE001 — discard mixed work if an optional renewal fails
                            return await self._fallback()
                    if not self.fresh():
                        return await self._fallback()
                    selected = await self._select(first, wide)
                    if not self.fresh():
                        return await self._fallback()
                    self.hits = selected
                    return self.hits
                except BaseException:
                    self._failed = True
                    raise


async def collect(
    request: SearchRequest,
    plan: ReadPlan,
    agent: str,
    *,
    keep: Callable[[_SearchHit], bool] | None = None,
) -> CheckedPassages:
    """Keep marked k/w requests distinct and ordinary widening lazy."""
    if request.input.org != plan.org:
        raise ValueError("document read scope changed")
    request = replace(request, input=request.input.model_copy(deep=True))
    agent = request_rule_agent(agent)
    top_k = request.input.top_k
    with private_work() if plan.marked else nullcontext():
        first = await knowledge_search.run_checked_search(request, plan)
        wide = None

        async def wider(width: int) -> list[_SearchHit]:
            nonlocal wide
            widened = replace(
                request, input=request.input.model_copy(update={"top_k": width}, deep=True)
            )
            wide = await knowledge_search.run_checked_search(widened, plan)
            return wide.output.results

        if plan.marked:
            width = hits_to_ask(top_k)
            if width > top_k:
                try:
                    await wider(width)
                except VectorCapacityError:
                    raise
                except Exception:  # noqa: BLE001 — a failed optional wide pass does not replace the first
                    wide = None
            result = CheckedPassages(first, wide, agent, top_k, keep, [])
            result.hits = await result._select(
                first.output.results, wide.output.results if wide else None
            )
            return result
        hits = await served_hits(
            first.output.results, wider, lambda hit: hit.tags, agent, top_k, keep=keep
        )
        return CheckedPassages(first, wide, agent, top_k, keep, hits)
