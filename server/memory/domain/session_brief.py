"""The session brief: a condensed, resumable state of one line of work.
Pure logic — no I/O; the handler owns the reads, the
LLM call and the write.

A coding session's context is its transcript: every prompt, reply, tool call
and tool result, resent to the model each turn. It outgrows the window, the
client compacts it, and the next session starts from nothing but a bookmark
(goal, tree, files, last reply). What a later session needs to
carry the work on is smaller than the transcript and larger than the
bookmark: why the current approach was chosen, what was tried and dropped,
what is still open, what comes next, and where the detail lives.

The brief is built in two stages so the expensive one sees little:

1. **Condensation** (here, deterministic, zero tokens). The client sends the
   session as typed events (a user ask, a reply, an edit, a command with its
   one-line description, an error, a read). `condense_events` renders them
   as one line each under a character budget that spends on the signal
   first: every user ask and every error survive, edits next, then replies
   and commands sampled head-and-tail, and reads and searches collapse to a
   count. A session too long for one budget is cut into windows.

2. **Revision** (one bounded LLM call per window, or the deterministic
   builder when no model is configured). The model sees the previous brief
   and the new window, and returns the *current* state sections (goal,
   status, open questions, next steps, files, constraints, pointers) plus the
   *new* history items (decisions, dead ends) of this window only.
   `merge_sections` replaces the state and appends the history. A brief
   revised many times therefore never collapses its earlier reasoning into a
   shorter summary of a summary: the history sections are only ever appended
   to (and capped from the oldest end), never rewritten.

Two guards keep the brief honest. `ground_sections` drops a file or pointer
the model names that appears nowhere in the evidence it was given (a brief
that sends the next session to a file that does not exist costs more than a
brief that says less). `render_brief` frames the text as a saved state of
earlier work, not a source of facts (an unframed bookmark
read first in a new session was repeated as the truth).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote, unquote

from pydantic import BaseModel, Field

PROMPT_VERSION = "sb-4"

# Address of a brief inside a namespace: "loom@feat/x", "auto/loom/main",
# a name the user chose. Path-like, no spaces, bounded.
BRIEF_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9_.:/@+-]{0,159}$"
_BRIEF_KEY_RE = re.compile(BRIEF_KEY_PATTERN)
# A key as stored: a requested key, optionally behind the owner prefix of
# `owner_key_prefix`. A namespace-wide token lists and addresses briefs this way.
STORED_KEY_PATTERN = r"^(~[A-Za-z0-9_.%-]*~([A-Za-z0-9_.%-]*~)?)?" + BRIEF_KEY_PATTERN[1:]
STORED_KEY_MAX = 600
_STORED_KEY_RE = re.compile(STORED_KEY_PATTERN)

EventKind = Literal[
    "user", "assistant", "edit", "command", "error", "read", "search", "tool", "note", "summary"
]

# Per-field caps of the stored brief. A full brief renders to at most 12k
# chars (≈3k tokens, under the ~14k-char summary a client writes at a
# compaction): room for a day's reasoning, small enough to load at the top of
# a session without crowding the work.
TEXT_CAP = 400
GOAL_CAP = 600
STATUS_CAP = 900
LIST_CAPS: dict[str, int] = {
    "decisions": 20,
    "dead_ends": 12,
    "done": 24,
    "open_questions": 8,
    "next_steps": 8,
    "files": 15,
    "constraints": 8,
    "pointers": 10,
}
RENDER_CHAR_CAP = 12_000

# Condensation: how many characters one event line may keep, by kind.
_LINE_CAP: dict[str, int] = {
    # The client's own compaction summary: written by the session's model with
    # every tool result in view, the densest evidence a window can carry.
    "summary": 9000,
    "user": 700,
    "error": 320,
    "note": 400,
    "edit": 200,
    "assistant": 420,
    "command": 200,
    "tool": 160,
    "read": 160,
    "search": 160,
}
# Budget priority: lower spends first. Tier-1 lines are always kept (trimmed
# harder when even they overflow); tier 3 is sampled head + tail; tier 4 is
# collapsed to a count.
_PRIORITY: dict[str, int] = {
    "summary": 1,
    "user": 1,
    "error": 1,
    "note": 2,
    "edit": 2,
    "assistant": 3,
    "command": 3,
    "tool": 4,
    "read": 4,
    "search": 4,
}
_COLLAPSIBLE = frozenset({"read", "search", "tool"})


class BriefEvent(BaseModel):
    """One thing that happened in the session, as the client saw it."""

    kind: EventKind
    # 4k for an ordinary event; a client compaction summary runs to ~15k.
    text: str = Field(default="", max_length=16000)
    path: str = Field(default="", max_length=500)
    # ISO-8601 timestamp from the transcript, when it has one.
    ts: str = Field(default="", max_length=40)
    # Where the full detail lives: a transcript message uuid, an observation
    # memory key. Carried into the condensed line so a pointer can name it.
    ref: str = Field(default="", max_length=120)


class BriefDecision(BaseModel):
    what: str = Field(default="", max_length=TEXT_CAP)
    why: str = Field(default="", max_length=TEXT_CAP)


class BriefDeadEnd(BaseModel):
    tried: str = Field(default="", max_length=TEXT_CAP)
    why_not: str = Field(default="", max_length=TEXT_CAP)


class BriefDone(BaseModel):
    """A finished piece of the work and the identifier that proves it."""

    what: str = Field(default="", max_length=TEXT_CAP)
    ref: str = Field(default="", max_length=200)


class BriefFile(BaseModel):
    path: str = Field(default="", max_length=500)
    role: str = Field(default="", max_length=200)


class BriefSections(BaseModel):
    """The structured brief. State sections describe now and are replaced on
    each revision; history sections (decisions, dead_ends, done) accumulate."""

    goal: str = Field(default="", max_length=GOAL_CAP)
    status: str = Field(default="", max_length=STATUS_CAP)
    decisions: list[BriefDecision] = Field(default_factory=list, max_length=40)
    dead_ends: list[BriefDeadEnd] = Field(default_factory=list, max_length=40)
    done: list[BriefDone] = Field(default_factory=list, max_length=60)
    open_questions: list[str] = Field(default_factory=list, max_length=40)
    next_steps: list[str] = Field(default_factory=list, max_length=40)
    files: list[BriefFile] = Field(default_factory=list, max_length=60)
    constraints: list[str] = Field(default_factory=list, max_length=40)
    pointers: list[str] = Field(default_factory=list, max_length=40)
    # The client's own summary from its latest compaction,
    # verbatim (never revised by the model), and when it was written. Served
    # below the brief only on `session.brief_get(full=true)`.
    client_summary: str = Field(default="", max_length=16000)
    client_summary_at: str = Field(default="", max_length=40)

    def item_count(self) -> int:
        return sum(len(getattr(self, name)) for name in LIST_CAPS)


def brief_key_ok(key: str) -> bool:
    return bool(_BRIEF_KEY_RE.match(key or ""))


def stored_key_ok(key: str) -> bool:
    return len(key or "") <= STORED_KEY_MAX and bool(_STORED_KEY_RE.match(key or ""))


def owner_key_prefix(user_id: str, session_id: str | None = None) -> str:
    """The stored-key prefix of a brief saved under a token bounded to one user
    (and session): `~<user>~` or `~<user>~<session>~`. A requested key never
    starts with or holds `~` (BRIEF_KEY_PATTERN), and `~` is escaped out of the
    ids, so two owners never share a stored key and none equals a plain key."""

    def tag(value: str) -> str:
        return quote(value, safe="").replace("~", "%7E")

    if session_id is None:
        return f"~{tag(user_id)}~"
    return f"~{tag(user_id)}~{tag(session_id)}~"


def key_owner(stored_key: str) -> tuple[str, str | None] | None:
    """The (user, session) an `owner_key_prefix` names on a stored key (session
    None for a `~<user>~` prefix), or None for a key with no prefix."""
    if not stored_key.startswith("~"):
        return None
    parts = stored_key.split("~")
    if len(parts) == 4:
        return unquote(parts[1]), unquote(parts[2])
    return unquote(parts[1]), None


def auto_brief_key(project: str, branch: str) -> str:
    """The default address of a coding session's brief: one per
    (project, branch), so every session on the same line of work revises the
    same brief and a new session on that branch finds it."""

    def part(s: str, n: int) -> str:
        return re.sub(r"[^A-Za-z0-9_.+-]+", "-", (s or "").strip()).strip("-")[:n]

    p = part(project, 60) or "project"
    b = part(branch.replace("/", "-") if branch else "", 80) or "detached"
    return f"auto/{p}/{b}"


# --------------------------------------------------------------------------
# Condensation
# --------------------------------------------------------------------------


def _flat(text: str) -> str:
    return " ".join(str(text or "").split())


def _clip(text: str, n: int) -> str:
    t = _flat(text)
    return t if len(t) <= n else t[: max(1, n - 1)].rstrip() + "…"


@dataclass
class CondenseStats:
    events: int = 0
    sampled_out: int = 0
    collapsed: int = 0


@dataclass
class _Line:
    idx: int
    kind: str
    text: str
    priority: int


def event_chars(events: list[BriefEvent]) -> int:
    return sum(len(e.text) + len(e.path) for e in events)


def _event_line(i: int, e: BriefEvent, cap: int) -> str:
    body = e.text
    if e.kind in ("edit", "read") and e.path:
        body = e.path + (f" — {e.text}" if e.text else "")
    elif e.path and e.path not in body:
        body = f"{body} [{e.path}]" if body else e.path
    ref = f" (ref {e.ref})" if e.ref and e.kind in ("error", "user") else ""
    return f"[{i} {e.kind}] {_clip(body, cap)}{ref}"


def _collapse_runs(events: list[BriefEvent]) -> tuple[list[_Line], int]:
    """One line per event, with consecutive reads/searches/tool calls folded
    into one line naming what they touched, and a command repeated back to
    back kept once. Returns (lines, events folded away)."""
    lines: list[_Line] = []
    folded = 0
    run: list[tuple[int, BriefEvent]] = []
    last_cmd = ""

    def flush() -> None:
        nonlocal folded
        if not run:
            return
        if len(run) == 1:
            i, e = run[0]
            lines.append(_Line(i, e.kind, _event_line(i, e, _LINE_CAP[e.kind]), _PRIORITY[e.kind]))
        else:
            first = run[0][0]
            names: list[str] = []
            for _, e in run:
                name = e.path or _clip(e.text, 60)
                if name and name not in names:
                    names.append(name)
            shown = ", ".join(names[:6]) + (f", … {len(names) - 6} more" if len(names) > 6 else "")
            text = f"[{first} reads] {len(run)} reads/searches: {shown}"
            lines.append(_Line(first, "read", _clip(text, 320), _PRIORITY["read"]))
            folded += len(run) - 1
        run.clear()

    for i, e in enumerate(events):
        if e.kind in _COLLAPSIBLE:
            run.append((i, e))
            continue
        flush()
        if e.kind == "command":
            key = _flat(e.text)[:200]
            if key and key == last_cmd:
                folded += 1
                continue
            last_cmd = key
        else:
            last_cmd = ""
        lines.append(_Line(i, e.kind, _event_line(i, e, _LINE_CAP[e.kind]), _PRIORITY[e.kind]))
    flush()
    return lines, folded


def _fit(lines: list[_Line], budget: int) -> tuple[list[_Line], int]:
    """Keep lines under `budget` characters by priority, in their original
    order. Returns (kept lines, lines sampled out)."""

    def size(ls: list[_Line]) -> int:
        return sum(len(x.text) + 1 for x in ls)

    if size(lines) <= budget:
        return lines, 0
    keep: dict[int, _Line] = {}
    used = 0
    for tier in (1, 2):
        for ln in lines:
            if ln.priority == tier:
                keep[ln.idx] = ln
                used += len(ln.text) + 1
    if used > budget:
        # Even the asks and errors overflow: trim each to share the budget.
        tier12 = [ln for ln in lines if ln.priority <= 2]
        share = max(80, budget // max(1, len(tier12)) - 1)
        keep = {ln.idx: _Line(ln.idx, ln.kind, _clip(ln.text, share), ln.priority) for ln in tier12}
        kept = sorted(keep.values(), key=lambda x: x.idx)
        while kept and size(kept) > budget:
            kept.pop(len(kept) // 2)  # the middle of a long session matters least
        return kept, len(lines) - len(kept)
    for tier in (3, 4):
        pool = [ln for ln in lines if ln.priority == tier]
        # Head and tail alternately: how the window started and how it ends.
        order: list[_Line] = []
        lo, hi = 0, len(pool) - 1
        while lo <= hi:
            order.append(pool[hi])
            if lo != hi:
                order.append(pool[lo])
            lo += 1
            hi -= 1
        for ln in order:
            if used + len(ln.text) + 1 > budget:
                continue
            keep[ln.idx] = ln
            used += len(ln.text) + 1
    kept = sorted(keep.values(), key=lambda x: x.idx)
    return kept, len(lines) - len(kept)


def condense_events(events: list[BriefEvent], budget_chars: int) -> tuple[str, CondenseStats]:
    """The events as one line each, chronological, under `budget_chars`.
    Deterministic: the same events and budget give the same text."""
    stats = CondenseStats(events=len(events))
    lines, folded = _collapse_runs(events)
    stats.collapsed = folded
    kept, dropped = _fit(lines, max(200, budget_chars))
    stats.sampled_out = dropped
    text = "\n".join(ln.text for ln in kept)
    if dropped:
        text += f"\n[… {dropped} lower-priority lines of this window omitted for length]"
    return text, stats


def split_windows(
    events: list[BriefEvent], window_chars: int, max_windows: int
) -> list[list[BriefEvent]]:
    """Cut a long run of events into consecutive windows of about
    `window_chars` raw characters each (at most `max_windows`; the last
    window takes the remainder and is condensed harder)."""
    if not events:
        return []
    windows: list[list[BriefEvent]] = [[]]
    used = 0
    for e in events:
        n = len(e.text) + len(e.path) + 16
        if windows[-1] and used + n > window_chars and len(windows) < max_windows:
            windows.append([])
            used = 0
        windows[-1].append(e)
        used += n
    return windows


# --------------------------------------------------------------------------
# The revision prompt
# --------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You maintain the resumable brief of a software engineering session: the "
    "notes a senior engineer leaves so that someone with none of the session's "
    "context (a new session, a teammate) can carry the work on without redoing "
    "it or undoing it. You receive the PREVIOUS BRIEF (JSON, may be empty) and "
    "the NEW EVENTS of the session since that brief, one per line as [index "
    "kind] text. Kinds: user = what the person said (their words carry the "
    "goal, corrections and rules); assistant = the agent's replies (they carry "
    "findings, measurements and reasons); edit = a file changed; command = a "
    "shell command with its description; error = a failed tool call; read = "
    "files read or searched; note = client notes (todo lists, identifiers a "
    "command printed); summary = the summary the client itself wrote when it "
    "compacted this session's context — dense and reliable about everything "
    "before it: mine it for decisions, done work, dead ends and rules, but keep "
    "what the previous brief already holds.\n\n"
    "Return ONE JSON object with exactly these keys:\n"
    '- "goal": the objective in the person\'s terms. Keep the previous goal '
    "word for word; when the person later sets a new or narrower objective, "
    "append it as ' Now: <new objective>.' (replacing any earlier 'Now:'), so "
    "the original mission is never lost.\n"
    '- "status": 2-4 sentences on where the work stands at the END of the new '
    "events: what is finished, what is in progress, what is failing — with the "
    "identifiers (PR numbers, branches, test names, error text).\n"
    '- "new_decisions": design or product choices made IN THE NEW EVENTS, as '
    '[{"what": ..., "why": ...}]. A decision picks one option over another '
    "(an approach, a default, a schema, a threshold, a rule) and a later "
    "session must not silently undo it. NOT a decision: routine actions "
    "(merged a PR, ran the tests, confirmed checks, cleaned a worktree, started "
    "a hook). `why` is the reason given in the events (a measurement, a "
    "failure, a constraint); if the events give no reason, leave why empty — "
    "never write a generic reason like 'to improve X'.\n"
    '- "new_done": pieces of work finished IN THE NEW EVENTS, as [{"what": '
    '..., "ref": ...}] with the identifier that proves it in ref (PR number, '
    "commit, decision or migration number, artifact URL). One line each, "
    "specific: 'negative outcomes demote ranking', ref 'PR #42'.\n"
    '- "new_dead_ends": approaches tried IN THE NEW EVENTS and abandoned or '
    'reverted, as [{"tried": ..., "why_not": ...}] with the evidence (a '
    "number, an error), so nobody retries them. NOT a dead end: tool "
    "mechanics (a file too large to read, a typo retried, a flaky command).\n"
    '- "open_questions": what is still unknown or unresolved at the end (drop '
    "previous ones the events answered).\n"
    '- "next_steps": the concrete next actions, in order, specific enough to '
    "act on (drop finished ones).\n"
    '- "files": the files that matter to continue, as [{"path": ..., "role": '
    "...}], paths exactly as they appear in the events or the previous brief.\n"
    '- "constraints": rules the person set for this work (never X, always Y, '
    "hold merges until Z, coordinate with W), close to their words.\n"
    '- "pointers": identifiers where detail lives: PR numbers, commits, '
    "branches, URLs, issue numbers, memory keys — only ones that appear "
    "verbatim in the events or the previous brief.\n\n"
    "Rules: state only what the events or the previous brief support; never "
    "invent a file, number or result. Specific beats general: names, numbers, "
    "error text. No narration ('the user asked', 'the assistant ran'); write "
    "the state of the work. Each string at most 300 characters; at most 6 "
    "new_decisions, 8 new_done, 6 new_dead_ends, 6 open_questions, 6 "
    "next_steps, 10 files, 6 constraints and 10 pointers — keep the ones that "
    "matter most. Use [] for an empty list. Output the JSON object only."
)


def previous_for_prompt(prev: BriefSections | None) -> str:
    if prev is None:
        return "{}"
    data = prev.model_dump()
    return json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def user_prompt(
    prev: BriefSections | None,
    condensed: str,
    *,
    window: int,
    windows: int,
    context_line: str,
) -> str:
    head = f"{context_line}\n" if context_line else ""
    part = f" (window {window} of {windows})" if windows > 1 else ""
    return f"{head}PREVIOUS BRIEF:\n{previous_for_prompt(prev)}\n\nNEW EVENTS{part}:\n{condensed}\n"


_JSON_OBJECT = re.compile(r"\{.*\}", re.S)


def _salvage_json(raw: str) -> Any:
    """The longest prefix of a truncated JSON object that parses once its
    open brackets are closed — a revision cut off by the output cap keeps
    every key it finished. None when nothing parses."""
    start = raw.find("{")
    if start < 0:
        return None
    text = raw[start:]
    stack: list[str] = []
    cuts: list[tuple[int, str]] = []
    in_str = esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
            if not stack:
                return None  # a complete object: not a truncation
        elif ch == ",":
            cuts.append((i, "".join(reversed(stack))))
    for i, closers in reversed(cuts):
        try:
            return json.loads(text[:i] + closers)
        except ValueError:
            continue
    return None


# At most this many items of any one list are read from a model reply: the
# model fields cap at 40, and the merge keeps far fewer (LIST_CAPS).
_PARSE_LIST_MAX = 40


def _as_str_list(v: Any, cap: int) -> list[str]:
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list):
        return []
    out: list[str] = []
    for x in v[: _PARSE_LIST_MAX * 2]:
        if isinstance(x, dict):
            x = " — ".join(str(y) for y in x.values() if y)
        s = _clip(str(x or ""), cap)
        if s and s not in out:
            out.append(s)
    return out[:_PARSE_LIST_MAX]


def _as_pairs(
    v: Any, a: str, b: str, *, cap_a: int = TEXT_CAP, cap_b: int = TEXT_CAP
) -> list[dict[str, str]]:
    """`[{a, b}]` from a model list, each field clipped to its model field's
    cap (a longer value is a ValidationError, not a longer brief)."""
    if not isinstance(v, list):
        return []
    out: list[dict[str, str]] = []
    for x in v[: _PARSE_LIST_MAX * 2]:
        if isinstance(x, str):
            x = {a: x}
        if not isinstance(x, dict):
            continue
        first = _clip(str(x.get(a) or ""), cap_a)
        if not first:
            continue
        out.append({a: first, b: _clip(str(x.get(b) or ""), cap_b)})
    return out[:_PARSE_LIST_MAX]


@dataclass
class WindowRevision:
    """One window's result: the current-state sections, and the history items
    (decisions, dead ends, done) this window added."""

    state: BriefSections
    decisions: list[BriefDecision] = field(default_factory=list)
    dead_ends: list[BriefDeadEnd] = field(default_factory=list)
    done: list[BriefDone] = field(default_factory=list)
    # True when the goal is a guess (the deterministic builder's first ask of
    # the window): it fills an empty brief but never replaces a goal.
    goal_is_fallback: bool = False


def parse_revision(text: str) -> WindowRevision | None:
    """The model's JSON → a WindowRevision, or None when there is no
    parseable object. Tolerates code fences, prose
    around the object, a string where a list was asked for, and missing
    keys; never raises."""
    raw = (text or "").strip()
    if not raw:
        return None
    m = _JSON_OBJECT.search(raw)
    try:
        data = json.loads(m.group(0)) if m else None
    except (ValueError, TypeError):
        data = None
    if data is None:
        data = _salvage_json(raw)
    if not isinstance(data, dict):
        return None
    state = BriefSections(
        goal=_clip(str(data.get("goal") or ""), GOAL_CAP),
        status=_clip(str(data.get("status") or ""), STATUS_CAP),
        open_questions=_as_str_list(data.get("open_questions"), TEXT_CAP),
        next_steps=_as_str_list(data.get("next_steps"), TEXT_CAP),
        files=[
            BriefFile(**p)
            for p in _as_pairs(data.get("files"), "path", "role", cap_a=500, cap_b=200)
        ],
        constraints=_as_str_list(data.get("constraints"), TEXT_CAP),
        pointers=_as_str_list(data.get("pointers"), 200),
    )
    return WindowRevision(
        state=state,
        decisions=[BriefDecision(**p) for p in _as_pairs(data.get("new_decisions"), "what", "why")],
        dead_ends=[
            BriefDeadEnd(**p) for p in _as_pairs(data.get("new_dead_ends"), "tried", "why_not")
        ],
        done=[BriefDone(**p) for p in _as_pairs(data.get("new_done"), "what", "ref", cap_b=200)],
    )


# --------------------------------------------------------------------------
# Grounding
# --------------------------------------------------------------------------

# Identifier-shaped tokens a pointer must be able to point at.
_POINTER_TOKEN = re.compile(
    r"https?://[^\s)\]>\"']+"
    r"|\b(?:decision|migration)\s+\d{3,6}\b"
    r"|#\d{1,7}\b"
    r"|\b[0-9a-f]{7,40}\b"
    r"|\b[\w.-]+/[\w./@-]+\b"
    r"|\b[\w-]+:[\w./:-]+\b",
    re.I,
)


def _norm_path(p: str) -> str:
    return p.strip().strip("`'\"").rstrip("/.,;:")


def _path_grounded(path: str, evidence: str) -> bool:
    p = _norm_path(path)
    if not p:
        return False
    if p in evidence:
        return True
    # A path shortened or lengthened by a working-directory prefix still
    # grounds when its last two components appear together.
    tail = "/".join(p.split("/")[-2:])
    return len(tail) >= 6 and tail in evidence


def _identifier_shaped(token: str) -> bool:
    """A token worth checking: `#12`, or at least five characters (so prose
    like `A/B` or `and/or` is not taken for a path)."""
    return token.startswith("#") or len(token.rstrip(".,;:")) >= 5


def _token_grounded(token: str, low_evidence: str) -> bool:
    """The token, or for `scheme:id` the id alone (the events say
    `10.1000/182`, the model writes `doi:10.1000/182`), is in the evidence."""
    t = token.rstrip(".,;:").lower()
    if t in low_evidence:
        return True
    head, sep, rest = t.partition(":")
    return bool(sep) and not t.startswith("http") and len(rest) >= 6 and rest in low_evidence


# A word: inner dots and dashes kept (file.py, pre-1800), edge punctuation not.
_WORD = re.compile(r"[a-z0-9](?:[a-z0-9_.-]*[a-z0-9])?")
_STOP = frozenset(
    "the and for with that this from are was were not but you your into have has "
    "will would should must never always only any all when then than them they "
    "its it's our out one two use using used".split()
)


def _content_words(text: str) -> set[str]:
    return {w for w in _WORD.findall(text.lower()) if len(w) >= 3 and w not in _STOP}


def _rule_grounded(rule: str, sources: list[set[str]]) -> bool:
    """A rule the user set must come from their words: at least half of its
    content words appear together in one user turn (or client summary, or a
    rule already in the brief)."""
    words = _content_words(rule)
    if not words:
        return False
    need = max(1, (len(words) + 1) // 2)
    return any(len(words & src) >= need for src in sources)


def rule_sources(prev: BriefSections | None, events: list[BriefEvent]) -> list[set[str]]:
    """Where a rule may come from: each user turn, each client summary, and
    the brief's existing rules."""
    texts = [e.text for e in events if e.kind in ("user", "summary")]
    if prev is not None:
        texts.extend(prev.constraints)
    return [_content_words(t) for t in texts if t]


