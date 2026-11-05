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
# The result rows the answer sees; past them the evidence says how many more there are.
MAX_EVIDENCE_ROWS = 50  # not a setting: what one prompt shows of a result

PLAN_SYSTEM = (
    "You turn a question about tabular datasets into a calculation plan, or say "
    "what you would need to ask first. Answer with one JSON object only: "
    '{"plan": {...}} or {"ambiguous": "<the question to ask the user>"}. '
    "A plan has the fields where, joins, group_by, aggregates, select, order_by "
    "and limit of the knowledge.query operation; name each column as "
    '{"dataset": "<alias>", "column": "<name>"} exactly as the schema lists it. '
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
    described = "\n".join(
        f"- {s['alias']}: {s['name']} ({s['row_count']} rows); columns: "
        + ", ".join(f"{c['name']} ({c['type']})" for c in s["columns"])
        for s in schemas
    )
    retry = (
        f"\n\nYour last plan was refused: {refused}\nPropose a corrected plan." if refused else ""
    )
    return f"QUESTION: {task}\n\nDATASETS (alias: name; columns):\n{described}{retry}\n\nJSON:"


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


def evidence(n: int, columns: Sequence[Mapping[str, str]], rows: Sequence[Sequence[Any]], *,
             truncated: bool, incomplete: Sequence[str]) -> str:  # fmt: skip
    """A calculation's result as a note the answer reads and cites."""
    shown = rows[:MAX_EVIDENCE_ROWS]
    head = " | ".join(f"{c['name']} ({c['type']})" for c in columns)
    body = "\n".join(" | ".join("" if v is None else str(v) for v in r) for r in shown)
    notes = []
    if truncated or len(rows) > len(shown):
        notes.append(f"only the first {len(shown)} result rows are shown; more matched")
    notes += list(incomplete)
    caveat = "".join(f"\nNOTE: {x}" for x in notes)
    return (
        f"CALCULATION [{CALCULATION_SOURCE}#{n}] over every row of the datasets named "
        f"(cite it as [{CALCULATION_SOURCE}#{n}]):\n{head}\n{body}{caveat}"
    )
