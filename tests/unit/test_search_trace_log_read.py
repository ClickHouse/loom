"""Unit: memory.search_trace_log read handler.

Maps loom.search_trace_log rows to the wire model and applies the optional
agent / run_id filters + time window. Uses a stub CH client (no server)."""

from __future__ import annotations

from datetime import datetime

from server.memory.api.models_search import SearchTraceLogInput
from server.memory.app.search import trace as ms


class _FakeCH:
    def __init__(self):
        self.sql = ""
        self.params: dict = {}

    async def query_all(self, sql, params=None):
        self.sql = sql
        self.params = params or {}
        return [
            {
                "ts": datetime(2026, 7, 9, 12, 0, 0),
                "agent": "clickgap",
                "query": "copy on write mutation",
                "returned_keys": ["k1", "k2"],
                "returned_scores": [0.9, 0.4],
                "top_k": 5,
                "result_count": 2,
                "abstain": 0,
                "retrieval_risk": "low",
                "embedding_model": "text-embedding-3-large",
                "latency_ms": 42,
                "run_id": "pr_1",
                "operation": "memory.ask",
            }
        ]


async def test_search_trace_log_maps_rows_and_filters(monkeypatch):
    fake = _FakeCH()
    monkeypatch.setattr(ms, "get_client", lambda: fake)

    out = await ms.handle_memory_search_trace_log(
        SearchTraceLogInput(
            org="o",
            namespace="n",
            since_hours=6,
            agent="clickgap",
            run_id="pr_1",
            limit=10,
        )
    )

    assert len(out.rows) == 1
    r = out.rows[0]
    assert r.returned_keys == ["k1", "k2"]
    assert r.returned_scores == [0.9, 0.4]
    assert r.result_count == 2
    assert r.abstain is False
    assert r.run_id == "pr_1"
    assert r.latency_ms == 42
    assert r.operation == "memory.ask"
    assert "operation" in fake.sql
    # Optional filters + window bind into the query.
    assert "run_id = {rid:String}" in fake.sql and fake.params["rid"] == "pr_1"
    assert "agent = {agent:String}" in fake.sql and fake.params["agent"] == "clickgap"
    assert fake.params["h"] == 6


def _as(monkeypatch, subject: str, *, admin: bool) -> None:
    from server.auth import authz
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

    set_current_auth(
        AuthInfo(
            subject=subject,
            org="o",
            namespaces=("n",),
            roles=(Role.VIEWER,),
            is_service_account=False,
        )
    )

    async def _row_in_scope(org, ns, role=Role.VIEWER):
        return admin or role.value <= Role.VIEWER.value

    monkeypatch.setattr(authz, "row_in_scope", _row_in_scope)


async def test_a_reader_sees_only_the_rows_stamped_for_it(monkeypatch):
    """The filter is the server-stamped actor, whatever `agent` the body names; the dashboard's
    history page (no agent) still shows the person their own searches."""
    from server.auth.context import set_current_auth

    fake = _FakeCH()
    monkeypatch.setattr(ms, "get_client", lambda: fake)
    _as(monkeypatch, "alice", admin=False)
    try:
        await ms.handle_memory_search_trace_log(SearchTraceLogInput(org="o", namespace="n"))
    finally:
        set_current_auth(None)
    assert "actor IN {actors:Array(String)}" in fake.sql
    assert fake.params["actors"] == ["human:alice"]


async def test_a_namespace_admin_sees_every_row(monkeypatch):
    from server.auth.context import set_current_auth

    fake = _FakeCH()
    monkeypatch.setattr(ms, "get_client", lambda: fake)
    _as(monkeypatch, "ops", admin=True)
    try:
        await ms.handle_memory_search_trace_log(SearchTraceLogInput(org="o", namespace="n"))
    finally:
        set_current_auth(None)
    assert "actor" not in fake.params


def test_the_trace_row_stamps_the_reader(monkeypatch):
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search import telemetry
    from server.operations.base import Role

    set_current_auth(
        AuthInfo(
            subject="bot", org="o", namespaces=("n",), roles=(Role.VIEWER,), is_service_account=True
        )
    )
    try:
        row = telemetry._build_trace_row(
            MemorySearchInput(org="o", namespace="n", agent="someone-else", query="q"),
            None,
            None,
            5,
        )
    finally:
        set_current_auth(None)
    assert row["actor"] == "process:bot" and row["agent"] == "someone-else"


async def test_every_spelling_of_the_reader_reads_one_history(monkeypatch):
    """A person signed in by login handle sees the searches recorded under their email too
    (the directory ties the two, as grants fold them)."""
    import server.auth.aliases as aliases
    from server.auth.context import set_current_auth

    async def _aliases(org, subject, **_):
        return [subject, "alice@x.com"] if subject == "alice" else [subject]

    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    fake = _FakeCH()
    monkeypatch.setattr(ms, "get_client", lambda: fake)
    _as(monkeypatch, "alice", admin=False)
    try:
        await ms.handle_memory_search_trace_log(SearchTraceLogInput(org="o", namespace="n"))
    finally:
        set_current_auth(None)
    assert fake.params["actors"] == ["human:alice", "human:alice@x.com"]
