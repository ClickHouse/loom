"""An MCP tool result carries the answer, not the ranking.

An operation's full output as `structuredContent` carries, for
`loom_search`, the per-hit score components, the reader's instruction
paragraph, the retrieval work and cost estimates, the plan string, and every
empty field of every hit: `loom_search` responses of p50 ~3.5k / p90 ~9k
tokens, about 7k tokens of JSON for a one-word answer. `structuredContent` is
meant for clients that want typed access, but Claude Code hands it to the
model too.

`lean(payload)` strips the diagnostic keys at every depth and drops empty
values (None, "", [], {}), leaving hits, their content, counts, paging,
abstain flags and confidence. `explain=true` on the call keeps everything.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from server.observability.consumer import ASSISTANT_CONSUMER

# Ranking and accounting detail the model never acts on. `explanation` is
# the per-hit provenance note the text block already folds into `why`.
# Kept on purpose: `version` (a write receipt clients read back — the MCP
# integration suite does), `trace_run_id` (the plugin's capture hook
# stashes it with the returned paths), paging and abstain fields.
DIAGNOSTIC_KEYS: frozenset[str] = frozenset(
    {
        "score_components",
        "explanation",
        "score_explanation",
        "calibration_explanation",
        "reader_instructions",
        "reader_block",  # duplicated by content.text's memory section
        "retrieval_work",
        "retrieval_cost_estimate",
        "retrieval_plan",
        "recipe",
        "route_agreement",
        "fallback_rounds_used",
        "fallback_budget_exhausted",
        "fallback_elapsed_ms",
        "rank_score_raw",
        "distance",
        "body_hash",
        "embedding_model",
        "chunk_share",
        "page_offset",
        "candidates_total",
        "reinforcement",
        "graph_seeds",
        "graph_neighbors_added",
        "triples_extracted_from_query",
        "triples_matched",
        "set_expansion_phrases",
        "top_k_ceiling",
        "degraded",
        "pending_extraction",
        "derived_aggregate",
        # Per-hit bookkeeping the reader never acts on (otherwise a memory
        # hit carries ~15 fields for a one-word answer).
        "temporal_confidence",
        "body_truncated",
        "verified",
        "about_other",
        "session_id",
        "lexical_score",
        "span_start",
        "span_end",
        "span_similarity",
        "siblings_folded",
        "chunk_idx",
        "section_path",
    }
)

# Keys whose empty value still carries meaning to a reader (a list that is
# empty says "none"), so they survive the empty-value drop.
KEEP_EMPTY: frozenset[str] = frozenset(
    {
        "results",
        "definitions",
        "candidates",
        "hits",
        "matches",
        "files",
        "memories",
        "runs",
        "repos",
    }
)


def _empty(v: Any) -> bool:
    return v is None or v == "" or v == [] or v == {}


def lean(payload: Any, *, top: bool = False) -> Any:
    """The trimmed copy of `payload`: diagnostic keys removed at every depth,
    empty values dropped (except the KEEP_EMPTY collections), order kept.
    At the `top` level an empty list survives whatever its key: `inbox.list`
    with no inboxes must not answer `{}`, or the model cannot tell "none" from
    "broken"."""
    if isinstance(payload, dict):
        out: dict[str, Any] = {}
        for k, v in payload.items():
            if k in DIAGNOSTIC_KEYS:
                continue
            lv = lean(v)
            if _empty(lv) and k not in KEEP_EMPTY and not (top and isinstance(v, list)):
                continue
            out[k] = lv
        return out
    if isinstance(payload, list):
        return [lean(x) for x in payload]
    return payload


def lean_result(payload: Any, arguments: dict[str, Any] | None) -> Any:
    """`payload` trimmed unless the caller asked to explain. A non-dict
    payload passes through unchanged."""
    if not isinstance(payload, dict) or _explained(arguments):
        return payload
    return lean(payload, top=True)


def _explained(arguments: dict[str, Any] | None) -> bool:
    """Whether the call keeps the full output: it asked to explain, or it came through the
    assistant toolset, whose answers cite the identities, versions and attestations the
    trimmed shape drops."""
    if not arguments:
        return False
    return bool(arguments.get("explain")) or arguments.get("consumer") == ASSISTANT_CONSUMER


# What an MCP client reads of a context.pack. The op's
# output carries the same text three times — `rendered`, `sections{}` and
# `blocks[].rendered` — plus per-section accounting (`section_meta`, the
# stable-prefix hash, fill ratios) that only a harness placing cache
# breakpoints acts on, and that harness calls /v1/context.pack (the plugin
# hooks do). Untrimmed, a pack of tokens_estimate ~1,000 costs ~3,600
# response tokens over MCP.
PACK_KEEP: tuple[str, ...] = ("rendered", "truncated")


def lean_pack(payload: Any) -> Any:
    """The MCP shape of a context.pack: the rendered bundle once, the
    citations of its blocks (deduplicated, in pack order), `truncated` when
    set, and the memory section's abstain / retrieval_risk verdict — the
    one piece of section accounting a reader acts on."""
    if not isinstance(payload, dict) or "rendered" not in payload:
        return payload
    out: dict[str, Any] = {k: payload[k] for k in PACK_KEEP if payload.get(k)}
    out.setdefault("rendered", payload.get("rendered", ""))
    cites: list[str] = []
    for b in payload.get("blocks") or []:
        for c in (b.get("citations") or []) if isinstance(b, dict) else []:
            if c not in cites:
                cites.append(c)
    if cites:
        out["citations"] = cites
    mem = (payload.get("section_meta") or {}).get("memories") or {}
    for k in ("abstain", "retrieval_risk"):
        if mem.get(k):
            out[k] = mem[k]
    return out


# A membership row's value when it says nothing (nearly every row is active,
# not inherited and private). The model reads what differs; the dashboard (HTTP) keeps
# the whole row.
_MEMBERSHIP_DEFAULTS: dict[str, Any] = {
    "status": "active",
    "inherited": False,
    "is_new": False,
    "visibility": "private",
    "kind": "memory",
    "pending_role": None,
    "pending_since": None,
    "pending_admins": [],
}
MEMBERSHIP_DESCRIPTION_CHARS = 80
_CODE_DESC_RE = re.compile(r"^Code index of (.+?)\s*\(read-only reference data\)\s*$")


def lean_memberships(payload: Any) -> Any:
    """namespace.list_mine rows without default-valued fields, descriptions
    clipped; the namespace and role always stay."""
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        return payload
    rows = []
    for r in payload["rows"]:
        if not isinstance(r, dict):
            rows.append(r)
            continue
        row = {
            k: v
            for k, v in r.items()
            if k not in _MEMBERSHIP_DEFAULTS or v != _MEMBERSHIP_DEFAULTS[k]
        }
        desc = row.get("description")
        if isinstance(desc, str):
            # the generated code-namespace text ("Code index of R @ B (read-only
            # reference data)") says only repo and branch
            m = _CODE_DESC_RE.match(desc)
            desc = m.group(1) if m else desc
            if len(desc) > MEMBERSHIP_DESCRIPTION_CHARS:
                desc = desc[:MEMBERSHIP_DESCRIPTION_CHARS].rstrip() + "…"
            row["description"] = desc
        if isinstance(row.get("joined_at"), str):
            row["joined_at"] = row["joined_at"][:10]  # the day is what "new here" needs
        rows.append(row)
    return {**payload, "rows": rows}


def _with_ignored_args(op_name: str, out: Any, arguments: dict[str, Any] | None) -> Any:
    """The keys the op did not read, on the structured
    result — Claude Code hands the model structuredContent only."""
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.arg_check import _note, attach, ignored_block
    from server.transport.epoch_nulls import scrub

    op = OPERATIONS_BY_NAME.get(op_name)
    if op is not None:
        out = scrub(op.output_model, out)  # a zero date is "never"
    block = ignored_block(op, arguments) if op is not None else {}
    if block:
        _note(op_name, block["ignored_args"])
    return attach(out, block)


def mcp_op_result(
    op_name: str, result: Any, arguments: dict[str, Any] | None, hint: tuple[str, str] | None
) -> Any:
    """The structured result an MCP `tools/call` returns for a registry op:
    the lean shape, a context.pack cut to its bundle, and
    the mentor hint as a key — unless the payload already carries it (a
    pack renders the same line in its `<mentor>` section)."""
    out = lean_result(result, arguments)
    if op_name == "context.pack":
        out = lean_pack(out)
    if op_name == "namespace.list_mine":
        out = lean_memberships(out)
    if op_name == "tracker.duplicate_check" and not _explained(arguments):
        out = lean_dupcheck(out)
    if op_name.startswith("code.") and not _explained(arguments):
        from server.transport.code_lean import lean_code

        out = lean_code(op_name, out)
    out = _with_ignored_args(op_name, out, arguments)
    if hint is not None and isinstance(out, dict):
        if hint[1] not in str(out.get("rendered") or ""):
            # A key, never a prose prefix: content[0].text stays parseable JSON.
            out = {**out, "hint": hint[1], "hint_code": hint[0]}
    return out


# What a duplicate check tells the model. About half of an untrimmed
# `loom_tracker_duplicate_check` answer is the fit's statistics, the
# thresholds, per-leg timings and top lists, per-namespace legs and each
# item's feature vector; a model's answer cites the verdict, the items and
# `nearest`, never those.
DUPCHECK_DROP: frozenset[str] = frozenset(
    # The rerank block is operator telemetry; its reading rides each
    # `nearest` row (`rerank: {label, confidence}`).
    {"fit", "fit_at", "thresholds", "query", "legs", "by_namespace", "rerank"}
)
DUPCHECK_ITEM_DROP: frozenset[str] = frozenset({"features", "relation_recorded"})


def lean_dupcheck(payload: Any) -> Any:
    """The verdict and what it rests on: items (without feature vectors),
    closest, nearest, needs, the namespaces read, epistemic and availability, and
    the degraded legs' reasons."""
    if not isinstance(payload, dict):
        return payload
    out = {k: v for k, v in payload.items() if k not in DUPCHECK_DROP}
    # only the legs that dropped: which one and why
    legs = out.pop("leg_status", None)
    down = [
        {k: r.get(k) for k in ("name", "namespace", "reason", "message", "retried") if r.get(k)}
        for r in legs or []
        if isinstance(r, dict) and r.get("status") == "degraded"
    ]
    if down:
        out["leg_status"] = down
    for key in ("items", "closest"):
        rows = out.get(key)
        if isinstance(rows, list):
            out[key] = [
                {k: v for k, v in r.items() if k not in DUPCHECK_ITEM_DROP}
                if isinstance(r, dict)
                else r
                for r in rows
            ]
    return out


