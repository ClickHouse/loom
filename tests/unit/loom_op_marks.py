"""What every rendered-text test shares: an op the listing leaves out is marked
"(via loom_op)" once and never inside a parenthesis, and the synthetic tools a
listing carries."""

from __future__ import annotations

import re

from server.transport.mcp_op import _MARK_TRAIL, OP_MENTION

# Every trail the renderer writes the marker after, as the renderer spells it.
_TRAIL = rf"(?:{_MARK_TRAIL})"
_MARKED = re.compile(rf"(?<![\w.])({OP_MENTION}){_TRAIL} \(via loom_op\)")


def marked(op: str) -> re.Pattern[str]:
    """A mention of ``op`` with its "(via loom_op)" marker."""
    return re.compile(rf"(?<![\w.]){re.escape(op)}{_TRAIL} \(via loom_op\)")


def assert_marked_once_outside_parentheses(text: str, context: object = "") -> None:
    for marker in re.finditer(re.escape("(via loom_op)"), text):
        # code spans do not count as parentheses
        before = re.sub(r"`[^`]*`", "", text[: marker.start()])
        assert before.count("(") == before.count(")"), (context, text)
    mentions = _MARKED.findall(text)
    assert len(mentions) == len(set(mentions)), (context, mentions)


def synthetic_descriptors(toolset: str) -> list[dict]:
    """The synthetic tools a listing on ``toolset`` carries, built as the listing builds them."""
    from server.transport import mcp_tools
    from server.transport.mcp_op import routes_writes

    return mcp_tools._synthetic_tool_descriptors(
        toolset, mcp_tools.listed_tool_names(toolset), writes=routes_writes(toolset)
    )
