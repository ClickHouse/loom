"""The store mentors the agent that uses it.

Most ops are callable by name but not listed in the default MCP toolset, and the
guidance an agent has is read before its first call (the primer, the skills) or
after its namespace is already an object store (`namespace.audit`). Nothing else
tells an agent, from what it is actually doing, which plane it is not using.

`mentor_profile(org, namespace, agent)` reads (through `insights.store.mentor`)
the agent's operation counts for the window from `loom.agent_events`, the
namespace facts a rule needs (indexed repos, a tracker source, documents,
records, writers, open conflicts, the row shapes the audit already grades)
and evaluates a fixed
rule table: each rule is a hint code, a condition on those counts, a
sentence of at most `HINT_CHARS` naming the situation and the op, and the
ops it recommends. The profile is cached per (org, namespace, agent) for
`settings.mentor_ttl_seconds`; a failed read is None, held briefly, never
raised.

Three channels read it: `context.pack` renders up to three hints as its
`mentor` section; both transports attach one `hint` to a successful
response when a cheap per-call rule fires or a profile hint has not been
shown this session (`call_hint`); `mentor.brief` returns the whole profile
with the evidence per hint. Every hint that reaches a response also reaches
the ledger as `out_hint_code` through `hint_context`, so whether hints are
followed is a query.

Nothing here writes, refuses or rewrites a call. The rules are the checks a
reviewer would run; when a rule is wrong it is edited, not re-prompted.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any

from server.core.background import create_deferred_task
from server.insights.app.mentor_rules import (
    CALL_RULES,
    GETS,
    HUGE_VALUE_CHARS,
    LARGE_VALUE_CHARS,
    PACKS,
    SEARCHES,
    SETS,
    MentorProfile,
    clip,
    evaluate_rules,
)
from server.insights.store.mentor import ProfileReads
from server.memory.domain.excerpt import EXCERPT_LIMIT
from server.memory.domain.namespace_shapes import EPISODIC_BLOB_CHARS
from server.observability.consumer import INSPECTOR_CONSUMERS
from server.settings import settings

log = logging.getLogger(__name__)

WINDOW_SECONDS_DEFAULT = 7 * 86400
# The pack section: three hints, hard-capped in characters.
PACK_MAX_HINTS = 3
PACK_CHARS = 480
# A failed profile read is remembered this long (bounded by the TTL).
FAILURE_TTL_SECONDS = 300
# The `skipped` label of the repository fact: a profile missing it is kept briefly.
REPOS_FACT = "repos"
CACHE_MAX_ENTRIES = 4096
# Per-session state for the per-call channel: which codes were shown, how
# many hints went out, and how often each key was set.
SESSION_TTL_SECONDS = 6 * 3600
SESSION_MAX_ENTRIES = 8192

# ---------------------------------------------------------------------------
# Caches
# ---------------------------------------------------------------------------

_cache: dict[tuple[str, str, str], tuple[float, MentorProfile | None]] = {}
_sessions: dict[str, dict[str, Any]] = {}
_inflight: dict[tuple[str, str, str], asyncio.Task[Any]] = {}
# Profile hint codes already shown to an agent within the profile's TTL: a
# profile hint says something about the agent's week, so it is said once per
# agent per hour, not once per session — a bot that opens forty short
# sessions an hour would otherwise read the same sentence forty times.
# Per-call rules stay per session; the pack section stays per session.
# Keyed like `_cache` and evicted with it, so it is bounded the same way.
_profile_shown: dict[tuple[str, str, str], set[str]] = {}

# Callers the mentor never speaks to: the dashboard and the assistant toolset, which
# inspect a namespace for a person rather than use it (and whose connection carries none
# of the tools a hint points at).
_SILENT_CALLERS = frozenset(INSPECTOR_CONSUMERS)
# A profile hint rides only the calls its advice is about:
# the graph hint on a knowledge.get or a code.* result is advice about
# something else. Codes absent here ride every `_CARRIES_PROFILE_HINTS` op.
_HINT_CARRIERS: dict[str, tuple[str, ...]] = {
    "facts_graph_unused": ("memory.", "context.", "facts."),
}
# A profile hint rides only a call an agent makes about its work; telemetry,
# billing, grants and listing calls are not the moment for advice.
_CARRIES_PROFILE_HINTS = (
    "memory.",
    "knowledge.",
    "context.",
    "code.",
    "tracker.",
    "run.",
    "working_memory.",
    "facts.",
    "decision.",
    "procedure.",
    "conversation.",
)


def reset_cache() -> None:
    _cache.clear()
    _sessions.clear()
    _inflight.clear()
    _profile_shown.clear()


def _cache_put(key: tuple[str, str, str], expires_at: float, value: MentorProfile | None) -> None:
    if key not in _cache and len(_cache) >= CACHE_MAX_ENTRIES:
        oldest = min(_cache, key=lambda k: _cache[k][0])
        _cache.pop(oldest, None)
        _profile_shown.pop(oldest, None)
    _cache[key] = (expires_at, value)
    # A fresh profile starts a fresh "already said" set — seeded with the
    # codes the ledger saw this agent shown within the TTL:
    # each uvicorn worker keeps its own set, so a session spread over four
    # workers would read one profile hint four times.
    shown = (value.facts.get("shown_codes") if value is not None else None) or ()
    if shown:
        _profile_shown[key] = {str(c) for c in shown}
    else:
        _profile_shown.pop(key, None)


def cached_profile(
    org: str, namespace: str, agent: str, *, clock: Callable[[], float] = time.monotonic
) -> MentorProfile | None:
    """The profile if the cache holds a live one; no read otherwise."""
    hit = _cache.get((str(org), str(namespace), str(agent)))
    if hit is not None and hit[0] > clock():
        return hit[1]
    return None


def _ttl() -> int:
    return int(settings.mentor_ttl_seconds)


def _session(session_key: str, *, clock: Callable[[], float]) -> dict[str, Any]:
    now = clock()
    st = _sessions.get(session_key)
    if st is None or st["expires"] <= now:
        if len(_sessions) >= SESSION_MAX_ENTRIES:
            for k in sorted(_sessions, key=lambda k: _sessions[k]["expires"])[
                : SESSION_MAX_ENTRIES // 8
            ]:
                _sessions.pop(k, None)
        st = {"shown": set(), "count": 0, "set_keys": {}, "expires": now + SESSION_TTL_SECONDS}
        _sessions[session_key] = st
    return st


# ---------------------------------------------------------------------------
# The profile read
# ---------------------------------------------------------------------------


class _Read:
    """One profile read in progress: the store's reads, the facts being
    filled and the steps that failed."""

    def __init__(self, org: str, namespace: str, agent: str, window_seconds: int) -> None:
        self.org, self.namespace = org, namespace
        self.store = ProfileReads(
            org,
            namespace,
            agent,
            window_seconds,
            ops={"sets": SETS, "gets": GETS, "searches": SEARCHES, "packs": PACKS},
            limits={
                "ttl": max(1, _ttl()),
                "large": LARGE_VALUE_CHARS,
                "huge": HUGE_VALUE_CHARS,
                "advisory": int(settings.memory_value_advisory_chars),
                "clip": EXCERPT_LIMIT,
                "episodic_chars": EPISODIC_BLOB_CHARS,
            },
        )
        self.usage: dict[str, int] = {}
        self.sessions = 0
        self.facts: dict[str, Any] = {}
        self.skipped: list[str] = []
        self.code_entries: list[Any] = []

    async def guarded(self, name: str, step: Callable[[], Any]) -> None:
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 — advisory; one failed fact must not sink the rest
            log.warning("mentor %s skipped: %s", name, exc)
            self.skipped.append(f"{name}: {type(exc).__name__}")

    # -- the agent's own calls ------------------------------------------------

    async def usage_by_op(self) -> None:
        self.usage = await self.store.op_counts()
        self.sessions = await self.store.named_sessions()

    async def shown_hints(self) -> None:
        self.facts["shown_codes"] = await self.store.shown_codes()

    async def session_shape(self) -> None:
        r = await self.store.session_openings()
        self.facts["sessions_with_id"] = r["sessions"]
        self.facts["pack_first_sessions"] = r["pack_first"]

    async def outcomes(self) -> None:
        self.facts.update(await self.store.outcomes())

    async def reliance(self) -> None:
        """What the agent said it relied on, and whether its reads answered: a
        chunk id credited is credit landing on a passage instead of the fact,
        and a key no live row answers is credit landing nowhere."""
        self.facts.update(await self.store.credited_keys())
        self.facts.update(await self.store.key_reads())

    async def hot_keys(self) -> None:
        self.facts.update(await self.store.hot_keys())
        # The hot read key's live value. A lease or a pointer is short; a long
        # row read in a loop is a memory recalled by key (a 1.8 KB finding
        # read 75× during a curation pass is not coordination state).
        if self.facts["hot_get_key"]:
            self.facts["hot_get_key_chars"] = await self.store.live_value_chars(
                self.facts["hot_get_key"]
            )

    async def id_prefixes(self) -> None:
        self.facts.update(await self.store.top_id_prefix())

    # -- the namespace --------------------------------------------------------

    async def namespace_ledger(self) -> None:
        r = await self.store.namespace_ledger()
        self.facts["ns_conflict_ops"] = r["conflict_ops"]
        self.facts["writers"] = r["writers"]

    async def namespace_rows(self) -> None:
        self.facts.update(await self.store.namespace_rows())

    async def documents(self) -> None:
        self.facts["documents"] = await self.store.live_documents()

    async def records(self) -> None:
        self.facts["records"] = await self.store.active_records()

    async def row_shapes(self) -> None:
        # Documents and checkpoints stored as memory rows: THIS agent's rows
        # (the hint goes to the writer, not to whoever asks).
        r = await self.store.row_shapes()
        self.facts["shape_oversized_values"] = r["oversized"]
        self.facts["shape_json_prefix_excerpts"] = r["json_prefix"]
        self.facts["shape_episodic_blobs"] = r["episodic_blobs"]

    async def code_access(self) -> None:
        """The indexed repos this agent may read — the plane is "available"
        only then. A code namespace is read through a grant
        or a token naming it; `org_readable` widens a standard entry to the
        whole org; `standard` alone is not a read right. The principals are
        the subjects the agent called under this week, and a service account
        inherits its creator's live grants (auth.context). Counted as
        DISTINCT repositories: nine release tiers of one repo are one repo."""
        if not self.code_entries:
            self.facts["repos"] = 0
            return
        subjects = await self.store.subjects()
        granted = await self.store.granted_namespaces(subjects) if subjects else set()
        readable = [c for c in self.code_entries if _readable_entry(c, self.namespace, granted)]
        names = list(dict.fromkeys(str(getattr(c, "repo", "")) for c in readable))
        self.facts["repos"] = len(names)
        self.facts["repo_entries"] = len(readable)
        self.facts["repo_names"] = names[:3]

    def in_process_facts(self) -> None:
        # Indexed repos in the org (which of them this agent may read is
        # `code_access`, a ledger + grants read) and the tracker source registry.
        try:
            from server.code.domain.index.repos import configured_repos

            self.code_entries = [
                c for c in configured_repos() if str(getattr(c, "org", "")) == self.org
            ]
            self.facts["repos_total"] = len(self.code_entries)
        except Exception as exc:  # noqa: BLE001 — optional fact source: named in skipped
            self.skipped.append(f"{REPOS_FACT}: {type(exc).__name__}")
        try:
            from server.tracker.domain import sources

            eps = sources.endpoints()
            names = [n for n in sorted(eps) if sources.credential_configured(n)]
            self.facts["tracker_sources"] = names
        except Exception as exc:  # noqa: BLE001 — optional fact source: named in skipped
            self.skipped.append(f"tracker_sources: {type(exc).__name__}")


def _readable_entry(cfg: Any, namespace: str, granted: set[str]) -> bool:
    """authz's read rule for a code-plane entry, without a request: the
    namespace's own entry, a granted namespace, or an org-readable standard
    entry. A restricted entry is never org-readable, whatever the flag says."""
    entry_ns = str(getattr(cfg, "namespace", ""))
    if entry_ns == namespace or entry_ns in granted:
        return True
    restricted = str(getattr(cfg, "classification", "standard")) == "restricted"
    return bool(getattr(cfg, "org_readable", False)) and not restricted


async def _compute(org: str, namespace: str, agent: str, window_seconds: int) -> MentorProfile:
    rd = _Read(org, namespace, agent, window_seconds)
    rd.in_process_facts()
    for name in (
        "usage_by_op",
        "session_shape",
        "outcomes",
        "reliance",
        "hot_keys",
        "id_prefixes",
        "namespace_ledger",
        "namespace_rows",
        "documents",
        "records",
        "row_shapes",
        "code_access",
        "shown_hints",
    ):
        await rd.guarded(name, getattr(rd, name))
    hints = evaluate_rules(rd.usage, rd.sessions, rd.facts, agent=agent)
    return MentorProfile(
        org=org,
        namespace=namespace,
        agent=agent,
        window_seconds=int(window_seconds),
        usage=rd.usage,
        sessions=rd.sessions,
        facts=rd.facts,
        hints=tuple(hints),
        skipped=tuple(rd.skipped),
    )


async def mentor_profile(
    org: str,
    namespace: str,
    agent: str,
    *,
    window_seconds: int = WINDOW_SECONDS_DEFAULT,
    clock: Callable[[], float] = time.monotonic,
) -> MentorProfile | None:
    """The cached profile of (org, namespace, agent); None when the TTL is 0
    (the mentor is off), when the read failed, or while a failure is still
    remembered. Never raises."""
    ttl = _ttl()
    if ttl <= 0 or not org or not namespace:
        return None
    agent = str(agent or "-")
    key = (str(org), str(namespace), agent)
    now = clock()
    hit = _cache.get(key)
    if hit is not None and hit[0] > now:
        return hit[1]
    try:
        profile: MentorProfile | None = await _compute(org, namespace, agent, window_seconds)
    except Exception as exc:  # noqa: BLE001 — advisory; the caller must not fail on it
        log.warning("mentor profile %s/%s/%s failed: %s", org, namespace, agent, exc)
        profile = None
    # A profile whose repository fact failed (the code registry unread, say) is short-lived.
    whole = profile is not None and not any(s.startswith(f"{REPOS_FACT}:") for s in profile.skipped)
    _cache_put(key, now + (ttl if whole else min(ttl, FAILURE_TTL_SECONDS)), profile)
    return profile


def warm_profile(org: str, namespace: str, agent: str) -> None:
    """Start the read in the background when nothing is cached; the next
    call or pack reads the result. Never blocks the caller."""
    key = (str(org), str(namespace), str(agent or "-"))
    if _ttl() <= 0 or not org or not namespace or key in _cache or key in _inflight:
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run() -> None:
        try:
            await mentor_profile(org, namespace, agent)
        finally:
            _inflight.pop(key, None)

    _inflight[key] = create_deferred_task(_run())


# ---------------------------------------------------------------------------
# The rules
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_pack_section(
    profile: MentorProfile | None, *, cap: int = PACK_CHARS, max_hints: int = PACK_MAX_HINTS
) -> str:
    """The `mentor` block of context.pack: up to `max_hints` hints, hard
    capped; "" when nothing fires. The tail names the op and the skill."""
    if profile is None or not profile.hints:
        return ""
    items = [f"{h.code}: {h.text}" for h in profile.hints[:max_hints]]
    # Each hint already names its op; pointing at the skill makes agents open
    # the skill file even in sessions whose results carry no hint.
    tail = "mentor.brief has the evidence"
    while True:
        body = "Loom mentor — " + " · ".join(items) + f" ({tail})" if items else ""
        if len(body) <= cap or len(items) <= 1:
            break
        items.pop()
    return body[:cap]


def top_hint_line(org: str, namespace: str, agent: str) -> str:
    """One line for the MCP handshake when the profile is already cached
    (never a read on `initialize`); "" otherwise."""
    p = cached_profile(org, namespace, agent)
    if p is None or not p.hints:
        return ""
    h = p.hints[0]
    return f"**Loom mentor:** {h.text} (`{h.code}`; `mentor.brief` for the evidence)"


# ---------------------------------------------------------------------------
# Value and query shapes (the call's own text, never stored)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The per-call channel
# ---------------------------------------------------------------------------


def _max_per_session() -> int:
    return int(settings.mentor_max_hints_per_session)


def call_hint(
    op_name: str,
    arguments: dict[str, Any],
    result: Any,
    *,
    session_key: str,
    org: str,
    namespace: str,
    agent: str,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[str, str] | None:
    """(code, text) for this successful call, or None. One hint per call,
    each code at most once per session, at most `mentor_max_hints_per_session`
    per session; a profile hint at most once per agent per profile TTL, and
    only on a call about the agent's work. Cheap: the call's own arguments
    and result, plus the cached profile — never a read. Never raises."""
    try:
        if _ttl() <= 0:
            return None
        if not org or not namespace:
            return None
        if agent in _SILENT_CALLERS or str(arguments.get("consumer") or "") in _SILENT_CALLERS:
            return None
        st = _session(session_key or f"{org}/{namespace}/{agent}", clock=clock)
        res = (
            result.model_dump()
            if hasattr(result, "model_dump")
            else (result if isinstance(result, dict) else {})
        )
        profile = cached_profile(org, namespace, agent, clock=clock)
        if profile is None:
            warm_profile(org, namespace, agent)
        facts = profile.facts if profile is not None else {}
        candidate: tuple[str, str] | None = None
        for rule in CALL_RULES:
            candidate = rule(op_name, arguments, res, facts, st)
            if candidate is not None:
                break
        profile_key = (str(org), str(namespace), str(agent or "-"))
        if candidate is None and profile is not None and op_name.startswith(_CARRIES_PROFILE_HINTS):
            said = _profile_shown.setdefault(profile_key, set())
            for h in profile.hints:
                if h.code in st["shown"] or h.code in said:
                    continue
                if not op_name.startswith(_HINT_CARRIERS.get(h.code, _CARRIES_PROFILE_HINTS)):
                    continue
                candidate = (h.code, h.text)
                break
        if candidate is None:
            return None
        code, text = candidate
        if code in st["shown"] or st["count"] >= _max_per_session():
            return None
        st["shown"].add(code)
        st["count"] += 1
        if profile is not None and profile.hint(code) is not None:
            _profile_shown.setdefault(profile_key, set()).add(code)
        return code, clip(text)
    except Exception as exc:  # noqa: BLE001 — advice never fails a call
        log.debug("mentor call_hint failed for %s: %s", op_name, exc)
        return None


def session_key_for(mcp_session_id: str, arguments: dict[str, Any]) -> str:
    sid = str(mcp_session_id or arguments.get("session_id") or "")
    if sid and sid != "-":
        return sid
    return (
        f"{arguments.get('org', '')}/{arguments.get('namespace', '')}/{arguments.get('agent', '')}"
    )


__all__ = [
    "call_hint",
    "cached_profile",
    "mentor_profile",
    "render_pack_section",
    "reset_cache",
    "session_key_for",
    "top_hint_line",
    "warm_profile",
]