# Fields a memory hit ships that the model never acts on in a loom_search
# answer (~90 tokens per hit). `rank_score` stays (the order's reason), as do
# `memory_type`, dates that say WHEN the fact holds, and the key and excerpt
# (the answer).
SEARCH_HIT_DROP: frozenset[str] = frozenset({"similarity", "status", "source_class", "version"})


def _explained_search(payload: dict[str, Any], why: Callable[[dict[str, Any]], str]) -> Any:
    """`explain=true` is the lean shape plus one `why` per
    memory hit (the provenance note and the signed score components, the
    same string the text block prints), not the whole debug payload: the
    ~700-token reader paragraph, the reader block that repeats the hits and
    the retrieval estimates would be 4x the lean answer for a client that
    reads `structuredContent` only (Claude Code)."""
    mem = payload.get("memory")
    hits = mem.get("results") if isinstance(mem, dict) else None
    notes = [why(h) if isinstance(h, dict) else "" for h in (hits or [])]
    out = lean(payload, top=True)
    lean_hits = (out.get("memory") or {}).get("results") or []
    for h, note in zip(lean_hits, notes, strict=False):
        if note and isinstance(h, dict):
            h["why"] = note
    return out


# What a knowledge hit carries into loom_search. `score` is
# the fusion score the order already encodes, `source` is always "knowledge"
# in the knowledge section, and `tags` are ingestion metadata (~8% of the
# section's tokens, read by no consumer); `bodies="full"`
# / `"unit"` keep the tags, `"none"` drops the chunk text too. The chunk text
# itself stays whole by default: a 300-char window lost the answer in 30% of
# relevant hits, and the best window policy saved less than the follow-up
# knowledge.get it forced would cost.
KNOWLEDGE_HIT_DROP: tuple[str, ...] = ("score", "source")


