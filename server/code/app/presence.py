"""Presence of a symbol or a pattern on several namespaces.

The release-containment question — "does symbol X exist on 26.4", "is this string on 26.8" — is one
verdict per release branch, not a ranked merge: the rank interleave code.search fans out with is the
wrong answer to it, and one single-namespace call per branch is eight round trips for one question.
`code.symbol` with `presence: true` and `code.grep` with `mode: "presence"` take a `namespaces`
list, authorize every entry first (fail closed, the fan-out gate), run the single-namespace op once
per namespace concurrently under the fan-out deadline, and return one row per namespace with a
verdict:

- `present`  — the namespace answered with a definition (symbol) or a
  matching file (grep); the row carries the first hit's path and span.
- `absent`   — the namespace answered EMPTY and has an index behind it (a
  git_head watermark in `as_of`): the thing is not in that index at that
  commit. Chunk coverage is a floor (code.verify's contract), so `absent`
  for grep is "no indexed chunk contains it".
- `unknown`  — the namespace did not answer: past the deadline, an error,
  no index watermark, or a `path_prefix` outside its indexed prefixes.
  `reason` says which. An unknown is never folded into absent.

The presence call writes no trace row of its own: each per-namespace call
goes through the ordinary handler and keeps its trace row and its memo.
Bodies and enrichment are not read for a symbol presence (the sub-call
asks for the definition rows only): the verdict is the answer.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Any

from server.code.api.models_grep import CodeGrepInput, CodeGrepOutput
from server.code.api.models_symbol import CodeSymbolInput, CodeSymbolOutput
from server.settings import settings

MAX_NAMESPACES = 8
VERDICTS = ("present", "absent", "unknown")

# ns -> (single-op output or None, as_of block, wall ms, error or None)
Answered = tuple[Any, dict[str, Any], int, BaseException | None]
RowOf = Callable[[str, Answered], dict[str, Any]]


# ---- the two public entry points --------------------------------------------


async def symbol_presence(
    inp: CodeSymbolInput, single: Callable[[CodeSymbolInput], Awaitable[CodeSymbolOutput]]
) -> CodeSymbolOutput:
    """code.symbol presence=true: the definition's presence per namespace."""
    from server.code.domain.units import estimate_json_tokens

    namespaces = _namespaces_of(inp)

    def sub_for(ns: str) -> CodeSymbolInput:
        return inp.model_copy(
            update={
                "namespace": ns,
                "namespaces": [],
                "presence": False,
                "include_body": False,
                "include_history": False,
                "include_examples": False,
                "include_lessons": False,
                "uses": False,
            }
        )

    rows = await run(inp, namespaces, sub_for, single, row_of=_symbol_row, op="code.symbol")
    summary = summarize(rows)
    _span(rows, summary, namespaces, inp.consumer)
    return CodeSymbolOutput(
        definitions=[],
        ambiguous=False,
        candidates=[],
        tokens_returned=estimate_json_tokens(rows) + estimate_json_tokens(summary),
        presence=rows,
        summary=summary,
    )


async def grep_presence(
    inp: CodeGrepInput, single: Callable[[CodeGrepInput], Awaitable[CodeGrepOutput]]
) -> CodeGrepOutput:
    """code.grep mode=presence: which namespaces contain the pattern. The
    per-namespace call is the files mode with limit 1 — `files_total` is
    counted before the limit, so the row has the count and the first path
    for the price of one grouped read, no line text."""
    namespaces = _namespaces_of(inp)

    def sub_for(ns: str) -> CodeGrepInput:
        return inp.model_copy(
            update={
                "namespace": ns,
                "namespaces": [],
                "mode": "files",
                "limit": 1,
            }
        )

    rows = await run(inp, namespaces, sub_for, single, row_of=_grep_row, op="code.grep")
    summary = summarize(rows)
    _span(rows, summary, namespaces, inp.consumer)
    return CodeGrepOutput(
        matches=[],
        files=[],
        files_total=0,
        chunks_matched=0,
        truncated=False,
        note=f"presence over {len(namespaces)} namespace(s); one row each, no line text",
        presence=rows,
        summary=summary,
    )


# ---- the runner ---------------------------------------------------------------


def _namespaces_of(inp: Any) -> list[str]:
    """The request's list, or the addressed namespace alone."""
    from server.code.app.common import resolve_code_identity

    if inp.namespaces:
        return list(inp.namespaces)
    return [resolve_code_identity(inp.org, inp.namespace)[1]]


def check_cap(op: str, namespaces: list[str]) -> None:
    from server.core.errors import ValidationError

    cap = max(1, min(MAX_NAMESPACES, int(settings.code_search_fanout_max_namespaces)))
    if len(namespaces) > cap:
        raise ValidationError(
            f"{op}: {len(namespaces)} namespaces exceed this deployment's fan-out cap of {cap}"
        )


