"""code.trace: the frames of a stack trace or backtrace resolved to definitions."""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any

from server.code.api.models_navigation import CodeTraceInput, CodeTraceOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
    tier_of,
)
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.resolve import _innermost
from server.code.store.index import CodeRepo

# Frame shapes seen in real ClickHouse traces: gdb/LLDB (`#3 0x… in
# DB::Foo::bar(...)`), fatal-log backtraces (`3. DB::Foo::bar(...) @ 0x…`),
# sanitizer frames (`#2 0x… in DB::Foo::bar path.cpp:42`), plus bare
# `path.cpp:123` locations. Qualified names must contain `::` — a bare word
# in prose is not a frame.
# Group 1 is the name, read from the first letter of a word: a start inside
# a word would rescan it, so a long word costs one pass, not one per letter.
_TRACE_SYMBOL_RE = re.compile(
    r"(?<![\w~])[^\WA-Za-z_]*+((?:[A-Za-z_~][\w~]*+::)+[A-Za-z_~][\w~]*+)"
)


# At least one directory component required: a bare `raise.c:50` is a libc
# frame the index can never resolve — repo paths in real traces carry dirs.
# A path is read from the start of its first directory only, so a long
# path costs one pass, not one per directory.
_TRACE_FILELINE_RE = re.compile(
    r"(?<![\w.+-])(?<![\w.+-]/)((?:[\w.+-]+/)+[\w.+-]+\.(?:cpp|cc|cxx|c|h|hpp|hh|proto))[:(](\d+)"
)


def _parse_trace_frames(trace: str, max_frames: int) -> list[dict[str, Any]]:
    """Ordered, deduped frames: {'frame', 'kind': 'symbol'|'file'}. Trace
    order is preserved — it IS the diagnostic artifact."""
    frames: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in trace.splitlines():
        for m in _TRACE_SYMBOL_RE.finditer(line):
            name = m.group(1)
            # skip stdlib / abseil / boost frames: they resolve to nothing and pad the
            # budget.

            if name.startswith(("std::", "__", "absl::", "boost::")):
                continue
            if name not in seen:
                seen.add(name)
                frames.append({"frame": name, "kind": "symbol"})
        for m in _TRACE_FILELINE_RE.finditer(line):
            key = f"{m.group(1)}:{m.group(2)}"
            if key not in seen:
                seen.add(key)
                frames.append(
                    {"frame": key, "kind": "file", "path": m.group(1), "line": int(m.group(2))}
                )
        if len(frames) >= max_frames:
            break
    return frames[:max_frames]


async def handle_code_trace(input: CodeTraceInput) -> CodeTraceOutput:
    from server.code.domain.units import budget_units

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    frames = _parse_trace_frames(input.trace, input.max_frames)

    with code_span("trace.resolve_frames", frames=len(frames)) as sp:
        out_frames, units = await _resolve_frames(repo, org, ns, frames)
        span_attrs(sp, results=len(units))

    with code_span("trace.budget"):
        budgeted, tokens = budget_units(units, input.token_budget)

    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.trace",
        query=input.trace[:200],
        route="symbol_exact",
        returned_ids=[u.unit_id for u in budgeted],
        returned_paths=[u.path for u in budgeted],
        returned_scores=[1.0] * len(budgeted),
        top_k=len(budgeted),
        tokens_returned=tokens,
        abstain=not budgeted,
        retrieval_risk="low" if budgeted else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    set_code_span_attributes(
        route="symbol_exact",
        tier=tier_of(budgeted),
        results=len(budgeted),
        frames=len(frames),
        tokens_returned=tokens,
        abstain=not budgeted,
        retrieval_risk="low" if budgeted else "abstain",
        consumer=input.consumer,
    )
    return CodeTraceOutput(
        frames=out_frames,
        definitions=budgeted,
        tokens_returned=tokens,
        trace_run_id=trace_run_id,
    )


async def _resolve_frame(
    repo: CodeRepo, org: str, ns: str, f: dict[str, Any]
) -> dict[str, Any] | None:
    """The definition one frame names: a symbol frame by name, a bare `file:line` by the
    innermost symbol enclosing the line, compiler tier first. None when nothing matches."""
    if f["kind"] == "symbol":
        rows = await repo.symbols_by_name(org, ns, f["frame"], limit=5)
        if not rows and "::" in f["frame"]:
            # Index qualified names don't always carry the outermost
            # namespace (tier-dependent); retry with it stripped.
            rows = await repo.symbols_by_name(org, ns, f["frame"].split("::", 1)[1], limit=5)
        return rows[0] if rows else None
    # Trace paths are often build-absolute; anchor at the repo-relative tail the index knows.
    path = f["path"]
    if "src/" in path and not path.startswith("src/"):
        path = "src/" + path.split("src/", 1)[1]
    for tier in ("scip", "treesitter"):
        hit = _innermost(await repo.live_symbols_for_path(org, ns, path, tier), f["line"])
        if hit is not None:
            return hit
    return None


async def _resolve_frames(
    repo: CodeRepo, org: str, ns: str, frames: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[Any]]:
    """Each frame -> (wire entry, definition unit when it resolved). The frames resolve
    concurrently; frame order is preserved and a definition appears once even if several
    frames hit it."""
    from server.code.domain.units import unit_from_symbol

    rows = await asyncio.gather(*(_resolve_frame(repo, org, ns, f) for f in frames))
    out_frames: list[dict[str, Any]] = []
    units: list[Any] = []
    seen_defs: set[str] = set()
    for f, row in zip(frames, rows, strict=True):
        entry = {"frame": f["frame"], "resolved": row is not None}
        if row is not None:
            entry.update(
                qualified_name=str(row["qualified_name"]),
                path=str(row["path"]),
                line=int(row["start_line"] or 0),
                symbol_id=str(row["symbol_id"]),
            )
            if str(row["symbol_id"]) not in seen_defs:
                seen_defs.add(str(row["symbol_id"]))
                units.append(unit_from_symbol(row))
        out_frames.append(entry)
    return out_frames, units
