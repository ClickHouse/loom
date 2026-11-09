"""The store mentors the agent that uses it.

The mentor service reads one agent's ledger profile and its namespace's
facts, evaluates a fixed rule table and hands back hints with evidence;
`context.pack` renders up to three at session start, both transports attach
one per call (each code once per session), `mentor.brief` returns the whole
profile with adoption, and every hint reaches the ledger as `out_hint_code`.
Everything here runs against a fake store: no ClickHouse.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.insights.app import mentor as m
from server.insights.app import mentor_brief as handler
from server.insights.app import mentor_rules
from server.observability import ledger
from server.settings import settings
from tests.unit.clickhouse_local import CLICKHOUSE, ddl, run


class _CH:
    """Answers `query_all` / `query_one` by the first marker found in the SQL."""

    def __init__(self, by_marker: dict[str, Any] | None = None):
        self.by_marker = by_marker or {}
        self.sqls: list[str] = []
        self.params: list[dict[str, Any]] = []

    def _answer(self, sql: str) -> Any:
        for marker, rows in self.by_marker.items():
            if marker in sql:
                if isinstance(rows, Exception):
                    raise rows
                return rows
        return None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        self.params.append(params or {})
        rows = self._answer(sql)
        return list(rows) if rows else []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.sqls.append(sql)
        self.params.append(params or {})
        rows = self._answer(sql)
        if isinstance(rows, list):
            return rows[0] if rows else None
        return rows


# Markers: one per read, unique to that read's SQL.
M_USAGE = "GROUP BY operation ORDER BY n DESC LIMIT 400"
M_SESS = "uniqExact(session_id) AS sessions"
M_FIRST = "AS first_op"
M_OUT = "AS dup_sets"
M_HOT = "maxIf(n, op_class = 'set')"
M_PREFIX = "AS top_prefix_sets"
M_NSLEDGER = "AS conflict_ops"
M_NSROWS = "AS coord_keys"
M_DOCS = "FROM loom.documents"
M_RECS = "FROM loom.memory_records"
# The agent-scoped shape read: documents and checkpoints this agent wrote.
M_SHAPES = "AS json_prefix"
# The hot read key's live value length, the agent's subjects and their grants.
M_HOTCHARS = "AS hot_key_chars"
# Reliance: what run.complete credited, and whether reads answered.
M_RELY = "AS credited_keys"
M_MISS = "AS keys_asked"
M_SUBJECTS = "SELECT DISTINCT subject"
M_GRANTS = "FROM loom.namespace_grants FINAL"


def _usage(**ops: int) -> list[dict[str, Any]]:
    return [{"operation": k.replace("__", "."), "n": v} for k, v in ops.items()]


def _store(usage: list[dict[str, Any]], sessions: int, **facts: Any) -> dict[str, Any]:
    """A fake store for one agent: usage rows, a session count and the facts
    the other reads answer (defaults are a quiet, healthy namespace)."""
    f = {
        "pack_first": 0,
        "dup_sets": 0,
        "abstains": 0,
        "large_sets": 0,
        "huge_sets": 0,
        "tracker_tagged_sets": 0,
        "id_keyed_sets": 0,
        "max_sets_one_key": 0,
        "max_gets_one_key": 0,
        "hot_set_key": "",
        "hot_get_key": "",
        "top_prefix_sets": 0,
        "top_prefix": "",
        "conflict_ops": 0,
        "writers": 1,
        "live_rows": 100,
        "entity_rows": 0,
        "conflicted_rows": 0,
        "coord_keys": 0,
        "documents": 0,
        "records": 0,
        "oversized": 0,
        "json_prefix": 0,
        "episodic_blobs": 0,
        "hot_key_chars": 0,
        "subjects": [],
        "granted": [],
        "credited_keys": 0,
        "credited_chunk_keys": 0,
        "credited_unresolvable": 0,
        "keys_asked": 0,
        "keys_missed": 0,
    }
    f.update(facts)
    return {
        M_USAGE: usage,
        M_SESS: [{"sessions": sessions}],
        M_FIRST: [{"sessions": sessions, "pack_first": f["pack_first"]}],
        M_OUT: [
            {
                k: f[k]
                for k in (
                    "dup_sets",
                    "abstains",
                    "large_sets",
                    "huge_sets",
                    "tracker_tagged_sets",
                    "id_keyed_sets",
                )
            }
        ],
        M_HOT: [
            {
                k: f[k]
                for k in ("max_sets_one_key", "max_gets_one_key", "hot_set_key", "hot_get_key")
            }
        ],
        M_PREFIX: [{"top_prefix_sets": f["top_prefix_sets"], "top_prefix": f["top_prefix"]}],
        M_NSLEDGER: [
            {
                "conflict_ops": f["conflict_ops"],
                "writers": f["writers"],
            }
        ],
        M_NSROWS: [
            {k: f[k] for k in ("live_rows", "entity_rows", "conflicted_rows", "coord_keys")}
        ],
        M_DOCS: [{"n": f["documents"]}],
        M_RECS: [{"n": f["records"]}],
        M_SHAPES: [
            {
                "oversized": f["oversized"],
                "json_prefix": f["json_prefix"],
                "episodic_blobs": f["episodic_blobs"],
            }
        ],
        M_HOTCHARS: [{"hot_key_chars": f["hot_key_chars"]}],
        M_RELY: [
            {k: f[k] for k in ("credited_keys", "credited_chunk_keys", "credited_unresolvable")}
        ],
        M_MISS: [{"keys_asked": f["keys_asked"], "keys_missed": f["keys_missed"]}],
        M_SUBJECTS: [{"subject": s} for s in f["subjects"]],
        M_GRANTS: [{"namespace": n} for n in f["granted"]],
    }


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch):
    m.reset_cache()
    monkeypatch.setattr(settings, "mentor_ttl_seconds", 3600, raising=False)
    monkeypatch.setattr(settings, "mentor_max_hints_per_session", 6, raising=False)
    yield
    m.reset_cache()


_last_ch: _CH | None = None


def _wire(
    monkeypatch: pytest.MonkeyPatch,
    by_marker: dict[str, Any],
    *,
    repos: int = 0,
    qa: bool = False,
) -> _CH:
    ch = _CH(by_marker)
    import server.core.ch as _ch
    from server.code.domain.index import repos as _repos
    from server.tracker.domain import sources as _sources

    monkeypatch.setattr(_ch, "get_client", lambda: ch)
    global _last_ch
    _last_ch = ch
    monkeypatch.setattr(
        _repos,
        "configured_repos",
        lambda: [
            SimpleNamespace(org="o", namespace=f"code-{i}", repo=f"acme/r{i}", org_readable=True)
            for i in range(repos)
        ],
    )
    monkeypatch.setattr(_sources, "endpoints", lambda: {"qa": object()} if qa else {})
    monkeypatch.setattr("server.tracker.domain.sources.credential_configured", lambda *_a, **_k: qa)
    return ch


def _profile(**kw: Any) -> mentor_rules.MentorProfile | None:
    return asyncio.run(m.mentor_profile("o", "ns", kw.pop("agent", "a"), **kw))


def _codes(p: mentor_rules.MentorProfile | None) -> set[str]:
    return {h.code for h in (p.hints if p else ())}


# --- the rule table -------------------------------------------------------------


def test_the_plugin_agent_of_the_ledger_is_told_about_the_code_index_and_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # claude-code: 1,159 sets, 5 searches, 4 gets, 46 packs, 0 code, 43 sessions,
    # in an org with indexed repos.
    _wire(
        monkeypatch,
        _store(
            _usage(memory__set=1159, memory__search=5, memory__get=4, context__pack=46),
            43,
        ),
        repos=3,
    )
    p = _profile()
    assert p is not None
    codes = _codes(p)
    assert "code_index_unused" in codes
    assert "runs_unused" in codes
    # 55 reads over 1,159 writes is above the 2 % floor — not "never read".
    assert "writes_never_read" not in codes
    # Packs are called, so the session-pack nudge stays quiet.
    assert "no_session_pack" not in codes
    code_hint = p.hint("code_index_unused")
    assert code_hint is not None and code_hint.severity == "warning"
    assert "code.search" in code_hint.text and code_hint.evidence == {"repos": 3}
    assert code_hint.ops[0] == "code.search"
    # Warnings before infos.
    assert [h.severity for h in p.hints] == sorted(
        (h.severity for h in p.hints), key=lambda s: {"critical": 0, "warning": 1, "info": 2}[s]
    )


def test_an_object_store_agent_is_told_to_recall_by_meaning_and_move_the_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A namespace that needs a reshape: 6,606 gets, 52 searches, one lease read 116×.
    _wire(
        monkeypatch,
        _store(
            _usage(memory__get=6606, memory__search=52, memory__set=1687),
            30,
            max_gets_one_key=116,
            hot_get_key="sre/lease",
        ),
    )
    p = _profile()
    codes = _codes(p)
    assert {"recall_by_key_only", "coordination_loop"} <= codes
    assert p is not None
    loop = p.hint("coordination_loop")
    assert loop is not None and "sre/lease" in loop.text and "working_memory" in loop.text
    # The sentence names the READ key with its read count and says nothing
    # about writes (the hottest written key is another row).
    assert "(sre/lease) was read 116×" in loop.text and "written" not in loop.text
    key_only = p.hint("recall_by_key_only")
    assert key_only is not None and key_only.evidence == {"gets": 6606, "searches": 52}


def test_a_balanced_reader_is_not_called_an_object_store(monkeypatch: pytest.MonkeyPatch) -> None:
    # clickgap: 47,119 gets to 23,973 searches — under the 5× ratio.
    _wire(
        monkeypatch,
        _store(
            _usage(memory__get=47119, memory__search=23973, memory__set=1986, run__begin=9), 1356
        ),
    )
    assert "recall_by_key_only" not in _codes(_profile())


def test_tracker_hint_needs_a_source_test_tagged_writes_and_no_qa_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(monkeypatch, _store(_usage(memory__set=40), 5, tracker_tagged_sets=12), qa=True)
    p = _profile()
    assert "tracker_unused" in _codes(p)
    assert p is not None and p.hint("tracker_unused").evidence == {  # type: ignore[union-attr]
        "tracker_tagged_sets": 12,
        "sources": ["qa"],
    }
    text = p.hint("tracker_unused").text  # type: ignore[union-attr]
    assert "0 tracker.* calls" in text and "qa." not in text
    m.reset_cache()
    _wire(
        monkeypatch,
        _store(_usage(memory__set=40, tracker__similar=3), 5, tracker_tagged_sets=12),
        qa=True,
    )
    assert "tracker_unused" not in _codes(_profile())
    m.reset_cache()
    _wire(monkeypatch, _store(_usage(memory__set=40), 5, tracker_tagged_sets=12), qa=False)
    assert "tracker_unused" not in _codes(_profile())


def test_duplicates_need_five_and_a_tenth_of_the_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _store(_usage(memory__set=100), 4, dup_sets=30))
    assert "duplicates_written" in _codes(_profile())
    m.reset_cache()
    _wire(monkeypatch, _store(_usage(memory__set=100), 4, dup_sets=3))
    assert "duplicates_written" not in _codes(_profile())


def test_every_other_rule_fires_on_its_fact_and_the_profile_keeps_eight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        _store(
            _usage(
                memory__set=200,
                memory__search=20,
                code__blame_range=2,
                code__history=1,
                memory__link=4,
            ),
            12,
            abstains=9,
            huge_sets=3,
            oversized=4,
            id_keyed_sets=40,
            top_prefix_sets=12,
            top_prefix="pr-",
            conflicted_rows=3,
            writers=2,
            coord_keys=1,
            entity_rows=60,
        ),
        repos=1,
    )
    p = _profile()
    assert p is not None
    codes = _codes(p)
    expected = {
        "search_abstains",
        "checkpoints_as_memory",
        "documents_as_memories",
        "records_for_identities",
        "conflicts_pending",
        "facts_graph_unused",
        "bisect_available",
        "runs_unused",
        "no_session_pack",
    }
    # Nine rules fire (the blame calls are code.* calls, so the code index is not
    # "unused"); the profile keeps the eight that matter most, warnings first.
    assert len(p.hints) == mentor_rules.PROFILE_MAX_HINTS
    assert codes <= expected
    assert {h.code for h in p.hints if h.severity == "warning"} == {
        "search_abstains",
        "checkpoints_as_memory",
        "documents_as_memories",
    }
    full = mentor_rules.evaluate_rules(p.usage, p.sessions, p.facts, agent=p.agent, max_hints=32)
    assert {h.code for h in full} == expected
    for h in full:
        assert len(h.text) <= mentor_rules.HINT_CHARS
        assert h.ops


def test_a_well_used_namespace_gets_no_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    # A reshaped namespace: eight planes in use.
    _wire(
        monkeypatch,
        _store(
            _usage(
                memory__get=6795,
                memory__search=1633,
                memory__set=4100,
                memory__record__write=10149,
                working_memory__set=113,
                context__offload=73,
                context__pack=40,
                run__begin=12,
                code__search=2,
                facts__traverse=5,
            ),
            40,
            pack_first=40,
            writers=2,
            entity_rows=900,
            documents=12,
            records=5000,
        ),
        repos=2,
    )
    p = _profile()
    assert p is not None
    assert not [h for h in p.hints if h.severity in ("warning", "critical")], p.hints


def test_reliance_rules_fire_on_chunk_credit_missing_keys_and_read_misses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A consumer that credits 600+ chunk ids a day in
    run.complete(memories_used) and whose bulk_get misses a quarter of its
    keys is visible in the run table and the ledger's read-outcome columns;
    the mentor reads them."""
    _wire(
        monkeypatch,
        _store(
            _usage(memory__search=300, memory__bulk_get=200, run__complete=300),
            12,
            credited_keys=1000,
            credited_chunk_keys=450,
            credited_unresolvable=80,
            keys_asked=4000,
            keys_missed=1100,
        ),
    )
    p = _profile(agent="clickgap")
    assert p is not None
    codes = _codes(p)
    assert {"relied_on_chunk_ids", "relied_on_missing_keys", "reads_miss"} <= codes
    by = {h.code: h for h in p.hints}
    assert by["relied_on_chunk_ids"].severity == "warning"
    assert "450 of 1000" in by["relied_on_chunk_ids"].text
    assert "parent_memory_key" in by["relied_on_chunk_ids"].text
    assert by["relied_on_chunk_ids"].ops == ("run.complete", "memory.bulk_get")
    assert "80 of 1000" in by["relied_on_missing_keys"].text
    assert "1100 of 4000" in by["reads_miss"].text
    # the facts come from the run table and the ledger's outcome columns
    assert any(
        "FROM loom.agent_runs FINAL" in q and "arrayJoin(memories_used)" in q for q in _last_ch.sqls
    )
    assert any("args['out_missing_n']" in q and "args['out_found']" in q for q in _last_ch.sqls)


