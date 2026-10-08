"""How message turns are shown to the model: role, index, speaker name and
turn time in front of each turn's content.
"""

from __future__ import annotations

import re
from collections.abc import Mapping


def _format_messages(
    messages: list[dict[str, object]],
    *,
    include_indices: bool = False,
) -> str:
    """Turn the messages list into a single user-prompt string."""
    lines: list[str] = []
    for idx, m in enumerate(messages):
        role = str(m.get("role", "user"))
        content = str(m.get("content", "")).strip()
        if content:
            # A per-message timestamp is shown as
            # `[role @ YYYY-MM-DD HH:MM]` so the extractor can anchor
            # state-at-the-time facts to the turn.
            ts = _message_stamp(m)
            who = f"{idx}:{role}" if include_indices else role
            # A speaker name rides after the role: `[user Priya]`.
            name = _message_speaker(m)
            if name:
                who = f"{who} {name}"
            prefix = f"[{who} @ {ts}]" if ts else f"[{who}]"
            lines.append(f"{prefix} {content}")
    return "\n".join(lines)


def _message_speaker(m: Mapping[str, object]) -> str:
    """The `name` a message carries, stripped; '' when absent."""
    return str(m.get("name") or "").strip()


def _message_stamp(m: Mapping[str, object]) -> str:
    """`YYYY-MM-DD HH:MM` for a message carrying a `timestamp`, else ''."""
    raw = m.get("timestamp")
    if not raw:
        return ""
    txt = str(raw).strip().replace("T", " ")[:16]
    return txt if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$", txt) else ""


def _format_indexed_turns(messages: list[dict[str, object]], indices: list[int]) -> str:
    """The selected turns, each prefixed with its ORIGINAL index (so the
    sweep's source_message_indices point into the same list the first pass
    cited), the role, the speaker and the turn time."""
    lines: list[str] = []
    for idx in indices:
        if not 0 <= idx < len(messages):
            continue
        m = messages[idx]
        content = str(m.get("content", "") or "").strip()
        if not content:
            continue
        who = f"{idx}:{m.get('role', 'user')}"
        name = _message_speaker(m)
        if name:
            who = f"{who} {name}"
        ts = _message_stamp(m)
        prefix = f"[{who} @ {ts}]" if ts else f"[{who}]"
        lines.append(f"{prefix} {content}")
    return "\n".join(lines)
