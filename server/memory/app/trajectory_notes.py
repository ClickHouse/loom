"""What a session of tool calls teaches: transition events and two notes.

An agent learns from its own trajectories through three pools: raw state
slices (what a page or a command output looked like), state-transition events
(what changed after an action) and per-trajectory notes (the reusable procedure
and the durable pitfalls). Each of the last two carries its own class of
questions: events the static, dynamic and gotcha ones, notes the workflow ones.

Loom's ambient capture already holds the raw slices: one episodic row per
tool call, session-ordered (`observe.record`). The distiller
(`observe_extract`) replays them through fact extraction. This module adds
the other two pools at the same moment, when a session is distilled:

* **events** — one memory per adjacent pair of observations, "after
  <action>, <what changed>", written as a searchable `semantic` memory
  tagged `event` and dated at the later observation, with a `precedes`
  edge to the previous event of the session (the digest chain's verb) so
  the graph leg can walk a session in order;
* **notes** — one call per session that yields a procedure (name,
  situation, steps → `procedure.add`, the procedure plane) and a hint
  (durable environment facts and pitfalls → a `semantic` memory tagged
  `hint`).

Everything is bounded (events per session, characters per prompt), every
write is fail-open except for a call the switch holds (`customer.HOLDS`),
which propagates so the caller leaves the session to distil again, and the
pure parts (pairing, parsing, rendering) are testable without a model or a
database.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now
from server.llm import customer
from server.memory.api.models_crud import MemoryDeleteInput, MemorySetInput
from server.memory.domain.extraction_policy import with_policy
from server.memory.domain.transcript_rows import EVENT_SOURCE_TRACE, EVENT_TAG, FROM_OBSERVATION_TAG
from server.observability.genai import llm_stage

log = logging.getLogger(__name__)

# A replayed observation is the agent's action, not the
# user's words. The worker replays each observation as a `[user <agent>]`
# turn (a named speaker: the subject is that name, never "User") stamped with
# the observation's time, under these instructions. Without them the distilled
# facts read "User ran …" for actions the agent performed.
TRAJECTORY_EXTRACTION_INSTRUCTIONS = (
    "These turns are not a conversation. Each turn is ONE tool action performed by the "
    "coding agent whose name follows `user` in the turn label, inside a software session: "
    "the tool it called, the input it gave and what the tool returned. Attribute every "
    'action, edit, command and result to that agent BY NAME, never to "User" — the person '
    "operating the session did not run these commands. Tool output is evidence of what "
    "happened (a test failed, a file changed, a command errored, a query returned rows): "
    "extract the OUTCOME as a fact about the work — which file, which command, which "
    "error, what fixed it, what was verified — with the agent as subject. Prefer what an "
    "engineer would want later: what was tried, what failed and why, what worked, where "
    "things live. Do not extract facts about the operator's personal life, schedule or "
    "health from tool inputs; a memory-tool call's payload is not a fact about anyone."
)


def actor_name(agent: str) -> str:
    """The speaker name a replayed observation carries: the agent axis when
    it names one, "" for the wildcard/unspecified agent (then the turn stays
    an unnamed `[user]` turn)."""
    a = (agent or "").strip()
    return "" if a in ("", "*", "-") else a[:80]


HINT_TAG = "hint"
HINT_SOURCE_TRACE = "observe.extract:hint"
CHAIN_RELATION = "precedes"
CHAIN_RUN_ID = "trajectory_events"


_MAX_NARRATIVE_CHARS = 1200
_MAX_SESSION_CHARS = 12000
_TRIVIAL_RE = re.compile(r"^\[[a-z_]+\]\s+tool=\S+\s*$")

_EVENT_SYSTEM = (
    "You turn one transition in an agent's tool-call trajectory into a short, "
    "retrieval-ready event. You get the observation before an action, the action "
    "itself and the observation after it. Reply with exactly one JSON object: "
    '{"title": "...", "transition": "..."}. The title is at most 14 words and '
    "names the action with its specifics — the command run, the file and what "
    "changed in it, the query made — never a generic verb alone. The transition "
    "states what changed after the action — a "
    "new file or page, revealed content, changed values, a confirmation, an error, a "
    "blocker, or that nothing visible changed. Preserve exact names, paths, labels "
    "and error text. Only what the evidence shows; never invent."
)

_NOTES_SYSTEM = (
    "You convert one agent session — an ordered list of tool calls with their "
    "inputs, outputs and errors — into two reusable notes for a future agent in the "
    "same environment. Reply with exactly one JSON object: "
    '{"procedure": {"name": "...", "situation": "...", "steps": ["...", "..."]}, '
    '"hint": {"title": "...", "content": "- ...\\n- ..."}}. '
    "The procedure is the reliable core workflow the session demonstrates: a name, "
    "the situation it applies to, and 2 to 12 concrete steps; use null when the "
    "session shows no repeatable workflow. The hint records durable facts about "
    "this environment and its pitfalls: paths and commands that worked, controls "
    "or options that exist or do not, errors and what resolved them, distinctions "
    "between easily confused things; use null when there is nothing durable. Only "
    "what the evidence shows; never invent files, commands, options or outcomes."
)


@dataclass(frozen=True)
class Observation:
    """The part of an episodic row the notes need."""

    memory_key: str
    kind: str
    tool_name: str
    narrative: str
    error: str
    file_paths: tuple[str, ...]
    created_at: datetime | None
    valid_at: datetime | None

    @property
    def trivial(self) -> bool:
        """A narrative that is only the tool name says
        nothing an event or a note could use."""
        n = (self.narrative or "").strip()
        return not n or bool(_TRIVIAL_RE.match(n))


def observation_from_row(row: Any) -> Observation:
    value = getattr(row, "value", None)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            value = {"narrative": value}
    value = value if isinstance(value, dict) else {}
    return Observation(
        memory_key=str(getattr(row, "memory_key", "") or ""),
        kind=str(value.get("kind") or ""),
        tool_name=str(value.get("tool_name") or ""),
        narrative=str(value.get("narrative") or "")[:_MAX_NARRATIVE_CHARS],
        error=str(value.get("error") or ""),
        file_paths=tuple(str(p) for p in (value.get("file_paths") or []) if p),
        created_at=getattr(row, "created_at", None),
        valid_at=getattr(row, "valid_at", None),
    )


def transition_pairs(
    observations: list[Observation],
    *,
    max_pairs: int,
    only_after: set[str] | None = None,
) -> list[tuple[Observation, Observation]]:
    """Adjacent (before, after) pairs in session order, skipping pairs whose
    later observation is trivial (nothing changed that we can see) and
    keeping at most `max_pairs`, errors first so a failing session's
    gotchas survive the cap. With `only_after`, only pairs
    whose AFTER side is one of those keys — the observations this pass is
    distilling — while the BEFORE side may be an earlier, already-distilled
    observation, so the transition across a crystallize boundary is kept."""
    ordered = sorted(
        observations, key=lambda o: (o.created_at is None, o.created_at or datetime.min)
    )
    pairs = [(a, b) for a, b in zip(ordered, ordered[1:], strict=False) if not b.trivial]
    if only_after is not None:
        pairs = [(a, b) for a, b in pairs if b.memory_key in only_after]
    if max_pairs <= 0:
        return []
    if len(pairs) <= max_pairs:
        return pairs
    with_error = [p for p in pairs if p[1].error]
    without = [p for p in pairs if not p[1].error]
    return (with_error + without)[:max_pairs]


def event_prompt(before: Observation, after: Observation) -> str:
    return (
        f"BEFORE (state after the previous call): {before.narrative or '(nothing recorded)'}\n\n"
        f"ACTION: tool={after.tool_name} kind={after.kind}"
        + (f" files={', '.join(after.file_paths)}" if after.file_paths else "")
        + "\n\n"
        f"AFTER (what this call returned): {after.narrative}"
        + (f"\nERROR: {after.error}" if after.error else "")
    )


def _json_object(text: str) -> dict[str, Any] | None:
    s = text or ""
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(s[start : end + 1])
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_event(text: str) -> tuple[str, str] | None:
    """(title, transition) from the model's reply; None when either is missing."""
    obj = _json_object(text)
    if not obj:
        return None
    title = " ".join(str(obj.get("title") or "").split())[:160]
    transition = " ".join(str(obj.get("transition") or "").split())[:1500]
    if not title or not transition:
        return None
    return title, transition


