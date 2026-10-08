"""code.outline, and the skeleton + member outline code.symbol serves for a large
container instead of its joined body.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from server.code.api.models_navigation import CodeOutlineInput, CodeOutlineOutput
from server.code.app.common import (
    index_as_of,
    new_trace_run_id,
    repo_relative,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
    tier_of,
)
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.resolve import _file_row, symbol_body
from server.code.domain.extract.lang_reference import REFERENCE_LANG
from server.code.domain.index.verdicts import is_abstain, outline_verdict
from server.code.store.index import CodeRepo
from server.core.errors import ValidationError

logger = logging.getLogger(__name__)


# A container whose span reaches this many lines is served as a skeleton + outline by
# code.symbol instead of a joined body (the skeleton chunk already elides method bodies,
# the outline adds the spans).
AUTO_OUTLINE_MIN_LINES = 200

# The container kinds that get that treatment: C++ class / struct plus the lane languages'
# containers (TypeScript interface / enum / const object / vitest suite / module, Rust
# trait / impl, Prisma model). Distinct from the callers' member fold set
# (`dispatch._MEMBER_FOLD_KINDS`), which answers a different question.
_OUTLINE_CONTAINER_KINDS = frozenset(
    {
        "class",
        "struct",
        "interface",
        "enum",
        "const",
        "suite",
        "module",
        "trait",
        "impl",
        "model",
    }
)


def _is_large_container(row: dict[str, Any]) -> bool:
    if str(row.get("kind") or "") not in _OUTLINE_CONTAINER_KINDS:
        return False
    lo, hi = int(row.get("start_line") or 0), int(row.get("end_line") or 0)
    return hi - lo + 1 >= AUTO_OUTLINE_MIN_LINES


_SCIP_PLACEHOLDER_SIG = re.compile(r"^\(scip:[0-9a-f]+\)$")


def outline_entry(row: dict[str, Any]) -> dict[str, Any]:
    """The wire shape of one outline row: what a reader needs to choose a
    span, nothing that needs the body. The compiler tier stores an overload
    hash as its signature (`(scip:49f6e7a0…)`); that is an identity, not a
    signature, and blanking it saves ~10 tokens on every such row."""
    sig = str(row.get("signature") or "")[:300]
    if _SCIP_PLACEHOLDER_SIG.match(sig):
        sig = ""
    return {
        "kind": str(row.get("kind") or "function"),
        "qualified_name": str(row.get("qualified_name") or ""),
        "signature": sig,
        "start_line": int(row.get("start_line") or 0),
        "end_line": int(row.get("end_line") or 0),
        "tier": str(row.get("tier") or ""),
    }


def budget_outline(
    rows: list[dict[str, Any]], token_budget: int
) -> tuple[list[dict[str, Any]], int, str]:
    """Whole-row fill in source order: an entry ships complete or not at
    all (a signature is never cut), the first entry that does not fit names
    its own code.symbol call. Returns (entries, tokens, next)."""
    from server.code.domain.units import estimate_json_tokens

    out: list[dict[str, Any]] = []
    used = 0
    for r in rows:
        e = outline_entry(r)
        cost = estimate_json_tokens(e)
        if used + cost > token_budget:
            return out, used, f"code.symbol name={e['qualified_name']}"
        out.append(e)
        used += cost
    return out, used, ""


async def _container_skeleton(
    repo: CodeRepo, org: str, ns: str, sym: dict[str, Any], token_budget: int
) -> tuple[str, list[dict[str, Any]], str]:
    """Skeleton chunk + member outline for a large class/struct. The
    skeleton is priced first (it is the unit's body); the outline fills the
    budget that remains, whole rows, and names `code.outline path=…` when
    members did not fit. Fail-soft to the joined body when the skeleton
    chunk or the outline read is unavailable, so a repo without the readers
    (tests) or a chunker version that emitted no skeleton still answers."""
    from server.code.domain.units import estimate_unit_tokens, unit_from_symbol

    path, qn = str(sym["path"]), str(sym["qualified_name"])
    lo, hi = int(sym.get("start_line") or 0), int(sym.get("end_line") or 0)
    try:
        skel, rows = await asyncio.gather(
            repo.class_skeleton_chunk(org, ns, path, qn),
            repo.outline_for_path(org, ns, path),
        )
    except Exception:  # noqa: BLE001 — additive: the joined body still ships
        logger.warning("code.symbol skeleton read failed for %s", qn)
        return await symbol_body(repo, org, ns, sym), [], ""
    body = str(skel["content"]) if skel else await symbol_body(repo, org, ns, sym)
    members = [
        r
        for r in rows
        if lo <= int(r.get("start_line") or 0) <= hi and str(r.get("qualified_name") or "") != qn
    ]
    spent = estimate_unit_tokens(unit_from_symbol(sym, body=body))
    entries, _tokens, nxt = budget_outline(members, max(0, token_budget - spent - 40))
    if nxt or (members and not entries):
        nxt = f"code.outline path={path}"
    return body, entries, nxt


async def handle_code_outline(input: CodeOutlineInput) -> CodeOutlineOutput:
    """One file's declarations with line spans, in source order, whole-row budgeted. The
    answer to "what is in this file?", which the plugin's read shunt redirects here
    instead of to a symbol lookup that needs a name the agent does not yet have. One
    symbol-table read (path bloom index) + as_of; no chunk read, no body."""
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    path = repo_relative(input.path)
    kinds = tuple(dict.fromkeys(k.strip() for k in input.kinds if k.strip()))
    if not path or not kinds:
        raise ValidationError("code.outline needs a repo-relative path and at least one kind")

    with code_span("outline.symbols", paths=1) as sp:
        rows = await repo.outline_for_path(org, ns, path, kinds=kinds)
        span_attrs(sp, rows=len(rows))
    with code_span("outline.budget"):
        entries, tokens, nxt = budget_outline(rows, input.token_budget)
    # An empty outline names why. One point read of the file row, only on the empty path,
    # splits "the index holds this file and it declares nothing of these kinds" (an
    # answer) from "the index does not hold this source file" / "its parse failed"
    # (abstains).
    verdict = ""
    head, head_truncated = "", False
    if not rows:
        file_row = await _file_row(repo, org, ns, path)
        verdict = outline_verdict(path=path, rows=rows, file_row=file_row)
        if file_row is not None and str(file_row.get("lang") or "") == REFERENCE_LANG:
            # A reference fixture declares nothing, but its head IS the answer: the
            # expected output an agent wants to read. One point read of the file's single
            # chunk.
            head, head_truncated = await _reference_head(repo, org, ns, path)
            verdict = "" if head else verdict
    abstain = is_abstain(verdict)
    risk = "abstain" if abstain else "low"
    if head:
        tokens += len(head) // 4

    took_ms = int((time.monotonic() - started) * 1000)
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.outline",
        query=path,
        route="outline",
        returned_ids=[str(r.get("symbol_id") or "") for r in rows[: len(entries)]],
        returned_paths=[path] if entries or head else [],
        returned_scores=[1.0] * len(entries),
        top_k=len(entries),
        tokens_returned=tokens,
        abstain=abstain,
        retrieval_risk=risk,
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
        verdict=verdict,
    )
    set_code_span_attributes(
        route="outline",
        tier=tier_of(entries),
        results=len(entries),
        tokens_returned=tokens,
        abstain=abstain,
        verdict=verdict,
        retrieval_risk=risk,
        consumer=input.consumer,
    )
    return CodeOutlineOutput(
        path=path,
        entries=entries,
        total=len(rows),
        returned=len(entries),
        elided=len(rows) - len(entries),
        next=nxt,
        indexed=bool(rows) or bool(head),
        verdict=verdict,
        tokens_returned=tokens,
        took_ms=took_ms,
        head=head,
        head_truncated=head_truncated,
        as_of=await index_as_of(repo, org, ns),
        trace_run_id=trace_run_id,
    )


async def _reference_head(repo: CodeRepo, org: str, ns: str, path: str) -> tuple[str, bool]:
    """(head, truncated) of a reference fixture's one chunk; ('', False) when the read
    fails or the row is not there, and the verdict then stands."""
    try:
        rows = await repo.chunks_of_path(org, ns, path, limit=1)
    except Exception as e:  # noqa: BLE001
        logger.debug("reference head read skipped: %s", e)
        return "", False
    if not rows:
        return "", False
    head = str(rows[0].get("content") or "")
    truncated = str(rows[0].get("context_description") or "").startswith("truncated:")
    return head, truncated
