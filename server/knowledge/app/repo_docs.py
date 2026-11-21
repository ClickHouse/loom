"""A repository's documents.

Repository documents live in the trunk code namespace
(`server/code/app/repo_scope.py`). Ordinary documents inherit the repository's
access. Mirrored documents keep their source's permissions for each reader.

Under a code scope the repository's documents join `loom_search`'s knowledge
section, and `code.context_pack` returns them as its `docs` section. Every
read passes the gate for the current principal first and is bounded; a
failure answers nothing, never an error — the leg is additive.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import time
from contextlib import nullcontext
from typing import TYPE_CHECKING, TypedDict

from server.core.background import gather_or_cancel
from server.core.diagnostics import private_work
from server.knowledge.api.models_knowledge import (
    KnowledgeListInput,
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.knowledge.app import source_publication
from server.settings import settings

log = logging.getLogger("loom.repo_docs")


if TYPE_CHECKING:
    from server.knowledge.app.knowledge_search import CheckedSearch, SearchRequest
    from server.knowledge.app.source_access import ReadPlan
    from server.knowledge.app.source_list_proofs import ListProofs
    from server.knowledge.app.source_search_proofs import SearchProofs


@dataclasses.dataclass(frozen=True)
class RepoSearchRequest:
    base: SearchRequest
    extra: SearchRequest | None
    repo_namespace: str
    extra_deadline: float

    @property
    def namespaces(self) -> list[str]:
        return list(
            dict.fromkeys((*self.base.namespaces, *(self.extra.namespaces if self.extra else ())))
        )

    def wider(self, top_k: int) -> RepoSearchRequest:

        def resize(request: SearchRequest, size: int) -> SearchRequest:
            return dataclasses.replace(
                request, input=request.input.model_copy(update={"top_k": size}, deep=True)
            )

        return RepoSearchRequest(
            resize(self.base, top_k),
            resize(self.extra, max(1, min(top_k, 20))) if self.extra is not None else None,
            self.repo_namespace,
            asyncio.get_running_loop().time() + settings.knowledge_repo_docs_timeout_s,
        )


@dataclasses.dataclass
class RepoSearch:
    request: RepoSearchRequest
    base: CheckedSearch
    extra: CheckedSearch | None

    _lost: bool = dataclasses.field(default=False, init=False)
    _base_used: bool = dataclasses.field(default=False, init=False)
    _extra_used: bool = dataclasses.field(default=False, init=False)
    _checking: asyncio.Lock = dataclasses.field(
        default_factory=asyncio.Lock, init=False, repr=False
    )

    @property
    def lost(self) -> bool:
        return self._lost or any(child.lost for child in self.children)

    def fresh(self) -> bool:
        """Check retained permissions and additive timing without another await."""
        if self.lost:
            return False
        if (
            self.extra is not None
            and self.extra.proofs is not None
            and asyncio.get_running_loop().time() >= self.request.extra_deadline
        ):
            return False
        return all(child.proofs is None or child.proofs.fresh() for child in self.children)

    async def _renew_extra(self, *, ordinary: bool = False) -> None:
        extra = self.extra
        if extra is None:
            return
        try:
            if extra.proofs is None:
                # A cached ordinary reply needs no further permission or provider work.
                await extra.ordinary() if ordinary else await extra.current()
            else:
                if asyncio.get_running_loop().time() >= self.request.extra_deadline:
                    raise TimeoutError()
                async with asyncio.timeout_at(self.request.extra_deadline):
                    if ordinary:
                        await extra.ordinary()
                    else:
                        await extra.current()
        except Exception:  # noqa: BLE001 — an additive renewal failure leaves the base search intact
            self._lost |= extra.lost
            self.extra = None
            log.info("repo docs search did not answer")

    def _drop_expired_extra(self) -> None:
        if (
            self.extra is not None
            and self.extra.proofs is not None
            and asyncio.get_running_loop().time() >= self.request.extra_deadline
        ):
            self._lost |= self.extra.lost
            self.extra = None

    async def _ordinary(self) -> KnowledgeSearchOutput:
        self._lost = True
        await gather_or_cancel(self.base.ordinary(), self._renew_extra(ordinary=True))
        self._drop_expired_extra()
        return self.output

    async def ordinary(self) -> KnowledgeSearchOutput:
        async with self._checking:
            with private_work() if self.base.marked else nullcontext():
                return await self._ordinary()

    async def current(self) -> KnowledgeSearchOutput:
        """Renew both legs without charging the base to the additive deadline."""
        async with self._checking:
            with private_work() if self.base.marked else nullcontext():
                if self.lost:
                    return await self._ordinary()
                await self._renew_extra()
                await self.base.current()
                if self.lost or any(
                    child.proofs is not None and not child.proofs.fresh() for child in self.children
                ):
                    return await self._ordinary()
                self._drop_expired_extra()
                return self.output

    @property
    def children(self) -> tuple[CheckedSearch, ...]:
        return (self.base, self.extra) if self.extra is not None else (self.base,)

    @property
    def publication_proofs(self) -> tuple[SearchProofs, ...]:
        children = ((self.base,) if self._base_used else ()) + (
            (self.extra,) if self._extra_used and self.extra is not None else ()
        )
        return tuple(child.proofs for child in children if child.proofs is not None)

    @property
    def output(self) -> KnowledgeSearchOutput:
        extra = (
            []
            if self.extra is None
            else [
                hit
                if hit.namespace
                else hit.model_copy(update={"namespace": self.request.repo_namespace})
                for hit in self.extra.output.results
            ]
        )
        output = _merge_output(self.base.output, extra, int(self.request.base.input.top_k))
        selected = {id(hit) for hit in output.results}
        self._base_used = self.base.metadata_uses_mirrors or any(
            id(hit) in selected for hit in self.base.output.results
        )
        self._extra_used = any(id(hit) in selected for hit in extra)
        return output


async def resolve_repo_search(
    input: KnowledgeSearchInput, *, repo_namespace: str
) -> RepoSearchRequest:
    from server.knowledge.app.knowledge_search import resolve_search

    input = input.model_copy(deep=True)
    base = await resolve_search(input)
    extra = None
    deadline = asyncio.get_running_loop().time() + settings.knowledge_repo_docs_timeout_s
    if repo_namespace and input.query.strip():
        try:
            async with asyncio.timeout_at(deadline):
                extra = await resolve_search(
                    KnowledgeSearchInput(
                        org=input.org,
                        namespace=repo_namespace,
                        query=input.query,
                        top_k=max(1, min(int(input.top_k), 20)),
                        namespace_scope="this",
                        # A model reranker would outlast this additive leg's deadline.
                        rerank="none",
                    )
                )
        except Exception:  # noqa: BLE001 — an additive scope failure leaves the base search intact
            log.info("repo docs search did not answer")
    return RepoSearchRequest(base, extra, repo_namespace, deadline)


async def search_repo_docs(request: RepoSearchRequest, plan: ReadPlan) -> CheckedSearch | None:
    """Run the resolved additive leg within its remaining scope-and-search budget."""
    from server.knowledge.app.knowledge_search import run_checked_search

    if request.extra is None:
        return None
    try:
        if asyncio.get_running_loop().time() >= request.extra_deadline:
            raise TimeoutError()
        async with asyncio.timeout_at(request.extra_deadline):
            return await run_checked_search(request.extra, plan)
    except Exception:  # noqa: BLE001 — a failed additive search cannot expose private details
        log.info("repo docs search did not answer")
        return None


async def run_repo_search(request: RepoSearchRequest, plan: ReadPlan) -> RepoSearch:
    from server.knowledge.app.knowledge_search import run_checked_search

    if any(child.input.org != plan.org for child in (request.base, request.extra) if child):
        raise ValueError("document read scope changed")
    with private_work() if plan.marked else nullcontext():
        base, extra = await gather_or_cancel(
            run_checked_search(request.base, plan), search_repo_docs(request, plan)
        )
        assert base is not None
        return RepoSearch(request, base, extra)


def merge_hits(base: list[_SearchHit], extra: list[_SearchHit], top_k: int) -> list[_SearchHit]:
    """One knowledge list: both legs' hits, one per (namespace, document,
    chunk), by fused score, then similarity, the noise sections last (`noise_last`)."""
    from server.knowledge.app.knowledge_search import noise_last

    seen: set[tuple[str, str, int]] = set()
    out: list[_SearchHit] = []
    for h in sorted(
        [*base, *extra],
        key=lambda x: (x.score, x.similarity),
        reverse=True,
    ):
        key = (h.namespace, h.source_uri, h.chunk_idx)
        if key in seen:
            continue
        seen.add(key)
        out.append(h)
    return noise_last(out)[: max(1, int(top_k))]


async def knowledge_with_repo_docs(
    know_input: KnowledgeSearchInput, *, repo_namespace: str
) -> KnowledgeSearchOutput:
    """Merge checked base and repository searches after their final shared barrier."""
    from server.knowledge.app.source_access import prepare

    request = await resolve_repo_search(know_input, repo_namespace=repo_namespace)
    plan = await prepare(know_input.org, request.namespaces)
    with private_work() if plan.marked else nullcontext():
        result = await run_repo_search(request, plan)
        output = await result.current()
        for proof in result.publication_proofs:
            source_publication.retain(proof)
        return output


def _merge_output(
    base: KnowledgeSearchOutput, extra: list[_SearchHit], top_k: int
) -> KnowledgeSearchOutput:
    from server.knowledge.app.knowledge_search import noise_last

    if not extra:
        return base
    picked = merge_hits(list(base.results), extra, top_k)
    if base.reranked_by:
        # Scores select repository hits; the base reranker's order stays intact.
        listed = {(h.namespace, h.source_uri, h.chunk_idx) for h in base.results}
        repo = [h for h in picked if (h.namespace, h.source_uri, h.chunk_idx) not in listed]
        results = noise_last([*base.results[: top_k - len(repo)], *repo])
        return base.model_copy(update={"results": results})
    return base.model_copy(update={"results": picked})


class RepoDoc(TypedDict):
    source_uri: str
    title: str
    description: str


DOCS_SECTION_MAX_TOKENS = 300
DOCS_SECTION_MAX_DOCS = 12
_LIST_CACHE: dict[tuple[str, str, int], tuple[float, list[RepoDoc]]] = {}


@dataclasses.dataclass(frozen=True)
class RepoListing:
    namespace: str
    docs: list[RepoDoc]
    proof: ListProofs | None = None


async def list_repo_docs(
    org: str,
    code_namespace: str,
    *,
    limit: int = DOCS_SECTION_MAX_DOCS,
    timeout_s: float | None = None,
) -> RepoListing:
    """Return repository titles with the physical owners of the selected page."""
    if timeout_s is None:
        timeout_s = settings.knowledge_repo_docs_timeout_s
    from server.code.app.repo_scope import readable_repo_namespace

    ns = await readable_repo_namespace(org, code_namespace)
    if not ns:
        return RepoListing("", [])
    try:
        from server.knowledge.app.document_reads import resolve_list, run_checked_list
        from server.knowledge.app.source_access import prepare
        from server.knowledge.app.source_read_results import settle

        async with asyncio.timeout(timeout_s):
            request = await resolve_list(
                KnowledgeListInput(
                    org=org,
                    namespace=ns,
                    limit=max(1, min(int(limit), 50)),
                    fields=["source_uri", "title", "description"],
                )
            )
            plan = await prepare(org, list(request.namespaces))
            if not plan.marked:
                hit = _LIST_CACHE.get((org, ns, limit))
                if hit is not None and hit[0] > time.monotonic():
                    return RepoListing(ns, list(hit[1]))
            checked = await run_checked_list(request, plan)
            await settle([checked])
        # Check retained deadlines after the timeout context has finished its cleanup.
        if checked.proofs is not None and not checked.proofs.fresh():
            await checked.ordinary()
    except Exception:  # noqa: BLE001 — a failed additive listing cannot expose private details
        log.info("repo docs listing did not answer")
        return RepoListing(ns, [])
    docs = [
        RepoDoc(source_uri=r.source_uri, title=r.title, description=r.description)
        for r in checked.output.rows
    ]
    if not plan.marked:
        _LIST_CACHE[(org, ns, limit)] = (
            time.monotonic() + settings.knowledge_repo_docs_list_ttl_s,
            docs,
        )
    proof = checked.proofs
    if proof is not None:
        # The docs section uses page rows/counts, never knowledge.list's cursor.
        proof = proof.selected({(ns, row.source_uri) for row in checked.output.rows})
    return RepoListing(ns, list(docs), proof)


def reset_cache_for_tests() -> None:
    _LIST_CACHE.clear()


def docs_section(ns: str, docs: list[RepoDoc], max_tokens: int) -> str:
    """The context pack's `Repository docs` section: one line per document
    — what it is and where to read it — within `max_tokens` (the pack's
    estimate, 4 characters a token); "" when there are none."""
    if not ns or not docs or max_tokens <= 0:
        return ""
    head = (
        f"## Repository docs ({ns})\n"
        f"Read one with knowledge.get(namespace={ns!r}, source_uri=…); "
        "loom_search in this checkout searches them.\n"
    )
    lines: list[str] = []
    budget = max_tokens * 4 - len(head)
    for d in docs:
        title = d.get("title") or d.get("source_uri") or ""
        desc = (d.get("description") or "").replace("\n", " ").strip()
        line = f"- {title} — {d.get('source_uri')}" + (f": {desc[:160]}" if desc else "")
        if budget - len(line) - 1 < 0:
            lines.append(f"- … {len(docs) - len(lines)} more (knowledge.list on {ns})")
            break
        budget -= len(line) + 1
        lines.append(line)
    return head + "\n".join(lines) if lines else ""