_INPUT_LINE_RE = re.compile(r"(?m)^input:\s*(.+)$")


def action_summary(after: Observation, *, limit: int = 120) -> str:
    """The action's own words — the `input:` line of the observation's
    narrative (the command, the edit's file and strings, the query) — so an
    event says WHICH edit or command it followed, not just the tool. A
    reader ordering two edits of one file needs the difference."""
    m = _INPUT_LINE_RE.search(after.narrative or "")
    if not m:
        return ""
    text = _action_words(m.group(1))
    return text if len(text) <= limit else text[: limit - 1] + "…"


# The input keys worth reading, in the order a reader wants them: what was
# run, which file, what changed. Anything else in the JSON is noise here.
_ACTION_KEYS = (
    "command",
    "cmd",
    "file_path",
    "path",
    "pattern",
    "query",
    "url",
    "old_string",
    "new_string",
)


def _action_words(raw: str) -> str:
    """A JSON tool input rendered as `key=value` words (old→new for an edit);
    anything that is not a JSON object is returned as its own collapsed text."""
    text = " ".join((raw or "").split())
    try:
        obj = json.loads(text)
    except ValueError:
        return text
    if not isinstance(obj, dict):
        return text
    parts: list[str] = []
    if "old_string" in obj or "new_string" in obj:
        old, new = str(obj.get("old_string") or ""), str(obj.get("new_string") or "")
        for k in ("file_path", "path"):
            if obj.get(k):
                parts.append(f"{k}={obj[k]}")
                break
        parts.append(f"{old[:60]!r} → {new[:60]!r}")
        return " ".join(parts)
    for k in _ACTION_KEYS:
        if k in obj and obj[k] not in (None, "", [], {}):
            v = obj[k]
            parts.append(f"{k}={v if isinstance(v, str) else json.dumps(v)}")
    return " ".join(parts) if parts else text