def test_reliance_rules_stay_quiet_below_their_floors(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(
        monkeypatch,
        _store(
            _usage(memory__search=300, memory__bulk_get=200, run__complete=300),
            12,
            credited_keys=1000,
            credited_chunk_keys=50,  # 5% < 10%
            credited_unresolvable=20,  # 2% < 5%
            keys_asked=4000,
            keys_missed=400,  # 10% < 20%
        ),
    )
    codes = _codes(_profile(agent="clickgap"))
    assert not ({"relied_on_chunk_ids", "relied_on_missing_keys", "reads_miss"} & codes)


def test_a_missing_fact_keeps_its_rule_quiet_and_lands_in_skipped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(_usage(memory__set=100), 4, dup_sets=50)
    store[M_OUT] = RuntimeError("ledger down")
    _wire(monkeypatch, store)
    p = _profile()
    assert p is not None
    assert "duplicates_written" not in _codes(p)
    assert any(s.startswith("outcomes:") for s in p.skipped)


def test_shape_read_is_scoped_to_the_agent_and_skips_structured_types(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _wire(monkeypatch, _store(_usage(memory__set=40), 3, oversized=2))
    p = _profile(agent="claude-code")
    assert p is not None and "documents_as_memories" in _codes(p)
    shape_sql = [q for q in ch.sqls if M_SHAPES in q]
    assert len(shape_sql) == 1
    assert "agent = {agent:String}" in shape_sql[0]
    assert "memory_type NOT IN {structured:Array(String)}" in shape_sql[0]
    hint = p.hint("documents_as_memories")
    assert hint is not None and "this agent wrote" in hint.text


def test_planes_carry_the_mentored_flag() -> None:
    assert mentor_rules.plane_mentored("code index") and mentor_rules.plane_mentored("documents")
    for plane in ("conversations", "profile", "namespace", "erasure"):
        assert not mentor_rules.plane_mentored(plane), plane
    assert not mentor_rules.plane_mentored("nonexistent")


# --- cache ----------------------------------------------------------------------


def test_profile_is_cached_per_agent_and_a_failure_is_remembered_briefly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _wire(monkeypatch, _store(_usage(memory__set=5), 1))
    clock = [1000.0]
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: clock[0]))
    n = len(ch.sqls)
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: clock[0]))
    assert len(ch.sqls) == n  # cache hit
    asyncio.run(m.mentor_profile("o", "ns", "b", clock=lambda: clock[0]))
    assert len(ch.sqls) > n  # another agent is another profile
    clock[0] += 3601
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: clock[0]))
    assert len(ch.sqls) > 2 * n - 1  # expired → re-read

    # A total failure: None, remembered for FAILURE_TTL_SECONDS, no re-read inside it.
    m.reset_cache()
    ch2 = _wire(monkeypatch, {M_USAGE: RuntimeError("down")})

    async def _boom(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(m, "_compute", _boom)
    assert asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: 5.0)) is None
    before = len(ch2.sqls)
    assert asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: 6.0)) is None
    assert len(ch2.sqls) == before
    assert (
        asyncio.run(
            m.mentor_profile(
                "o",
                "ns",
                "a",
                clock=lambda: 5.0 + m.FAILURE_TTL_SECONDS + 1,
            )
        )
        is None
    )


