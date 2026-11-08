"""profile.* handlers — named, char-limited, agent-editable sections.

set/append/replace are thin shims over memory.set/get that pin
memory_type='profile' + a default char_limit; render fans out across
agents to assemble the prompt fragment; activity aggregates
loom.agent_events.
"""

from __future__ import annotations

import logging
from html import escape
from typing import Any

from server.core.errors import caller_text
from server.memory.api.models_common import _Identity
from server.memory.api.models_crud import MemoryGetInput, MemorySetInput
from server.memory.api.models_profile import (
    DEFAULT_CHAR_LIMIT,
    ProfileActivityEntry,
    ProfileActivityInput,
    ProfileActivityOutput,
    ProfileAppendInput,
    ProfileMutationOutput,
    ProfileRenderInput,
    ProfileRenderOutput,
    ProfileReplaceInput,
    ProfileSetInput,
)
from server.memory.domain.context_layout import escape_data
from server.settings import settings

log = logging.getLogger(__name__)


def _canonical_json_str(s: str) -> str:
    """The canonical-JSON form the server measures `value` against char_limit,
    mirrored here so the handler can return an accurate value_length."""
    import json as _json

    return _json.dumps(s)


def profile_user_clause(
    scope: str, user_id: str | None, session_id: str | None
) -> tuple[str, dict[str, Any]]:
    """The user/session predicate for a profile render under a read scope. `namespace` adds
    nothing. `own_user` keeps this user's
    sections AND the namespace-level ones (user_id '-': a persona authored for
    the namespace is not another user's memory); `own_session` narrows the
    same way on session_id. Pure, unit-tested."""
    if scope == "namespace" or not user_id:
        return "", {}
    clause = "AND (user_id = {uid:String} OR user_id = '-')"
    params: dict[str, Any] = {"uid": user_id}
    if scope == "own_session" and session_id:
        clause += " AND (session_id = {sid:String} OR session_id = '-')"
        params["sid"] = session_id
    return clause, params


def _to_memory_set_input(
    inp: ProfileSetInput | _Identity,
    *,
    label: str,
    value: str,
    char_limit: int,
    if_match_sha256: str = "",
    source_trace: str = "",
) -> MemorySetInput:
    """Build a MemorySetInput from a profile input. Pins memory_type +
    char_limit; carries the caller's source_trace so provenance can class an
    LLM-folded section `derived`."""
    return MemorySetInput(
        org=inp.org,
        namespace=inp.namespace,
        agent=inp.agent,
        user_id=inp.user_id,
        session_id=inp.session_id,
        run_id=inp.run_id,
        memory_key=label,
        memory_type="profile",
        value=value,
        char_limit=char_limit,
        if_match_sha256=if_match_sha256,
        source_trace=source_trace or getattr(inp, "source_trace", "") or "",
    )


async def write_section(mset: MemorySetInput) -> Any:
    """memory.set for a profile section, then a re-embed of its current text queued off the
    request: memory.set gives a profile row no inline vector, and the worker's reconcile
    only embeds a section that has none, so without this an edited section would keep the
    vector of its first value."""
    from server.memory.app.profile_search import schedule_reembed_after_write
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(mset)
    schedule_reembed_after_write(
        mset.org, (mset.namespace, mset.agent, mset.user_id, mset.session_id, mset.memory_key)
    )
    return out


async def handle_profile_set(input: ProfileSetInput) -> ProfileMutationOutput:
    mset = _to_memory_set_input(
        input,
        label=input.label,
        value=input.value,
        char_limit=input.char_limit,
        if_match_sha256=input.if_match_sha256,
    )
    out = await write_section(mset)
    return ProfileMutationOutput(
        version=out.version,
        memory_id=out.memory_id,
        content_sha256=out.content_sha256,
        namespace=out.namespace,
        label=input.label,
        char_limit=input.char_limit,
        value_length=len(_canonical_json_str(input.value)),
    )