def render_event(
    title: str,
    transition: str,
    after: Observation,
    *,
    step: int = 0,
    total: int = 0,
) -> str:
    """`Step k/n · After <tool> `<action>`: <title>. <transition>` — the step
    index orders the session's events for a reader that sees several, the
    action names which call this was."""
    head = f"After {after.tool_name}" if after.tool_name else "After the action"
    action = action_summary(after)
    if action:
        head = f"{head} `{action}`"
    prefix = f"Step {step}/{total} · " if step and total else ""
    return f"{prefix}{head}: {title}. {transition}"


def event_memory_key(session_id: str, after_key: str) -> str:
    digest = hashlib.sha1(f"{session_id}|{after_key}".encode()).hexdigest()[:16]
    return f"event:{digest}"


def notes_prompt(observations: list[Observation]) -> str:
    lines: list[str] = []
    total = 0
    for i, o in enumerate(observations, 1):
        line = f"{i}. [{o.kind}] tool={o.tool_name}"
        if o.file_paths:
            line += f" files={', '.join(o.file_paths[:6])}"
        if o.narrative and not o.trivial:
            line += f"\n   {o.narrative}"
        if o.error:
            line += f"\n   ERROR: {o.error}"
        if total + len(line) > _MAX_SESSION_CHARS:
            lines.append(f"... ({len(observations) - i + 1} more calls omitted)")
            break
        lines.append(line)
        total += len(line)
    return "Session tool calls, in order:\n\n" + "\n".join(lines)


@dataclass
class Notes:
    procedure: dict[str, Any] | None = None
    hint: dict[str, str] | None = None