def test_ttl_zero_turns_the_mentor_off(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _wire(monkeypatch, _store(_usage(memory__set=500), 9))
    monkeypatch.setattr(settings, "mentor_ttl_seconds", 0, raising=False)
    assert _profile() is None
    assert ch.sqls == []
    assert (
        m.call_hint(
            "memory.get",
            {"memory_key": "k"},
            {"found": False},
            session_key="s",
            org="o",
            namespace="ns",
            agent="a",
        )
        is None
    )


# --- rendering ------------------------------------------------------------------


def _fake_profile(*hints: mentor_rules.Hint, **kw: Any) -> mentor_rules.MentorProfile:
    return mentor_rules.MentorProfile(
        org="o",
        namespace="ns",
        agent=kw.get("agent", "a"),
        window_seconds=604800,
        usage=kw.get("usage", {"memory.set": 10}),
        sessions=kw.get("sessions", 3),
        facts=kw.get("facts", {}),
        hints=tuple(hints),
    )


def _h(code: str, severity: str = "warning", text: str | None = None) -> mentor_rules.Hint:
    return mentor_rules.Hint(
        code=code, severity=severity, text=text or f"{code} text", ops=("memory.search",)
    )


def test_pack_section_renders_three_hints_within_the_cap_and_nothing_when_quiet() -> None:
    assert m.render_pack_section(None) == ""
    assert m.render_pack_section(_fake_profile()) == ""
    p = _fake_profile(_h("a"), _h("b"), _h("c"), _h("d"))
    body = m.render_pack_section(p)
    assert body.startswith("Loom mentor — a: a text · b: b text · c: c text")
    assert "d: d text" not in body and "mentor.brief" in body
    long = _fake_profile(*[_h(f"c{i}", text="x" * mentor_rules.HINT_CHARS) for i in range(3)])
    body = m.render_pack_section(long)
    assert len(body) <= m.PACK_CHARS and body.count("·") <= 1


def test_top_hint_line_reads_only_the_cache() -> None:
    assert m.top_hint_line("o", "ns", "a") == ""
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(_h("code_index_unused")))
    assert "code_index_unused text" in m.top_hint_line("o", "ns", "a")
    assert "mentor.brief" in m.top_hint_line("o", "ns", "a")


# --- the per-call channel --------------------------------------------------------


def _call(
    op: str, args: dict[str, Any], result: Any, *, session: str = "s1"
) -> tuple[str, str] | None:
    return m.call_hint(op, args, result, session_key=session, org="o", namespace="ns", agent="a")


def test_the_dashboard_and_the_assistant_get_no_call_hint() -> None:
    miss = {"found": False}
    for consumer in ("loom-ui", "loom-assistant"):
        args = {"memory_key": "deploy_window", "consumer": consumer}
        assert _call("memory.get", args, miss, session=f"silent-{consumer}") is None
    assert _call("memory.get", {"memory_key": "deploy_window"}, miss, session="loud") is not None


def test_get_miss_fires_once_per_session() -> None:
    hint = _call("memory.get", {"memory_key": "deploy_window"}, {"found": False})
    assert hint is not None and hint[0] == "get_miss" and "deploy_window" in hint[1]
    assert "memory.search" in hint[1]
    assert _call("memory.get", {"memory_key": "other"}, {"found": False}) is None
    assert _call("memory.get", {"memory_key": "other"}, {"found": False}, session="s2") is not None
    # A found row says nothing.
    assert (
        _call("memory.get", {"memory_key": "k"}, {"found": True, "value": "v"}, session="s3")
        is None
    )


def test_set_rules_duplicate_large_value_and_churn() -> None:
    dup = _call("memory.set", {"memory_key": "k", "value": "v"}, {"duplicate_of": "k0"})
    assert dup is not None and dup[0] == "set_duplicate" and "k0" in dup[1]
    big = _call("memory.set", {"memory_key": "k2", "value": "x" * 5000}, {"memory_id": "1"})
    assert big is not None and big[0] == "set_large_value" and "knowledge.upsert" in big[1]
    for i in range(4):
        assert (
            _call(
                "memory.set",
                {"memory_key": "hot", "value": str(i)},
                {"memory_id": "1"},
                session="s9",
            )
            is None
        )
    churn = _call(
        "memory.set", {"memory_key": "hot", "value": "5"}, {"memory_id": "1"}, session="s9"
    )
    assert churn is not None and churn[0] == "set_churn" and "working_memory" in churn[1]


def test_run_over_budget_fires_only_when_run_get_names_a_budget() -> None:
    run = {"status": "active", "budget_usd": 2.0}
    within = {"run": run, "over_budget": [], "cost_usd": 0.4, "elapsed_seconds": 30}
    assert _call("run.get", {"run_id": "r1"}, within) is None
    over = {"run": run, "over_budget": ["cost"], "cost_usd": 2.5, "elapsed_seconds": 30}
    hint = _call("run.get", {"run_id": "r1"}, over, session="s2")
    assert hint is not None and hint[0] == "run_over_budget"
    assert "cost budget ($2.50 of $2.00 spent)" in hint[1] and "run.complete" in hint[1]


def test_run_over_budget_names_only_the_budgets_it_went_past() -> None:
    run = {"status": "active", "budget_usd": 0.005, "budget_seconds": 60}
    time_only = {"run": run, "over_budget": ["time"], "cost_usd": 0.0, "elapsed_seconds": 90}
    hint = _call("run.get", {"run_id": "r1"}, time_only, session="s-time")
    assert hint is not None and "time budget (1m 30s of 60s)" in hint[1] and "$" not in hint[1]
    both = {"run": run, "over_budget": ["cost", "time"], "cost_usd": 0.0062, "elapsed_seconds": 90}
    hint = _call("run.get", {"run_id": "r1"}, both, session="s-both")
    # A sub-cent budget is shown as it is, not as $0.00.
    expected = "cost and time budgets ($0.0062 of $0.005 spent, 1m 30s of 60s)"
    assert hint is not None and expected in hint[1]


def test_run_over_budget_shows_a_spend_just_past_the_budget() -> None:
    run = {"status": "active", "budget_usd": 1.0}
    just_over = {"run": run, "over_budget": ["cost"], "cost_usd": 1.000004}
    hint = _call("run.get", {"run_id": "r1"}, just_over, session="s-just")
    assert hint is not None and "$1.000004 of $1.00 spent" in hint[1]
    # A budget is not rounded up past the spend either.
    run = {"status": "active", "budget_usd": 1.235}
    just_over = {"run": run, "over_budget": ["cost"], "cost_usd": 1.23516}
    hint = _call("run.get", {"run_id": "r1"}, just_over, session="s-odd")
    assert hint is not None and "$1.23516 of $1.235 spent" in hint[1]


def test_run_over_budget_spells_a_large_spend_in_dollars() -> None:
    run = {"status": "active", "budget_usd": 10000.0}
    big = {"run": run, "over_budget": ["cost"], "cost_usd": 12345.0}
    hint = _call("run.get", {"run_id": "r1"}, big, session="s-big")
    assert hint is not None and "$12,345.00 of $10,000.00 spent" in hint[1]


def test_run_over_budget_spells_a_long_time_budget_as_the_runs_page_does() -> None:
    run = {"status": "active", "budget_seconds": 3600}
    over = {"run": run, "over_budget": ["time"], "elapsed_seconds": 5400}
    hint = _call("run.get", {"run_id": "r1"}, over, session="s-long")
    assert hint is not None and "time budget (1h 30m of 60m 0s)" in hint[1]