def ground_sections(
    sections: BriefSections, evidence: str, rules_from: list[set[str]] | None = None
) -> tuple[BriefSections, int]:
    """Drop the files and pointers the evidence does not contain, and (given
    `rules_from`) the rules no user turn states. Returns (grounded sections,
    how many items were dropped)."""
    dropped = 0
    constraints = list(sections.constraints)
    if rules_from is not None:
        constraints = [c for c in sections.constraints if _rule_grounded(c, rules_from)]
        dropped += len(sections.constraints) - len(constraints)
    low = evidence.lower()
    files: list[BriefFile] = []
    for f in sections.files:
        if _path_grounded(f.path, evidence):
            files.append(f)
        else:
            dropped += 1
    pointers: list[str] = []
    for p in sections.pointers:
        tokens = [t for t in _POINTER_TOKEN.findall(p) if _identifier_shaped(t)]
        if tokens and all(_token_grounded(t, low) for t in tokens):
            pointers.append(p)
        else:
            dropped += 1
    grounded = sections.model_copy(
        update={"files": files, "pointers": pointers, "constraints": constraints}
    )
    return grounded, dropped


# --------------------------------------------------------------------------
# Merge
# --------------------------------------------------------------------------


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()[:120]


def _union_str(new: list[str], old: list[str], cap: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for s in [*new, *old]:
        k = _key(s)
        if k and k not in seen:
            seen.add(k)
            out.append(s)
    return out[:cap]


def _append_history(old: list[Any], new: list[Any], attr: str, cap: int) -> list[Any]:
    """History sections only grow; the oldest items leave when over cap."""
    seen = {_key(getattr(x, attr)) for x in old}
    out = list(old)
    for x in new:
        k = _key(getattr(x, attr))
        if k and k not in seen:
            seen.add(k)
            out.append(x)
    return out[-cap:]


def merge_sections(prev: BriefSections | None, rev: WindowRevision) -> BriefSections:
    """Revise `prev` with one window's result: state sections take the new
    values (falling back to the previous goal / status when the window said
    nothing), history sections append, files/constraints/pointers union with
    the new ones first."""
    p = prev or BriefSections()
    state = rev.state
    files: list[BriefFile] = []
    seen_paths: set[str] = set()
    for f in [*state.files, *p.files]:
        k = _norm_path(f.path)
        if k and k not in seen_paths:
            seen_paths.add(k)
            files.append(f)
    goal = (p.goal or state.goal) if rev.goal_is_fallback else (state.goal or p.goal)
    return BriefSections(
        goal=goal,
        status=state.status or p.status,
        decisions=_append_history(p.decisions, rev.decisions, "what", LIST_CAPS["decisions"]),
        dead_ends=_append_history(p.dead_ends, rev.dead_ends, "tried", LIST_CAPS["dead_ends"]),
        done=_append_history(p.done, rev.done, "what", LIST_CAPS["done"]),
        open_questions=state.open_questions[: LIST_CAPS["open_questions"]],
        next_steps=state.next_steps[: LIST_CAPS["next_steps"]],
        files=files[: LIST_CAPS["files"]],
        constraints=_union_str(state.constraints, p.constraints, LIST_CAPS["constraints"]),
        pointers=_union_str(state.pointers, p.pointers, LIST_CAPS["pointers"]),
    )


def overlay_sections(prev: BriefSections | None, given: BriefSections) -> BriefSections:
    """A brief the agent wrote itself (`sections` on the save): every section
    it filled replaces the previous one; history sections it filled append;
    the ones it left empty keep the previous value."""
    p = prev or BriefSections()
    return BriefSections(
        goal=given.goal or p.goal,
        status=given.status or p.status,
        decisions=_append_history(p.decisions, given.decisions, "what", LIST_CAPS["decisions"]),
        dead_ends=_append_history(p.dead_ends, given.dead_ends, "tried", LIST_CAPS["dead_ends"]),
        done=_append_history(p.done, given.done, "what", LIST_CAPS["done"]),
        open_questions=(given.open_questions or p.open_questions)[: LIST_CAPS["open_questions"]],
        next_steps=(given.next_steps or p.next_steps)[: LIST_CAPS["next_steps"]],
        files=(given.files or p.files)[: LIST_CAPS["files"]],
        constraints=_union_str(given.constraints, p.constraints, LIST_CAPS["constraints"]),
        pointers=_union_str(given.pointers, p.pointers, LIST_CAPS["pointers"]),
    )


# --------------------------------------------------------------------------
# The deterministic builder (no model configured, or the model failed)
# --------------------------------------------------------------------------

_RULE_WORDS = re.compile(
    r"\b(never|always|don'?t|do not|must|must not|only|no need to|avoid|keep)\b", re.I
)
_POINTER_FIND = re.compile(
    r"https?://[^\s)\]>\"']+|(?:PR|pull request|issue)\s*#\d{1,7}|#\d{2,7}\b"
    r"|\b(?:decision|migration)\s+\d{3,6}\b",
    re.I,
)


def deterministic_revision(events: list[BriefEvent]) -> WindowRevision:
    """What a brief can say without a model: the first ask is the goal, the
    last reply is the status, edited files are the files, a late error is an
    open question, rule-shaped user sentences are constraints, PR/URL tokens
    are pointers, and a command that printed an identifier (a PR URL, a
    commit) is done. No decisions or dead ends — those need judgment."""
    users = [e for e in events if e.kind == "user" and e.text.strip()]
    replies = [e for e in events if e.kind == "assistant" and e.text.strip()]
    goal = _clip(users[0].text, GOAL_CAP) if users else ""
    status = _clip(replies[-1].text, STATUS_CAP) if replies else ""
    files: list[BriefFile] = []
    seen: set[str] = set()
    for e in reversed(events):
        if e.kind == "edit" and e.path and e.path not in seen:
            seen.add(e.path)
            files.append(BriefFile(path=e.path, role="edited"))
    open_q: list[str] = []
    tail_start = int(len(events) * 0.8)
    last_edit = max((i for i, e in enumerate(events) if e.kind == "edit"), default=-1)
    for i, e in enumerate(events):
        if e.kind == "error" and i >= tail_start and i > last_edit:
            open_q.append(_clip(f"unresolved error: {e.text}", TEXT_CAP))
    constraints: list[str] = []
    for e in users:
        for sent in re.split(r"(?<=[.!?\n])\s+", e.text):
            if _RULE_WORDS.search(sent) and 12 <= len(sent) <= 300:
                constraints.append(_clip(sent, TEXT_CAP))
    pointers: list[str] = []
    for e in [*users, *replies]:
        for m in _POINTER_FIND.finditer(e.text):
            tok = m.group(0).rstrip(".,;:)")
            if tok not in pointers:
                pointers.append(tok)
    done: list[BriefDone] = []
    for i, e in enumerate(events):
        if e.kind != "note" or not e.text.startswith("output: "):
            continue
        cmd = next((c for c in reversed(events[:i]) if c.kind == "command"), None)
        what = _clip(cmd.text.split(" — $ ", 1)[0], TEXT_CAP) if cmd else "command output"
        done.append(BriefDone(what=what, ref=_clip(e.text[len("output: ") :], 200)))
    state = BriefSections(
        goal=goal,
        status=status,
        open_questions=open_q[-LIST_CAPS["open_questions"] :],
        files=files[: LIST_CAPS["files"]],
        constraints=_union_str(
            constraints[-LIST_CAPS["constraints"] :], [], LIST_CAPS["constraints"]
        ),
        pointers=pointers[-LIST_CAPS["pointers"] :],
    )
    return WindowRevision(state=state, done=done[-LIST_CAPS["done"] :], goal_is_fallback=True)
