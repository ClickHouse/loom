"""mentor.brief handler: the profile, the planes, the hints with evidence, and adoption —
hints shown vs followed per code."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from server.core.ch import get_client
from server.insights.api.models_mentor import (
    MentorAdoption,
    MentorBriefInput,
    MentorBriefOutput,
    MentorHint,
    MentorPlane,
)
from server.insights.app import mentor as svc
from server.insights.app import mentor_rules
from server.insights.store import mentor as mentor_store

log = logging.getLogger(__name__)

# Adoption reads are bounded: this many hint-carrying rows, newest first.
ADOPTION_ROW_CAP = 5000

CALL_HINT_OPS = mentor_rules.CALL_HINT_OPS


# Agent names that are nobody in particular: the anonymous buckets and the
# dashboard's own traffic. A brief about them describes the UI's polling.
ANONYMOUS_AGENTS = frozenset({"", "-", "*", "loom-ui"})
TOP_WRITERS_LIMIT = 8


def _caller_agent(inp: MentorBriefInput) -> str:
    if inp.agent:
        return inp.agent
    try:
        from server.auth.context import current_auth

        auth = current_auth()
        for attr in ("agent", "default_agent", "client_name"):
            v = getattr(auth, attr, None) if auth is not None else None
            if v:
                return str(v)
    except Exception:  # noqa: BLE001 — an anonymous caller is '-'
        pass
    return "-"


async def _top_writers(org: str, namespace: str, window_seconds: int) -> list[str]:
    """The namespace's writers in the window, most active first — the agents
    a brief is about. Anonymous names and the dashboard are left out. One
    bounded read; a failure is an empty list."""
    try:
        return await mentor_store.top_writers(
            org,
            namespace,
            window_seconds,
            write_ops=mentor_rules.SETS
            + ("knowledge.add", "knowledge.upsert", "memory.record.write"),
            anonymous=ANONYMOUS_AGENTS,
            limit=TOP_WRITERS_LIMIT,
            ch=get_client(),
        )
    except Exception as exc:  # noqa: BLE001 — the brief omits writers
        log.warning("mentor top writers read failed: %s", exc)
        return []


async def _resolve_agent(inp: MentorBriefInput) -> tuple[str, list[str], bool]:
    """(agent, writers, defaulted): the named or calling agent when it is a
    real one; otherwise the namespace's most active writer, and a flag that
    says the brief chose it."""
    given = _caller_agent(inp)
    writers = await _top_writers(inp.org, inp.namespace, int(inp.window_seconds))
    if given not in ANONYMOUS_AGENTS:
        return given, writers, False
    if writers:
        return writers[0], writers, True
    return given or "-", writers, False


def _planes(profile: mentor_rules.MentorProfile) -> list[MentorPlane]:
    calls: dict[str, int] = {}
    for op, n in profile.usage.items():
        calls[mentor_rules.plane_of(op)] = calls.get(mentor_rules.plane_of(op), 0) + int(n)
    f = profile.facts
    avail: dict[str, bool | None] = {
        "code index": (int(f["repos"]) > 0) if "repos" in f else None,
        "tracker": bool(f["tracker_sources"]) if "tracker_sources" in f else None,
        "documents": (int(f["documents"]) > 0) if "documents" in f else None,
        "records": (int(f["records"]) > 0) if "records" in f else None,
        "graph": (int(f["entity_rows"]) > 0) if "entity_rows" in f else None,
        "conflicts": (int(f["conflicted_rows"]) > 0) if "conflicted_rows" in f else None,
    }
    out: list[MentorPlane] = []
    seen: set[str] = set()
    for _, plane, when, _ in mentor_rules.PLANES:
        if plane in seen:
            continue
        seen.add(plane)
        out.append(
            MentorPlane(
                plane=plane,
                calls=calls.get(plane, 0),
                available=avail.get(plane, True),
                when=when,
                mentored=mentor_rules.plane_mentored(plane),
            )
        )
    return out


async def _adoption(profile: mentor_rules.MentorProfile) -> list[MentorAdoption]:
    """Sessions where a hint of each code reached this agent, and how many
    then called one of the hinted ops later in the same session — ANY call
    after the hint, not the session's first (an agent that read by key
    before a `duplicates_written` hint and again after it followed it).
    Only client-named sessions count: the placeholder ids ('-', '*') a
    client sends when it names none are one bucket, not a session.
    Two bounded reads; a failure is an empty list."""
    ch = get_client()
    who = (profile.org, profile.namespace, profile.agent, int(profile.window_seconds))
    try:
        shown = await mentor_store.hints_shown(*who, cap=ADOPTION_ROW_CAP, ch=ch)
    except Exception as exc:  # noqa: BLE001 — the brief omits adoption
        log.warning("mentor adoption read failed: %s", exc)
        return []
    if not shown:
        return []
    sessions = sorted({str(r["session_id"]) for r in shown})
    try:
        later = await mentor_store.last_calls(*who, sessions=sessions, ch=ch)
    except Exception as exc:  # noqa: BLE001 — the brief omits adoption
        log.warning("mentor adoption follow-up read failed: %s", exc)
        return []
    last_call: dict[tuple[str, str], Any] = {
        (str(r["session_id"]), str(r["operation"])): r["last_ts"] for r in later
    }
    ops_for: dict[str, tuple[str, ...]] = dict(CALL_HINT_OPS)
    for h in profile.hints:
        ops_for.setdefault(h.code, h.ops)
    per_code: dict[str, list[int]] = {}
    for r in shown:
        code = str(r["code"])
        sid = str(r["session_id"])
        hinted_at = r["first_ts"]
        followed = any(
            (sid, op) in last_call and last_call[(sid, op)] > hinted_at
            for op in ops_for.get(code, ())
        )
        cell = per_code.setdefault(code, [0, 0])
        cell[0] += 1
        cell[1] += 1 if followed else 0
    return [
        MentorAdoption(code=code, shown_sessions=v[0], followed_sessions=v[1])
        for code, v in sorted(per_code.items(), key=lambda kv: (-kv[1][0], kv[0]))
    ]


async def handle_mentor_brief(inp: MentorBriefInput) -> MentorBriefOutput:
    agent, writers, defaulted = await _resolve_agent(inp)
    profile = await svc.mentor_profile(
        inp.org, inp.namespace, agent, window_seconds=int(inp.window_seconds)
    )
    if profile is None:
        # The mentor is off (TTL 0) or the read failed and is remembered:
        # an honest empty brief, not an error.
        return MentorBriefOutput(
            org=inp.org,
            namespace=inp.namespace,
            agent=agent,
            window_seconds=int(inp.window_seconds),
            computed_at=datetime.now(UTC).replace(tzinfo=None),
            sessions=0,
            agents=writers,
            agent_defaulted=defaulted,
            skipped=["profile: unavailable"],
            healthy=True,
        )
    hints = [
        MentorHint(
            code=h.code,
            severity=h.severity,
            text=h.text,
            ops=list(h.ops),
            evidence=dict(h.evidence),
        )
        for h in profile.hints
    ]
    return MentorBriefOutput(
        org=profile.org,
        namespace=profile.namespace,
        agent=profile.agent,
        window_seconds=profile.window_seconds,
        computed_at=profile.computed_at,
        sessions=profile.sessions,
        usage=dict(sorted(profile.usage.items(), key=lambda kv: (-kv[1], kv[0]))),
        planes=_planes(profile),
        hints=hints,
        adoption=await _adoption(profile),
        agents=writers,
        agent_defaulted=defaulted,
        skipped=list(profile.skipped),
        healthy=not any(h.severity in ("warning", "critical") for h in profile.hints),
    )


def _defaulted_note(out: MentorBriefOutput) -> str:
    if not out.agent_defaulted:
        return ""
    others = ", ".join(out.agents[1:])
    return f" (most active writer{'; others: ' + others if others else ''})"


def render_table(out: MentorBriefOutput) -> str:
    """The CLI's view: usage, planes, hints, adoption."""
    lines = [
        f"mentor.brief {out.org}/{out.namespace} agent={out.agent}"
        f"{_defaulted_note(out)} "
        f"window={out.window_seconds // 86400}d "
        f"sessions={out.sessions} healthy={'yes' if out.healthy else 'no'}",
        "",
        "plane            calls  available  when",
    ]
    for p in out.planes:
        av = "-" if p.available is None else ("yes" if p.available else "no")
        lines.append(f"{p.plane:<16} {p.calls:>5}  {av:<9}  {p.when}")
    lines.append("")
    if not out.hints:
        lines.append(
            "hints: none — every plane the namespace offers is in use, or the window is quiet"
        )
    else:
        lines.append("code                      severity  text")
        for h in out.hints:
            lines.append(f"{h.code:<25} {h.severity:<9} {h.text}")
            ev = ", ".join(f"{k}={v}" for k, v in h.evidence.items())
            if ev:
                lines.append(f"{'':<25} {'':<9} evidence: {ev}")
    if out.adoption:
        lines.append("")
        lines.append("adoption (sessions shown → followed)")
        for a in out.adoption:
            lines.append(f"  {a.code:<25} {a.shown_sessions:>4} → {a.followed_sessions}")
    if out.skipped:
        lines.append("")
        lines.append("skipped: " + "; ".join(out.skipped))
    return "\n".join(lines)
