"""Fill digest listings from eligible sources rather than raw candidate limits."""

from __future__ import annotations

from typing import Any, Literal

from server.core.ch import set_query_tag
from server.core.errors import BackendUnavailableError
from server.core.repo_base import _CHClient
from server.memory.store import summary_sources
from server.settings import settings

Order = Literal["oldest", "newest", "session"]


def _ordering(order: Order) -> tuple[str, str, str]:
    if order == "session":
        return "session_id", "session_id, version DESC, agent, user_id, embedding_model", ">"
    date = "d" if order == "newest" else "created_at"
    key = f"({date}, session_id, agent, user_id, embedding_model)"
    return key, key + (" DESC" if order == "newest" else " ASC"), "<" if order == "newest" else ">"


async def verified_summary_page(
    ch: _CHClient,
    *,
    org: str,
    namespace: str,
    where: str,
    params: dict[str, Any],
    limit: int,
    order: Order = "oldest",
) -> list[dict[str, Any]]:
    """Return up to limit+1 proven rows; refuse an incomplete bounded scan."""
    key, ordering, comparison = _ordering(order)
    after: Any = None
    found: list[dict[str, Any]] = []
    for page in range(settings.memory_digest_listing_max_pages):
        set_query_tag("memory.verified_digest_page")
        size = settings.memory_digest_listing_page_size
        if page == 0:
            size = min(size, limit + 1)
        cursor_type = (
            "String" if order == "session" else "Tuple(DateTime,String,String,String,String)"
        )
        cursor = f" AND {key} {comparison} {{after:{cursor_type}}}" if after is not None else ""
        rows = await ch.query_all(
            "SELECT agent, user_id, session_id, embedding_model, summary_text, key_facts, "
            "derived_from, valid_at, created_at, version, length(key_facts) AS kf, "
            "length(derived_from) AS df, coalesce(valid_at, created_at) AS d "
            "FROM loom.session_summaries FINAL WHERE "
            + where
            + cursor
            + " ORDER BY "
            + ordering
            + (" LIMIT 1 BY session_id" if order == "session" else "")
            + " LIMIT {lim:UInt32}",
            params={**params, "lim": size, "after": after},
        )
        found.extend(
            await summary_sources.current_summary_sources(ch, rows, org=org, namespace=namespace)
        )
        if len(found) > limit or len(rows) < size:
            return found[: limit + 1]
        last = rows[-1]
        after = (
            str(last["session_id"])
            if order == "session"
            else (
                last["d" if order == "newest" else "created_at"],
                *(str(last.get(c, "-")) for c in ("session_id", "agent", "user_id")),
                str(last.get("embedding_model") or ""),
            )
        )
    raise BackendUnavailableError("Digest source verification exceeded its scan budget")
