"""The exposure ledger: one row per read, keyed on the token,
carrying the returned keys and a query digest — never the text.

Pure unit: the builder, the gate, the hit projection, the SQL shapes and the
table shapes in the schema dump. No ClickHouse.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

from server.auth.jwt import AuthInfo, mint_token, verify_token
from server.memory.app import exposure_ledger as led
from tests.unit.schema_ddl import column_names, ddl

_SECRET = "unit-secret-unit-secret-unit-secret-32"


def _auth(**kw) -> AuthInfo:
    base = dict(
        subject="sa:shop-bot",
        org="acme",
        namespaces=("shop",),
        roles=(),
        is_service_account=True,
    )
    base.update(kw)
    return AuthInfo(**base)


def test_digest_is_the_agent_events_shape_and_never_the_text():
    d = led.query_digest("I lost previous example queries, please enter them")
    assert re.fullmatch(r"sha256:[0-9a-f]{16}/\d+", d)
    assert "previous" not in d
    assert led.query_digest("") == "" and led.query_digest(None) == ""
    assert led.query_digest("same") == led.query_digest("same")


def test_row_carries_the_token_subject_not_only_the_agent():
    row = led.build_row(
        operation="memory.search",
        org="acme",
        namespace="shop",
        agent="webshop",
        user_id="u1",
        session_id=None,
        run_id="-",
        read_scope="own_user",
        query="blue headphones",
        top_k=5,
        keys=["k1", "k2"],
        below_floor=1,
        auth=_auth(token_id="jti-9"),
        request_id="req-1",
    )
    assert row["actor"] == "sa:shop-bot" and row["token_id"] == "jti-9"
    assert row["agent"] == "webshop"
    assert (row["user_id"], row["session_id"], row["run_id"]) == ("u1", "-", "-")
    assert row["returned_keys"] == ["k1", "k2"] and row["returned_count"] == 2
    assert row["below_floor_count"] == 1 and row["top_k"] == 5
    assert row["read_scope"] == "own_user" and row["request_id"] == "req-1"
    assert row["query_digest"].startswith("sha256:") and "headphones" not in str(row)


def test_row_without_auth_has_empty_actor():
    row = led.build_row(operation="memory.list", org="o", namespace="n", keys=["k"], auth=None)
    assert row["actor"] == "" and row["token_id"] == ""
    assert row["operation"] == "memory.list"


def test_keys_of_skips_computed_rows_and_counts_below_floor():
    hits = [
        SimpleNamespace(memory_key="a", source="fact", score_components={}),
        SimpleNamespace(memory_key="derived_aggregate", source="computed", score_components={}),
        SimpleNamespace(memory_key="b", source="fact", score_components={"below_floor": 1.0}),
        SimpleNamespace(memory_key="", source="fact", score_components=None),
    ]
    keys, below = led.keys_of(hits)
    assert keys == ["a", "b"] and below == 1


def test_record_is_a_no_op_when_disabled_or_empty(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_exposure_ledger_enabled", False)
    assert (
        led.record_exposure(operation="memory.search", org="o", namespace="n", keys=["k"]) is None
    )
    monkeypatch.setattr(settings, "memory_exposure_ledger_enabled", True)
    assert led.record_exposure(operation="memory.search", org="o", namespace="n", keys=[]) is None


def test_record_builds_the_row_from_the_request_context(monkeypatch):
    from server.auth.context import set_current_auth
    from server.core.logging_config import request_id_var
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_exposure_ledger_enabled", True)
    set_current_auth(_auth())
    tok = request_id_var.set("req-42")
    try:
        # No running loop: the row is built and returned, not written.
        row = led.record_exposure(
            operation="memory.ask",
            org="acme",
            namespace="shop",
            keys=["k1"],
            query="q",
        )
    finally:
        request_id_var.reset(tok)
        set_current_auth(None)
    assert row is not None
    assert row["actor"] == "sa:shop-bot" and row["request_id"] == "req-42"
    assert row["operation"] == "memory.ask"


def test_jti_becomes_token_id():
    import jwt as pyjwt

    tok = mint_token("sa:bot", "acme", namespaces=["shop"], roles=["viewer"], secret=_SECRET)
    payload = pyjwt.decode(tok, _SECRET, algorithms=["HS256"], options={"verify_aud": False})
    # A minted token carries a jti, and that is its id.
    assert verify_token(tok, secret=_SECRET).token_id == payload["jti"]
    # A token without a jti still has a credential id — the
    # sha256 prefix of the token itself, stable for its lifetime.
    import hashlib

    bare = pyjwt.encode(
        {k: v for k, v in payload.items() if k != "jti"},
        _SECRET,
        algorithm="HS256",
        headers={"kid": "override"},
    )
    assert (
        verify_token(bare, secret=_SECRET).token_id
        == hashlib.sha256(bare.encode()).hexdigest()[:16]
    )
    payload["jti"] = "abc-123"
    tok2 = pyjwt.encode(payload, _SECRET, algorithm="HS256", headers={"kid": "override"})
    assert verify_token(tok2, secret=_SECRET).token_id == "abc-123"


def test_summary_sql_merges_the_breadth_states():
    assert "uniqExactMerge(keys_state)" in led.SUMMARY_SQL
    assert "uniqExactMerge(calls_state)" in led.SUMMARY_SQL
    assert led.BREADTH_TABLE in led.SUMMARY_SQL
    assert "{org:String}" in led.SUMMARY_SQL and "{ns:String}" in led.SUMMARY_SQL
    assert "tombstoned_at IS NULL" in led.NAMESPACE_ROWS_SQL


def test_schema_has_the_ledger_the_writer_fills():
    ledger = ddl(led.TABLE.removeprefix("loom."))
    breadth = ddl(led.BREADTH_TABLE.removeprefix("loom."))
    refresh = ddl("memory_exposure_breadth_hourly_refresh")
    row = led.build_row(operation="memory.search", org="o", namespace="n", keys=["k"])
    assert set(row) <= set(column_names("memory_exposures"))
    assert "ORDER BY (org, namespace, actor, ts)" in ledger
    assert "TTL ts + toIntervalDay(180)" in ledger
    assert "`keys_state` AggregateFunction(uniqExact, String)" in breadth
    assert f"APPEND TO {led.BREADTH_TABLE}" in refresh
    assert "uniqExactState(k) AS keys_state" in refresh
    assert "ARRAY JOIN returned_keys AS k" in refresh
    # The query itself is never stored: only its digest column exists.
    assert "`query` String" not in ledger and "`query_digest` String" in ledger


def test_exposure_summary_op_is_admin_only():
    from server.memory.api.ops_memory import OPERATIONS
    from server.operations.base import Role

    op = next(o for o in OPERATIONS if o.name == "memory.exposure_summary")
    assert op.required_role == Role.ADMIN
    assert op.idempotent is True
