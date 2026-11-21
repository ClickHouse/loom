"""Read-only, current-index evidence for prose that names no exact code file."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

from server.auth.authz import row_in_scope
from server.code.api.models_search import CodeSearchInput
from server.code.app.common import mirror_fetch_overdue
from server.code.app.index.pass_stream import SLICE_KEY
from server.code.app.index_status import published_clone_head
from server.code.app.search import handler as search
from server.code.domain.index.repos import repo_for_namespace
from server.code.domain.index.state import STAGE_KEY, IndexState
from server.code.domain.units import CodeUnit
from server.code.store.index import CodeRepo
from server.embedding.embedder import get_embedder
from server.memory.domain.decision_extract import governs
from server.operations.base import Role


@dataclass(frozen=True)
class CodeCandidate:
    path: str
    symbol: str
    start_line: int
    end_line: int
    excerpt: str


@dataclass
class CodeDiscovery:
    status: Literal["candidates", "abstained", "unavailable", "deferred"]
    namespace: str
    quote: str = ""
    note: str = ""
    as_of: dict[str, Any] = field(default_factory=dict)
    candidates: list[CodeCandidate] = field(default_factory=list)


async def discover_candidates(org: str, namespace: str, quote: str) -> CodeDiscovery:
    """Bound the entire read, including authorization, metadata and grounding.

    The caller supplies document quotations. Nothing here writes or verifies a link.
    """
    result = CodeDiscovery("unavailable", namespace, quote[:2000])
    try:
        async with asyncio.timeout(3):
            await _discover(org, result)
    except TimeoutError:
        result.status, result.note = "deferred", "code discovery deadline reached"
        result.candidates.clear()
    except Exception:  # noqa: BLE001 — failed discovery never changes exact links or writes
        result.status, result.note = "unavailable", "could not read the code index"
        result.candidates.clear()
    return result


async def _discover(org: str, result: CodeDiscovery) -> None:
    ns = result.namespace
    # The single-namespace handler assumes transport authorization; this call is in-process.
    if not org or not ns or not await row_in_scope(org, ns, Role.VIEWER):
        result.note = "code index access unavailable"
        return
    cfg = repo_for_namespace(org, ns)
    if cfg is None:
        result.note = "no repository index configured for this namespace"
        return
    repo = CodeRepo()
    state = await repo.all_state(org, ns)
    head = str((state.get("git_head") or {}).get("value") or "")
    if not head:
        result.note = "code index has no published commit"
        return
    if _settled_head(state, cfg.git_branch) != head:
        result.status, result.note = "deferred", "code index publication is incomplete"
        return
    model = get_embedder().model_name
    out = await search.handle_code_search(
        CodeSearchInput(
            org=org,
            namespace=ns,
            query=result.quote,
            top_k=3,
            token_budget=1200,
            legs="hybrid",
            bodies="none",
            fresh=True,
            expand=False,
            llm_legs="none",
            rerank="none",
            annotate=False,
            include_lessons=False,
            embedding_model=model,
            deadline_ms=1500,
            consumer="decision-preview",
        )
    )
    result.as_of = dict(out.as_of)
    if (
        out.partial
        or out.epistemic == "partial"
        or out.legs_timed_out
        or out.as_of.get("commit") != head
        or out.as_of.get("branch") != cfg.git_branch
        or out.as_of.get("status") != "fresh"
        or out.as_of.get("stale")
        or out.as_of.get("failing_tiers")
    ):
        result.status, result.note = "deferred", "retrieval or index freshness is incomplete"
        return
    if out.abstain:
        result.status = "abstained"
        result.note = out.abstain_reason or out.abstain_code or "search abstained"
        return
    units = [u for u in out.results[:3] if u.namespace in ("", ns) and governs(u.path)]
    rows = await repo.chunks_by_keys(org, ns, model, [(u.path, u.unit_id) for u in units])
    current = {(str(r["path"]), str(r["chunk_id"])): r for r in rows}
    for unit in units:
        if candidate := _ground(unit, current.get((unit.path, unit.unit_id))):
            result.candidates.append(candidate)
    if _settled_head(await repo.all_state(org, ns), cfg.git_branch) != head:
        result.candidates.clear()
        result.status, result.note = "deferred", "code index changed during discovery"
    elif result.candidates:
        result.status = "candidates"
    elif out.results:
        result.status, result.note = (
            "deferred",
            "retrieved candidates have no current code evidence",
        )
    else:
        result.status, result.note = "abstained", "no code candidates returned"


def _settled_head(state: IndexState, branch: str) -> str:
    """A completed index at its fetched branch tip, from uncached publication rows."""
    head = str((state.get("git_head") or {}).get("value") or "")
    clone = state.get("clone_head") or {}
    if (
        published_clone_head(state, branch) != head
        or (state.get(SLICE_KEY) or {}).get("value")
        or (state.get(STAGE_KEY) or {}).get("value", "") not in ("", "ready")
        or mirror_fetch_overdue(clone.get("updated_at"))
    ):
        return ""
    return head


def _ground(unit: CodeUnit, row: dict[str, Any] | None) -> CodeCandidate | None:
    if row is None or not row.get("content"):
        return None
    start, end = int(row["start_line"]), int(row["end_line"])
    symbol = str(row.get("scope") or "")
    if (start, end) != (unit.start_line, unit.end_line) or symbol != unit.symbol:
        return None
    # Excerpts come from live source chunks, not generated descriptions or search cards.
    excerpt = "\n".join(str(row["content"]).splitlines()[:16])[:1600]
    return CodeCandidate(unit.path, symbol, start, end, excerpt)