@pytest.mark.parametrize("status", ["completed", "failed", "cancelled"])
def test_run_over_budget_says_nothing_about_a_closed_run(status: str) -> None:
    closed = {"run": {"status": status}, "over_budget": ["time"], "elapsed_seconds": 90}
    assert _call("run.get", {"run_id": "r1"}, closed, session=f"s-{status}") is None


def test_search_abstain_points_at_the_plane_the_namespace_has() -> None:
    # No cached profile: nothing to say, and a background read is scheduled (not run here).
    assert _call("memory.search", {"query": "q"}, {"abstain": True, "hits": []}) is None
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(facts={"repos": 2, "documents": 0}))
    hint = _call("memory.search", {"query": "q"}, {"abstain": True}, session="s2")
    assert hint is not None and hint[0] == "search_abstain_code" and "code.symbol" in hint[1]
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(facts={"repos": 0, "documents": 7}))
    hint = _call("memory.search", {"query": "q"}, {"abstain": True}, session="s3")
    assert hint is not None and hint[0] == "search_abstain_knowledge" and "7 documents" in hint[1]
    # A search that answered says nothing.
    assert _call("memory.search", {"query": "q"}, {"abstain": False}, session="s4") is None


def test_profile_hints_ride_calls_once_each_up_to_the_session_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(*[_h(f"r{i}") for i in range(8)]))
    monkeypatch.setattr(settings, "mentor_max_hints_per_session", 3, raising=False)
    seen = []
    for _ in range(6):
        hint = _call("memory.list", {}, {"items": []}, session="cap")
        if hint:
            seen.append(hint[0])
    assert seen == ["r0", "r1", "r2"]


def test_call_hint_never_raises_on_odd_input() -> None:
    # The contract is "no exception"; an odd input may still earn a hint.
    hint = _call("memory.get", {"memory_key": object()}, object())
    assert hint is None or (isinstance(hint, tuple) and all(isinstance(p, str) for p in hint))
    assert _call("memory.set", {"value": {"a": [1, 2]}}, None) is None


# --- the ledger row ----------------------------------------------------------------


def test_the_hint_code_reaches_the_ledger_projection() -> None:
    from server.observability.event_args import IDENTIFIER_KEYS, project_result

    assert "hint_code" in IDENTIFIER_KEYS
    assert project_result({"hint_code": "get_miss", "value": "secret"}) == {
        "out_hint_code": "get_miss"
    }
    ledger.set_hint_context("set_churn")
    assert ledger.current_hint_code() == "set_churn"
    ledger.set_hint_context("")
    assert ledger.current_hint_code() == ""


def test_emit_agent_event_carries_the_current_hint_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.observability import ledger

    rows: list[Any] = []

    class _Emitter:
        def emit(self, ev: Any) -> None:
            rows.append(ev)

    monkeypatch.setattr(ledger, "get_emitter", lambda: _Emitter())
    ledger.set_hint_context("get_miss")
    try:
        ledger.emit_agent_event(
            op_name="memory.get",
            identity={
                "org": "o",
                "namespace": "ns",
                "agent": "a",
                "user_id": "-",
                "session_id": "s",
            },
            request_id="r",
            latency_ms=1,
            success=True,
            error_code="",
            raw_args={"memory_key": "k"},
        )
    finally:
        ledger.set_hint_context("")
    assert rows and rows[0].args.get("out_hint_code") == "get_miss"


# --- the transports -----------------------------------------------------------------


def test_mcp_helper_attaches_the_hint_and_publishes_the_code() -> None:
    from server.transport import mcp_outcome

    hint = mcp_outcome._mentor_hint(
        "memory.get",
        {"org": "o", "namespace": "ns", "agent": "a", "memory_key": "k"},
        {"found": False},
        "mcp-session-1",
    )
    assert hint is not None and hint[0] == "get_miss"
    assert ledger.current_hint_code() == "get_miss"
    # Same session, same code: nothing more, and the context is cleared.
    assert (
        mcp_outcome._mentor_hint(
            "memory.get",
            {"org": "o", "namespace": "ns", "memory_key": "k2"},
            {"found": False},
            "mcp-session-1",
        )
        is None
    )
    assert ledger.current_hint_code() == ""


def test_http_helper_rides_the_hint_as_headers() -> None:
    from fastapi import Response

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import http

    op = OPERATIONS_BY_NAME["memory.get"]
    resp = Response()
    http._mentor_headers(
        op,
        {"org": "o", "namespace": "ns", "agent": "a", "memory_key": "k", "session_id": "h1"},
        {"found": False},
        resp,
    )
    assert resp.headers["X-Loom-Hint-Code"] == "get_miss"
    assert "memory.search" in resp.headers["X-Loom-Hint"]
    assert ledger.current_hint_code() == "get_miss"
    ledger.set_hint_context("")


# --- context.pack -------------------------------------------------------------------


