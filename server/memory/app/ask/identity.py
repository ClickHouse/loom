"""The identity predicates of the ask pipeline's own `loom.memory` reads.

memory.search bounds every leg by the caller's identity
(`search_sql._build_where`); the pipeline's statements that read
`loom.memory` beside it must be bounded the same way, or a row memory.search
would never return reaches synthesis through them.
"""

from __future__ import annotations

from typing import Any


def memory_identity_where(
    search_input: Any, alias: str = ""
) -> tuple[list[str], dict[str, Any]] | None:
    """(clauses, params) bounding a `loom.memory` read to what memory.search
    would read for `search_input`; None when the read must not run at all —
    `loom.memory` holds private rows only, so a shared-scope ask has nothing
    there it may read. `alias` prefixes the columns (`m` -> `m.agent`). The
    params use the names `_build_where` binds, so a statement that carries
    both binds one value for each."""
    if str(getattr(search_input, "scope", "private") or "private") != "private":
        return None
    col = f"{alias}." if alias else ""
    clauses = [f"{col}org = {{org:String}}", f"{col}namespace = {{ns:String}}"]
    params: dict[str, Any] = {
        "org": getattr(search_input, "org", ""),
        "ns": getattr(search_input, "namespace", ""),
    }
    # `*` and the `-` sentinel read across agents, as in memory.search.
    agent = str(getattr(search_input, "agent", "*") or "*")
    if agent not in ("*", "-"):
        clauses.append(f"{col}agent = {{agent:String}}")
        params["agent"] = agent
    for field, name in (("user_id", "user_id"), ("session_id", "session_id"), ("run_id", "run_id")):
        value = getattr(search_input, field, None)
        if value is not None:
            clauses.append(f"{col}{field} = {{{name}:String}}")
            params[name] = str(value)
    session_ids = [str(s) for s in (getattr(search_input, "session_ids", None) or [])]
    if session_ids:
        clauses.append(f"{col}session_id IN {{session_ids:Array(String)}}")
        params["session_ids"] = session_ids
    return clauses, params
