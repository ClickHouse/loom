"""Blacklist sweep: a pattern reaches the rows written before it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app import blacklist_sweep as bs


def test_sweep_where_mirrors_the_in_process_matcher() -> None:
    where, params = bs.sweep_where("substring", "namespace", "personal")
    assert "position(value, {pat:String}) > 0" in where and params == {"ns": "personal"}
    assert "namespace = {ns:String}" in where and "tombstoned_at IS NULL" in where
    where_re, params_re = bs.sweep_where("regex", "org", "personal")
    assert "match(value, {pat:String})" in where_re and "namespace" not in where_re
    assert params_re == {}


class _CH:
    def __init__(self, rows: list[dict[str, Any]], fail: bool = False) -> None:
        self.rows, self.fail = rows, fail
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.queries.append((" ".join(sql.split()), dict(params or {})))
        if self.fail:
            raise RuntimeError("Regexp (?<=x) — lookbehind not supported")
        return list(self.rows)


async def test_sweep_tombstones_matches_through_the_cascade_and_records_drops(monkeypatch) -> None:
    calls: dict[str, Any] = {}

    row = SimpleNamespace(
        org="o",
        namespace="a",
        agent="ag",
        user_id="-",
        session_id="-",
        memory_key="k1",
        memory_id="m1",
    )

    async def fake_cascade(org, ids, *, when, namespace, update=None, ch=None, skip=None):
        # One call per namespace: a restored namespace keeps the memory ids.
        calls.setdefault("ids", []).extend((namespace, mid) for mid in ids)
        calls["override"] = update(row) if update else {}
        return [{"memory_id": mid, "version": 7} for mid in ids]

    announced: list[tuple[str, dict[str, Any]]] = []

    async def fake_announce(changes):
        announced.extend(changes)

    drops: list[tuple[str, str, str, str, int]] = []
    monkeypatch.setattr("server.memory.store.tombstone_cascade_by_ids", fake_cascade)
    monkeypatch.setattr("server.memory.app.webhooks.announce", fake_announce)
    monkeypatch.setattr(
        "server.memory.app.drops.record_drop",
        lambda org, ns, stage, reason, *, item="", n=1: drops.append((org, ns, stage, reason, n)),
    )
    ch = _CH(
        [
            {"memory_id": "m1", "namespace": "a"},
            {"memory_id": "m2", "namespace": "a"},
            {"memory_id": "m3", "namespace": "b"},
        ]
    )
    res = await bs.sweep_pattern(
        org="o",
        namespace="a",
        pattern_id="p1",
        pattern="secret",
        pattern_type="substring",
        scope="org",
        ch=ch,
    )
    assert (res.matched, res.tombstoned, res.truncated) == (3, 3, False)
    assert calls["ids"] == [("a", "m1"), ("a", "m2"), ("b", "m3")]
    assert calls["override"] == {"expire_reason": "blacklist.sweep:p1"}
    # One event per row the cascade tombstoned, at the version it wrote.
    assert [(event, change["memory_id"], change["version"]) for event, change in announced] == [
        ("memory.tombstoned", "m1", 7),
        ("memory.tombstoned", "m2", 7),
        ("memory.tombstoned", "m3", 7),
    ]
    assert sorted(drops) == [
        ("o", "a", "blacklist.sweep", "pattern:p1", 2),
        ("o", "b", "blacklist.sweep", "pattern:p1", 1),
    ]
    sql, params = ch.queries[0]
    assert "FROM loom.memory FINAL" in sql and params["pat"] == "secret" and params["org"] == "o"
    assert "namespace = {ns:String}" not in sql  # org scope: the whole org


async def test_sweep_reports_truncation_and_a_failed_regex(monkeypatch) -> None:
    async def fake_cascade(org, ids, *, when, namespace, update=None, ch=None, skip=None):
        return [{"memory_id": mid} for mid in ids]

    monkeypatch.setattr("server.memory.store.tombstone_cascade_by_ids", fake_cascade)
    monkeypatch.setattr("server.memory.app.drops.record_drop", lambda *a, **k: None)
    rows = [{"memory_id": f"m{i}", "namespace": "a"} for i in range(4)]
    res = await bs.sweep_pattern(
        org="o",
        namespace="a",
        pattern_id="p",
        pattern="x",
        pattern_type="substring",
        scope="namespace",
        limit=3,
        ch=_CH(rows),
    )
    assert res.matched == 3 and res.tombstoned == 3 and res.truncated is True
    bad = await bs.sweep_pattern(
        org="o",
        namespace="a",
        pattern_id="p",
        pattern="(?<=x)y",
        pattern_type="regex",
        scope="namespace",
        ch=_CH([], fail=True),
    )
    assert bad.matched == 0 and "lookbehind" in bad.error


async def test_add_with_sweep_reports_the_swept_count(monkeypatch) -> None:
    import server.memory.app.blacklist as h
    from server.memory.api.models_blacklist import BlacklistAddInput

    inserted: list[dict[str, Any]] = []

    async def fake_insert(row):
        inserted.append(row)

    async def fake_bump(**kw):
        return None

    async def fake_sweep(**kw):
        return bs.SweepResult(kw["pattern_id"], 5, 5, False)

    monkeypatch.setattr(h.blacklist_repo, "insert_row", fake_insert)
    monkeypatch.setattr(h, "_bump_version", fake_bump)
    monkeypatch.setattr(h, "invalidate_cache", lambda: None)
    monkeypatch.setattr("server.memory.app.blacklist_sweep.sweep_pattern", fake_sweep)
    out = await h.handle_blacklist_add(
        BlacklistAddInput(org="o", namespace="a", pattern="secret", sweep=True)
    )
    assert out.swept == 5 and out.swept_truncated is False and inserted[0]["pattern"] == "secret"
    plain = await h.handle_blacklist_add(BlacklistAddInput(org="o", namespace="a", pattern="s2"))
    assert plain.swept == 0


async def test_sweep_op_404s_on_an_unknown_pattern(monkeypatch) -> None:
    from fastapi import HTTPException

    import server.memory.app.blacklist as h
    from server.memory.api.models_blacklist import BlacklistSweepInput

    async def none(*a, **k):
        return None

    monkeypatch.setattr(h.blacklist_repo, "find_active_pattern", none)
    with pytest.raises(HTTPException) as ei:
        await h.handle_blacklist_sweep(BlacklistSweepInput(org="o", namespace="a", pattern_id="zz"))
    assert ei.value.status_code == 404


def test_sweep_is_hidden_from_the_mcp_chat_surface() -> None:
    from server.operations.toolsets import _MCP_HIDDEN_OPS

    assert "blacklist.sweep" in _MCP_HIDDEN_OPS and "blacklist.add" not in _MCP_HIDDEN_OPS
