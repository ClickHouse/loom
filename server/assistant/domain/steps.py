"""What an assistant answer read, as steps a person follows: one line per tool call, from the
call's ledger row, in fixed words per op. Pure: the reads are in the app layer
(server/assistant/app/support.py and bridge.py). The steps are display only; the label is
support's (docs/architecture/assistant.md#steps)."""

from __future__ import annotations

from dataclasses import dataclass

from server.assistant.domain.support import CallRow, kept_rows

# Per op: what the step says once the call succeeded, and what it says when the call failed
# or was refused. Every op of the assistant toolset has a line
# (tests/unit/test_assistant_steps.py); a row of any other op (a derived row such as
# memory.ask.consistency) is no step.
STEP_TEXT: dict[str, tuple[str, str]] = {
    "namespace.get": ("Read the namespace", "Couldn't read the namespace"),
    "namespace.summary": ("Read the namespace summary", "Couldn't read the namespace summary"),
    "namespace.inventory": ("Read the inventory", "Couldn't read the inventory"),
    "memory.inspect": ("Read a subject's memories", "Couldn't read a subject's memories"),
    "knowledge.inspect": ("Read the documents", "Couldn't read the documents"),
    "namespace.tune": ("Drafted improvements", "Couldn't draft improvements"),
    "namespace.taxonomy": ("Read the tags", "Couldn't read the tags"),
    "namespace.audit": ("Read the audit", "Couldn't read the audit"),
    "namespace.validate": ("Validated the namespace", "Couldn't validate the namespace"),
    "namespace.guide_get": ("Read the guide", "Couldn't read the guide"),
    "namespace.guide_history": ("Read the guide's history", "Couldn't read the guide's history"),
    "namespace.list_categories": ("Read the categories", "Couldn't read the categories"),
    "mentor.brief": ("Read the mentor brief", "Couldn't read the mentor brief"),
    "mentor.guide": ("Read Loom's memory guide", "Couldn't read Loom's memory guide"),
    "help.search": ("Searched Loom's docs", "Couldn't search Loom's docs"),
    "help.get": ("Read a page of Loom's docs", "Couldn't read a page of Loom's docs"),
    "grants.list": ("Read the grants", "Couldn't read the grants"),
    "memory.search": ("Searched memories", "Couldn't search memories"),
    "memory.ask": ("Asked the memories", "Couldn't ask the memories"),
    "memory.get": ("Read a memory", "Couldn't read a memory"),
    "memory.list": ("Listed memories", "Couldn't list memories"),
    "memory.history": ("Read a memory's history", "Couldn't read a memory's history"),
    "memory.provenance": (
        "Read where memories came from",
        "Couldn't read where memories came from",
    ),
    "memory.aggregate": ("Counted memories", "Couldn't count memories"),
    "facts.traverse": ("Walked the graph", "Couldn't walk the graph"),
    "facts.path": ("Found a path in the graph", "Couldn't find a path in the graph"),
    "knowledge.search": ("Searched documents", "Couldn't search documents"),
    "knowledge.get": ("Read a document", "Couldn't read a document"),
    "knowledge.list": ("Listed documents", "Couldn't list documents"),
    "code.search": ("Searched code", "Couldn't search code"),
    "code.symbol": ("Looked up a symbol", "Couldn't look up a symbol"),
    "insights.catalog": ("Read the SQL catalog", "Couldn't read the SQL catalog"),
    "insights.query": ("Ran a SQL query", "Couldn't run a SQL query"),
}


@dataclass(frozen=True)
class Step:
    """One step: the op, its words, and whether the call succeeded."""

    op: str
    text: str
    ok: bool


def steps_of(rows: list[CallRow]) -> tuple[list[Step], bool]:
    """The steps of `rows` in their order, one per row of an op of the toolset, and whether
    the rows were cut (support.kept_rows: the label and the steps are judged on the same
    rows)."""
    steps: list[Step] = []
    kept, cut = kept_rows(rows)
    for r in kept:
        words = STEP_TEXT.get(r.operation)
        if words is None:
            continue
        done, failed = words
        steps.append(Step(r.operation, done if r.success else failed, r.success))
    return steps, cut
