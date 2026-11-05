"""The calculation step of `knowledge.synthesize`: a model reads the task and
the schemas of the datasets the caller named, and either proposes a
`knowledge.query` plan or says what it would need to ask. The server validates
and runs the plan; the model never sees a database or writes SQL. Its result is
evidence the answer cites as `[knowledge.query#1]`, checked like any other
citation.

Column names and the task are source and caller text: they shape a plan the
server checks against the schemas, never anything run as given.

Pure and I/O-free.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# The source a calculation is cited by, as `[knowledge.query#<n>]`.
CALCULATION_SOURCE = "knowledge.query"
# What one prompt shows: the result rows and each value's characters (the evidence
# says when it cut), and each dataset's columns.
MAX_EVIDENCE_ROWS = 50  # not a setting: what one prompt shows of a result
MAX_EVIDENCE_CELL = 200  # not a setting: what one prompt shows of a value
# Each name whole (a cut name could not be planned over), at most MAX_COLUMN_CHARS.
MAX_PROMPT_COLUMNS = 100  # not a setting: what one prompt shows of a schema

PLAN_SYSTEM = (
    "You turn a question about tabular datasets into a calculation plan, or say "
    "what you would need to ask first. Answer with one JSON object only: "
    '{"plan": {...}} or {"ambiguous": "<the question to ask the user>"}. '
    "A plan has only these fields, each optional: "
    'where: [{"column": C, "op": "eq|ne|lt|le|gt|ge|in|not_in|is_null|not_null|contains|'
    'starts_with", "value": V} or with "values": [V, ...] for in and not_in]; '
    'joins: [{"left": C, "right": C}] (one per dataset past the first); '
    'group_by: [{"column": C, "bucket": "|day|month|year", "name": N}]; '
    'aggregates: [{"fn": "count|count_distinct|sum|avg|min|max", "column": C, "name": N}] '
    "(count of rows takes no column); select: [C, ...] for rows instead of aggregates; "
    'order_by: [{"by": N, "desc": true|false}]; limit: 1-1000. '
    'C is {"dataset": "<alias>", "column": "<name>"} exactly as the schema lists it; '
    "N names a result column. Example: "
    '{"plan": {"where": [{"column": {"dataset": "d", "column": "placed_at"}, "op": "ge", '
    '"value": "2026-03-01"}], "aggregates": [{"fn": "count", "name": "orders"}]}}. '
    "Join datasets only on columns the question makes clear belong together, never "
    "because their names look alike. If which column, which unit, which time "
    "period or how datasets relate is unclear, answer ambiguous instead of guessing."
)


@dataclass(frozen=True)
class Proposal:
    """A model's reply: a `plan` (the query fields, datasets excluded), or what it
    would `ask`, or neither when the reply was no such object (`unreadable`)."""

    plan: dict[str, Any] | None = None
    ask: str = ""
    unreadable: str = ""


def plan_prompt(task: str, schemas: Sequence[Mapping[str, Any]], refused: str = "") -> str:
    """The plan call's prompt: the task, each dataset's alias, columns and row
    count, and, on a second try, why the first plan was refused."""
    described = "\n".join(_described(s) for s in schemas)
    retry = (
        f"\n\nYour last plan was refused: {refused}\nPropose a corrected plan." if refused else ""
    )
    return f"QUESTION: {task}\n\nDATASETS (alias: name; columns):\n{described}{retry}\n\nJSON:"


def _described(schema: Mapping[str, Any]) -> str:
    """One dataset as the prompt lists it, its columns and their names bounded."""
    cols = list(schema["columns"])
    shown = ", ".join(f"{c['name']} ({c['type']})" for c in cols[:MAX_PROMPT_COLUMNS])
    more = (
        f", and {len(cols) - MAX_PROMPT_COLUMNS} more not shown"
        if len(cols) > MAX_PROMPT_COLUMNS
        else ""
    )
    head = f"- {schema['alias']}: {schema['name']} ({schema['row_count']} rows)"
    return f"{head}; columns: {shown}{more}"


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_proposal(text: str) -> Proposal:
    """The plan or the question in a model's reply; the first `{` to the last `}`
    is read, so a reply wrapped in prose or a code fence still reads."""
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return Proposal(unreadable="the reply held no JSON object")
    try:
        obj = json.loads(text[start : end + 1])
    except json.JSONDecodeError as e:
        return Proposal(unreadable=f"the reply was not JSON: {e.msg}")
    if not isinstance(obj, dict):
        return Proposal(unreadable="the reply was not a JSON object")
    if isinstance(obj.get("ambiguous"), str) and obj["ambiguous"].strip():
        return Proposal(ask=obj["ambiguous"].strip())
    plan = obj.get("plan")
    if not isinstance(plan, dict):
        return Proposal(unreadable='the reply had neither "plan" nor "ambiguous"')
    # The datasets are the caller's: a plan cannot add or swap one.
    return Proposal(
        plan={k: v for k, v in plan.items() if k not in ("datasets", "org", "namespace")}
    )


def evidence(
    n: int,
    plan: Mapping[str, Any],
    columns: Sequence[Mapping[str, str]],
    rows: Sequence[Sequence[Any]],
    *,
    truncated: bool,
    incomplete: Sequence[str],
    max_chars: int,
) -> str:
    """A calculation as a note the answer reads and cites, at most `max_chars`: what
    it may not cover and the plan that ran first (so the answer can tell what was
    counted, and no cut loses either), then the result's columns and as many of its
    first rows as fit."""
    ran = json.dumps(
        {k: v for k, v in plan.items() if k != "datasets" and v not in (None, [], {})},
        separators=(",", ":"),
    )
    lines = [
        " | ".join("" if v is None else _cut(str(v), MAX_EVIDENCE_CELL) for v in r)
        for r in rows[:MAX_EVIDENCE_ROWS]
    ]
    # Each name cut like a value, and the line to a third of the room, so a wide
    # selection's heads leave its rows room.
    head = _cut(
        " | ".join(f"{_cut(c['name'], MAX_EVIDENCE_CELL)} ({c['type']})" for c in columns),
        max(1, max_chars // 3),
    )
    warnings = list(incomplete)
    if truncated:
        warnings.insert(0, f"more rows matched than the {len(rows)} the calculation returned")
    # The plan takes at most a third of the room, so a long `in` list leaves rows room.
    plan_text = _cut(ran, max(1, max_chars // 3))

    def note(kept: int) -> str:
        # Which of the rows returned are shown: the first MAX_EVIDENCE_ROWS, or fewer to fit.
        shown = f"only the first {kept} of the {len(rows)} result rows are shown"
        caveat = "".join(f"\nNOTE: {x}" for x in warnings + ([shown] if kept < len(rows) else []))
        return (
            f"CALCULATION [{CALCULATION_SOURCE}#{n}] over every row of the datasets named "
            f"(cite it as [{CALCULATION_SOURCE}#{n}]){caveat}\nPLAN: {plan_text}\n{head}\n"
            + "\n".join(lines[:kept])
        )

    kept = len(lines)
    while kept and len(note(kept)) > max_chars:
        kept -= 1
    # The warnings are never cut; only a note too short for them is.
    return note(kept)[:max_chars]