async def write_folded_section(
    *, org: str, namespace: str, user_id: str, label: str, value: str, memory_ids: list[str]
) -> None:
    """Write a consolidation fold's text as the user's `label` section.

    The section is user-scoped (user_id); agent='-' so profile.render's
    per-user fan-out picks it up for every client. The trace names the fold
    so provenance classes the row `derived`, not `asserted`: an LLM's summary
    of a user's working style is an inference. It also names the memories the
    fold read, so `memory.redact` of one of them scrubs this version too."""
    await handle_profile_set(
        ProfileSetInput(
            org=org,
            namespace=namespace,
            agent="-",
            user_id=user_id,
            label=label,
            value=value,
            source_trace=f"profile.fold:{label}:" + ",".join(memory_ids),
        )
    )


async def handle_profile_append(input: ProfileAppendInput) -> ProfileMutationOutput:
    """Atomic append: read the current value + sha, concat, set with
    if_match_sha256, retry on conflict. Preserves the existing
    char_limit. Treats a missing section as initial set."""
    from fastapi import HTTPException

    from server.memory.app.handlers import handle_memory_get

    get_input = MemoryGetInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        memory_key=input.label,
    )

    last_err: Exception | None = None
    for _ in range(settings.memory_profile_cas_max_retries):
        existing = await handle_memory_get(get_input)
        if not existing.found:
            return await handle_profile_set(
                ProfileSetInput(
                    org=input.org,
                    namespace=input.namespace,
                    agent=input.agent,
                    user_id=input.user_id,
                    session_id=input.session_id,
                    run_id=input.run_id,
                    label=input.label,
                    value=input.addition,
                    char_limit=DEFAULT_CHAR_LIMIT,
                )
            )

        if not isinstance(existing.value, str):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SECTION_NOT_STRING",
                    "message": (
                        f"profile.append requires string-valued sections; "
                        f"section {input.label!r} holds "
                        f"{type(existing.value).__name__}"
                    ),
                },
            )

        new_value = existing.value + input.separator + input.addition
        char_limit = existing.char_limit or DEFAULT_CHAR_LIMIT
        # The get resolves the section under any agent, so the append extends THAT row.
        # Writing under the caller's own agent with the found row's hash would fail every
        # retry (409 "current_sha256=''"), e.g. Codex appending to a section Claude Code wrote.
        found_agent = str(getattr(existing, "agent", "") or "")
        owner = found_agent if found_agent and found_agent != input.agent else ""
        try:
            mset = _to_memory_set_input(
                input.model_copy(update={"agent": owner}) if owner else input,
                label=input.label,
                value=new_value,
                char_limit=char_limit,
                if_match_sha256=existing.content_sha256,
            )
            out = await write_section(mset)
            return ProfileMutationOutput(
                version=out.version,
                memory_id=out.memory_id,
                content_sha256=out.content_sha256,
                namespace=out.namespace,
                label=input.label,
                char_limit=char_limit,
                value_length=len(_canonical_json_str(new_value)),
            )
        except HTTPException as e:
            # Only retry on precondition mismatch — every other 4xx is
            # the caller's bug. Char-limit 422 surfaces to the caller.
            if e.status_code == 409:
                last_err = e
                continue
            raise

    raise last_err if last_err else RuntimeError("append exhausted CAS retries")