def _pack(monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    from server.memory.api.models_context import ContextPackInput
    from server.memory.app.context_pack import handle_context_pack

    async def _none(*a: Any, **k: Any) -> Any:
        raise RuntimeError("section off in this test")

    for target in (
        "server.memory.app.working_memory.handle_working_memory_render",
        "server.memory.app.stack.handle_memory_stack",
        "server.memory.app.profile.handle_profile_render",
        "server.memory.store.list_by_namespace",
    ):
        monkeypatch.setattr(target, _none)
    from server.memory.app import namespace_health as nh

    async def _healthy(*a: Any, **k: Any) -> Any:
        return None

    monkeypatch.setattr(nh, "namespace_health", _healthy)
    inp = ContextPackInput(org="o", namespace="ns", agent="a", mode="startup", **kw)
    return asyncio.run(handle_context_pack(inp))


def test_pack_renders_the_mentor_section_and_stays_silent_otherwise(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prof = _fake_profile(_h("code_index_unused"), _h("runs_unused", "info"))

    async def _p(*a: Any, **k: Any) -> Any:
        return prof

    monkeypatch.setattr(m, "mentor_profile", _p)
    out = _pack(monkeypatch)
    assert "mentor" in out.sections
    assert out.sections["mentor"].startswith("Loom mentor — code_index_unused")
    assert out.section_meta["mentor"]["codes"] == ["code_index_unused", "runs_unused"]
    assert "mentor" in out.stable_sections

    async def _quiet(*a: Any, **k: Any) -> Any:
        return _fake_profile()

    monkeypatch.setattr(m, "mentor_profile", _quiet)
    assert "mentor" not in _pack(monkeypatch).sections

    async def _fail(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(m, "mentor_profile", _fail)
    assert "mentor" not in _pack(monkeypatch).sections  # failure-isolated

    monkeypatch.setattr(m, "mentor_profile", _p)
    assert "mentor" not in _pack(monkeypatch, only_sections=["profile"]).sections


# --- mentor.brief ---------------------------------------------------------------------


def test_mentor_brief_returns_the_profile_planes_and_adoption(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.api.models_mentor import MentorBriefInput
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    assert "mentor.brief" in OPERATIONS_BY_NAME and "mentor.brief" in TOOL_DESCRIPTIONS
    prof = _fake_profile(
        _h("code_index_unused"),
        usage={"memory.set": 10, "memory.search": 2},
        facts={
            "repos": 2,
            "documents": 0,
            "records": 0,
            "entity_rows": 3,
            "conflicted_rows": 0,
        },
    )

    async def _p(*a: Any, **k: Any) -> Any:
        return prof

    monkeypatch.setattr(m, "mentor_profile", _p)
    ch = _CH(
        {
            "args['out_hint_code'] != ''": [
                {"session_id": "s1", "code": "get_miss", "first_ts": 10},
                {"session_id": "s2", "code": "get_miss", "first_ts": 10},
                {"session_id": "s2", "code": "code_index_unused", "first_ts": 11},
                {"session_id": "s3", "code": "get_miss", "first_ts": 10},
            ],
            "session_id IN {sessions:Array(String)}": [
                {"session_id": "s1", "operation": "memory.search", "last_ts": 12},
                {"session_id": "s2", "operation": "memory.search", "last_ts": 5},
                {"session_id": "s2", "operation": "memory.get", "last_ts": 9},
                # s3 searched before the hint AND after it: the last call decides.
                {"session_id": "s3", "operation": "memory.search", "last_ts": 30},
            ],
        }
    )
    monkeypatch.setattr(handler, "get_client", lambda: ch)
    out = asyncio.run(
        handler.handle_mentor_brief(MentorBriefInput(org="o", namespace="ns", agent="a"))
    )
    assert out.agent == "a" and out.healthy is False
    assert [h.code for h in out.hints] == ["code_index_unused"]
    planes = {p.plane: p for p in out.planes}
    assert planes["claims"].calls == 12 and planes["code index"].available is True
    assert planes["documents"].available is False and planes["tracker"].available is None
    assert planes["erasure"].mentored is False and planes["code index"].mentored is True
    # A named agent is used as given; the writers list rode along (empty here).
    assert out.agent == "a" and out.agent_defaulted is False
    adoption = {a.code: (a.shown_sessions, a.followed_sessions) for a in out.adoption}
    # s1 and s3 searched after the hint (followed); s2 only before it (not followed).
    assert adoption == {"get_miss": (3, 2), "code_index_unused": (1, 0)}
    # Only client-named sessions are shown sessions; the follow-up
    # read asks for the LAST call of each op, not the first.
    shown_sql = next(s for s in ch.sqls if "args['out_hint_code'] != ''" in s)
    later_sql = next(s for s in ch.sqls if "session_id IN {sessions:Array(String)}" in s)
    assert "session_id NOT IN ('', '-', '*')" in shown_sql and "max(ts) AS last_ts" in later_sql
    table = handler.render_table(out)
    assert "code_index_unused" in table and "adoption" in table and "code index" in table


M_EVIDENCE = "AS returned_hits"
M_INDEXED = "FROM loom.memory_embeddings FINAL"
M_RETENTION = "FROM loom.namespaces FINAL"
M_REPORTED = "SELECT uniqExact(k) AS n FROM ("
M_GRADED = "SELECT count() AS n FROM loom.agent_runs FINAL"
# Every evidence read answering, for a test about one of them.
_EVIDENCE_ROWS: dict[str, Any] = {
    M_EVIDENCE: [{"attempts": 1, "successes": 1, "committed_writes": 0, "returned_hits": 0}],
    M_INDEXED: [{"n": 0}],
    M_REPORTED: [{"n": 0}],
    M_GRADED: [{"n": 0}],
}


def _brief(monkeypatch: pytest.MonkeyPatch, ch: _CH, **facts: Any) -> Any:
    from server.insights.api.models_mentor import MentorBriefInput

    async def _p(*a: Any, **k: Any) -> Any:
        return _fake_profile(facts=facts)

    monkeypatch.setattr(m, "mentor_profile", _p)
    monkeypatch.setattr(handler, "get_client", lambda: ch)
    return asyncio.run(
        handler.handle_mentor_brief(MentorBriefInput(org="o", namespace="ns", agent="a"))
    )


def test_mentor_brief_climbs_the_evidence_ladder_from_attempt_to_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = {
        "attempts": 40,
        "successes": 37,
        "committed_writes": 12,
        "returned_hits": 9,
        "oldest_row": datetime(2026, 1, 2),
    }
    ch = _CH(
        {
            M_EVIDENCE: [calls],
            M_INDEXED: [{"n": 11}],
            M_REPORTED: [{"n": 4}],
            M_GRADED: [{"n": 2}],
            M_RETENTION: [{"retention_days": 0}],
        }
    )
    # The cached profile's own count is not what the ladder reports: it reads live.
    out = _brief(monkeypatch, ch, credited_keys=99)
    ladder = {e.rung: (e.count, e.source) for e in out.usage_evidence}
    assert list(ladder) == [
        "attempt",
        "success",
        "committed_write",
        "indexed_write",
        "returned_hit",
        "client_acknowledged",
        "task_outcome",
    ]
    assert ladder["attempt"] == (40, "server") and ladder["success"] == (37, "server")
    assert ladder["committed_write"] == (12, "server") and ladder["returned_hit"] == (9, "server")
    assert ladder["indexed_write"] == (11, "server")
    # What the agent said it relied on is its own report.
    assert ladder["client_acknowledged"] == (4, "client")
    # Graded runs are the grader's report, not something Loom checked.
    assert ladder["task_outcome"] == (2, "client")
    assert out.ledger_from == datetime(2026, 1, 2)
    rows = handler.render_table(out).splitlines()
    (attempt_row,) = [r for r in rows if r.startswith("attempt ")]
    assert attempt_row.split()[1:3] == ["40", "server"]


def test_the_ladder_counts_the_window_of_the_profile_the_brief_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.api.models_mentor import MentorBriefInput

    async def _p(*a: Any, **k: Any) -> Any:
        return _fake_profile()  # cached over 7 days

    ch = _CH(_EVIDENCE_ROWS)
    monkeypatch.setattr(m, "mentor_profile", _p)
    monkeypatch.setattr(handler, "get_client", lambda: ch)
    out = asyncio.run(
        handler.handle_mentor_brief(
            MentorBriefInput(org="o", namespace="ns", agent="a", window_seconds=30 * 86400)
        )
    )
    assert out.window_seconds == 604800
    windows = {
        marker: p["win"]
        for sql, p in zip(ch.sqls, ch.params, strict=True)
        for marker in (M_EVIDENCE, M_INDEXED, M_REPORTED, M_GRADED)
        if marker in sql and p
    }
    assert windows == dict.fromkeys((M_EVIDENCE, M_INDEXED, M_REPORTED, M_GRADED), 604800)


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
def test_indexed_writes_count_memories_first_created_in_the_window() -> None:
    from server.insights.store import mentor as store

    def memory(user: str, key: str, days_ago: int, version: int) -> str:
        return (
            "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
            f"value, version, created_at) VALUES ('o', 'ns', 'a', '{user}', '-', '{key}', 'v', "
            f"{version}, now() - INTERVAL {days_ago} DAY)"
        )

    def embedding(user: str, key: str, dim: int, version: int) -> str:
        # Written now, as every version's embedding row is; the codec needs full vectors.
        return (
            "INSERT INTO loom.memory_embeddings (org, namespace, agent, user_id, session_id, "
            "memory_key, embedding_model, embedding_dim, embedding, memory_key_embedding, "
            "status, version, created_at) SELECT 'o', 'ns', 'a', "
            f"'{user}', '-', '{key}', 'm', {dim}, v, v, 'active', {version}, now() "
            "FROM (SELECT arrayWithConstant(3072, toBFloat16(0)) AS v)"
        )

    class _Local:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            setup = ["SET enable_quantized_codec = 1", ddl("memory"), ddl("memory_embeddings")]
            return run([*setup, *rows], sql, params)

    rows = [
        # u1's old key, revised today: not created in the window.
        memory("u1", "pref/tz", 60, 2),
        embedding("u1", "pref/tz", 3072, 2),
        # u2 created the same key today and it is not embedded yet.
        memory("u2", "pref/tz", 0, 1),
        embedding("u2", "pref/tz", 0, 1),
        # u3, u4 and u5 each created and embedded the same key today: three memories.
        *(
            row
            for user in ("u3", "u4", "u5")
            for row in (memory(user, "pref/lang", 0, 1), embedding(user, "pref/lang", 3072, 1))
        ),
    ]
    n = asyncio.run(store.indexed_writes("o", "ns", "a", 7 * 86400, model="m", ch=_Local()))
    assert n == 3


def test_the_namespace_retention_wins_over_the_ledger_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "retention_agent_events_days", 30)
    ch = _CH({**_EVIDENCE_ROWS, M_RETENTION: [{"retention_days": 7}]})
    assert _brief(monkeypatch, ch).ledger_retention_days == 7
    ch = _CH({**_EVIDENCE_ROWS, M_RETENTION: [{"retention_days": 0}]})
    out = _brief(monkeypatch, ch)
    assert out.skipped == []
    assert out.ledger_retention_days == 30
    assert "the call ledger keeps 30d of rows" in handler.render_table(out)
    # Kept until deleted: no retention line.
    monkeypatch.setattr(settings, "retention_agent_events_days", 0)
    kept = _brief(monkeypatch, _CH({**_EVIDENCE_ROWS, M_RETENTION: [{"retention_days": 0}]}))
    assert kept.ledger_retention_days == 0
    assert "the call ledger keeps" not in handler.render_table(kept)


def test_a_failed_retention_read_leaves_the_retention_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out = _brief(monkeypatch, _CH({**_EVIDENCE_ROWS, M_RETENTION: RuntimeError("down")}))
    assert out.ledger_retention_days is None and "retention: RuntimeError" in out.skipped


def test_an_embedder_that_fails_leaves_only_the_indexed_rung_uncounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def broken() -> Any:
        raise RuntimeError("no embedder")

    monkeypatch.setattr(handler, "get_embedder", broken)
    ch = _CH({**_EVIDENCE_ROWS, M_RETENTION: [{"retention_days": 0}]})
    out = _brief(monkeypatch, ch)
    ladder = {e.rung: e.count for e in out.usage_evidence}
    assert ladder["indexed_write"] is None and ladder["attempt"] == 1
    assert out.skipped == ["indexed_writes: RuntimeError"]


def test_a_brief_about_no_agent_in_particular_has_no_ladder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.api.models_mentor import MentorBriefInput

    async def _p(*a: Any, **k: Any) -> Any:
        return _fake_profile(agent="-")

    monkeypatch.setattr(m, "mentor_profile", _p)
    ch = _CH(_EVIDENCE_ROWS)
    monkeypatch.setattr(handler, "get_client", lambda: ch)
    out = asyncio.run(
        handler.handle_mentor_brief(MentorBriefInput(org="o", namespace="ns", agent="-"))
    )
    assert out.usage_evidence == [] and not [q for q in ch.sqls if M_EVIDENCE in q]


def test_a_failed_evidence_read_leaves_its_rungs_uncounted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH(
        {
            **_EVIDENCE_ROWS,
            M_INDEXED: [{"n": 3}],
            M_RETENTION: [{"retention_days": 0}],
            M_EVIDENCE: RuntimeError("down"),
            M_REPORTED: RuntimeError("x"),
        }
    )
    out = _brief(monkeypatch, ch)
    ladder = {e.rung: e.count for e in out.usage_evidence}
    assert ladder["attempt"] is None and ladder["returned_hit"] is None
    assert ladder["client_acknowledged"] is None
    # The other reads still answer.
    assert ladder["indexed_write"] == 3 and ladder["task_outcome"] == 0
    assert out.skipped == ["calls: RuntimeError", "reported_keys: RuntimeError"]
    assert out.ledger_from is None and out.ledger_retention_days == 0


def test_mentor_brief_when_the_mentor_is_off_is_an_honest_empty_brief(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.api.models_mentor import MentorBriefInput

    monkeypatch.setattr(settings, "mentor_ttl_seconds", 0, raising=False)
    ch = _CH({**_EVIDENCE_ROWS, M_RETENTION: [{"retention_days": 0}]})
    monkeypatch.setattr(handler, "get_client", lambda: ch)
    out = asyncio.run(
        handler.handle_mentor_brief(MentorBriefInput(org="o", namespace="ns", agent="a"))
    )
    assert out.healthy is True and out.hints == [] and out.skipped == ["profile: unavailable"]
    # The ladder reads no profile, so it still answers.
    assert {e.rung: e.count for e in out.usage_evidence}["attempt"] == 1


def test_planes_cover_every_registered_op_prefix() -> None:
    from server.operations.registry import ALL_OPERATIONS

    when_of = {plane: when for _, plane, when, _ in mentor_rules.PLANES}
    mapped = 0
    for op in ALL_OPERATIONS:
        plane = mentor_rules.plane_of(op.name)
        # Ops outside the plane table fall back to their own prefix; the
        # planes an agent is mentored about all have a "when".
        if plane in when_of:
            mapped += 1
            assert when_of[plane]
    # Positive control: most ops map to a mentored plane, so the loop checks something.
    assert mapped >= len(ALL_OPERATIONS) // 2, (mapped, len(ALL_OPERATIONS))


# --- shape rules and identity ---------------------------------


_TRACE = (
    "Traceback (most recent call last):\n"
    '  File "/app/server/memory/app/handlers.py", line 42, in handle\n'
    "    x = y()\n"
    '  File "/app/server/x.py", line 9, in y\n'
    "    raise ValueError('boom')\n"
    "ValueError: boom"
)
_CH_TRACE = (
    "DB::Exception: Memory limit exceeded. Stack trace:\n"
    "0. 0x00007f0 DB::Exception::Exception(...) @ 0x0000000012a in /usr/bin/clickhouse\n"
    "1. 0x00007f1 DB::MemoryTracker::allocImpl(long, bool) @ 0x0000000012b in /usr/bin/clickhouse\n"
    "2. 0x00007f2 DB::StorageMergeTree::merge(...) @ 0x0000000012c in /usr/bin/clickhouse\n"
)


def test_shape_detectors() -> None:
    assert mentor_rules.looks_like_identifier_query("StorageSharedMergeTree::merge")
    assert mentor_rules.looks_like_identifier_query("handle_memory_set")
    assert mentor_rules.looks_like_identifier_query("server/memory/app/handlers.py")
    assert mentor_rules.looks_like_identifier_query("getClient()")
    assert not mentor_rules.looks_like_identifier_query(
        "what did we decide about the deploy window"
    )
    assert not mentor_rules.looks_like_identifier_query("")
    assert mentor_rules.looks_like_stack_trace(_TRACE) and mentor_rules.looks_like_stack_trace(
        _CH_TRACE
    )
    assert not mentor_rules.looks_like_stack_trace(
        "The deploy failed with an error: timeout after 30 s."
    )
    assert mentor_rules.issue_references(
        "fixes #81234; see https://github.com/x/y/pull/81300 and (#77)"
    ) == [
        "81234",
        "81300",
    ]
    assert mentor_rules.issue_references("room 101 on floor #2") == []


def test_stack_trace_written_as_a_memory_is_pointed_at_the_plane_the_namespace_has() -> None:
    m._cache_put(
        ("o", "ns", "a"), 10**12, _fake_profile(facts={"repos": 1, "tracker_sources": ["qa"]})
    )
    hint = _call("memory.set", {"memory_key": "crash/1", "value": _CH_TRACE}, {"memory_id": "1"})
    assert hint is not None and hint[0] == "set_stack_trace"
    assert "code.trace" in hint[1] and "tracker.similar" in hint[1]
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(facts={"repos": 0, "tracker_sources": []}))
    hint = _call(
        "memory.set", {"memory_key": "crash/2", "value": _TRACE}, {"memory_id": "1"}, session="s2"
    )
    assert hint is not None and hint[0] == "set_stack_trace" and "context.offload" in hint[1]
    # A trace beats the length rule for the same write.
    hint = _call(
        "memory.set", {"memory_key": "crash/3", "value": _TRACE + "x" * 5000}, {}, session="s3"
    )
    assert hint is not None and hint[0] == "set_stack_trace"


def test_an_issue_number_in_a_write_points_at_tracker_item_or_records() -> None:
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(facts={"tracker_sources": ["qa"]}))
    hint = _call(
        "memory.set", {"memory_key": "pr-81234", "value": "CI red on #81234"}, {"memory_id": "1"}
    )
    assert hint is not None and hint[0] == "set_issue_reference" and "tracker.item" in hint[1]
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(facts={"tracker_sources": []}))
    hint = _call(
        "memory.set",
        {"memory_key": "note", "value": "see /pull/512"},
        {"memory_id": "1"},
        session="s2",
    )
    assert (
        hint is not None
        and hint[0] == "set_issue_reference"
        and "memory_schema.register" in hint[1]
    )
    assert (
        _call(
            "memory.set",
            {"memory_key": "note2", "value": "room #2"},
            {"memory_id": "1"},
            session="s3",
        )
        is None
    )


def test_an_identifier_query_to_memory_is_redirected_when_the_index_exists() -> None:
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(facts={"repos": 2}))
    hint = _call(
        "memory.search", {"query": "StorageSharedMergeTree::merge"}, {"abstain": False, "hits": []}
    )
    assert hint is not None and hint[0] == "search_identifier_query" and "code.symbol" in hint[1]
    # A question stays a memory question even when thin.
    assert (
        _call(
            "memory.search",
            {"query": "why did we pick postgres"},
            {"abstain": False, "hits": []},
            session="s2",
        )
        is None
    )
    # A rich answer is not redirected.
    assert (
        _call(
            "memory.search",
            {"query": "handle_memory_set"},
            {"abstain": False, "hits": [{"k": 1}]},
            session="s3",
        )
        is None
    )
    # No index: the abstain rule speaks instead, or nothing.
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(facts={"repos": 0, "documents": 0}))
    assert (
        _call("memory.search", {"query": "handle_memory_set"}, {"abstain": True}, session="s4")
        is None
    )


