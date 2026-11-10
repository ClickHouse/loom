"""Handlers for session.brief_*.

`session.brief_save` reads the stored brief, revises it with the new events
(one model call per condensed window, or the deterministic builder) or with
sections the agent wrote, and writes the next revision. The revisions table
records it through the materialized view on the same INSERT.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now
from server.core.errors import BackendUnavailableError
from server.core.naming import named
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.llm import customer
from server.memory.api.models_session_brief import (
    SessionBriefDeleteInput,
    SessionBriefDeleteOutput,
    SessionBriefGetInput,
    SessionBriefGetOutput,
    SessionBriefListInput,
    SessionBriefListOutput,
    SessionBriefSaveInput,
    SessionBriefSaveOutput,
    SessionBriefStats,
    SessionBriefSummary,
)
from server.memory.domain import session_brief as sb
from server.memory.domain import session_brief_render as sb_render
from server.memory.domain.extraction_policy import with_policy
from server.memory.store import session_briefs as repo
from server.observability.budget_shape import note_budget_shape
from server.observability.genai import staged
from server.settings import settings
from server.settings.budgets import llm_max_tokens

log = logging.getLogger(__name__)

_MAX_SOURCE_SESSIONS = 20


def resolve_key(inp: Any) -> str:
    key = (inp.brief_key or "").strip()
    if key:
        return key
    if inp.project or inp.branch:
        return sb.auto_brief_key(inp.project, inp.branch)
    sid = named((inp.session_id or "").strip())
    if sid:
        return f"session/{sid}"[:160]
    raise HTTPException(
        status_code=422,
        detail={
            "code": "BRIEF_KEY_REQUIRED",
            "message": "session.brief_save needs brief_key, or project/branch, or session_id",
        },
    )


def _age(ts: Any) -> int:
    if not isinstance(ts, datetime):
        return 0
    t = ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    return max(0, int((datetime.now(UTC) - t).total_seconds()))


def _iso(ts: Any) -> str:
    return ts.strftime("%Y-%m-%d %H:%MZ") if isinstance(ts, datetime) else ""


@dataclass
class _Revision:
    sections: sb.BriefSections
    author: str
    windows: int = 0
    dropped: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    llm_model: str = ""


def _use_llm(inp: Any) -> tuple[bool, Any]:
    """(use the model, client). The fake provider never runs: its echo is
    not a brief, and the deterministic builder is the honest fallback."""
    wanted = inp.use_llm if inp.use_llm is not None else bool(settings.session_brief_llm)
    if not wanted:
        return False, None
    from server.llm.client import get_llm_client_for_model

    # The brief model first (a keyless provider resolves to the fake client),
    # then the deployment's LLM; the deterministic builder when neither is real.
    for model in dict.fromkeys([settings.session_brief_llm_model or None, None]):
        client = get_llm_client_for_model(model)
        if str(getattr(client, "provider", "fake")) != "fake":
            return True, client
    return False, None


def _context_line(inp: Any) -> str:
    bits = [
        f"project {inp.project}" if inp.project else "",
        f"branch {inp.branch}" if inp.branch else "",
        f"head {inp.head_sha[:12]}" if inp.head_sha else "",
    ]
    bits = [b for b in bits if b]
    return ("Session context: " + ", ".join(bits) + ".") if bits else ""


@staged("session_brief")
async def _revise_window(
    client: Any,
    prev: sb.BriefSections | None,
    events: list[sb.BriefEvent],
    *,
    index: int,
    total: int,
    budget: int,
    context_line: str,
    operator_policy: str,
) -> tuple[sb.BriefSections, int, int, int, bool]:
    """One window through the model. Returns (merged sections, dropped,
    tokens_in, tokens_out, used_model). A failed or unparseable call falls
    back to the deterministic builder for this window only, or, under an
    operator's policy, keeps `prev`: the builder copies the raw events. Under a
    policy, a call for an org whose model calls are switched off raises
    `customer.ModelCallsDisabled`."""
    condensed, _stats = sb.condense_events(events, budget)

    def fallback() -> sb.BriefSections:
        if operator_policy:
            return prev or sb.BriefSections()
        return sb.merge_sections(prev, sb.deterministic_revision(events))

    try:
        result = await client.complete(
            system=with_policy(sb.SYSTEM_PROMPT, operator_policy),
            user=sb.user_prompt(
                prev, condensed, window=index, windows=total, context_line=context_line
            ),
            max_tokens=llm_max_tokens("session_brief_revision"),
            response_format={"type": "json_object"},
        )
    except Exception as e:  # a failed call costs this window only
        if operator_policy and isinstance(e, customer.ModelCallsDisabled):
            raise  # a switched-off org stops the save under a policy
        log.warning(
            "session.brief_save: model call failed (%s); window revised without the model",
            e,
        )
        return fallback(), 0, 0, 0, False
    try:
        parsed = sb.parse_revision(result.text)
    except Exception as e:  # noqa: BLE001 — a rejected reply costs this window only
        log.warning(
            "session.brief_save: model reply rejected (%s); window revised without the model", e
        )
        parsed = None
    if parsed is None:
        log.warning(
            "session.brief_save: unparseable model output; window revised without the model"
        )
        return (
            fallback(),
            0,
            int(result.tokens_in),
            int(result.tokens_out),
            False,
        )
    grounded, dropped = sb.ground_sections(
        parsed.state,
        sb_render.evidence_text(prev, events, condensed),
        sb.rule_sources(prev, events),
    )
    parsed.state = grounded
    merged = sb.merge_sections(prev, parsed)
    return merged, dropped, int(result.tokens_in), int(result.tokens_out), True


async def _revise_from_events(
    inp: Any, prev: sb.BriefSections | None, policy: str
) -> _Revision | None:
    """The brief revised with `inp.events`. Under an operator's `policy` the
    deterministic builder, which copies the raw events, never runs: with no
    model to call (no real model, `use_llm` false, or the org's model calls
    switched off before the first window was revised) the answer is None and
    nothing is written. An org switched off later keeps the windows already
    revised and stops there, like a failed window. When every window's call
    failed, was held by an unread switch or did not parse, it raises
    BackendUnavailableError: a save of events alone is refused (503) so the
    client sends them again, and one with the agent's sections saves those."""
    budget = int(settings.session_brief_window_chars)
    windows = sb.split_windows(
        list(inp.events), budget * 3, max(1, int(settings.session_brief_max_windows))
    )
    use, client = _use_llm(inp)
    if not use:
        if policy:
            return None
        cur = prev
        for w in windows:
            cur = sb.merge_sections(cur, sb.deterministic_revision(w))
        return _Revision(
            sections=cur or sb.BriefSections(), author="deterministic", windows=len(windows)
        )
    note_budget_shape("model")
    rev = _Revision(
        sections=prev or sb.BriefSections(),
        author="llm",
        windows=len(windows),
        llm_model=str(getattr(client, "model", "")),
    )
    cur = prev
    any_model = False
    ctx = _context_line(inp)
    for i, w in enumerate(windows, 1):
        try:
            cur, dropped, t_in, t_out, used = await _revise_window(
                client,
                cur,
                w,
                index=i,
                total=len(windows),
                budget=budget,
                context_line=ctx,
                operator_policy=policy,
            )
        except customer.ModelCallsDisabled:  # raised only under a policy
            if not any_model:
                return None
            break
        rev.dropped += dropped
        rev.tokens_in += t_in
        rev.tokens_out += t_out
        any_model = any_model or used
    if not any_model and policy:
        raise BackendUnavailableError(
            "the brief could not be revised under the operator's extraction policy; try again"
        )
    rev.sections = cur or sb.BriefSections()
    if not any_model:
        rev.author = "deterministic"
    return rev


