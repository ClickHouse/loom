"""The calculation step of `knowledge.synthesize`: a model reads the task and
the schemas of the datasets the caller named, and either proposes a
`knowledge.query` plan or says what it would need to ask. The server validates
and runs the plan; the model never sees a database or writes SQL. Its result is
evidence the answer cites as `[knowledge.query#1]`, checked like any other
citation.

Column names, dataset names and the task are source and caller text: they
shape a plan the server checks against the schemas, never anything run as
given.

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
MAX_EVIDENCE_CELL = 200  # not a setting: what one prompt shows of a value or a dataset name
MAX_PROMPT_COLUMNS = 100  # not a setting: what one prompt shows of a schema
MAX_PROMPT_SCHEMA_CHARS = 24_000  # not a setting: what one prompt shows of the schemas
# A question to ask or a refusal's reason, as an abstention or a retry carries it.
MAX_REASON_CHARS = 1000  # not a setting: what one clarification holds

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
    'C is {"dataset": "<alias>", "column": "<name>"}, the name being the JSON string '
    "the schema lists, exactly; "
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
    room = MAX_PROMPT_SCHEMA_CHARS // max(1, len(schemas))
    described = "\n".join(_described(s, room) for s in schemas)
    why = cut(refused, MAX_REASON_CHARS)
    retry = f"\n\nYour last plan was refused: {why}\nPropose a corrected plan." if refused else ""
    return f"QUESTION: {task}\n\nDATASETS (alias: name; columns):\n{described}{retry}\n\nJSON:"


def _described(schema: Mapping[str, Any], room: int) -> str:
    """One dataset as the prompt lists it, on one line of at most `room` characters
    unless its first column alone is longer: its name, its first columns, each name
    whole, as many as MAX_PROMPT_COLUMNS and the room hold, and how many more. Names
    are JSON strings, so one holding a line break or a comma reads as one name and
    the plan can copy a column's back exactly."""
    cols = list(schema["columns"])
    name = json.dumps(cut(schema["name"], MAX_EVIDENCE_CELL), ensure_ascii=False)
    head = f"- {schema['alias']}: {name} ({schema['row_count']} rows); columns: "
    shown: list[str] = []
    used = len(head)
    for i, c in enumerate(cols[:MAX_PROMPT_COLUMNS]):
        entry = f"{', ' if shown else ''}{json.dumps(c['name'], ensure_ascii=False)} ({c['type']})"
        # The line ends with the count of columns not shown when this one is the last.
        rest = len(cols) - i - 1
        tail = f", and {rest} more not shown" if rest else ""
        if shown and used + len(entry) + len(tail) > room:
            break
        shown.append(entry)
        used += len(entry)
    more = f", and {len(cols) - len(shown)} more not shown" if len(cols) > len(shown) else ""
    return f"{head}{''.join(shown)}{more}"


def _cell(value: Any) -> str:
    """A value on one line of a row: a line break or `|` in source text would split
    the row or shift its columns, so each is escaped."""
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("|", "\\|")
    )


def cut(text: str, limit: int) -> str:
    """`text` to `limit` characters, ending `…` when it was cut."""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def read_proposal(text: str) -> Proposal:
    """The plan or the question in a model's reply: the first JSON object in it, so
    a reply wrapped in prose or a code fence, or with braces in a note after the
    object, still reads."""
    start = text.find("{")
    if start < 0:
        return Proposal(unreadable="the reply held no JSON object")
    obj: Any = None
    error = ""
    while start >= 0 and not isinstance(obj, dict):
        try:
            obj, _ = json.JSONDecoder().raw_decode(text, start)
        except json.JSONDecodeError as e:
            error = error or e.msg
        start = text.find("{", start + 1)
    if not isinstance(obj, dict):
        return Proposal(unreadable=f"the reply was not JSON: {error}")
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
    it may not cover first (no cut loses a warning), then the plan that ran (so the
    answer can tell what was counted; cut to a third of the room), the result's
    columns and as many of its first rows as fit."""
    ran = json.dumps(
        {k: v for k, v in plan.items() if k != "datasets" and v not in (None, [], {})},
        separators=(",", ":"),
        ensure_ascii=False,
    )
    lines = [
        # A null, such as the max of no rows, reads apart from an empty string.
        " | ".join("NULL" if v is None else cut(_cell(v), MAX_EVIDENCE_CELL) for v in r)
        for r in rows[:MAX_EVIDENCE_ROWS]
    ]
    # Each name cut like a value, and the line to a third of the room, so a wide
    # selection's heads leave its rows room.
    head = cut(
        " | ".join(f"{cut(_cell(c['name']), MAX_EVIDENCE_CELL)} ({c['type']})" for c in columns),
        max(1, max_chars // 3),
    )
    warnings = list(incomplete)
    if truncated:
        warnings.insert(0, f"more rows matched than the {len(rows)} the calculation returned")
    # The plan takes at most a third of the room, so a long `in` list leaves rows room.
    plan_text = cut(ran, max(1, max_chars // 3))

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