def parse_notes(text: str) -> Notes:
    obj = _json_object(text)
    out = Notes()
    if not obj:
        return out
    proc = obj.get("procedure")
    if isinstance(proc, dict):
        name = " ".join(str(proc.get("name") or "").split())[:128]
        situation = " ".join(str(proc.get("situation") or "").split())[:2048]
        steps = [
            " ".join(str(s).split())[:512] for s in (proc.get("steps") or []) if str(s).strip()
        ]
        if name and situation and len(steps) >= 2:
            out.procedure = {"name": name, "situation": situation, "steps": steps[:64]}
    hint = obj.get("hint")
    if isinstance(hint, dict):
        title = " ".join(str(hint.get("title") or "").split())[:160]
        content = str(hint.get("content") or "").strip()[:4000]
        if title and content:
            out.hint = {"title": title, "content": content}
    return out


_MAX_HINT_ENTITIES = 12


def hint_entities_for(observations: list[Observation]) -> list[str]:
    """The distinct file paths a session touched, first-seen order, bounded —
    the entities a hint note carries so the entity-gated fact leg can find it
    when a question names one of them."""
    out: list[str] = []
    seen: set[str] = set()
    for o in observations:
        for pth in o.file_paths:
            if pth and pth not in seen:
                seen.add(pth)
                out.append(pth)
                if len(out) >= _MAX_HINT_ENTITIES:
                    return out
    return out


def hint_memory_key(session_id: str) -> str:
    return f"hint:{hashlib.sha1(session_id.encode()).hexdigest()[:16]}"


def session_tag(session_id: str) -> str:
    """The tag a session's procedure carries so the next pass over the same
    session can supersede it: the procedure's key is derived
    from the model's name for it, which changes from pass to pass."""
    return f"session:{hashlib.sha1(session_id.encode()).hexdigest()[:16]}"


def chain_edge_row(
    *, org: str, namespace: str, from_key: str, to_key: str, now: datetime | None = None
) -> dict[str, Any]:
    """`from_key` precedes `to_key` in loom.operator_edges — the digest chain's
    verb between two events of one session."""

    ts = now or naive_utc_now()
    return {
        "org": org,
        "namespace": namespace,
        "from_key": from_key,
        "to_key": to_key,
        "relation": CHAIN_RELATION,
        "label": "",
        "explanation": "consecutive transition events of one agent session",
        "evidence": [from_key, to_key],
        "confidence": 1.0,
        "weight": 1.0,
        "author": "loom",
        "run_id": CHAIN_RUN_ID,
        "created_at": ts,
        "tombstoned_at": None,
    }


@dataclass
class ExtrasResult:
    events_written: int = 0
    edges_written: int = 0
    procedure_written: bool = False
    hint_written: bool = False
    llm_calls: int = 0
    failures: list[str] = field(default_factory=list)
    procedures_superseded: int = 0