def _conflict(stored: int, wanted: int) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail={
            "code": "BRIEF_REVISION_MISMATCH",
            "message": f"brief is at revision {stored}, not {wanted}; re-read and retry",
            "revision": stored,
        },
    )


# Saves the client makes on its own. They follow the namespace owner's Record
# conversations switch: a brief is condensed from the transcript, so with recording off an
# automatic save stores nothing. A save the person asked for ("save this session") is
# their own act, like memory.set.
_AUTOMATIC_TRIGGERS = frozenset({"precompact", "session_end", "stop"})


async def _recording_off(inp: Any) -> bool:
    if inp.trigger not in _AUTOMATIC_TRIGGERS:
        return False
    from server.knowledge.app.conversations import namespace_recording_enabled

    return not await namespace_recording_enabled(inp.org, inp.namespace)


# A brief key is one row per namespace. A token bounded to one user (or session) stores
# its briefs under a prefix naming that owner, so two users saving the same key get two
# briefs, and neither can see or overwrite the other's. A namespace-wide token uses the
# key as given, and may name a stored `~<user>~…` key as session.brief_list shows it.
def _key_prefix(scope: ReadFilters) -> str:
    if not scope.narrowed:
        return ""
    session = scope.session_id if scope.scope == "own_session" else None
    return sb.owner_key_prefix(scope.user_id or "", session)


