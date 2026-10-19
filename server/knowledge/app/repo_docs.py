"""A repository's documents.

A document a repository depends on — a design note, a runbook, an internal
spec — is a knowledge document in the repository's docs namespace: its trunk
branch's code namespace (`repos.docs_namespaces`). It is written there by a
maintainer (admin on the code namespace, so `knowledge.add` passes the
ordinary WRITER gate) and read by whoever may read the repository, so a
restricted repository's documents are as restricted as its code.

Under a code scope the repository's documents join `loom_search`'s knowledge
section, and `code.context_pack` returns them as its `docs` section. Every
read passes the gate for the current principal first and is bounded; a
failure answers nothing, never an error — the leg is additive.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TypedDict

from server.knowledge.api.models_knowledge import (
    KnowledgeListInput,
    KnowledgeSearchInput,
    KnowledgeSearchOutput,
    _SearchHit,
)
from server.settings import settings

log = logging.getLogger("loom.repo_docs")

REPO_DOCS_TIMEOUT_S = 1.5


def docs_namespace_of(org: str, code_namespace: str) -> str:
    """The docs namespace of the repository `code_namespace` indexes; "" when
    it is not a repository index."""
    from server.code.domain.index.repos import docs_namespaces, repo_for_namespace

    cfg = repo_for_namespace(org, code_namespace)
    if cfg is None:
        return ""
    return docs_namespaces().get((org, str(cfg.repo)), "")


async def readable_docs_namespace(org: str, code_namespace: str) -> str:
    """`docs_namespace_of`, passed through the gate for the current principal
    (VIEWER on the docs namespace); "" when unbound, refused, or the check
    fails (closed)."""
    ns = docs_namespace_of(org, code_namespace)
    if not ns:
        return ""
    try:
        from server.auth.authz import row_in_scope
        from server.operations.base import Role

        return ns if await row_in_scope(org, ns, Role.VIEWER, hides_answer=False) else ""
    except Exception as e:  # noqa: BLE001 — closed
        log.debug("repo docs gate failed (closed): %s", e)
        return ""


async def search_repo_docs(
    org: str, code_namespace: str, query: str, top_k: int, *, timeout_s: float = REPO_DOCS_TIMEOUT_S
) -> tuple[str, list[_SearchHit]]:
    """(docs namespace, hits) for `query` over the repository's documents;
    ("", []) when there is nothing the caller may read, or on any failure."""
    ns = await readable_docs_namespace(org, code_namespace)
    if not ns or not query.strip():
        return "", []
    try:
        from server.knowledge.app.knowledge_search import handle_knowledge_search

        out = await asyncio.wait_for(
            handle_knowledge_search(
                KnowledgeSearchInput(
                    org=org,
                    namespace=ns,
                    query=query,
                    top_k=max(1, min(int(top_k), 20)),
                    namespace_scope="this",
                )
            ),
            timeout=timeout_s,
        )
    except Exception as e:  # noqa: BLE001 — an additive leg never fails the caller
        log.info("repo docs search on %s/%s did not answer: %s", org, ns, e)
        return ns, []
    hits = list(out.results)
    for h in hits:
        if not h.namespace:
            h.namespace = ns
    return ns, hits


def merge_hits(base: list[_SearchHit], extra: list[_SearchHit], top_k: int) -> list[_SearchHit]:
    """One knowledge list: both legs' hits, one per (namespace, document,
    chunk), in the handler's own order (score, then similarity)."""
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
    return out[: max(1, int(top_k))]


async def knowledge_with_repo_docs(know_input: KnowledgeSearchInput) -> KnowledgeSearchOutput:
    """`loom_search`'s knowledge leg: the knowledge search, and — under a code
    scope — the scoped repository's documents, merged into the one section.
    Raises only what the base search raises."""
    from server.code.domain.scope import current_code_scope
    from server.knowledge.app.knowledge_search import handle_knowledge_search

    scope = current_code_scope()
    if not scope:
        return await handle_knowledge_search(know_input)
    org, code_ns = scope
    base, (_ns, extra) = await asyncio.gather(
        handle_knowledge_search(know_input),
        search_repo_docs(org, code_ns, str(know_input.query), int(know_input.top_k)),
    )
    if not extra:
        return base
    return base.model_copy(
        update={"results": merge_hits(list(base.results), extra, int(know_input.top_k))}
    )


class RepoDoc(TypedDict):
    source_uri: str
    title: str
    description: str


DOCS_SECTION_MAX_TOKENS = 300
DOCS_SECTION_MAX_DOCS = 12
_LIST_CACHE: dict[tuple[str, str, int], tuple[float, list[RepoDoc]]] = {}


async def list_repo_docs(
    org: str,
    code_namespace: str,
    *,
    limit: int = DOCS_SECTION_MAX_DOCS,
    timeout_s: float = REPO_DOCS_TIMEOUT_S,
) -> tuple[str, list[RepoDoc]]:
    """(docs namespace, [{source_uri, title, description}]) — the
    repository's live documents, newest first as `knowledge.list` orders
    them; ("", []) when there is nothing the caller may read, or on failure."""
    ns = await readable_docs_namespace(org, code_namespace)
    if not ns:
        return "", []
    # the gate ran for this caller above; the listing itself is the same for
    # every reader of the namespace, so it is cached briefly per namespace
    hit = _LIST_CACHE.get((org, ns, limit))
    if hit is not None and hit[0] > time.monotonic():
        return ns, list(hit[1])
    try:
        from server.knowledge.app.document_reads import handle_knowledge_list

        out = await asyncio.wait_for(
            handle_knowledge_list(
                KnowledgeListInput(
                    org=org,
                    namespace=ns,
                    limit=max(1, min(int(limit), 50)),
                    fields=["source_uri", "title", "description"],
                )
            ),
            timeout=timeout_s,
        )
    except Exception as e:  # noqa: BLE001 — additive
        log.info("repo docs listing on %s/%s did not answer: %s", org, ns, e)
        return ns, []
    docs = [
        RepoDoc(source_uri=r.source_uri, title=r.title, description=r.description) for r in out.rows
    ]
    _LIST_CACHE[(org, ns, limit)] = (
        time.monotonic() + settings.knowledge_repo_docs_list_ttl_s,
        docs,
    )
    return ns, list(docs)


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
