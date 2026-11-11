"""The plane a mentor hint names (`MentorHint.plane`, mentor_rules.known_plane) and the plane
names the dashboard maps to its pages, pinned in snapshots/mentor_planes.json (the dashboard's
own test, frontend/src/views/assistant/cards/planes.test.ts, reads it)."""

from __future__ import annotations

import ast
import json
import os
import re
from pathlib import Path

from server.insights.app import mentor_rules
from server.insights.app.mentor_rules import PLANES, known_plane

SNAPSHOT = Path(__file__).resolve().parent / "snapshots" / "mentor_planes.json"
NAMES = {plane for _, plane, _, _ in PLANES}
_OP = re.compile(r"^[a-z_]+(\.[a-z_]+)+$|^loom_[a-z_]+$")


def test_a_hint_names_the_plane_of_its_first_op_that_has_one() -> None:
    # The search_abstains hint leads with loom_search, which has no plane.
    assert mentor_rules.hint_plane(("loom_search", "namespace.audit", "memory.set")) == "namespace"
    assert mentor_rules.hint_plane(("loom_search", "memory.set")) == "claims"
    assert mentor_rules.hint_plane(("loom_search", "loom_activate")) is None
    assert mentor_rules.hint_plane(()) is None


def test_known_plane_names_a_plane_or_none() -> None:
    assert known_plane("loom_search") is None
    assert known_plane("facts.traverse") == "graph"
    # The records prefix comes before the memory one.
    assert known_plane("memory.record.x") == "records"
    assert known_plane("memory.search") == "claims"
    assert known_plane("") is None
    assert known_plane("nothing.here") is None


def _rule_op_literals() -> set[str]:
    """Every string in the rules' module shaped like an op name: what a hint's ops can hold."""
    tree = ast.parse(Path(mentor_rules.__file__).read_text(encoding="utf-8"))
    return {
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and _OP.match(n.value)
    }


# The coding-agent tools a hint can name that belong to no plane: their hints get no page.
_NO_PLANE = {"loom_activate", "loom_search"}


def test_every_op_a_rule_names_has_a_plane_unless_it_is_a_tool_of_none() -> None:
    ops = _rule_op_literals()
    # Positive control: the rules name ops across several planes.
    assert len({known_plane(op) for op in ops} - {None}) > 3
    missing = {op for op in ops if known_plane(op) is None}
    assert missing == _NO_PLANE, "a rule names an op of no plane: add its prefix to PLANES"


def test_the_plane_names_the_dashboard_maps_are_pinned() -> None:
    names = {"planes": sorted(NAMES)}
    if os.environ.get("LOOM_UPDATE_MENTOR_SNAPSHOT"):
        SNAPSHOT.write_text(json.dumps(names, indent=2) + "\n", encoding="utf-8")
    assert json.loads(SNAPSHOT.read_text(encoding="utf-8")) == names, (
        "the mentor's planes changed: map the new one in the dashboard "
        "(frontend/src/views/assistant/cards/planes.ts), then "
        "LOOM_UPDATE_MENTOR_SNAPSHOT=1 .venv/bin/python -m pytest tests/unit/test_mentor_planes.py"
    )
