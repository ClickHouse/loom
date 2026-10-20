"""The behavioral half of the erasure compliance bench — pure
helpers only (the run needs a server and ClickHouse)."""

from __future__ import annotations

import sys
from types import SimpleNamespace

from bench import erasure_compliance as b
from server.erasure.store import census as erasure_census


def test_embedding_cache_key_matches_the_servers_shared_cache_key() -> None:
    from server.embedding._cache import _key

    for text in ("a  b", "The launch code is zq1.", " leading and trailing "):
        assert b.embedding_cache_key("text-embedding-3-large", text, "acme") == _key(
            "text-embedding-3-large", text, "acme"
        )


def test_focus_tables_are_census_tables_or_the_two_core_ones() -> None:
    census = {t.split(".", 1)[1] for t in erasure_census.census_tables("namespace")}
    for table in b.FOCUS_TABLES:
        assert table in census, table
    # the review's five uncounted readable tables + the events stream are all focused
    for t in (
        "search_trace_log",
        "memory_exposures",
        "conversations",
        "code_search_trace",
        "events",
    ):
        assert t in b.FOCUS_TABLES, t
    for pred in b.FOCUS_TABLES.values():
        assert "{tok:String}" in pred or "{ktok:String}" in pred
    # soft-deleted tables count LIVE rows (a user-scope erasure tombstones)
    for t in ("memory", "memory_embeddings", "conversations", "context_blobs"):
        assert "tombstoned_at IS NULL" in b.FOCUS_TABLES[t], t
    # the table the user saga cannot reach (no user axis) is reported, not gated
    assert b.NAMESPACE_ONLY_FOCUS == {"search_trace_log"}


def test_search_trace_query_is_no_longer_allowed_residue() -> None:
    assert ("search_trace_log", "query") not in b.ALLOWED_RESIDUE
    hits = b.classify_hits([("search_trace_log", "query", 1), ("audit_log", "subject", 2)])
    assert [(h.table, h.allowed) for h in hits] == [
        ("search_trace_log", False),
        ("audit_log", True),
    ]


def test_read_shaped_ops_are_the_viewer_ops_minus_the_skip_list() -> None:
    from server.operations.registry import ALL_OPERATIONS

    ops = b.read_shaped_ops(list(ALL_OPERATIONS), b.write_shaped_ops())
    names = {o.name for o in ops}
    assert {
        "memory.search",
        "memory.ask",
        "memory.get",
        "conversation.get",
        "context.fetch",
        "code.search",
        "memory.search_trace_log",
    } <= names
    for w in (
        "memory.set",
        "memory.delete",
        "erasure.request",
        "memory.cross_promote",
        "conversation.append",
        "context.offload",
        "code.outcome",
        "namespace.register",
    ):
        assert w not in names, w
    assert all(int(o.required_role) <= 10 for o in ops) and len(ops) >= 100
    fake = [
        SimpleNamespace(name="x.read", required_role=10),
        SimpleNamespace(name="x.write", required_role=20),
    ]
    assert [o.name for o in b.read_shaped_ops(fake, set())] == ["x.read"]


def test_scope_report_gate_ignores_the_unscopable_cache_but_nothing_else() -> None:
    rep = b.ScopeReport(scope_type="namespace_list", scope_value="ns")
    rep.erasure = {"status": "completed", "residue_count": 0}
    rep.positive_control = True
    rep.after = {"memory": 0, "search_trace_log": 0, "embedding_cache": 3}
    rep.scan = {"census_gaps": []}
    assert rep.passed() and rep.residue_tables == {"embedding_cache": 3}
    rep.after["search_trace_log"] = 1
    assert not rep.passed()
    rep.after["search_trace_log"] = 0
    rep.read_leaks = {"memory.search": ["zq1"]}
    assert not rep.passed()
    rep.read_leaks = {}
    rep.erasure = {"status": "failed", "residue_count": 0}
    assert not rep.passed()
    rep.erasure = {"status": "completed", "residue_count": 2}
    assert not rep.passed()
    rep.erasure = {"status": "completed", "residue_count": 0}
    rep.scan = {"census_gaps": ["entity_vectors"]}
    assert not rep.passed()
    rep.scan = {"census_gaps": []}
    rep.positive_control = False
    assert not rep.passed()  # an empty result set cannot pass by accident
    rep.positive_control = True
    # a 5xx is a handler defect, reported; --strict-status gates on it
    rep.bad_status = {"procedure.locate": 500}
    assert rep.passed() and not rep.passed(strict_status=True)


def test_user_scope_gate_reports_but_does_not_gate_the_tables_without_a_user_axis() -> None:
    rep = b.ScopeReport(scope_type="user_id", scope_value="alice")
    rep.erasure = {"status": "completed", "residue_count": 0}
    rep.positive_control = True
    rep.scan = {"census_gaps": []}
    rep.after = {"memory": 0, "search_trace_log": 2, "conversations": 0}
    assert rep.passed() and rep.residue_tables == {"search_trace_log": 2}
    assert "search_trace_log" not in rep.gated_tables and "conversations" in rep.gated_tables
    rep.after["conversations"] = 1
    assert not rep.passed()
    rep.after["conversations"] = 0
    rep.read_leaks = {"memory.get": ["zq1"]}
    assert not rep.passed() and rep.unexplained_leaks == {"memory.get": ["zq1"]}
    ns = b.ScopeReport(scope_type="namespace_list", scope_value="ns")
    ns.read_leaks = {"memory.get": ["zq1"]}
    assert ns.unexplained_leaks == {"memory.get": ["zq1"]}
    assert "search_trace_log" in ns.gated_tables


def test_a_focus_table_the_bench_could_not_count_is_not_clean() -> None:
    rep = b.ScopeReport(scope_type="namespace_list", scope_value="ns")
    rep.erasure = {"status": "completed", "residue_count": 0}
    rep.positive_control = True
    rep.scan = {"census_gaps": []}
    rep.after = {"memory": -1, "search_trace_log": 0}
    assert rep.residue_tables == {"memory": -1} and not rep.passed()


def test_behavioral_refuses_a_namespace_it_did_not_mint(monkeypatch, capsys) -> None:
    def _no_wire(*_a, **_k):
        raise AssertionError("no request may leave before the namespace check")

    monkeypatch.setattr(b, "run_scope", _no_wire)
    monkeypatch.setattr(b, "_try", _no_wire)
    args = SimpleNamespace(namespace="team-notes", org="o", token="t")
    assert b.behavioral(args) == 2
    assert "purges" in capsys.readouterr().err


def test_writing_modes_need_an_explicit_org(monkeypatch) -> None:
    def _no_wire(*_a, **_k):
        raise AssertionError("no request may leave before the org check")

    for name in ("behavioral", "write_canary", "scan"):
        monkeypatch.setattr(b, name, _no_wire)
    for mode in (["--behavioral"], []):
        monkeypatch.setattr(sys, "argv", ["erasure_compliance.py", "--token", "t", *mode])
        assert b.main() == 2