async def handle_profile_replace(input: ProfileReplaceInput) -> ProfileMutationOutput:
    """Substring rewrite with CAS. Refuses to no-op when `old` isn't
    in the section (the agent should know what it's editing)."""
    from fastapi import HTTPException

    from server.memory.app.handlers import handle_memory_get

    get_input = MemoryGetInput(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
        run_id=input.run_id,
        memory_key=input.label,
    )

    last_err: Exception | None = None
    for _ in range(settings.memory_profile_cas_max_retries):
        existing = await handle_memory_get(get_input)
        if not existing.found:
            raise HTTPException(
                status_code=404,
                detail={
                    "code": "SECTION_NOT_FOUND",
                    "message": f"profile section {input.label!r} does not exist",
                },
            )
        if not isinstance(existing.value, str):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SECTION_NOT_STRING",
                    "message": (
                        f"profile.replace requires string-valued sections; "
                        f"section {input.label!r} holds "
                        f"{type(existing.value).__name__}"
                    ),
                },
            )
        if input.old not in existing.value:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "SUBSTRING_NOT_FOUND",
                    "message": (f"substring {input.old!r} not found in section {input.label!r}"),
                },
            )

        new_value = existing.value.replace(input.old, input.new)
        char_limit = existing.char_limit or DEFAULT_CHAR_LIMIT
        try:
            mset = _to_memory_set_input(
                input,
                label=input.label,
                value=new_value,
                char_limit=char_limit,
                if_match_sha256=existing.content_sha256,
            )
            out = await write_section(mset)
            return ProfileMutationOutput(
                version=out.version,
                memory_id=out.memory_id,
                content_sha256=out.content_sha256,
                namespace=out.namespace,
                label=input.label,
                char_limit=char_limit,
                value_length=len(_canonical_json_str(new_value)),
            )
        except HTTPException as e:
            if e.status_code == 409:
                last_err = e
                continue
            raise

    raise last_err if last_err else RuntimeError("replace exhausted CAS retries")


async def handle_profile_render(input: ProfileRenderInput) -> ProfileRenderOutput:
    """Read every section in (org, namespace), format, return.

    Fan-out: scope='namespace' (default) ignores the calling agent's
    identity and reads section rows written by any agent in the namespace
    — matches the natural user model where a persona authored once via
    the dashboard is visible to every MCP client connecting to that
    namespace. scope='this_agent' keeps the per-agent isolation.

    Implementation uses memory_repo directly so this is one CH query +
    one fanned hydrate, not N+1 calls through handle_memory_list +
    handle_memory_get.
    """
    import json as _json

    from fastapi import HTTPException

    from server.core.ch import get_client as _get_ch

    if input.format not in ("xml", "markdown"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_FORMAT",
                "message": "format must be 'xml' or 'markdown'",
            },
        )
    if input.scope not in ("namespace", "this_agent"):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "INVALID_SCOPE",
                "message": "scope must be 'namespace' or 'this_agent'",
            },
        )

    ch = _get_ch()
    # Read scope: the profile section is injected ahead of every query, so a namespace-wide
    # render under a one-token-many-users deployment would put every user's folded profile
    # into every user's pack.
    from server.auth.context import current_auth
    from server.identity.domain.read_scope import resolve_read_filters

    rf = resolve_read_filters(
        requested=input.read_scope,
        user_id=input.user_id,
        session_id=input.session_id,
        auth=current_auth(),
    )
    user_clause, params = profile_user_clause(rf.scope, rf.user_id, rf.session_id)
    params.update({"org": input.org, "ns": input.namespace})
    agent_clause = ""
    if input.scope != "namespace":
        agent_clause = "AND agent = {agent:String}"
        params["agent"] = input.agent
    from server.core.ch import own_write_reads
    from server.memory.domain.temporal_sql import served_status_sql

    with own_write_reads():  # the section a call ago appended to
        rows = await ch.query_all(
            f"""
            SELECT memory_key, agent, value, char_limit, updated_at
            FROM loom.memory FINAL
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              {agent_clause}
              {user_clause}
              AND memory_type = 'profile'
              AND tombstoned_at IS NULL
              -- a head scrubbed by memory.redact reads as no section, not as its marker
              AND redacted_at IS NULL
              -- rendered into packs and prompts; a high section is readable by key only
              AND sensitivity != 'high'
              AND (expires_at IS NULL OR expires_at > now())
              AND {served_status_sql()}
            ORDER BY memory_key, agent
            """,
            params=params,
        )

    # Deserialize JSON-encoded values (memory.value is canonical JSON).
    sections: list[tuple[str, str]] = []
    for r in rows:
        try:
            raw = r.get("value") or ""
            parsed = _json.loads(raw) if raw else ""
            value = parsed if isinstance(parsed, str) else _json.dumps(parsed)
        except (TypeError, ValueError):
            value = str(r.get("value") or "")
        sections.append((str(r["memory_key"]), value))

    # Apply label filter if provided. Order = caller's label order (so
    # the caller can pin layout); else alphabetical (SDK default).
    if input.labels is not None:
        by_label = {lbl: val for lbl, val in sections}
        sections = [(lbl, by_label[lbl]) for lbl in input.labels if lbl in by_label]
    else:
        sections.sort(key=lambda p: p[0])

    rendered = _render_sections(sections, input.format)

    return ProfileRenderOutput(
        rendered=rendered,
        section_count=len(sections),
        total_chars=len(rendered),
        labels=[lbl for lbl, _ in sections],
    )