def _stored_key(key: str, scope: ReadFilters) -> str:
    if key.startswith("~") and scope.narrowed:
        raise HTTPException(status_code=422, detail={"code": "BAD_BRIEF_KEY", "message": key})
    return _key_prefix(scope) + key


def _shown_key(stored: str, scope: ReadFilters) -> str:
    return stored.removeprefix(_key_prefix(scope))


def _row_owner(inp: Any, key: str, live: dict[str, Any] | None) -> tuple[str, str]:
    """The user_id / session_id the saved row carries. A namespace-wide save to an
    owner's `~<user>~…` key keeps that owner, so the owner's own reads still find it."""
    owner = sb.key_owner(key)
    if owner is None:
        return inp.user_id, inp.session_id
    user, session = owner
    if session is None:
        session = str(live["session_id"]) if live else "-"
    return user, session


async def handle_session_brief_save(inp: SessionBriefSaveInput) -> SessionBriefSaveOutput:
    from server.memory.app.provenance import writer_actor

    key = resolve_key(inp)
    if not sb.stored_key_ok(key):
        raise HTTPException(status_code=422, detail={"code": "BAD_BRIEF_KEY", "message": key})
    ch = get_client()
    stored_key = _stored_key(key, request_scope())
    # An owner prefix built from long ids could store a key get and delete refuse.
    if not sb.stored_key_ok(stored_key):
        raise HTTPException(
            status_code=422,
            detail={"code": "BAD_BRIEF_KEY", "message": "user_id or session_id is too long"},
        )
    # The stored key names its owner, so the row under it is the caller's own.
    row = await repo.current(ch, inp.org, inp.namespace, stored_key, scope=WHOLE_NAMESPACE)
    live = row if row and row.get("tombstoned_at") is None else None
    stored_rev = int(row.get("revision") or 0) if row else 0
    if inp.if_revision is not None and inp.if_revision != (stored_rev if live else 0):
        raise _conflict(stored_rev if live else 0, inp.if_revision)
    prev = sb_render.sections_from_json(live["sections"]) if live else None

    def unchanged(reason: str) -> SessionBriefSaveOutput:
        return SessionBriefSaveOutput(
            brief_key=key,
            revision=stored_rev if live else 0,
            changed=False,
            reason=reason,
            rendered=str(live.get("rendered") or "") if live else "",
        )

    if not inp.events and inp.sections is None:
        return unchanged("nothing-to-save")
    if await _recording_off(inp):
        return unchanged("recording_off")
    if (
        live
        and inp.event_cursor
        and inp.sections is None
        and live.get("event_cursor") == inp.event_cursor
        and live.get("session_id") == inp.session_id
    ):
        return unchanged("already-saved")

    # The brief is written from the session's own events, so the model is given
    # the operator's extraction policy; one this process cannot read refuses
    # the save (503), and the client sends the events again.
    from server.memory.app import extraction_policy

    policy = await extraction_policy.effective(inp.org, inp.namespace) if inp.events else ""
    if inp.sections is not None:
        base = prev
        if inp.events:
            # The agent's own sections are saved even when no window could be
            # revised under the policy, as when there is no model to call.
            try:
                revised = await _revise_from_events(inp, prev, policy)
            except BackendUnavailableError:
                revised = None
            base = revised.sections if revised is not None else prev
        rev = _Revision(sections=sb.overlay_sections(base, inp.sections), author="model")
    else:
        revised = await _revise_from_events(inp, prev, policy)
        if revised is None:
            return unchanged("not-revised")
        rev = revised
    # A client's compaction summary is raw text: under a policy none is taken from
    # this save's events, and the brief keeps the one it held.
    summaries = [] if policy else inp.events
    rev.sections = sb_render.keep_client_summary(prev, rev.sections, summaries)

    events_in = len(inp.events)
    events_total = int(live.get("events_total") or 0) + events_in if live else events_in
    source_chars = (int(live.get("source_chars") or 0) if live else 0) + sb.event_chars(inp.events)
    sessions = list(live.get("source_sessions") or []) if live else []
    sid = named(inp.session_id) or ""
    if sid and sid not in sessions:
        sessions.append(sid)
    sessions = sessions[-_MAX_SOURCE_SESSIONS:]
    revision = stored_rev + 1
    stamp = naive_utc_now()
    meta = sb_render.RenderMeta(
        brief_key=key,
        revision=revision,
        updated_at=_iso(stamp),
        trigger=inp.trigger,
        project=inp.project or (live or {}).get("project", ""),
        branch=inp.branch or (live or {}).get("branch", ""),
        head_sha=inp.head_sha or (live or {}).get("head_sha", ""),
        sessions=len(sessions),
        events_total=events_total,
        source_chars=source_chars,
        latest_session=sid,
    )
    rendered = sb_render.render_brief(rev.sections, meta)
    user_id, session_id = _row_owner(inp, key, live)
    new_row = repo.build_brief_row(
        live,
        org=inp.org,
        namespace=inp.namespace,
        brief_key=stored_key,
        agent=inp.agent,
        user_id=user_id,
        session_id=session_id,
        source_sessions=sessions,
        project=meta.project,
        repo=inp.repo or (live or {}).get("repo", ""),
        branch=meta.branch,
        head_sha=meta.head_sha,
        revision=revision,
        trigger=inp.trigger,
        author=rev.author,
        goal=sb_render.one_line(rev.sections.goal, 300),
        sections=sb_render.sections_json(rev.sections),
        rendered=rendered,
        event_cursor=inp.event_cursor,
        events_total=events_total,
        source_chars=source_chars,
        brief_chars=len(rendered),
        dropped_ungrounded=min(65535, rev.dropped),
        prompt_version=sb.PROMPT_VERSION,
        llm_model=rev.llm_model,
        tokens_in=rev.tokens_in,
        tokens_out=rev.tokens_out,
        actor=writer_actor(fallback="process:session_brief"),
        source_class="derived",
        created_at=(live or {}).get("created_at") or stamp,
        updated_at=stamp,
        tombstoned_at=None,
    )
    await repo.insert(ch, new_row)
    return SessionBriefSaveOutput(
        brief_key=key,
        revision=revision,
        changed=True,
        author=rev.author,
        rendered=rendered,
        stats=SessionBriefStats(
            events_in=events_in,
            windows=rev.windows,
            events_total=events_total,
            source_chars=source_chars,
            brief_chars=len(rendered),
            compression_ratio=round(source_chars / len(rendered), 1) if rendered else 0.0,
            dropped_ungrounded=rev.dropped,
            tokens_in=rev.tokens_in,
            tokens_out=rev.tokens_out,
            llm_model=rev.llm_model,
        ),
    )


