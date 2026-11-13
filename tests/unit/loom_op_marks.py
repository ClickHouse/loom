"""The check every rendered text shares: an op the listing leaves out is marked
"(via loom_op)" once, and never inside a parenthesis."""

from __future__ import annotations

import re

# A marked mention, with every trail the renderer writes the marker after.
_MARKED = re.compile(r"([a-z_]+(?:\.[a-z_]+)+)(?:`(?:'s?)?|(?:'s)?) \(via loom_op\)")


def assert_marked_once_outside_parentheses(text: str, context: object = "") -> None:
    for marker in re.finditer(re.escape("(via loom_op)"), text):
        # code spans do not count as parentheses
        before = re.sub(r"`[^`]*`", "", text[: marker.start()])
        assert before.count("(") == before.count(")"), (context, text)
    marked = _MARKED.findall(text)
    assert len(marked) == len(set(marked)), (context, marked)