def _render_sections(sections: list[tuple[str, str]], fmt: str) -> str:
    if fmt == "markdown":
        return "\n\n".join(f"## {lbl}\n{val}" for lbl, val in sections)
    # Escaped like every recalled value, so a section cannot close its own tag.
    return "\n".join(
        f'<section label="{escape(lbl, quote=True)}">{escape_data(val)}</section>'
        for lbl, val in sections
    )


# Every op whose response carries the caller's profile sections: `loom_activate` (the typed
# "use loom"), `profile.render` (the SDK's system_prompt), `profile.structured` (a chat
# bot's structured profile), `context.pack` (the plugin's session-start primer, profile
# section by default) and `mcp.initialize` (the handshake's instructions inline the primer).
# Counting fewer would tell a plugin user their profile was "not yet read by any AI client"
# while every session began with it.
PROFILE_READ_OPERATIONS: frozenset[str] = frozenset(
    {"loom_activate", "profile.render", "profile.structured", "context.pack", "mcp.initialize"}
)


async def handle_profile_activity(
    input: ProfileActivityInput,
) -> ProfileActivityOutput:
    from fastapi import HTTPException

    from server.core.ch import get_client as _get_ch

    days = max(1, min(30, input.lookback_days))
    ch = _get_ch()
    try:
        rows = await ch.query_all(
            """
            SELECT
              agent AS agent,
              max(ts) AS last_ts,
              count() AS n,
              groupUniqArray(operation) AS ops
            FROM loom.agent_events
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND operation IN ({ops:Array(String)})
              AND success = true
              AND ts >= now() - toIntervalDay({days:UInt32})
              AND agent != 'loom-ui'
            GROUP BY agent
            ORDER BY last_ts DESC
            LIMIT 20
            """,
            params={
                "org": input.org,
                "ns": input.namespace,
                "days": days,
                "ops": sorted(PROFILE_READ_OPERATIONS),
            },
        )
    except Exception as e:
        # 503 not 500 so the dashboard degrades gracefully on a CH failure. The driver's
        # text can carry the statement or the server's address: logged only.
        log.warning("profile.activity: the agent_events read failed: %s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ACTIVITY_UNAVAILABLE",
                "message": f"the agent_events read failed ({caller_text(e)}); retry",
            },
        ) from e

    entries: list[ProfileActivityEntry] = []
    for r in rows:
        ts = r.get("last_ts")
        if ts is not None and hasattr(ts, "isoformat"):
            ts_str = ts.isoformat()
        else:
            ts_str = str(ts) if ts is not None else ""
        entries.append(
            ProfileActivityEntry(
                agent=str(r.get("agent") or ""),
                last_activated_at=ts_str,
                event_count=int(r.get("n") or 0),
                operations=sorted(str(o) for o in (r.get("ops") or []) if o),
            )
        )
    return ProfileActivityOutput(entries=entries)
