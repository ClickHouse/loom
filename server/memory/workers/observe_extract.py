"""Deferred-extraction worker for the ambient-capture (observe) plane.

`observe.record` writes raw tool-trace observations into the episodic plane
with no LLM/embedding. `observe.crystallize` tags a session's observations
READY (`crystallize:ready`). This worker picks up READY rows per session,
replays their narratives through `memory.set_from_messages` (LLM extraction
+ embedding + conflict-surfacing + blacklist), and rewrites each source row
`crystallize:done` so the next pass skips it — idempotent. A row deleted or
given new text during the replay is left as it now is.

Off by default (`LOOM_OBSERVE_EXTRACT_ENABLED`); requires LLM credentials.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.llm import customer
from server.memory.app.observe_replay import _extract_replay, replay_messages
from server.memory.domain.observation_tags import (
    CRYSTALLIZE_DONE_TAG,
    CRYSTALLIZE_READY_TAG,
    OBSERVATION_TAG,
)
from server.memory.domain.sensitivity import value_reads_high
from server.memory.domain.trust import subject_of
from server.memory.store.rows import next_version
from server.memory.store.sql import _identity_key
from server.settings import settings
from server.workers.status import record_pass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ObserveExtractConfig:
    # Max sessions distilled per pass (each session = one extraction call).
    max_sessions_per_pass: int = 10
    # Max observations replayed per session into one extraction call.
    max_observations_per_session: int = 100
    # Optional scope filters — tests pass these to keep the scan
    # deterministic under shared CH state; production runs unscoped.
    org_filter: str | None = None
    namespace_filter: str | None = None
    # Buffer threshold: None = server setting; 0 disables.
    buffer_chars: int | None = None
    buffer_min_observations: int | None = None


@dataclass(frozen=True, slots=True)
class DistilledSession:
    """What one session's distillation wrote."""

    org: str
    namespace: str
    session_id: str
    observations: int
    written: int
    events: int
    notes: int
    extra_failures: tuple[str, ...]


@dataclass
class ObserveExtractResult:
    distilled_sessions: list[DistilledSession] = field(default_factory=list)
    written_memories: int = 0
    observations_consumed: int = 0
    failures: list[str] = field(default_factory=list)
    # The event and note pools written next to the facts.
    events_written: int = 0

    @property
    def distilled_count(self) -> int:
        return len(self.distilled_sessions)