def test_an_unnamed_agent_is_told_to_name_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    _wire(monkeypatch, _store(_usage(memory__set=50, memory__search=10), 3))
    p = _profile(agent="-")
    assert p is not None and "agent_unnamed" in _codes(p)
    m.reset_cache()
    _wire(monkeypatch, _store(_usage(memory__set=50, memory__search=10), 3))
    assert "agent_unnamed" not in _codes(_profile(agent="claude-code"))


def test_code_index_availability_is_the_readable_repos_not_the_org_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _wire(monkeypatch, _store(_usage(memory__set=40), 3))
    from server.code.domain.index import repos as _repos

    monkeypatch.setattr(
        _repos,
        "configured_repos",
        lambda: [
            SimpleNamespace(
                org="o",
                namespace="code-private",
                repo="acme/secret",
                classification="restricted",
                org_readable=False,
            ),
            SimpleNamespace(
                org="other",
                namespace="code-x",
                repo="x/y",
                classification="standard",
                org_readable=True,
            ),
        ],
    )
    p = _profile()
    assert p is not None
    assert p.facts["repos"] == 0 and p.facts["repos_total"] == 1
    assert "code_index_unused" not in _codes(p)
    assert ch.sqls  # the read happened
    m.reset_cache()
    monkeypatch.setattr(
        _repos,
        "configured_repos",
        lambda: [
            SimpleNamespace(
                org="o",
                namespace="ns",
                repo="acme/own",
                classification="restricted",
                org_readable=False,
            )
        ],
    )
    p = _profile()
    assert p is not None and p.facts["repos"] == 1 and "code_index_unused" in _codes(p)


