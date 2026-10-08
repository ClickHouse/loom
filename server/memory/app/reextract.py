"""Re-extract a namespace's memories from its raw evidence.

`loom reextract` reads the message rows `memory.set_from_messages` left in
`loom.memory_evidence` (one row per turn, session-grouped, ordered by the
turn index, dated by `observed_at`), rebuilds each session's
request, and replays it through the live write path of a running server into
a TARGET namespace. The source namespace is never written. The target is a
fresh store built by the current extractor — a new writer model, a sweep
revision, a prompt fix — that `bench/k_curve.py --reuse` can compare with the
source before an operator swaps them. Raw histories are the only source such a
rebuild can start from.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import httpx

_EVIDENCE_SQL = """
    SELECT session_id, agent, user_id, role, content, observed_at,
           toInt32OrNull(JSONExtractString(metadata, 'index')) AS idx
    FROM loom.memory_evidence
    WHERE org = {org:String} AND namespace = {ns:String}
      AND source_type = 'message' AND tombstoned_at IS NULL
      AND JSONExtractString(metadata, 'operation') = 'memory.set_from_messages'
    ORDER BY session_id, idx, observed_at
"""


@dataclass
class Session:
    session_id: str
    agent: str
    user_id: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    observed: list[datetime] = field(default_factory=list)

    @property
    def observation_date(self) -> str:
        """The session's day: the earliest turn's `observed_at` (the session
        date, or the turn's own timestamp)."""
        return min(self.observed).date().isoformat() if self.observed else ""

    @property
    def dated_by_ingest(self) -> bool:
        """Rows written before session dating carry the ingest minute, not the
        session day: every turn stamped inside one minute, none at midnight.
        Such a session's date is unknown; the replay sends none."""
        if not self.observed:
            return True
        if any(t.hour == 0 and t.minute == 0 and t.second == 0 for t in self.observed):
            return False
        return (max(self.observed) - min(self.observed)).total_seconds() < 120


def sessions_from_rows(rows: list[dict[str, Any]]) -> list[Session]:
    """Group evidence rows into sessions in turn order. A row without an index
    keeps its query order. A turn's own timestamp is carried
    when it is not the session-day midnight. Pure."""
    by_id: dict[str, Session] = {}
    for r in rows:
        sid = str(r.get("session_id") or "-")
        s = by_id.get(sid)
        if s is None:
            s = by_id[sid] = Session(sid, str(r.get("agent") or "-"), str(r.get("user_id") or "-"))
        content = str(r.get("content") or "")
        if not content.strip():
            continue
        msg: dict[str, Any] = {"role": str(r.get("role") or "user"), "content": content}
        at = r.get("observed_at")
        if isinstance(at, datetime):
            s.observed.append(at)
            if (at.hour, at.minute, at.second) != (0, 0, 0):
                msg["timestamp"] = at.isoformat()
        s.messages.append(msg)
    return [s for s in by_id.values() if s.messages]


def request_body(
    s: Session, *, org: str, into: str, dates: str = "auto", max_messages: int = 400
) -> list[dict[str, Any]]:
    """The `memory.set_from_messages` request(s) for one session, cut into
    400-message calls (the op's ceiling). `dates="auto"` sends the session
    day unless the rows are ingest-dated; `"none"` never sends one."""
    date = "" if dates == "none" or s.dated_by_ingest else s.observation_date
    # An ingest-minute stamp is not the turn's moment: undated sessions send
    # bare messages (sessions_from_rows keeps a timestamp only off midnight).
    msgs = (
        [{k: v for k, v in m.items() if k != "timestamp"} for m in s.messages]
        if not date
        else s.messages
    )
    out: list[dict[str, Any]] = []
    for i in range(0, len(msgs), max_messages):
        body: dict[str, Any] = {
            "org": org,
            "namespace": into,
            "agent": s.agent,
            "session_id": s.session_id,
            "messages": msgs[i : i + max_messages],
        }
        if s.user_id and s.user_id != "-":
            body["user_id"] = s.user_id
        if date:
            body["observation_date"] = date
        out.append(body)
    return out


@dataclass
class Report:
    sessions: int = 0
    messages: int = 0
    calls: int = 0
    failed: int = 0
    undated: int = 0
    errors: list[str] = field(default_factory=list)


async def load_sessions(ch: Any, *, org: str, namespace: str) -> list[Session]:
    rows = await ch.query_all(_EVIDENCE_SQL, params={"org": org, "ns": namespace})
    return sessions_from_rows(list(rows or []))


async def replay(
    sessions: list[Session],
    *,
    base_url: str,
    token: str,
    org: str,
    into: str,
    dates: str = "auto",
    concurrency: int = 2,
    dry_run: bool = False,
    client: Any | None = None,
    log: Any = None,
) -> Report:
    """POST every session's request to `base_url`. One failure fails that
    session only (counted, named); the rest continue."""
    log = log or (lambda s: print(s, file=sys.stderr, flush=True))
    rep = Report()
    sem = asyncio.Semaphore(max(1, concurrency))
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    async def one(cl: Any, s: Session) -> None:
        bodies = request_body(s, org=org, into=into, dates=dates)
        rep.sessions += 1
        rep.messages += len(s.messages)
        if not bodies or "observation_date" not in bodies[0]:
            rep.undated += 1
        for body in bodies:
            rep.calls += 1
            if dry_run:
                log(
                    f"  dry-run {s.session_id}: {len(body['messages'])} messages, "
                    f"date {body.get('observation_date') or '-'}"
                )
                continue
            try:
                async with sem:
                    r = await cl.post(
                        base_url + "/v1/memory.set_from_messages",
                        json=body,
                        headers=headers,
                        timeout=600,
                    )
                if r.status_code >= 400:
                    rep.failed += 1
                    rep.errors.append(f"{s.session_id}: HTTP {r.status_code} {r.text[:160]}")
            except Exception as e:  # noqa: BLE001 — one session, not the replay
                rep.failed += 1
                rep.errors.append(f"{s.session_id}: {type(e).__name__}: {e}")

    if client is not None:
        await asyncio.gather(*(one(client, s) for s in sessions))
    else:
        async with httpx.AsyncClient() as cl:
            await asyncio.gather(*(one(cl, s) for s in sessions))
    return rep


def render_report(rep: Report, *, source: str, into: str, dry_run: bool) -> str:
    head = "would replay" if dry_run else "replayed"
    lines = [
        f"{head} {rep.sessions} session(s), {rep.messages} message(s), {rep.calls} call(s) "
        f"from {source} into {into}; failed {rep.failed}; undated {rep.undated}"
    ]
    if rep.undated:
        lines.append(
            "  undated sessions carry ingest-time evidence (rows written before session dating): "
            "their facts are extracted without a session date"
        )
    lines += [f"  ✗ {e}" for e in rep.errors[:20]]
    return "\n".join(lines)