def lean_knowledge_hits(section: Any, bodies: Any, home_ns: str = "") -> None:
    """Trim loom_search's knowledge hits in place."""
    if not isinstance(section, dict):
        return
    mode = str(bodies or "excerpt")
    for h in section.get("results") or []:
        if not isinstance(h, dict):
            continue
        for k in KNOWLEDGE_HIT_DROP:
            h.pop(k, None)
        if mode not in ("full", "unit"):
            h.pop("tags", None)
        if mode == "none":
            h.pop("content", None)
        if home_ns and str(h.get("namespace") or "") == home_ns:
            h.pop("namespace", None)


def lean_conversation_hits(section: Any, bodies: Any, home_ns: str = "") -> None:
    """Trim loom_search's conversation hits in place: the
    per-turn `msg_id` (the `thread_id` opens the thread; 13% of the section's
    tokens), `ts` to the minute, the home namespace. `bodies="full"` /
    `"unit"` keep the msg_id."""
    if not isinstance(section, dict):
        return
    keep_ids = str(bodies or "excerpt") in ("full", "unit")
    for h in section.get("results") or []:
        if not isinstance(h, dict):
            continue
        if not keep_ids:
            h.pop("msg_id", None)
        ts = h.get("ts")
        if isinstance(ts, str) and len(ts) > 16:
            h["ts"] = ts[:16]
        if home_ns and str(h.get("namespace") or "") == home_ns:
            h.pop("namespace", None)