def test_call_hint_ops_cover_every_per_call_code() -> None:
    codes = {
        "get_miss",
        "search_identifier_query",
        "search_abstain_code",
        "search_abstain_knowledge",
        "set_duplicate",
        "set_large_value",
        "set_churn",
        "set_stack_trace",
        "set_issue_reference",
        "run_over_budget",
    }
    assert set(mentor_rules.CALL_HINT_OPS) == codes
    assert handler.CALL_HINT_OPS is mentor_rules.CALL_HINT_OPS


# --- first production hour: profile hints once per agent per TTL, not per session ---------


def test_a_profile_hint_is_said_once_per_agent_not_once_per_session() -> None:
    m._cache_put(
        ("o", "ns", "a"), 10**12, _fake_profile(_h("duplicates_written"), _h("runs_unused", "info"))
    )
    first = _call("memory.list", {}, {"items": []}, session="s1")
    assert first is not None and first[0] == "duplicates_written"
    second = _call("memory.list", {}, {"items": []}, session="s2")
    assert (
        second is not None and second[0] == "runs_unused"
    )  # the next code, not the same one again
    assert _call("memory.list", {}, {"items": []}, session="s3") is None
    # A recomputed profile starts a fresh "already said" set.
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(_h("duplicates_written")))
    again = _call("memory.list", {}, {"items": []}, session="s4")
    assert again is not None and again[0] == "duplicates_written"
    # Per-call rules stay per session.
    assert _call("memory.get", {"memory_key": "k"}, {"found": False}, session="s5") is not None
    assert _call("memory.get", {"memory_key": "k"}, {"found": False}, session="s6") is not None


def test_profile_hints_ride_only_calls_about_the_agents_work() -> None:
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(_h("facts_graph_unused", "info")))
    assert _call("ui.vitals", {}, {"ok": True}, session="t1") is None
    assert _call("cost.budget_status", {}, {"ok": True}, session="t1") is None
    assert _call("grants.pending_for_me", {}, {"rows": []}, session="t1") is None
    hint = _call("memory.search", {"query": "q"}, {"hits": [{"k": 1}]}, session="t1")
    assert hint is not None and hint[0] == "facts_graph_unused"


def test_the_dashboards_own_traffic_is_never_mentored() -> None:
    m._cache_put(("o", "ns", "loom-ui"), 10**12, _fake_profile(_h("documents_as_memories")))
    assert (
        m.call_hint(
            "memory.get",
            {"memory_key": "k"},
            {"found": False},
            session_key="u1",
            org="o",
            namespace="ns",
            agent="loom-ui",
        )
        is None
    )
    assert (
        m.call_hint(
            "memory.get",
            {"memory_key": "k", "consumer": "loom-ui"},
            {"found": False},
            session_key="u2",
            org="o",
            namespace="ns",
            agent="a",
        )
        is None
    )


def test_handshake_warms_the_profile_when_nothing_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.transport import mcp_call, mcp_primer

    warmed: list[tuple[str, str, str]] = []
    monkeypatch.setattr(m, "warm_profile", lambda o, n, a: warmed.append((o, n, a)))

    async def _defaults(auth: Any, client_name: Any) -> dict[str, str]:
        return {"org": "o", "namespace": "ns", "agent": "a"}

    monkeypatch.setattr(mcp_call, "_caller_defaults", _defaults)
    monkeypatch.setattr(mcp_primer, "_caller_defaults", _defaults)
    assert asyncio.run(mcp_primer._mentor_line(None)) == ""
    assert warmed == [("o", "ns", "a")]
    m._cache_put(("o", "ns", "a"), 10**12, _fake_profile(_h("code_index_unused")))
    line = asyncio.run(mcp_primer._mentor_line(None))
    assert "code_index_unused" in line and len(warmed) == 1


def test_brief_for_an_anonymous_caller_is_about_the_top_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.insights.api.models_mentor import MentorBriefInput

    seen: list[str] = []

    async def _p(org: str, ns: str, agent: str, **kw: Any) -> Any:
        seen.append(agent)
        return _fake_profile(_h("code_index_unused"), agent=agent)

    monkeypatch.setattr(m, "mentor_profile", _p)
    ch = _CH(
        {
            "GROUP BY agent ORDER BY n DESC": [
                {"agent": "claude-code", "n": 224},
                {"agent": "obs-verify", "n": 12},
            ],
            "args['out_hint_code'] != ''": [],
        }
    )
    monkeypatch.setattr(handler, "get_client", lambda: ch)
    out = asyncio.run(
        handler.handle_mentor_brief(MentorBriefInput(org="o", namespace="ns", agent="-"))
    )
    assert out.agent == "claude-code" and out.agent_defaulted is True
    assert out.agents == ["claude-code", "obs-verify"]
    assert seen == ["claude-code"]
    assert "most active writer" in handler.render_table(out)
    # The dashboard's own name is anonymous too.
    out = asyncio.run(
        handler.handle_mentor_brief(MentorBriefInput(org="o", namespace="ns", agent="loom-ui"))
    )
    assert out.agent == "claude-code" and out.agent_defaulted is True
    # A real name is used as given.
    out = asyncio.run(
        handler.handle_mentor_brief(MentorBriefInput(org="o", namespace="ns", agent="sre-bot"))
    )
    assert (
        out.agent == "sre-bot"
        and out.agent_defaulted is False
        and out.agents == ["claude-code", "obs-verify"]
    )
    # Anonymous writers, the dashboard and the assistant are excluded from the writers read.
    writers_sql = [q for q in ch.sqls if "GROUP BY agent ORDER BY n DESC" in q][0]
    assert (
        "agent NOT IN {anon:Array(String)}" in writers_sql
        and "consumer NOT IN {inspectors:Array(String)}" in writers_sql
    )
    # Every ledger read the brief makes leaves the inspectors out, by a bound parameter.
    ledger = [
        (sql, p) for sql, p in zip(ch.sqls, ch.params, strict=True) if "loom.agent_events" in sql
    ]
    assert ledger
    for sql, p in ledger:
        assert "consumer NOT IN {inspectors:Array(String)}" in sql, sql
        assert p["inspectors"] == ["loom-ui", "loom-assistant"]


# --- a public tracker namespace's brief -----------------------------


def test_placeholder_session_ids_are_not_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """clickgap names a session on `run.begin` alone; every other call carries
    '-'. Read as sessions, those placeholders would make a week of calls one
    session (a hot key) and every run.begin a one-call session "never opened
    with a pack"."""
    ch = _wire(monkeypatch, _store(_usage(memory__get=5000, memory__search=13000), 1655))
    p = _profile()
    assert p is not None
    for marker in (M_SESS, M_FIRST, M_HOT):
        sql = next(s for s in ch.sqls if marker in s)
        assert "session_id NOT IN ('', '-', '*')" in sql, marker
    first_sql = next(s for s in ch.sqls if M_FIRST in s)
    assert "HAVING uniqExact(operation) >= 2" in first_sql


def test_a_long_row_read_in_a_loop_is_recall_not_a_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    # A 1.8 KB finding read 75× in one session (a curation pass) is a memory
    # recalled by key; the working_memory advice would drop its embedding.
    _wire(
        monkeypatch,
        _store(
            _usage(memory__get=500, memory__search=600),
            30,
            max_gets_one_key=75,
            hot_get_key="finding_h_pr119211_001",
            hot_key_chars=1814,
        ),
    )
    p = _profile()
    assert p is not None and "coordination_loop" not in _codes(p)
    assert p.facts["hot_get_key_chars"] == 1814
    m.reset_cache()
    # The same reads of a short value are a lease.
    _wire(
        monkeypatch,
        _store(
            _usage(memory__get=500, memory__search=600),
            30,
            max_gets_one_key=75,
            hot_get_key="sre/lease",
            hot_key_chars=58,
        ),
    )
    p = _profile()
    assert p is not None and "coordination_loop" in _codes(p)


