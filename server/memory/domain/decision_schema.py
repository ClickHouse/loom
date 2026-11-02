"""The built-in record schema a decision is written under: its name and its fields."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

SCHEMA_NAME = "decision"

# The tags a decision's memory row carries: its marker, and after decision.outcome the
# outcome-known mark and its success or failure.
DECISION_TAG = "decision"
OUTCOME_KNOWN_TAG = "decision:outcome-known"
SUCCESS_TAG = "decision:success"
FAILURE_TAG = "decision:failure"
# The tag on a decision row whose source document no longer says what it says;
# memory.for_file prints it beside the row's pin marker.
SOURCE_CHANGED_TAG = "source-changed"


def is_decision(memory_type: str, tags: Collection[str]) -> bool:
    """A row typed `decision`, or tagged so."""
    return memory_type == SCHEMA_NAME or DECISION_TAG in tags


# category + scenario identify a decision: re-deciding the same scenario supersedes the
# earlier record (history keeps it), which is what "what is our current decision on X"
# needs; precedent search still sees every decision row because memory.search reads the
# memory plane, not the record.
SCHEMA_FIELDS: list[dict[str, Any]] = [
    {
        "name": "category",
        "type": "string",
        "required": True,
        "identity": True,
        "description": "Decision class, e.g. vendor_selection, deploy, triage.",
    },
    {
        "name": "scenario",
        "type": "string",
        "required": True,
        "identity": True,
        "description": "What was being decided, as the deciding agent framed it.",
    },
    {
        "name": "reasoning",
        "type": "string",
        "required": True,
        "description": "Why this outcome — the rationale as stated at decision time.",
    },
    {
        "name": "outcome",
        "type": "string",
        "required": True,
        "description": "The verdict: what was chosen or done.",
    },
    {
        "name": "alternatives",
        "type": "string",
        "description": "Options considered and not chosen, newline-separated.",
    },
    {
        "name": "confidence",
        "type": "number",
        "description": "Decider's confidence in the outcome, 0..1.",
    },
    {"name": "decision_maker", "type": "string", "description": "Agent or person who decided."},
    {
        "name": "run_id",
        "type": "string",
        "description": "The run (run.begin) in which the decision was made.",
    },
    {
        "name": "realized_outcome",
        "type": "string",
        "description": "What actually happened, written later by decision.outcome.",
    },
    {
        "name": "success",
        "type": "boolean",
        "description": "Whether the realized outcome matched the intent.",
    },
]
