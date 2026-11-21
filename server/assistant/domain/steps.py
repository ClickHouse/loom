"""What an assistant answer read, as steps a person follows: one line per tool receipt,
in fixed words per op. Pure: authenticated calls and receipts belong to the app layer.
The steps are display only; the label is
support's (docs/architecture/assistant.md#steps)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

# Each assistant operation has fixed display text for its successful receipt.
STEP_TEXT: dict[str, str] = {
    "namespace.get": "Read the namespace",
    "namespace.summary": "Read the namespace summary",
    "namespace.inventory": "Read the inventory",
    "memory.inspect": "Read a subject's memories",
    "knowledge.inspect": "Read the documents",
    "namespace.tune": "Drafted improvements",
    "namespace.taxonomy": "Read the tags",
    "namespace.audit": "Read the audit",
    "namespace.validate": "Validated the namespace",
    "namespace.guide_get": "Read the guide",
    "namespace.guide_history": "Read the guide's history",
    "namespace.list_categories": "Read the categories",
    "mentor.brief": "Read the mentor brief",
    "mentor.guide": "Read Loom's memory guide",
    "help.search": "Searched Loom's docs",
    "help.get": "Read a page of Loom's docs",
    "grants.list": "Read the grants",
    "memory.search": "Searched memories",
    "memory.ask": "Asked the memories",
    "memory.get": "Read a memory",
    "memory.list": "Listed memories",
    "memory.history": "Read a memory's history",
    "memory.provenance": "Read where memories came from",
    "memory.aggregate": "Counted memories",
    "facts.traverse": "Walked the graph",
    "facts.path": "Found a path in the graph",
    "knowledge.search": "Searched documents",
    "knowledge.get": "Read a document",
    "knowledge.list": "Listed documents",
    "code.search": "Searched code",
    "code.symbol": "Looked up a symbol",
    "insights.catalog": "Read the SQL catalog",
    "insights.query": "Ran a SQL query",
}


@dataclass(frozen=True)
class Step:
    """One successful tool step: its op, display text and wire success marker."""

    op: str
    text: str
    ok: bool


def steps_of(operations: Iterable[str]) -> list[Step]:
    """Display the successful tool operations in receipt order."""
    return [Step(op, STEP_TEXT[op], True) for op in operations if op in STEP_TEXT]