async def _find(inp: SessionBriefGetInput) -> dict[str, Any] | None:
    ch = get_client()
    scope = request_scope()
    if inp.brief_key:
        key = _stored_key(inp.brief_key, scope)
        return await repo.current(ch, inp.org, inp.namespace, key, scope=scope)
    rows = await repo.listing(
        ch,
        inp.org,
        inp.namespace,
        project=inp.project,
        from_session=inp.from_session,
        limit=1,
        scope=scope,
        key_prefix=_key_prefix(scope),
    )
    return rows[0] if rows else None


async def handle_session_brief_get(inp: SessionBriefGetInput) -> SessionBriefGetOutput:
    if not (inp.brief_key or inp.from_session or inp.project):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "BRIEF_SELECTOR_REQUIRED",
                "message": "pass brief_key, from_session or project (session.brief_list "
                "shows the briefs this namespace holds)",
            },
        )
    row = await _find(inp)
    if row is None:
        return SessionBriefGetOutput(found=False, brief_key=inp.brief_key)
    scope = request_scope()
    key = str(row["brief_key"])
    ch = get_client()
    revisions = await repo.revision_count(ch, inp.org, inp.namespace, key)
    base: dict[str, Any] = {
        "brief_key": _shown_key(key, scope),
        "project": str(row.get("project") or ""),
        "branch": str(row.get("branch") or ""),
        "source_sessions": list(row.get("source_sessions") or []),
        "revisions": revisions,
    }
    # A deleted brief serves nothing, not even an earlier revision: its writer
    # asked for it to be forgotten. The rows stay until erasure (like
    # memory_versions under memory.delete); reads stop here.
    if row.get("tombstoned_at") is not None:
        return SessionBriefGetOutput(
            found=False, deleted=True, revision=int(row["revision"]), **base
        )
    if inp.revision:
        past = await repo.by_revision(
            ch,
            inp.org,
            inp.namespace,
            key,
            inp.revision,
            scope=scope,
        )
        # A revision from before a delete belongs to the forgotten brief: the
        # current life of the key started at its `created_at`.
        born = row.get("created_at")
        if past is None or (
            born is not None and past.get("updated_at") is not None and past["updated_at"] < born
        ):
            return SessionBriefGetOutput(found=False, **base)
        row = {**row, **past}
    deleted = False
    sections = sb_render.sections_from_json(str(row.get("sections") or "{}"))
    rendered = str(row.get("rendered") or "")
    return SessionBriefGetOutput(
        found=True,
        revision=int(row.get("revision") or 0),
        rendered=sb_render.with_client_summary(rendered, sections) if inp.full else rendered,
        sections=sections if inp.include_sections else None,
        head_sha=str(row.get("head_sha") or ""),
        author=str(row.get("author") or ""),
        trigger=str(row.get("trigger") or ""),
        actor=str(row.get("actor") or ""),
        updated_at=row.get("updated_at"),
        age_seconds=_age(row.get("updated_at")),
        events_total=int(row.get("events_total") or 0),
        source_chars=int(row.get("source_chars") or 0),
        brief_chars=int(row.get("brief_chars") or 0),
        deleted=deleted,
        **base,
    )