def test_coordination_sentence_gives_each_key_its_own_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        _store(
            _usage(memory__set=300, memory__get=300, memory__search=300),
            30,
            max_sets_one_key=12,
            hot_set_key="a/lease",
            max_gets_one_key=40,
            hot_get_key="b/pointer",
            hot_key_chars=20,
        ),
    )
    p = _profile()
    assert p is not None
    loop = p.hint("coordination_loop")
    assert loop is not None
    assert "one key (a/lease) was written 12×" in loop.text
    assert "one key (b/pointer) was read 40×" in loop.text
    assert loop.evidence["key"] == "a/lease" and loop.evidence["hot_get_key_chars"] == 20
    m.reset_cache()
    _wire(
        monkeypatch,
        _store(
            _usage(memory__set=300, memory__get=300, memory__search=300),
            30,
            max_sets_one_key=12,
            hot_set_key="a/lease",
            max_gets_one_key=40,
            hot_get_key="a/lease",
        ),
    )
    p = _profile()
    assert p is not None
    loop = p.hint("coordination_loop")
    assert loop is not None and "one key (a/lease) was written 12× and read 40×" in loop.text


def _tiers(*specs: tuple[str, str, str]) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(org="o", namespace=ns, repo=repo, classification=cls, org_readable=False)
        for ns, repo, cls in specs
    ]


def test_code_index_counts_distinct_repos_the_agents_principal_may_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nine release tiers of one repository are one repo, and `standard` is
    not a read right: the plane is available through the
    grants the agent's subjects hold — a service account's creator's too."""
    from server.code.domain.index import repos as _repos

    entries = _tiers(
        ("code-ch-26-6", "acme/ch", "standard"),
        ("code-ch-26-7", "acme/ch", "standard"),
        ("code-ch-26-8", "acme/ch", "standard"),
        ("code-go", "acme/go", "standard"),
        ("code-private", "acme/private", "restricted"),
    )
    ch = _wire(
        monkeypatch,
        _store(
            _usage(memory__set=40, memory__search=30),
            5,
            subjects=["sa:bot-1"],
            granted=["code-ch-26-7", "code-ch-26-8", "ns"],
        ),
    )
    monkeypatch.setattr(_repos, "configured_repos", lambda: entries)
    p = _profile()
    assert p is not None
    assert p.facts["repos"] == 1 and p.facts["repo_entries"] == 2
    assert p.facts["repos_total"] == 5 and p.facts["repo_names"] == ["acme/ch"]
    hint = p.hint("code_index_unused")
    assert hint is not None and hint.text.startswith("1 indexed repo (acme/ch) and 0 code.* calls")
    grants_sql = next(s for s in ch.sqls if M_GRANTS in s)
    assert "inherit_creator_grants" in grants_sql and "loom.service_accounts FINAL" in grants_sql
    m.reset_cache()
    # No grant anywhere: standard entries alone do not make the plane available.
    _wire(monkeypatch, _store(_usage(memory__set=40, memory__search=30), 5, subjects=["sa:bot-1"]))
    monkeypatch.setattr(_repos, "configured_repos", lambda: entries)
    p = _profile()
    assert p is not None and p.facts["repos"] == 0 and "code_index_unused" not in _codes(p)
    m.reset_cache()
    # Four distinct repos: three named, the rest counted.
    _wire(
        monkeypatch,
        _store(
            _usage(memory__set=40, memory__search=30),
            5,
            subjects=["u1"],
            granted=["code-ch-26-8", "code-go", "code-py", "code-rs"],
        ),
    )
    monkeypatch.setattr(
        _repos,
        "configured_repos",
        lambda: (
            entries + _tiers(("code-py", "acme/py", "standard"), ("code-rs", "acme/rs", "standard"))
        ),
    )
    p = _profile()
    assert p is not None and p.facts["repos"] == 4
    hint = p.hint("code_index_unused")
    assert hint is not None and "4 indexed repos (acme/ch, acme/go, acme/py, +1)" in hint.text


def test_a_tag_naming_a_file_is_not_a_tracker_tag() -> None:
    import re

    from server.insights.store import mentor as ms

    path_re = re.compile(ms._PATH_TAG_RE)
    tracker_re = re.compile(ms._TRACKER_TAG_RE)
    for tag in ("entity:file_path:tests/integration/test_s3.py", "entity:file_path:test.py"):
        assert path_re.search(tag) and tracker_re.search(
            tag.lower()
        )  # matched only through the path
    for tag in ("bug_class:TEST_DEFECT", "flaky", "ci", "subsystem:Tests", "pr:118279"):
        assert not path_re.search(tag)
    assert tracker_re.search("bug_class:test_defect") and not tracker_re.search("pr:118279")


def test_records_hint_states_the_prefix_share_and_skips_aggregate_when_used(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _wire(
        monkeypatch,
        _store(
            _usage(memory__set=900, memory__search=900, memory__aggregate=373),
            30,
            id_keyed_sets=801,
            top_prefix_sets=60,
            top_prefix="finding_phase_d_pr",
        ),
    )
    p = _profile()
    assert p is not None
    hint = p.hint("records_for_identities")
    assert hint is not None
    assert "801 writes keyed by a number, 60 of them finding_phase_d_pr<id>" in hint.text
    assert "memory.aggregate" not in hint.text
    m.reset_cache()
    _wire(
        monkeypatch,
        _store(
            _usage(memory__set=900, memory__search=900),
            30,
            id_keyed_sets=801,
            top_prefix_sets=60,
            top_prefix="finding_phase_d_pr",
        ),
    )
    p = _profile()
    hint = p.hint("records_for_identities") if p else None
    assert hint is not None and "memory.aggregate counts them" in hint.text


def test_the_shown_codes_leave_with_the_profile_the_cache_evicts(monkeypatch) -> None:
    """`_profile_shown` is keyed like the profile cache; an agent whose profile
    is evicted takes its shown-codes set with it, so the map stays bounded."""
    monkeypatch.setattr(m, "CACHE_MAX_ENTRIES", 2)
    shown = {"shown_codes": ["x"]}
    m._cache_put(("o", "ns", "a1"), 100.0, _fake_profile(agent="a1", facts=shown))
    m._cache_put(("o", "ns", "a2"), 200.0, _fake_profile(agent="a2", facts=shown))
    assert set(m._profile_shown) == {("o", "ns", "a1"), ("o", "ns", "a2")}
    m._cache_put(("o", "ns", "a3"), 300.0, _fake_profile(agent="a3", facts=shown))
    assert ("o", "ns", "a1") not in m._cache
    assert set(m._profile_shown) == {("o", "ns", "a2"), ("o", "ns", "a3")}


def test_every_profile_ledger_read_leaves_the_inspectors_out() -> None:
    from server.insights.store.mentor import ProfileReads

    ch = _CH()
    reads = ProfileReads(
        "o",
        "ns",
        "a",
        3600,
        ops={"sets": m.SETS, "gets": m.GETS, "searches": m.SEARCHES, "packs": m.PACKS},
        limits={"ttl": 60, "large": 1, "huge": 2, "advisory": 3, "clip": 4, "episodic_chars": 5},
        ch=ch,
    )
    # One read of the agent's own calls and one of the namespace's.
    asyncio.run(reads.op_counts())
    asyncio.run(reads.namespace_ledger())
    assert len(ch.sqls) == 2
    for sql, params in zip(ch.sqls, ch.params, strict=True):
        assert "consumer NOT IN {inspectors:Array(String)}" in sql
        assert params["inspectors"] == ["loom-ui", "loom-assistant"]


def test_a_profile_whose_repository_fact_failed_is_remembered_briefly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import dataclasses

    m.reset_cache()
    computed: list[int] = []
    profile = _fake_profile()

    async def _compute(*a: Any, **k: Any) -> Any:
        computed.append(1)
        return profile

    monkeypatch.setattr(m, "_compute", _compute)
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: 5.0))
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: 5.0 + m.FAILURE_TTL_SECONDS + 1))
    assert len(computed) == 1  # a whole profile is kept for the full TTL
    m.reset_cache()
    profile = dataclasses.replace(profile, skipped=("repos: RegistryUnread",))
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: 5.0))
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: 6.0))
    assert len(computed) == 2  # still cached inside the short TTL
    asyncio.run(m.mentor_profile("o", "ns", "a", clock=lambda: 5.0 + m.FAILURE_TTL_SECONDS + 1))
    assert len(computed) == 3
    m.reset_cache()