async def distill_events(
    session: dict[str, Any],
    observations: list[Observation],
    *,
    llm: Any,
    max_events: int,
    ch: Any = None,
    res: ExtrasResult | None = None,
    only_after: set[str] | None = None,
) -> ExtrasResult:
    """One model call per transition pair → one `semantic` memory tagged
    `event`, dated at the later observation, chained to the previous event
    with a `precedes` edge. Fail-open per pair, except for a call the switch
    holds (`customer.HOLDS`), which propagates, as does an extraction policy
    this process cannot read. Each call ends with the operator's policy."""
    from server.memory.app import extraction_policy
    from server.memory.app.write import handle_memory_set

    res = res or ExtrasResult()
    pairs = transition_pairs(observations, max_pairs=max_events, only_after=only_after)
    if not pairs:
        return res
    system = with_policy(
        _EVENT_SYSTEM, await extraction_policy.effective(session["org"], session["namespace"])
    )
    ordered = sorted(
        observations, key=lambda o: (o.created_at is None, o.created_at or datetime.min)
    )
    position = {o.memory_key: i + 1 for i, o in enumerate(ordered)}
    total = len(ordered)
    sid = str(session.get("session_id") or "")
    prev_key = ""
    if only_after is not None and pairs[0][0].memory_key not in only_after:
        # The first transition of this pass follows an observation an earlier
        # pass distilled; chain onto that pass's last event.
        prev_key = event_memory_key(sid, pairs[0][0].memory_key)
    edge_rows: list[dict[str, Any]] = []
    for before, after in pairs:
        try:
            with llm_stage("observe_extract.event"):
                out = await llm.complete(system, event_prompt(before, after), max_tokens=300)
            res.llm_calls += 1
            parsed = parse_event(str(getattr(out, "text", "") or ""))
            if parsed is None:
                continue
            title, transition = parsed
            key = event_memory_key(sid, after.memory_key)
            tags = [EVENT_TAG, FROM_OBSERVATION_TAG, f"tool:{after.tool_name}"[:64]]
            if after.kind:
                tags.append(f"kind:{after.kind}")
            if after.error:
                tags.append("error")
            await handle_memory_set(
                MemorySetInput(
                    org=session["org"],
                    namespace=session["namespace"],
                    agent=session.get("agent") or "-",
                    user_id=session.get("user_id") or "-",
                    session_id=session.get("session_id") or "-",
                    memory_key=key,
                    memory_type="semantic",
                    value=render_event(
                        title,
                        transition,
                        after,
                        step=position.get(after.memory_key, 0),
                        total=total,
                    ),
                    tags=tags,
                    entities=list(after.file_paths[:12]),
                    confidence=0.9,
                    source_trace=f"{EVENT_SOURCE_TRACE}:{before.memory_key}>{after.memory_key}",
                    valid_at=after.valid_at or after.created_at,
                )
            )
            res.events_written += 1
            if prev_key:
                edge_rows.append(
                    chain_edge_row(
                        org=session["org"],
                        namespace=session["namespace"],
                        from_key=prev_key,
                        to_key=key,
                    )
                )
            prev_key = key
        except customer.HOLDS:
            raise  # not this pair's failure: the session is distilled again later
        except Exception as e:  # noqa: BLE001 — one pair's failure is one pair
            res.failures.append(f"event: {type(e).__name__}: {e}")
    if edge_rows:
        try:
            if ch is None:
                from server.core.ch import get_client

                ch = get_client()
            await ch.insert("loom.operator_edges", edge_rows)
            res.edges_written = len(edge_rows)
        except Exception as e:  # noqa: BLE001 — recorded in failures
            res.failures.append(f"edges: {type(e).__name__}: {e}")
    return res