def _ago(seconds: int) -> str:
    if seconds < 90:
        return "just now"
    if seconds < 5400:
        return f"{seconds // 60} min ago"
    if seconds < 172_800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86_400} d ago"


async def handle_session_brief_list(inp: SessionBriefListInput) -> SessionBriefListOutput:
    scope = request_scope()
    rows = await repo.listing(
        get_client(),
        inp.org,
        inp.namespace,
        project=inp.project,
        agent=inp.agent if inp.mine_only else "",
        from_session=inp.from_session,
        limit=inp.limit,
        scope=scope,
        key_prefix=_key_prefix(scope),
    )
    items: list[SessionBriefSummary] = []
    lines: list[str] = []
    for r in rows:
        secs = sb_render.sections_from_json(str(r.get("sections") or "{}"))
        age = _age(r.get("updated_at"))
        item = SessionBriefSummary(
            brief_key=_shown_key(str(r["brief_key"]), scope),
            revision=int(r.get("revision") or 0),
            project=str(r.get("project") or ""),
            branch=str(r.get("branch") or ""),
            agent=str(r.get("agent") or ""),
            goal=sb_render.one_line(secs.goal, 160),
            status=sb_render.one_line(secs.status, 200),
            author=str(r.get("author") or ""),
            actor=str(r.get("actor") or ""),
            updated_at=r.get("updated_at"),
            age_seconds=age,
            brief_chars=int(r.get("brief_chars") or 0),
        )
        items.append(item)
        lines.append(
            f"- {item.brief_key} (rev {item.revision}, {_ago(age)}"
            + (f", {item.agent}" if item.agent and item.agent != "-" else "")
            + f"): {item.goal or item.status or '(no goal recorded)'}"
        )
    return SessionBriefListOutput(briefs=items, rendered="\n".join(lines))


async def handle_session_brief_delete(inp: SessionBriefDeleteInput) -> SessionBriefDeleteOutput:
    ch = get_client()
    scope = request_scope()
    key = _stored_key(inp.brief_key, scope)
    row = await repo.current(ch, inp.org, inp.namespace, key, scope=scope)
    if row is None or row.get("tombstoned_at") is not None:
        return SessionBriefDeleteOutput(deleted=False, brief_key=inp.brief_key)
    from server.memory.app.provenance import writer_actor

    stamp = naive_utc_now()
    await repo.insert(
        ch,
        repo.build_brief_row(
            row,
            tombstoned_at=stamp,
            updated_at=stamp,
            actor=writer_actor(fallback="process:session_brief"),
        ),
    )
    return SessionBriefDeleteOutput(
        deleted=True, brief_key=inp.brief_key, revision=int(row.get("revision") or 0)
    )
