"""The built-in record schema a decision is written under: its name and its fields."""

from __future__ import annotations

from typing import Any

SCHEMA_NAME = "decision"

# The built-in schema. category + scenario identify a decision: re-deciding the
# same scenario supersedes the earlier record (history keeps it), which is what
# "what is our current decision on X" needs; precedent search still sees every
# decision row because memory.search reads the memory plane, not the record.
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
