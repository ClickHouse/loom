"""Key-only component scoring stays scalar when vector credits are occupied."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from server.core import ch as ch_module
from server.core.errors import VectorAdmissionError
from server.core.vectors import admission
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.search import key_match as app
from server.memory.app.search.sql import _key_match_where, key_match_component_sql, key_match_sql
from server.settings import settings
from tests.unit.test_vector_read_admission import client


@pytest.mark.parametrize("component_only", [True, False], ids=["key-components", "ranked-vector"])
@pytest.mark.parametrize("busy", [False, True], ids=["free-credits", "occupied-credits"])
async def test_key_match_owns_only_its_actual_vector_read(monkeypatch, component_only, busy):
    gate = admission._Admission()
    monkeypatch.setattr(admission, "_admission", gate)
    monkeypatch.setattr(settings, "vector_scan_max_concurrent_per_org", 2)
    monkeypatch.setattr(settings, "vector_scan_max_waiters_per_org", 4)
    monkeypatch.setattr(settings, "memory_search_key_match_min_strength", 0.2)
    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    monkeypatch.setattr(app, "key_match_query_stems", lambda _query: ["task", "worktre"])
    monkeypatch.setattr(app, "key_match_gate", lambda **_kwargs: "pool_key")
    ch, _unused_driver = client(monkeypatch, pool=2)
    calls = []
    native_rows = [
        {
            "memory_key": "task_worktree",
            "strength": 0.9,
            "agent": "a",
            "user_id": "u",
            "session_id": "s1",
        },
        {
            "memory_key": "task_path",
            "strength": 0.5,
            "agent": "a",
            "user_id": "u",
            "session_id": "s1",
        },
    ]

    class Driver:
        async def query(self, sql, *, parameters, settings, **_kwargs):
            calls.append((sql, deepcopy(parameters), deepcopy(settings), admission.current_work()))
            rows = [{"any_key": 1}] if "SELECT 1 AS any_key" in sql else deepcopy(native_rows)
            names = list(rows[0])
            return SimpleNamespace(
                column_names=names,
                result_rows=[tuple(row[k] for k in names) for row in rows],
                summary={},
            )

    monkeypatch.setattr(ch, "_client", Driver())
    monkeypatch.setattr(ch_module, "get_client", lambda: ch)
    hits = [
        SimpleNamespace(
            namespace="private",
            memory_key=row["memory_key"],
            content_excerpt=f"Complete {index} body.\nIts next line stays.",
            rank_score=score,
            score_components={"rank_score_raw": score},
            source="fact",
            session_id="s1",
            _row_identity=("a", "u", "s1"),
        )
        for index, (row, score) in enumerate(zip(native_rows, [0.8, 0.7], strict=True))
    ]
    before = [deepcopy(vars(hit)) for hit in hits]
    inp = MemorySearchInput(
        org="one-org",
        namespace="private",
        agent="a",
        user_id="u",
        session_id="s1",
        query="task worktree",
        top_k=2,
        key_match_mode="gated" if component_only else "always",
        search_mode="semantic",
    )
    ctx = SimpleNamespace(
        input=inp,
        all_hits=hits,
        actual_work={},
        namespaces=["private"],
        query_vec=[0.1, 0.2],
        embedder=SimpleNamespace(model_name="fake"),
        endpoint=None,
        query_shape="",
    )
    occupied = admission.VectorWork("one-org", ("private",), 2)
    holder = (await gate.enter(occupied, 1))[0] if busy else None
    refused = busy and not component_only
    try:
        with admission._without_admission_wait():
            if refused:
                with pytest.raises(VectorAdmissionError) as caught:
                    await app._run_key_match_pool(ctx)
                assert caught.value.code == "VECTOR_CAPACITY_REFUSED"
                assert caught.value.status_code == 429
                assert caught.value.extra == {"reason": "org_queue", "retry_after_s": 1}
                assert caught.value.headers() == {"Retry-After": "1"}
            else:
                await app._run_key_match_pool(ctx)
        assert ctx.all_hits == hits and len(ctx.all_hits) == 2
        expected = deepcopy(before)
        if not refused:
            for hit, row in zip(expected, native_rows, strict=True):
                hit["score_components"]["key_match"] = row["strength"]
            assert ctx.actual_work["key_match_added"] == 0
            assert ctx.actual_work["key_match_precheck_skipped"] == 0
        assert [vars(hit) for hit in ctx.all_hits] == expected
        assert [hit.rank_score for hit in ctx.all_hits] == [0.8, 0.7]
        _assert_dispatched_queries(calls, ctx, component_only, refused)
        assert gate.population == (2 if busy else 0)
        assert ch._inflight == {} and ch._retired == {}
        assert ch._slots is not None and ch._slots._value == 2
        assert admission.current_work() is None
    finally:
        if holder is not None:
            gate.leave(occupied, holder)
    assert gate.population == 0 and gate.orgs == {}


def _assert_dispatched_queries(calls, ctx, component_only, refused):
    if component_only:
        assert ctx.actual_work["key_match_gate"] == "pool_key"
        assert ctx.actual_work["key_match_component_only"] is True
    else:
        assert calls[0][0].strip().endswith("LIMIT 1")
        assert calls[0][1] == {"org": "one-org", "ns": "private", "stems": ["task", "worktre"]}
        assert calls[0][3] is None and "max_rows_to_read" not in calls[0][2]
    if not refused:
        sql, params, native, work = calls[-1]
        expected_params = {
            "org": "one-org",
            "ns": "private",
            "stems": ["task", "worktre"],
            "limit": 2 if component_only else 20,
            "per_key": 3,
            "model": "fake",
            "min_strength": 0.2,
            "agent": "a",
            "rs_user_id": "u",
            "rs_session_id": "s1",
        }
        if component_only:
            expected_params["pool_keys"] = ["task_path", "task_worktree"]
        else:
            expected_params["qv"] = [0.1, 0.2]
        assert params == expected_params
        builder = key_match_component_sql if component_only else key_match_sql
        assert sql == builder(" AND ".join(_key_match_where(ctx.input, dict(expected_params))))
        assert ("cosineDistance" in sql) is (not component_only)
        assert work == (
            None if component_only else admission.VectorWork("one-org", ("private",), 1)
        )
        assert ("max_rows_to_read" in native) is (not component_only)
        if not component_only:
            assert native["max_rows_to_read"] == settings.vector_scan_max_rows_to_read
            assert native["read_overflow_mode"] == "throw"
    assert len(calls) == (1 if component_only or refused else 2)
