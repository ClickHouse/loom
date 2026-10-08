"""find_by_identifier feeds memory.ask synthesis at rank 1.0, so it MUST apply
the same identity/TTL eligibility as the main vector leg — else it injects
another agent's/user's private or expired memories into a grounded answer.
These tests capture the SQL it builds and assert the predicates are present."""

from __future__ import annotations

import asyncio

from server.memory import store as memory_repo


class _FakeCH:
    def __init__(self):
        self.sql = ""
        self.params: dict = {}

    async def query_all(self, sql, params=None):
        self.sql = sql
        self.params = params or {}
        return []


def _run(**kw):
    ch = _FakeCH()
    asyncio.run(
        memory_repo.find_by_identifier(org="o", namespace="n", identifiers=["117585"], ch=ch, **kw)
    )
    return ch


def test_default_private_scope_and_ttl_always_present():
    ch = _run()
    assert "scope = {scope:String}" in ch.sql
    assert "expires_at IS NULL OR expires_at > now()" in ch.sql
    assert ch.params["scope"] == "private"
    assert "status IN ('active', 'duplicate', 'conflicted')" in ch.sql
    assert "tombstoned_at IS NULL" in ch.sql


def test_specific_agent_adds_agent_predicate():
    ch = _run(scope="private", agent="alice")
    assert "agent = {agent:String}" in ch.sql
    assert ch.params["agent"] == "alice"


def test_wildcard_agent_drops_agent_predicate():
    # '*' = deliberate cross-agent read (membership is the boundary); scope
    # still applies, but no per-agent filter — matches _build_where.
    ch = _run(scope="private", agent="*")
    assert "agent = {agent:String}" not in ch.sql


def test_user_id_scopes_when_set():
    ch = _run(user_id="u123")
    assert "user_id = {user_id:String}" in ch.sql
    assert ch.params["user_id"] == "u123"


def test_user_id_absent_by_default():
    ch = _run()
    assert "user_id = {user_id:String}" not in ch.sql


def test_long_numbers_match_on_digits_alone() -> None:
    """Participation Post_processing 195: the question writes 20202201042, the
    memory '202-022-01042'; the identifier read compares digits only for a
    number of seven or more digits, and text identifiers as before."""
    import asyncio

    from server.memory import store as memory_repo

    seen: dict[str, str] = {}

    class _CH:
        async def query_all(self, sql, params=None):
            seen["sql"] = sql
            seen.update(params or {})
            return []

    asyncio.run(
        memory_repo.find_by_identifier(
            org="o", namespace="ns", identifiers=["20202201042", "SamRivera"], ch=_CH()
        )
    )
    assert "replaceRegexpAll(content_excerpt, '[^0-9]', '')" in seen["sql"]
    assert seen["id0"] == "20202201042" and "position(content_excerpt, {id1:String})" in seen["sql"]
