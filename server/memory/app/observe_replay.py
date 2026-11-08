"""How one session's observations replay through memory.set_from_messages.

Each observation that carries a result becomes a compact synthetic `user`
turn, spoken by the agent at the observation's own time; the crystallize
worker (server/memory/workers/observe_extract.py) runs the turns through
extraction with the chunk layer suppressed.
"""

from __future__ import annotations

import re
from typing import Any

from server.memory.api.models_bulk import MemorySetFromMessagesInput, _Message

_PAYLOAD_LINE = re.compile(r"^(input|output):\s*(.*)$", re.S)
_KEPT_LINE = re.compile(r"^(\[[a-z_]+\]|error:|paths?:|files?:)", re.I)
# A payload survives the replay only as plain, short text — a shell command,
# a one-line result. JSON-shaped or long values are the tool's own words.
PAYLOAD_KEEP_CHARS = 240


def _payload_kept(value: str) -> str:
    """The payload text worth replaying: plain text under PAYLOAD_KEEP_CHARS,
    else "" (a JSON object/array, a body, a listing)."""
    v = value.strip()
    if not v or v[0] in "{[" or len(v) > PAYLOAD_KEEP_CHARS:
        return ""
    return v


def compact_narrative(narrative: str, paths: list[str] | None = None) -> str:
    """The replay text for a tool observation: the head
    (`[kind] tool=X`), any `error:` line, the file paths, and a payload only
    when it is short plain text (`input: pytest -q`, `output: 1 failed`).
    JSON-shaped or long payloads — an MCP tool's arguments, a search result,
    a file body — and their continuation lines are dropped: they are the
    tool's words, not the session's; replayed verbatim they would reach the
    extractor as 800-char JSON fragments and, chunked, outrank the facts."""
    kept: list[str] = []
    lines = str(narrative or "").splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = _PAYLOAD_LINE.match(line)
        if m:
            # A payload is judged by its whole block — the
            # first line AND its continuation lines. `cat CLAUDE.md` opens with
            # a short heading line; judged by that line alone, the rest of the
            # file would ride along and be crystallized into "facts".
            j = i + 1
            ends = (_PAYLOAD_LINE, _KEPT_LINE)
            while j < len(lines) and not any(r.match(lines[j]) for r in ends):
                j += 1
            block = "\n".join([m.group(2), *lines[i + 1 : j]])
            value = _payload_kept(block)
            if value:
                kept.append(f"{m.group(1)}: {value}")
            i = j
            continue
        if line.strip():
            kept.append(line.rstrip())
        i += 1
    shown = [str(p) for p in (paths or []) if str(p).strip()][:8]
    has_paths = any(k.lower().startswith(("paths:", "path:", "files:", "file:")) for k in kept)
    if shown and not has_paths:
        kept.append("paths: " + ", ".join(shown))
    return "\n".join(kept).strip()


def _observation_narrative(row: Any) -> str:
    """The replay text for one observation: the structured narrative,
    compacted to its head, error and paths; else the raw value string."""
    value = row.value
    if isinstance(value, dict):
        narrative = value.get("narrative")
        if isinstance(narrative, str) and narrative.strip():
            paths = value.get("file_paths") if isinstance(value.get("file_paths"), list) else None
            return compact_narrative(narrative, paths) or narrative.strip().splitlines()[0]
        # Reconstruct from the structured fields if narrative is absent.
        bits = []
        if value.get("kind"):
            bits.append(f"[{value['kind']}]")
        if value.get("tool_name"):
            bits.append(f"tool={value['tool_name']}")
        if value.get("error"):
            bits.append(f"error: {value['error']}")
        if bits:
            return " ".join(str(b) for b in bits)
    return str(value)[:2000]


def replay_messages(observations: list[Any], *, agent: str) -> list[Any]:
    """The synthetic turns one session's observations become:
    each a `user` turn whose speaker NAME is the agent (the subject is that
    name, never "User") and whose timestamp is the observation's own time (a
    state-at-the-time fact anchors to the step, not the session). With no
    named agent the turns are unnamed."""
    from server.memory.app.trajectory_notes import actor_name

    name = actor_name(agent)
    out: list[Any] = []
    for o in observations:
        if not replayable(o):
            continue
        ts = getattr(o, "created_at", None)
        out.append(
            _Message(
                role="user",
                name=name,
                content=_observation_narrative(o),
                timestamp=ts if hasattr(ts, "isoformat") else None,
            )
        )
    return out


# An observation replays only when its replay text carries a
# result — an `output:` / `result:` line or an error. Without one the
# extractor sees "[command_run] tool=Bash / paths: a.py" and can only narrate
# it ("Claude-Code ran a Bash command in the paths a.py"); a read, a listing
# or a bare edit is the same.
_RESULT_LINE = re.compile(
    r"^[^\S\n]*(?:output|result|stdout|stderr|error)\s*:", re.IGNORECASE | re.MULTILINE
)


def replayable(observation: Any) -> bool:
    """False for an observation whose replay could only narrate."""
    value = getattr(observation, "value", None)
    if isinstance(value, dict) and (value.get("error") or value.get("has_error")):
        return True
    return bool(_RESULT_LINE.search(_observation_narrative(observation)))


def replay_instructions() -> str:
    """The trajectory instructions the extractor runs under."""
    from server.memory.app.trajectory_notes import TRAJECTORY_EXTRACTION_INSTRUCTIONS

    return TRAJECTORY_EXTRACTION_INSTRUCTIONS


async def _extract_replay(session: dict[str, Any], msgs: list[Any], observation_date: str) -> int:
    """Run the replayed turns through extraction; 0 when there are none — a
    session of successful reads alone has nothing to extract."""
    if not msgs:
        return 0
    from server.memory.app.chunk_projection import suppress_chunk_layer
    from server.memory.app.extract.handler import handle_memory_set_from_messages

    extract_in = MemorySetFromMessagesInput(
        org=session["org"],
        namespace=session["namespace"],
        agent=session["agent"],
        user_id=session["user_id"],
        session_id=session["session_id"],
        messages=msgs,
        # Trace distilled memories back to their ambient-capture origin.
        extra_tags=["crystallized", "from_observation"],
        observation_date=observation_date,
        extraction_instructions=replay_instructions(),
    )
    # Facts, not verbatim chunks — the replayed lines are
    # already stored as the episodic observations themselves.
    with suppress_chunk_layer():
        out = await handle_memory_set_from_messages(extract_in)
    return len(out.written)