def lean_search_result(
    payload: Any,
    arguments: dict[str, Any] | None,
    home_ns: str = "",
    why: Callable[[dict[str, Any]], str] | None = None,
) -> Any:
    """`lean_result` for `loom_search`: the structured memory hits drop
    `similarity` (rank_score carries the order), `status` (always active on a
    served row), `source_class`, `version` (a write receipt, not a read),
    `created_at` when the row carries a `valid_at` or `temporal_anchor`, and
    `namespace` when it is the caller's home namespace; the memory section
    drops its zero counters. `explain=true` adds a `why` per hit to that shape
    when `why` is given."""
    explain = bool(arguments and arguments.get("explain"))
    if explain and why is not None and isinstance(payload, dict):
        out: Any = _explained_search(payload, why)
    else:
        out = lean_result(payload, arguments)
    if out is payload or not isinstance(out, dict):
        return out
    bodies = (arguments or {}).get("bodies")
    lean_knowledge_hits(out.get("knowledge"), bodies, home_ns)
    lean_conversation_hits(out.get("conversations"), bodies, home_ns)
    mem = out.get("memory")
    if not isinstance(mem, dict):
        return out
    for h in mem.get("results") or []:
        if not isinstance(h, dict):
            continue
        for k in SEARCH_HIT_DROP:
            h.pop(k, None)
        if h.get("valid_at") or h.get("temporal_anchor"):
            h.pop("created_at", None)
        if home_ns and str(h.get("namespace") or "") == home_ns:
            h.pop("namespace", None)
    for k in ("bodies_returned", "bodies_elided", "tokens_returned", "scratchpad_blocks"):
        if k in mem and not mem[k]:
            mem.pop(k, None)
    return out


# Clients whose model reads the whole CallToolResult — Codex calls MCP tools
# from its code-mode script and prints the result object, so the model reads
# `content.text` AND `structuredContent`, paying for the answer twice. For
# these the text block is a pointer and the typed half carries the answer (the
# half Claude Code already reads alone). Errors keep both: they are short and a
# client may surface only the text.
SINGLE_CHANNEL_AGENTS: frozenset[str] = frozenset({"codex"})
STRUCTURED_POINTER = "Result in structuredContent."


def single_channel(resp: dict[str, Any], agent: str) -> dict[str, Any]:
    """Drop the duplicate text block of a `tools/call` answer for a client
    that reads the whole result (`SINGLE_CHANNEL_AGENTS`)."""
    if agent not in SINGLE_CHANNEL_AGENTS:
        return resp
    result = resp.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return resp
    if not result.get("structuredContent") or not isinstance(result.get("content"), list):
        return resp
    if any(c.get("type") != "text" for c in result["content"] if isinstance(c, dict)):
        return resp  # images / resources ride only in content
    return {**resp, "result": {**result, "content": [{"type": "text", "text": STRUCTURED_POINTER}]}}