async def distill_notes(
    session: dict[str, Any],
    observations: list[Observation],
    *,
    llm: Any,
    res: ExtrasResult | None = None,
    ch: Any = None,
) -> ExtrasResult:
    """One model call per session → a procedure (procedure plane) and a hint
    (`semantic` memory tagged `hint`). Fail-open, except for a call the switch
    holds (`customer.HOLDS`), which propagates, as does an extraction policy
    this process cannot read. The call ends with the operator's policy."""
    res = res or ExtrasResult()
    useful = [o for o in observations if not o.trivial]
    # A procedure (and the session hint) is mined only from a
    # session that did enough work to have one — the WHOLE session so far is
    # passed, so a long session still gets its procedure on a later pass.
    # Procedures mined from very short sessions are a test run's trivia
    # ("Update naming file with comment").
    from server.settings import settings

    if len(useful) < int(settings.observe_extract_notes_min_observations):
        return res
    from server.memory.app import extraction_policy

    system = with_policy(
        _NOTES_SYSTEM, await extraction_policy.effective(session["org"], session["namespace"])
    )
    try:
        with llm_stage("observe_extract.notes"):
            out = await llm.complete(system, notes_prompt(observations), max_tokens=900)
        res.llm_calls += 1
        notes = parse_notes(str(getattr(out, "text", "") or ""))
    except customer.HOLDS:
        raise
    except Exception as e:  # noqa: BLE001 — recorded in failures; nothing to write
        res.failures.append(f"notes: {type(e).__name__}: {e}")
        return res
    sid = str(session.get("session_id") or "-")
    if notes.procedure:
        try:
            from server.memory.api.models_procedure import ProcedureAddInput
            from server.memory.app.procedure import handle_procedure_add

            added = await handle_procedure_add(
                ProcedureAddInput(
                    org=session["org"],
                    namespace=session["namespace"],
                    agent=session.get("agent") or "-",
                    user_id=session.get("user_id") or "-",
                    session_id=sid,
                    name=notes.procedure["name"],
                    situation=notes.procedure["situation"],
                    steps=notes.procedure["steps"],
                    tags=[FROM_OBSERVATION_TAG, "mined", session_tag(sid)],
                    metadata={"source": "observe.extract", "session_id": sid},
                )
            )
            res.procedure_written = True
            # One procedure per session. An earlier pass over
            # the same session (a session crystallizes at every Stop) wrote
            # one under the model's name for that fragment; it is superseded.
            res.procedures_superseded += await supersede_session_procedures(
                session, keep_key=str(getattr(added, "memory_key", "") or ""), ch=ch
            )
        except customer.HOLDS:
            raise
        except Exception as e:  # noqa: BLE001 — recorded in failures; the hint still writes
            res.failures.append(f"procedure: {type(e).__name__}: {e}")
    if notes.hint:
        try:
            from server.memory.api.models_crud import MemorySetInput
            from server.memory.app.write import handle_memory_set

            dated: list[datetime] = [
                d for o in observations if (d := (o.valid_at or o.created_at)) is not None
            ]
            # A derived row with no entities gets no entity-ANN boost and no
            # entity-overlap credit when the question names one ("what about
            # server/main.py"): reachable by cosine, but un-boosted. The
            # hint's entities are the session's paths.
            hint_entities = hint_entities_for(observations)
            await handle_memory_set(
                MemorySetInput(
                    org=session["org"],
                    namespace=session["namespace"],
                    agent=session.get("agent") or "-",
                    user_id=session.get("user_id") or "-",
                    session_id=sid,
                    memory_key=hint_memory_key(sid),
                    memory_type="semantic",
                    value=f"{notes.hint['title']}\n{notes.hint['content']}",
                    tags=[HINT_TAG, FROM_OBSERVATION_TAG],
                    entities=hint_entities,
                    confidence=0.85,
                    source_trace=HINT_SOURCE_TRACE,
                    valid_at=max(dated) if dated else None,
                )
            )
            res.hint_written = True
        except customer.HOLDS:
            raise
        except Exception as e:  # noqa: BLE001 — recorded in failures
            res.failures.append(f"hint: {type(e).__name__}: {e}")
    return res


async def supersede_session_procedures(
    session: dict[str, Any], *, keep_key: str, ch: Any = None
) -> int:
    """Tombstone the session's earlier mined procedures (tag `session:<sha>`),
    keeping `keep_key`. Returns how many were superseded; fail-open. Only
    rows of this session's own identity (agent, user_id, session_id — what
    the procedure was written under): the tag hashes the session id alone,
    and two principals can share one (`-`, a reused id)."""
    from server.memory.app.handlers import handle_memory_delete

    sid = str(session.get("session_id") or "-")
    try:
        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
        rows = await ch.query_all(
            """
            SELECT memory_key, agent, user_id, session_id
            FROM loom.memory FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = {agent:String} AND user_id = {uid:String}
              AND session_id = {sid:String}
              AND memory_type = 'procedural' AND has(tags, {tag:String})
              AND tombstoned_at IS NULL
            """,
            params={
                "org": session["org"],
                "ns": session["namespace"],
                "agent": session.get("agent") or "-",
                "uid": session.get("user_id") or "-",
                "sid": sid,
                "tag": session_tag(sid),
            },
        )
    except Exception as e:  # noqa: BLE001 — no read: nothing superseded this pass
        log.debug("session procedures read skipped: %s", e)
        return 0
    n = 0
    for r in rows or []:
        key = str(r.get("memory_key") or "")
        if not key or key == keep_key:
            continue
        try:
            await handle_memory_delete(
                MemoryDeleteInput(
                    org=session["org"],
                    namespace=session["namespace"],
                    agent=str(r.get("agent") or session.get("agent") or "-"),
                    user_id=str(r.get("user_id") or session.get("user_id") or "-"),
                    session_id=str(r.get("session_id") or sid),
                    memory_key=key,
                )
            )
            n += 1
        except Exception as e:  # noqa: BLE001 — this key is left for the next pass
            log.debug("session procedure supersede skipped for %s: %s", key, e)
    return n