async def run(
    inp: Any,
    namespaces: list[str],
    sub_for: Callable[[str], Any],
    single: Callable[[Any], Awaitable[Any]],
    *,
    row_of: RowOf,
    op: str,
) -> list[dict[str, Any]]:
    """Authorize every namespace (fail closed), then run the single op per
    namespace beside its as_of read, concurrently, under one deadline. A
    namespace still running at the deadline is cancelled and reported
    unknown(deadline); one that raised is unknown(error:<code>)."""
    from server.code.app import search_fanout as fanout
    from server.code.app.common import resolve_code_identity
    from server.code.store.index import CodeRepo

    org, _ = resolve_code_identity(inp.org, namespaces[0])
    await fanout.authorize(org, namespaces, op=op)
    check_cap(op, namespaces)
    timeout_ms = int(settings.code_search_fanout_timeout_ms)
    repo = CodeRepo()
    tasks = {
        ns: asyncio.ensure_future(_one(org, ns, repo, single(sub_for(ns)))) for ns in namespaces
    }
    try:
        _, pending = await asyncio.wait(tasks.values(), timeout=max(0.001, timeout_ms / 1000.0))
    except BaseException:
        # the caller was cancelled: `wait` leaves its tasks running
        for t in tasks.values():
            t.cancel()
        raise
    for t in pending:
        t.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    rows: list[dict[str, Any]] = []
    for ns in namespaces:
        t = tasks[ns]
        if t in pending:
            rows.append(_row(ns, "unknown", ms=timeout_ms, reason="deadline"))
        else:
            rows.append(row_of(ns, t.result()))
    return rows


async def _one(org: str, ns: str, repo: Any, call: Awaitable[Any]) -> Answered:
    """One namespace: the op and its provenance block side by side. The op's
    own error is caught here — a failing namespace is an unknown row, not a
    failed request — but a cancellation propagates so the deadline holds."""
    from server.code.app.common import index_as_of

    t0 = time.monotonic()
    out: Any = None
    err: BaseException | None = None
    try:
        out, as_of = await asyncio.gather(call, index_as_of(repo, org, ns))
    except asyncio.CancelledError:
        raise
    except Exception as e:  # noqa: BLE001 — per-namespace, disclosed as unknown
        err, as_of = e, {}
    return out, dict(as_of or {}), int((time.monotonic() - t0) * 1000), err


# ---- rows --------------------------------------------------------------------


def _row(
    ns: str,
    verdict: str,
    *,
    ms: int,
    reason: str = "",
    as_of: dict[str, Any] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    block = as_of or {}
    row: dict[str, Any] = {
        "namespace": ns,
        "verdict": verdict,
        "path": "",
        "start_line": 0,
        "end_line": 0,
        "tier": "",
        "indexed_commit": str(block.get("commit") or ""),
        "index_status": str(block.get("status") or "unknown"),
        "ms": ms,
        "reason": reason,
    }
    row.update(extra)
    return row


def _error_code(err: BaseException) -> str:
    return str(getattr(err, "code", "") or type(err).__name__)


def _empty_verdict(as_of: dict[str, Any]) -> tuple[str, str]:
    """An empty answer is `absent` only with an index behind it."""
    if as_of.get("commit"):
        return "absent", ""
    return "unknown", "no_index"


def _symbol_row(ns: str, answered: Answered) -> dict[str, Any]:
    out, as_of, ms, err = answered
    if err is not None:
        return _row(
            ns, "unknown", ms=ms, reason=f"error:{_error_code(err)}", as_of=as_of, definitions=0
        )
    defs = list(getattr(out, "definitions", []) or [])
    if not defs:
        verdict, reason = _empty_verdict(as_of)
        return _row(ns, verdict, ms=ms, reason=reason, as_of=as_of, definitions=0)
    first = defs[0]
    return _row(
        ns,
        "present",
        ms=ms,
        as_of=as_of,
        path=first.path,
        start_line=int(first.start_line),
        end_line=int(first.end_line),
        tier=str(first.tier or ""),
        definitions=len(defs),
    )


def _grep_row(ns: str, answered: Answered) -> dict[str, Any]:
    out, as_of, ms, err = answered
    if err is not None:
        return _row(
            ns, "unknown", ms=ms, reason=f"error:{_error_code(err)}", as_of=as_of, files_total=0
        )
    if getattr(out, "not_covered", False):
        return _row(ns, "unknown", ms=ms, reason="not_covered", as_of=as_of, files_total=0)
    total = int(getattr(out, "files_total", 0) or 0)
    files = list(getattr(out, "files", []) or [])
    if (total <= 0 or not files) and getattr(out, "partial", False):
        # the scan stopped at its deadline before it found a match: it never
        # read the rest of the index, so nothing says the code is absent
        cut = str(getattr(out, "partial_reason", "") or "deadline")
        return _row(ns, "unknown", ms=ms, reason=f"partial:{cut}", as_of=as_of, files_total=0)
    if total <= 0 or not files:
        verdict, reason = _empty_verdict(as_of)
        return _row(ns, verdict, ms=ms, reason=reason, as_of=as_of, files_total=0)
    first = files[0]
    line = int(first.get("first_line") or 0)
    return _row(
        ns,
        "present",
        ms=ms,
        as_of=as_of,
        path=str(first.get("path") or ""),
        start_line=line,
        end_line=line,
        files_total=total,
    )


def summarize(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    return {v: [r["namespace"] for r in rows if r["verdict"] == v] for v in VERDICTS}


def _span(
    rows: list[dict[str, Any]], summary: dict[str, list[str]], namespaces: list[str], consumer: str
) -> None:
    from server.code.app.spans import set_code_span_attributes

    set_code_span_attributes(
        route="presence",
        fanout=len(namespaces),
        fanout_dropped=sum(1 for r in rows if r["reason"] == "deadline"),
        results=len(summary["present"]),
        abstain=not summary["present"],
        retrieval_risk="low" if summary["present"] else "abstain",
        consumer=consumer,
    )
