"""The run's scratchpad on memory.search and context.pack.

An agent needs two stores: a searchable memory and a scratchpad it re-reads on
EVERY step. `loom.working_memory` is that scratchpad. `memory.ask` puts the
run's blocks in front of its synthesizer (`render_run_blocks`); this module does
the same for `memory.search` and widens `context.pack`.
`load` returns the blocks in scope, rendered as markdown and bounded by
`max_chars`, so a search response can carry them.

Scope: a run's blocks come first, read by `(org, namespace, run_id)` — the
run id is unique to one execution and the reader (search
with `agent="*"`) does not know the writer's agent. Then the blocks written
without a run (`run_id = '-'`) for the same session axis follow; the plugin
writes its "where was I" note there and its per-turn conclusions at run
scope, so a pack for the run sees both. Identity axes the caller left open
(`agent="*"`, no `user_id`) are not filtered on. Every read also carries the
request's read scope, so a token bounded to one user never reads another
user's run by naming its run id.

Inert by construction when no block exists for the scope: indexed point
reads on the `working_memory` primary key, no LLM call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from server.core.naming import SENTINEL, named
from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and

TRUNCATED_MARK = "\n(truncated)"


@dataclass
class ScratchpadBlocks:
    rendered: str = ""
    block_count: int = 0
    total_chars: int = 0
    truncated: bool = False
    block_keys: list[str] = field(default_factory=list)
    # "run" when at least one block came from the run scope, else "session".
    scope: str = ""


def _real(value: Any) -> str:
    return named(str(value or "").strip(), wildcard=True) or ""


def render(blocks: list[dict[str, Any]], *, max_chars: int) -> ScratchpadBlocks:
    """Markdown `## key\\ncontent` per block, cut at `max_chars` with a
    marker. Pure; the unit under test."""
    parts: list[str] = []
    keys: list[str] = []
    total = 0
    for row in blocks:
        key = str(row.get("block_key") or "").strip()
        content = str(row.get("content") or "").strip()
        if not key or not content:
            continue
        parts.append(f"## {key}\n{content}")
        keys.append(key)
        total += len(content)
    if not parts:
        return ScratchpadBlocks()
    text = "\n\n".join(parts)
    truncated = False
    if max_chars > 0 and len(text) > max_chars:
        cut = max(0, max_chars - len(TRUNCATED_MARK))
        text = text[:cut].rstrip() + TRUNCATED_MARK
        truncated = True
    return ScratchpadBlocks(
        rendered=text,
        block_count=len(parts),
        total_chars=total,
        truncated=truncated,
        block_keys=keys,
    )


async def _select(ch: Any, where: str, params: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    """Blocks matching `where`, newest first, inside the request's read scope."""
    params = {**params, "lim": int(limit)}
    where += scope_and(request_scope(), params)
    rows = await ch.query_all(
        f"""
        SELECT block_key, content, version, updated_at
        FROM loom.working_memory FINAL
        WHERE {where}
        ORDER BY updated_at DESC, block_key
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [dict(r) for r in (rows or [])]


_LIVE = "org = {org:String} AND namespace = {ns:String} AND tombstoned_at IS NULL"


async def run_rows(
    ch: Any, org: str, namespace: str, run_id: str, *, limit: int
) -> list[dict[str, Any]]:
    """The run's blocks. The run id is unique to one execution: it IS the scope."""
    params = {"org": org, "ns": namespace, "run": run_id}
    return await _select(ch, f"{_LIVE} AND run_id = {{run:String}}", params, limit)


async def _session_rows(
    ch: Any,
    org: str,
    namespace: str,
    *,
    agent: str,
    user_id: str,
    session_id: str,
    limit: int,
) -> list[dict[str, Any]]:
    """The session axis's blocks written without a run, on the identity axes the
    caller named."""
    clauses = [_LIVE, "session_id = {sid:String}", "run_id = {norun:String}"]
    params: dict[str, Any] = {"org": org, "ns": namespace, "sid": session_id, "norun": SENTINEL}
    if agent:
        clauses.append("agent = {agent:String}")
        params["agent"] = agent
    if user_id:
        clauses.append("user_id = {uid:String}")
        params["uid"] = user_id
    return await _select(ch, " AND ".join(clauses), params, limit)


async def load(
    org: str,
    namespace: str,
    agent: Any,
    user_id: Any,
    session_id: Any,
    run_id: Any,
    *,
    max_chars: int,
    ch: Any = None,
    limit: int = 20,
) -> ScratchpadBlocks | None:
    """Blocks for the identity, run scope first then session scope. None
    when the request names neither a session nor a run (no scope to read
    a scratchpad for) or when nothing is stored."""
    sid = _real(session_id)
    rid = _real(run_id)
    if not sid and not rid:
        return None
    ag = _real(agent)
    uid = _real(user_id)
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    sid_axis = sid or SENTINEL
    rows: list[dict[str, Any]] = []
    scope = ""
    if rid:
        rows = await run_rows(ch, org, namespace, rid, limit=limit)
        if rows:
            scope = "run"
    # Widen to the blocks written without a run (run_id '-') for the same
    # session axis: always when no run was named, and when the run's blocks
    # did not fill the scope on their own. With no session named the axis is
    # '-' too, which is where the plugin's agent-scope note lives.
    if len(rows) < limit:
        seen = {str(r.get("block_key")) for r in rows}
        session_rows = await _session_rows(
            ch, org, namespace, agent=ag, user_id=uid, session_id=sid_axis, limit=limit
        )
        rows.extend(r for r in session_rows if str(r.get("block_key")) not in seen)
        if session_rows and not scope:
            scope = "session"
    if not rows:
        return None
    out = render(rows, max_chars=max_chars)
    if not out.block_count:
        return None
    out.scope = scope
    return out