async def find_ready_sessions(
    cfg: ObserveExtractConfig,
    ch: Any | None = None,
) -> list[dict[str, Any]]:
    """Return the (org, namespace, agent, user_id, session_id) tuples that
    have at least one READY-but-not-DONE episodic observation, newest
    activity first. One tuple = one extraction batch.

    FINAL collapses the RMT so the tag state read is the latest version.
    """
    from server.core.ch import get_client

    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {
        "obs_tag": OBSERVATION_TAG,
        "ready_tag": CRYSTALLIZE_READY_TAG,
        "done_tag": CRYSTALLIZE_DONE_TAG,
        "lim": cfg.max_sessions_per_pass,
    }
    if cfg.org_filter is not None:
        scope += " AND org = {org_f:String}"
        params["org_f"] = cfg.org_filter
    if cfg.namespace_filter is not None:
        scope += " AND namespace = {ns_f:String}"
        params["ns_f"] = cfg.namespace_filter
    # An org whose model calls are switched off waits, and takes no slot.
    scope += " AND NOT has({off:Array(String)}, org)"
    params["off"] = sorted(await customer.switched_off_orgs())
    rows = await ch.query_all(
        f"""
        SELECT org, namespace, agent, user_id, session_id,
               count() AS n, max(created_at) AS last_at
        FROM loom.memory FINAL
        WHERE memory_type = 'episodic'
          AND has(tags, {{obs_tag:String}})
          AND has(tags, {{ready_tag:String}})
          AND NOT has(tags, {{done_tag:String}})
          AND tombstoned_at IS NULL
          {scope}
        GROUP BY org, namespace, agent, user_id, session_id
        ORDER BY last_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    out = [{**dict(r), "trigger": "ready"} for r in rows]

    # Buffer threshold: sessions whose un-distilled
    # observations have grown past `buffer_chars` are distilled without an
    # explicit READY, the way a short-term buffer flushes when full. Sessions
    # already listed above are not repeated.
    from server.settings import settings

    buf_chars = (
        cfg.buffer_chars if cfg.buffer_chars is not None else settings.observe_extract_buffer_chars
    )
    buf_min = (
        cfg.buffer_min_observations
        if cfg.buffer_min_observations is not None
        else settings.observe_extract_buffer_min_observations
    )
    if buf_chars and buf_chars > 0 and len(out) < cfg.max_sessions_per_pass:
        seen = {(r["org"], r["namespace"], r["agent"], r["user_id"], r["session_id"]) for r in out}
        params2 = {**params, "buf_chars": int(buf_chars), "buf_min": int(max(1, buf_min))}
        params2.pop("ready_tag", None)
        more = await ch.query_all(
            f"""
            SELECT org, namespace, agent, user_id, session_id,
                   count() AS n, max(created_at) AS last_at,
                   sum(length(value)) AS chars
            FROM loom.memory FINAL
            WHERE memory_type = 'episodic'
              AND has(tags, {{obs_tag:String}})
              AND NOT has(tags, {{done_tag:String}})
              AND tombstoned_at IS NULL
              {scope}
            GROUP BY org, namespace, agent, user_id, session_id
            HAVING chars >= {{buf_chars:UInt64}} AND n >= {{buf_min:UInt32}}
            ORDER BY chars DESC
            LIMIT {{lim:UInt32}}
            """,
            params=params2,
        )
        for r in more:
            key = (r["org"], r["namespace"], r["agent"], r["user_id"], r["session_id"])
            if key in seen or len(out) >= cfg.max_sessions_per_pass:
                continue
            seen.add(key)
            out.append({**dict(r), "trigger": "buffer"})
    return out


async def _load_session_observations(
    session: dict[str, Any],
    cfg: ObserveExtractConfig,
    ch: Any,
) -> list[Any]:
    """Full-projection rows for one session's READY observations, oldest
    first (chronological replay order). The DONE rewrite re-reads each head
    before it writes; the `content_sha256` read here is what it compares against."""
    import server.memory.store as memory_repo

    rows = await ch.query_all(
        f"""{memory_repo._MEMORY_FINAL_PROJECTION}
        WHERE org = {{org:String}}
          AND namespace = {{ns:String}}
          AND agent = {{agent:String}}
          AND user_id = {{uid:String}}
          AND session_id = {{sid:String}}
          AND memory_type = 'episodic'
          AND has(tags, {{obs_tag:String}})
          AND (has(tags, {{ready_tag:String}}) OR {{buffer_trigger:UInt8}} = 1)
          AND NOT has(tags, {{done_tag:String}})
          AND tombstoned_at IS NULL
        ORDER BY created_at ASC
        LIMIT {{lim:UInt32}}
        """,
        params={
            "org": session["org"],
            "ns": session["namespace"],
            "agent": session["agent"],
            "uid": session["user_id"],
            "sid": session["session_id"],
            "obs_tag": OBSERVATION_TAG,
            "ready_tag": CRYSTALLIZE_READY_TAG,
            "done_tag": CRYSTALLIZE_DONE_TAG,
            "lim": cfg.max_observations_per_session,
            # A buffer-triggered session distils every un-done observation,
            # READY-tagged or not.
            "buffer_trigger": 1 if session.get("trigger") == "buffer" else 0,
        },
    )
    return [memory_repo._hydrate(r) for r in rows]


def _writer(observations: list[Any]) -> str:
    """The newest principal whose observations can be sent to the model."""
    for o in sorted(observations, key=lambda o: int(o.version), reverse=True):
        if _sendable(o.sensitivity, o.value):
            if subject := subject_of(str(getattr(o, "actor", "") or "")):
                return subject
    return ""


async def _distill_session(
    session: dict[str, Any],
    observations: list[Any],
) -> DistilledSession:
    """Replay one session's observations through memory.set_from_messages
    (one synthetic `user` turn per narrative) and mark the sources DONE, except a
    source deleted or given new text since it was read."""
    import server.memory.store as memory_repo

    # An observation that holds a high shape is never sent to the model, labelled or not:
    # capture writes it unlabelled and only the sweep labels it later. It is still marked
    # DONE below.
    readable = [o for o in observations if _sendable(o.sensitivity, o.value)]
    msgs = replay_messages(readable, agent=str(session.get("agent") or ""))
    # observation_date anchor: use the newest valid_at the capture path
    # stamped, if any, so relative time refs resolve.
    observation_date = ""
    for o in observations:
        va = getattr(o, "valid_at", None)
        if va is not None:
            observation_date = va.date().isoformat()

    written = await _extract_replay(session, msgs, observation_date)
    # The transition events and the two session notes, from the same observations,
    # before they are marked DONE. Fail-open: the facts are already written, and a
    # failed note is a count, not a lost pass.
    # A call the switch holds (`customer.HOLDS`: switched off since the session was
    # picked, or a copy too old to read) propagates instead: the sources stay
    # un-DONE and the whole session is distilled again once the org is back on.
    # Its events and hints are keyed by the session and its facts deduplicated by
    # hash, so the rerun replaces them rather than adding copies.
    context = await _load_session_context(session, ch=get_client_for_context())
    extras = await _distill_extras(session, readable, context=context)

    # Mark sources DONE from the head as it is now: a key deleted or rewritten during the
    # replay is skipped. The stamp is drawn first so a later write wins.
    from server.core.ch import get_client

    ch = get_client()
    version = next_version()
    keys = [_identity_key(vars(o)) for o in observations]
    heads = await memory_repo.memory_rows_by_identity_keys(keys, ch=ch, sequential=True)
    version_rows = []
    for key, existing in zip(keys, observations, strict=True):
        head = heads.get(key)
        if head is None or head.content_sha256 != existing.content_sha256:
            continue
        new_tags = [t for t in (head.tags or []) if t != CRYSTALLIZE_READY_TAG]
        if CRYSTALLIZE_DONE_TAG not in new_tags:
            new_tags.append(CRYSTALLIZE_DONE_TAG)
        # a head written after the stamp is the one this row copies, so outrank it
        row_version = max(version, int(head.version) + 1)
        version_rows.append(
            memory_repo.build_memory_version_row(head, version=row_version, tags=new_tags)
        )
    # One committed insert for the session's observations.
    await memory_repo.insert_memory_versions(version_rows, ch=ch)

    return DistilledSession(
        org=str(session["org"]),
        namespace=str(session["namespace"]),
        session_id=str(session["session_id"]),
        observations=len(version_rows),
        written=written,
        events=int(extras.events_written),
        notes=int(extras.procedure_written) + int(extras.hint_written),
        extra_failures=tuple(extras.failures),
    )


def get_client_for_context() -> Any:
    from server.core.ch import get_client

    return get_client()


async def _load_session_context(session: dict[str, Any], *, ch: Any) -> list[Any]:
    """Every observation of the session so far — the ones earlier passes
    already distilled and the ones this pass is about to — as
    `trajectory_notes.Observation`s, oldest first, bounded.
    Content-less rows (`skipped:*`) are left out. [] when the read fails;
    the caller then falls back to this pass's rows."""
    from types import SimpleNamespace

    from server.memory.app import trajectory_notes as tn

    try:
        rows = await ch.query_all(
            """
            SELECT memory_key, value, created_at, valid_at
            FROM loom.memory FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND agent = {agent:String} AND user_id = {uid:String}
              AND session_id = {sid:String}
              AND memory_type = 'episodic' AND has(tags, {obs_tag:String})
              AND NOT arrayExists(t -> startsWith(t, 'skipped:'), tags)
              AND sensitivity != 'high'
              AND tombstoned_at IS NULL
            ORDER BY created_at ASC
            LIMIT {lim:UInt32}
            """,
            params={
                "org": session["org"],
                "ns": session["namespace"],
                "agent": session["agent"],
                "uid": session["user_id"],
                "sid": session["session_id"],
                "obs_tag": OBSERVATION_TAG,
                "lim": int(settings.memory_observe_extract_session_context_limit),
            },
        )
    except Exception as e:  # noqa: BLE001 — the session distills without context
        log.debug("session context read skipped: %s", e)
        return []
    return [
        tn.observation_from_row(SimpleNamespace(**dict(r)))
        for r in rows or []
        if _sendable("", r.get("value"))  # the statement already left out labelled rows
    ]


def _sendable(sensitivity: str, value: Any) -> bool:
    """Whether an observation may go to the model: neither labelled 'high' nor reading as
    high, whatever level its writer set."""
    return sensitivity != "high" and not value_reads_high(value)


async def _distill_extras(
    session: dict[str, Any], observations: list[Any], *, context: list[Any] | None = None
) -> Any:
    """Events + notes for one session; a missing model or any error leaves an
    empty result, except a call the switch holds (`customer.HOLDS`), which
    propagates so the session is not marked done. With `context` the events
    are numbered over, and the notes written from, the WHOLE session so far;
    only this pass's observations get new events."""
    from server.memory.app import trajectory_notes as tn
    from server.settings import settings

    res = tn.ExtrasResult()
    obs = [tn.observation_from_row(o) for o in observations]
    if all(o.trivial for o in obs):
        return res  # tool names only: nothing to learn from
    new_keys = {o.memory_key for o in obs}
    only_after: set[str] | None = None
    if context:
        known = {o.memory_key for o in context}
        # this pass's rows may not be visible to the context read yet
        obs = list(context) + [o for o in obs if o.memory_key not in known]
        only_after = new_keys
    try:
        from server.llm.client import get_llm_client

        llm = get_llm_client()
    except Exception as e:  # noqa: BLE001 — recorded as this session's failure, never raised
        res.failures.append(f"llm: {type(e).__name__}: {e}")
        return res
    await tn.distill_events(
        session,
        obs,
        llm=llm,
        max_events=int(settings.observe_extract_max_events_per_session),
        res=res,
        only_after=only_after,
    )
    await tn.distill_notes(session, obs, llm=llm, res=res)
    return res


async def run_pass(
    cfg: ObserveExtractConfig | None = None,
) -> ObserveExtractResult:
    """One scan-and-distill pass. Picks up READY observation sessions and
    runs the deferred extraction for each. Returns the result for
    logging/metrics."""
    from server.core.ch import get_client

    cfg = cfg or ObserveExtractConfig()
    result = ObserveExtractResult()
    ch = get_client()

    with record_pass("observe_extract") as rec:
        sessions = await find_ready_sessions(cfg, ch=ch)
        for session in sessions:
            try:
                observations = await _load_session_observations(session, cfg, ch)
                if not observations:
                    continue
                with customer.for_org(
                    str(session["org"]),
                    namespace=str(session["namespace"]),
                    subject=_writer(observations),
                    user_id=str(session.get("user_id") or ""),
                ):
                    distilled = await _distill_session(session, observations)
                result.distilled_sessions.append(distilled)
                result.written_memories += distilled.written
                result.observations_consumed += distilled.observations
                result.events_written += distilled.events
                result.failures.extend(distilled.extra_failures)
            except Exception as e:
                log.exception(
                    "observe_extract failed for %s/%s/%s",
                    session.get("org"),
                    session.get("namespace"),
                    session.get("session_id"),
                )
                result.failures.append(f"{type(e).__name__}: {e}")
                # Keep going — one bad session shouldn't stop the pass.
        rec.outcome = {
            "distilled_sessions": result.distilled_count,
            "written_memories": result.written_memories,
            "observations_consumed": result.observations_consumed,
            "failed": len(result.failures),
        }

    if result.distilled_count or result.failures:
        log.info(
            "observe_extract: distilled=%d sessions, %d memories, %d observations done, %d failed",
            result.distilled_count,
            result.written_memories,
            result.observations_consumed,
            len(result.failures),
        )
    return result


__all__ = [
    "ObserveExtractConfig",
    "ObserveExtractResult",
    "find_ready_sessions",
    "run_pass",
]
