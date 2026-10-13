"""get_many_by_keys must treat agent="*" as a fan-out wildcard.

procedure.match defaults agent="*" and fetches procedure bodies via
memory.bulk_get → get_many_by_keys. If the query filters agent="*" literally
(no real row has that agent), the bodies come back empty and procedure.match
returns nothing even when search found the procedure. The "-" sentinel, by
contrast, is a real stored agent value and must stay a literal match.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.store import Identity, get_many_by_keys


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return []


def _identity(agent: str) -> Identity:
    return Identity(org="o", namespace="ns", agent=agent, user_id="-", session_id="-")


@pytest.mark.asyncio
async def test_wildcard_agent_drops_the_agent_predicate() -> None:
    ch = StubCH()
    await get_many_by_keys(_identity("*"), ["procedure_x"], ch=ch, scope=WHOLE_NAMESPACE)
    sql, params = ch.query_all_calls[0]
    assert "agent = {agent:String}" not in sql
    assert "agent" not in params


@pytest.mark.asyncio
async def test_concrete_agent_keeps_the_agent_predicate() -> None:
    for agent in ("t", "-"):
        ch = StubCH()
        await get_many_by_keys(_identity(agent), ["procedure_x"], ch=ch, scope=WHOLE_NAMESPACE)
        sql, params = ch.query_all_calls[0]
        assert "agent = {agent:String}" in sql
        assert params["agent"] == agent


# ------------------------------------------------------------ wildcard agent axes
# memory.get under agent="*" is the namespace-wide lookup: it drops agent,
# user_id AND session_id. bulk_get must drop the same axes, or a row written
# under a real user_id or session_id is found by get and "missing" from
# bulk_get for the same identity. The sentinel axes open with the wildcard
# agent; a pinned axis still filters.


@pytest.mark.asyncio
async def test_wildcard_agent_opens_the_unset_user_and_session_axes() -> None:
    ch = StubCH()
    await get_many_by_keys(_identity("*"), ["lesson_x"], ch=ch, scope=WHOLE_NAMESPACE)
    sql, params = ch.query_all_calls[0]
    assert "user_id = {uid:String}" not in sql and "uid" not in params
    assert "session_id = {sid:String}" not in sql and "sid" not in params
    # several identities may hold the key: the newest row answers, as get_by_key
    assert "LIMIT 1 BY memory_key" in sql
    assert "ORDER BY memory_key ASC, version DESC, created_at DESC" in sql


@pytest.mark.asyncio
async def test_wildcard_agent_keeps_a_pinned_user_or_session() -> None:
    ch = StubCH()
    ident = Identity(org="o", namespace="ns", agent="*", user_id="alice", session_id="-")
    await get_many_by_keys(ident, ["lesson_x"], ch=ch, scope=WHOLE_NAMESPACE)
    sql, params = ch.query_all_calls[0]
    assert "user_id = {uid:String}" in sql and params["uid"] == "alice"
    assert "session_id = {sid:String}" not in sql
    assert "LIMIT 1 BY memory_key" in sql


@pytest.mark.asyncio
async def test_concrete_agent_still_matches_the_sentinels_literally() -> None:
    ch = StubCH()
    await get_many_by_keys(_identity("recall-agent"), ["lesson_x"], ch=ch, scope=WHOLE_NAMESPACE)
    sql, params = ch.query_all_calls[0]
    assert "agent = {agent:String}" in sql
    assert "user_id = {uid:String}" in sql and params["uid"] == "-"
    assert "session_id = {sid:String}" in sql and params["sid"] == "-"
    assert "LIMIT 1 BY" not in sql  # the exact identity tuple is one row per key already
