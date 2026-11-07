"""Precedent answers for memory.ask.

A synthesized answer is served again, without an LLM call, only when the
fresh synthesis window is byte-identical to the evidence it was written
from: same (namespace, key) set, same content_sha256 per key. Everything
else — an edited memory, a key that left or entered the window, an unhashed
row, an expired row — is a miss and the ask synthesizes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_ask import MemoryAskInput
from server.memory.domain import precedents as pure
from server.memory.domain.precedents import Evidence, Precedent
from server.memory.store import ask_precedents as repo

H1 = "a" * 64
H2 = "b" * 64


def _ev(**pairs: str) -> Evidence:
    return Evidence({("ns", k): h for k, h in pairs.items()})


def _prec(answer: str = "Blue [m1].", evidence: Evidence | None = None, **kw: Any) -> Precedent:
    return Precedent(
        precedent_key="k" * 64,
        question="what is my favorite color",
        answer=answer,
        cited_keys=["m1"],
        evidence=evidence or _ev(m1=H1, m2=H2),
        **kw,
    )


# ---- pure part ------------------------------------------------------------


def test_key_ignores_case_whitespace_and_trailing_punctuation_but_not_shape() -> None:
    a = pure.precedent_key("What is my  favorite color?", "s1")
    assert a == pure.precedent_key("what is my favorite color", "s1")
    assert a != pure.precedent_key("what is my favorite color", "s2")
    assert a != pure.precedent_key("what is my favourite color", "s1")
    assert len(a) == 64


def test_window_identity_is_ordered_deduplicated_and_namespace_aware() -> None:
    w = [
        SimpleNamespace(memory_key="m1", namespace=""),
        SimpleNamespace(memory_key="m2", namespace="other"),
        SimpleNamespace(memory_key="m1", namespace=""),
        SimpleNamespace(memory_key="", namespace=""),
    ]
    assert pure.window_identity(w, "ns") == [("ns", "m1"), ("other", "m2")]


def test_evidence_columns_round_trip_and_unhashed_rows_are_unverifiable() -> None:
    ev = _ev(m2=H2, m1=H1)
    cols = ev.columns()
    assert cols == (["ns", "ns"], ["m1", "m2"], [H1, H2])
    assert Evidence.from_columns(*cols) == ev
    assert ev.verifiable
    assert not _ev(m1=H1, m3="0").verifiable
    assert not Evidence({}).verifiable
    assert Evidence.from_columns(["ns"], ["m1", "m2"], [H1]) == Evidence({})


def test_validity_requires_the_same_keys_with_the_same_hashes() -> None:
    p = _prec()
    assert pure.validity(p, _ev(m1=H1, m2=H2)) == ""
    assert pure.validity(p, _ev(m1=H1, m2="c" * 64)) == "content_changed"
    assert pure.validity(p, _ev(m1=H1)) == "window_changed"  # a key vanished
    assert pure.validity(p, _ev(m1=H1, m2=H2, m3=H1)) == "window_changed"  # a key arrived
    w = [("ns", "m1"), ("ns", "m2"), ("ns", "m3")]
    assert pure.validity(p, _ev(m1=H1, m2=H2), window=w) == "window_changed"  # arrived, no live row
    assert pure.validity(p, _ev(m1=H1), window=w[:2]) == "row_gone"  # in the window, no live row
    assert pure.validity(p, Evidence({})) == "empty_window"
    assert pure.validity(_prec(answer="  "), _ev(m1=H1, m2=H2)) == "empty_answer"
    assert (
        pure.validity(_prec(evidence=_ev(m1="0", m2=H2)), _ev(m1="0", m2=H2))
        == "stored_evidence_unverifiable"
    )


def test_validity_max_age_is_measured_from_the_answers_birth() -> None:
    born = datetime(2026, 9, 12, 8, 0, tzinfo=UTC)
    p = _prec(created_at=born)
    fresh = _ev(m1=H1, m2=H2)
    assert pure.validity(p, fresh, now=born + timedelta(hours=1), max_age_hours=2) == ""
    assert pure.validity(p, fresh, now=born + timedelta(hours=3), max_age_hours=2) == "expired"
    assert pure.validity(p, fresh, now=born + timedelta(days=30), max_age_hours=0) == ""


# ---- repository -----------------------------------------------------------


class _StubCH:
    def __init__(
        self, rows: list[dict[str, Any]] | None = None, one: dict[str, Any] | None = None
    ) -> None:
        self.rows = rows or []
        self.one = one
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.queries: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.queries.append((sql, params or {}))
        return self.rows

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.queries.append((sql, params or {}))
        return self.one

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.queries.append((sql, params or {}))

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.inserts.append((table, rows))


def test_evidence_for_reads_live_rows_by_key_and_keeps_only_the_asked_pairs() -> None:
    ch = _StubCH(
        rows=[
            {"namespace": "ns", "memory_key": "m1", "content_sha256": H1},
            {"namespace": "other", "memory_key": "m1", "content_sha256": H2},  # not asked for
        ]
    )
    ev = asyncio.run(repo.evidence_for("o", [("ns", "m1"), ("ns", "m2")], ch=ch))
    assert ev.hashes == {("ns", "m1"): H1}
    sql, params = ch.queries[0]
    assert "FROM loom.memory FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "nss": ["ns"], "keys": ["m1", "m2"]}
    assert asyncio.run(repo.evidence_for("o", [], ch=ch)) == Evidence({})


def test_lookup_store_and_touch_round_trip_through_the_row_shape() -> None:
    ch = _StubCH()
    asyncio.run(
        repo.store(
            org="o",
            namespace="ns",
            precedent_key="k" * 64,
            question="q",
            shape_sha256="s" * 64,
            answer="Blue [m1].",
            cited_keys=["m1"],
            evidence=_ev(m1=H1, m2=H2),
            synth_model="gpt-x",
            ch=ch,
        )
    )
    table, rows = ch.inserts[0]
    assert (
        table == repo.TABLE
        and rows[0]["window_keys"] == ["m1", "m2"]
        and rows[0]["reuse_count"] == 0
    )
    stored = dict(rows[0], created_at=datetime(2026, 9, 12, 8, 0))
    ch = _StubCH(one=stored)
    p = asyncio.run(repo.lookup("o", "ns", "k" * 64, ch=ch))
    assert p is not None and p.answer == "Blue [m1]." and p.evidence == _ev(m1=H1, m2=H2)
    assert p.created_at == datetime(2026, 9, 12, 8, 0, tzinfo=UTC) and p.synth_model == "gpt-x"
    assert "FINAL" in ch.queries[0][0] and ch.queries[0][1]["pk"] == "k" * 64
    asyncio.run(repo.touch(org="o", namespace="ns", prec=p, ch=ch))
    touched = ch.inserts[0][1][0]
    assert touched["reuse_count"] == 1 and touched["created_at"] == datetime(2026, 9, 12, 8, 0)
    assert touched["last_reused_at"] is not None
    assert asyncio.run(repo.lookup("o", "ns", "zz", ch=_StubCH(one=None))) is None


# ---- through the handler --------------------------------------------------


@pytest.fixture
def prec_env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    import server.llm.client as llm_client
    import server.memory.app.search as hsearch
    import server.observability.budget as budget
    from server.settings import settings
    from tests.unit.test_memory_ask import _FakeLLM, _hit, _search_output

    env = SimpleNamespace()
    env.llm = _FakeLLM("Your favorite color is blue [m1].")
    env.hits = [_hit("m1", excerpt="favorite color: blue"), _hit("m2", excerpt="lives in Oslo")]
    env.hashes = {"m1": H1, "m2": H2}
    env.stored: dict[str, dict[str, Any]] = {}
    env.inserts: list[dict[str, Any]] = []

    async def fake_search(search_input: Any):
        return _search_output(list(env.hits))

    async def fake_over_cap(org: str, ns: str) -> bool:
        return False

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None):
            keys = (params or {}).get("keys", [])
            return [
                {"namespace": "ns", "memory_key": k, "content_sha256": env.hashes[k]}
                for k in keys
                if k in env.hashes
            ]

        async def query_one(self, sql: str, params: dict[str, Any] | None = None):
            return env.stored.get((params or {}).get("pk", ""))

        async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
            return None

        async def insert(
            self, table: str, rows: list[dict[str, Any]], settings: Any = None
        ) -> None:
            for r in rows:
                env.inserts.append(r)
                env.stored[r["precedent_key"]] = dict(r)

    monkeypatch.setattr(hsearch, "handle_memory_search", fake_search)
    monkeypatch.setattr(budget, "namespace_over_cap", fake_over_cap)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: env.llm)
    monkeypatch.setattr(repo, "_default_client", lambda: _CH())
    monkeypatch.setattr(settings, "memory_ask_precedents", True)
    monkeypatch.setattr("server.memory.app.ask.handler._schedule_cited_used", lambda *a, **k: None)
    return env


async def _ask(**overrides: Any):
    from tests.unit.test_memory_ask import _run

    out = await _run(**overrides)
    # drain the fire-and-forget store/touch tasks
    for _ in range(5):
        await asyncio.sleep(0)
    return out


@pytest.mark.asyncio
async def test_second_identical_ask_is_served_from_the_precedent_without_an_llm_call(
    prec_env,
) -> None:
    first = await _ask()
    assert first.source == "synthesized" and len(prec_env.llm.calls) == 1
    assert len(prec_env.inserts) == 1
    row = prec_env.inserts[0]
    assert row["cited_keys"] == ["m1"] and row["window_keys"] == ["m1", "m2"]
    assert row["window_sha256"] == [H1, H2] and row["question"] == "what is my favorite color"

    second = await _ask(query="What is my favorite color?")  # same question, normalised
    assert second.source == "precedent"
    assert second.answer == first.answer and second.memory_keys == ["m1"]
    assert second.route_reason == "precedent:reused=1"
    assert len(prec_env.llm.calls) == 1  # no second synthesis
    assert prec_env.inserts[-1]["reuse_count"] == 1  # the reuse was recorded


@pytest.mark.asyncio
async def test_an_edited_memory_or_a_changed_window_misses_and_synthesizes_again(prec_env) -> None:
    from tests.unit.test_memory_ask import _hit

    await _ask()
    prec_env.hashes["m2"] = "c" * 64  # m2 edited
    out = await _ask()
    assert out.source == "synthesized" and len(prec_env.llm.calls) == 2
    prec_env.hits.append(_hit("m3", excerpt="new fact"))  # a key arrived
    out = await _ask()
    assert out.source == "synthesized" and len(prec_env.llm.calls) == 3


@pytest.mark.asyncio
async def test_a_different_shape_is_a_different_precedent(prec_env) -> None:
    await _ask()
    out = await _ask(top_k=7)
    assert out.source == "synthesized" and len(prec_env.llm.calls) == 2


@pytest.mark.asyncio
async def test_a_changed_answer_policy_setting_misses_the_precedent(
    prec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_world_knowledge", "allow")
    await _ask()
    monkeypatch.setattr(settings, "memory_ask_world_knowledge", "forbid")
    out = await _ask()
    assert out.source == "synthesized" and len(prec_env.llm.calls) == 2
    # Retrieval's fusion weights shape the window too.
    monkeypatch.setattr(settings, "memory_rrf_lexical_weight_floor", 0.3)
    out = await _ask()
    assert out.source == "synthesized" and len(prec_env.llm.calls) == 3
    # The precedent settings only decide reuse; they leave the stored answer valid.
    monkeypatch.setattr(settings, "memory_ask_precedents_max_age_hours", 48)
    out = await _ask()
    assert out.source == "precedent" and len(prec_env.llm.calls) == 3


@pytest.mark.asyncio
async def test_unhashed_rows_never_anchor_a_precedent(prec_env) -> None:
    prec_env.hashes["m2"] = "0"
    await _ask()
    assert prec_env.inserts == []
    out = await _ask()
    assert out.source == "synthesized" and len(prec_env.llm.calls) == 2


@pytest.mark.asyncio
async def test_flag_off_and_per_request_false_are_strict_noops(
    prec_env, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    await _ask(precedents=False)
    assert prec_env.inserts == []
    monkeypatch.setattr(settings, "memory_ask_precedents", False)
    await _ask()
    assert prec_env.inserts == [] and len(prec_env.llm.calls) == 2
    # per-request True opts in over the off flag
    await _ask(precedents=True)
    assert len(prec_env.inserts) == 1


@pytest.mark.asyncio
async def test_uncited_or_hedged_answers_are_not_stored(prec_env) -> None:
    prec_env.llm._text = "I have no memory of your favorite color."
    await _ask()
    assert prec_env.inserts == []
    prec_env.llm._text = "Blue, probably."  # no [key] marker
    await _ask()
    assert prec_env.inserts == []


def test_ask_shape_carries_the_user_and_session_the_read_filters_on() -> None:
    from server.memory.app.ask.handler import ask_shape_sha256
    from server.settings import settings
    from tests.unit.test_memory_ask import _input

    base = ask_shape_sha256(_input(), settings)
    assert base == ask_shape_sha256(
        _input(query="something else", org="o2", namespace="n2", run_id="r1"), settings
    )
    # A body user filters the read under namespace scope too: its own precedent.
    assert base != ask_shape_sha256(_input(user_id="alice"), settings)
    assert ask_shape_sha256(_input(user_id="alice"), settings) != ask_shape_sha256(
        _input(user_id="bob"), settings
    )
    assert base != ask_shape_sha256(_input(top_k=7), settings)
    assert base != ask_shape_sha256(_input(search_mode="semantic"), settings)
    own = ask_shape_sha256(_input(read_scope="own_user", user_id="alice"), settings)
    assert own != ask_shape_sha256(_input(read_scope="own_user", user_id="bob"), settings)


def test_ask_shape_hashes_only_the_settings_off_their_default() -> None:
    """A setting at its default leaves the shape as it was, so a release that adds one
    keeps the stored precedents; a value off the default is a different shape."""
    from server.memory.app.ask.handler import ask_shape_sha256
    from server.settings import Settings
    from tests.unit.test_memory_ask import _input

    base = Settings(_env_file=None)  # type: ignore[call-arg]
    at_default = base.model_copy(update={"memory_ask_world_knowledge": "forbid"})
    allow = base.model_copy(update={"memory_ask_world_knowledge": "allow"})
    assert Settings.model_fields["memory_ask_world_knowledge"].default == "forbid"
    assert ask_shape_sha256(_input(), base) == ask_shape_sha256(_input(), at_default)
    assert ask_shape_sha256(_input(), base) != ask_shape_sha256(_input(), allow)
    # At every default the digest is the request's alone, as a build without the
    # settings hashed it: an upgrade keeps the stored precedents.
    excluded = {"query", "org", "namespace", "run_id", "precedents", "user_id", "session_id"}
    request = _input().model_dump(exclude=excluded, exclude_defaults=True)
    canonical = json.dumps(request, sort_keys=True, default=str, separators=(",", ":"))
    assert ask_shape_sha256(_input(), base) == hashlib.sha256(canonical.encode()).hexdigest()


@pytest.mark.asyncio
async def test_cited_used_credit_fires_for_a_precedent(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app.ask import handler as ask_mod
    from server.memory.store import usage as usage

    seen: list[dict[str, Any]] = []

    async def fake(org: str, ns_keys: dict[str, list[str]], **kw: Any) -> None:
        seen.append(ns_keys)

    monkeypatch.setattr(usage, "record_usage_for_keys", fake)
    hits = [
        SimpleNamespace(memory_key="m1", namespace=""),
        SimpleNamespace(memory_key="m2", namespace=""),
    ]
    res = SimpleNamespace(source="precedent", abstain=False, answer="Blue [m1].", results=hits)
    # A named reader: `*` is a measurement label and credits nothing.
    ask_mod._schedule_cited_used(
        MemoryAskInput(org="o", namespace="ns", agent="reader", query="q"), res
    )
    for _ in range(5):
        await asyncio.sleep(0)
    assert seen == [{"ns": ["m1"]}]


def test_namespace_erasure_covers_precedents() -> None:
    from server.erasure.store import census as erasure_census
    from server.erasure.store import steps_graph as graph_steps

    assert "loom.memory_ask_precedents" in erasure_census.census_tables("namespace")
    # The user scope reaches precedents through their window keys
    assert "loom.memory_ask_precedents" in erasure_census.census_tables("user_id")
    ch = _StubCH()
    n = asyncio.run(graph_steps.delete_ask_precedents_by_namespace("o", "ns", ch=ch))
    assert n >= 0
    assert any("loom.memory_ask_precedents" in sql for sql, _ in ch.queries)


def test_non_memory_planes_are_hashed_from_their_excerpt_and_do_not_block_the_store() -> None:
    from server.memory.domain.precedents import local_evidence, merge, validity, window_identity

    w = [
        SimpleNamespace(memory_key="m1", namespace="", source="fact", content_excerpt="a"),
        SimpleNamespace(
            memory_key="code:repo/README.md:1",
            namespace="",
            source="code_index",
            content_excerpt="# README",
        ),
        SimpleNamespace(
            memory_key="sess-9", namespace="", source="summary", content_excerpt="digest"
        ),
    ]
    assert window_identity(w, "ns") == [("ns", "m1")]  # only the memory row
    loc = local_evidence(w)
    assert set(loc.hashes) == {("#code_index", "code:repo/README.md:1"), ("#summary", "sess-9")}
    assert loc.verifiable
    db = Evidence({("ns", "m1"): H1})
    stored = merge(db, loc)
    p = Precedent(
        precedent_key="k" * 64, question="q", answer="A [m1].", cited_keys=["m1"], evidence=stored
    )
    window = window_identity(w, "ns") + sorted(loc.hashes)
    assert validity(p, merge(db, loc), window=window) == ""
    # the code row's excerpt changed → the window changed
    w[1].content_excerpt = "# README v2"
    assert validity(p, merge(db, local_evidence(w)), window=window) == "content_changed"


@pytest.mark.asyncio
async def test_store_proceeds_with_a_code_row_in_the_window(prec_env) -> None:
    from tests.unit.test_memory_ask import _hit

    code_hit = _hit("code:repo/README.md:1", excerpt="# README")
    code_hit.source = "code_index"
    prec_env.hits.append(code_hit)
    first = await _ask()
    assert first.source == "synthesized" and len(prec_env.inserts) == 1
    row = prec_env.inserts[0]
    assert (
        "#code_index" in row["window_namespaces"] and "code:repo/README.md:1" in row["window_keys"]
    )
    second = await _ask()
    assert second.source == "precedent"


# ---- fast path: served before retrieval when nothing moved --


def _fast_prec(**kw: Any) -> Precedent:
    kw.setdefault("retrieved_at", datetime(2026, 9, 12, 8, 0, tzinfo=UTC))
    return _prec(**kw)


def test_fast_eligibility_needs_a_retrieval_time_a_covered_window_and_a_young_row() -> None:
    now = datetime(2026, 9, 12, 8, 30, tzinfo=UTC)
    assert pure.fast_eligibility(_fast_prec(), now=now) == ""
    assert pure.fast_eligibility(_prec(), now=now) == "no_retrieved_at"
    epoch = _prec(retrieved_at=datetime(1970, 1, 1, tzinfo=UTC))
    assert pure.fast_eligibility(epoch, now=now) == "no_retrieved_at"
    old = _fast_prec(retrieved_at=datetime(2026, 9, 12, 6, 0, tzinfo=UTC))
    assert pure.fast_eligibility(old, now=now, max_age_hours=1) == "rank_drift_bound"
    assert pure.fast_eligibility(old, now=now, max_age_hours=0) == ""
    covered = _fast_prec(evidence=Evidence({("ns", "m1"): H1, ("#summary", "s1"): H2}))
    assert pure.fast_eligibility(covered, now=now) == ""
    code = _fast_prec(evidence=Evidence({("ns", "m1"): H1, ("#code_index", "c1"): H2}))
    assert pure.fast_eligibility(code, now=now) == "uncovered_plane:code_index"
    assert pure.fast_eligibility(_fast_prec(answer=" "), now=now) == "empty_answer"


def test_fast_namespaces_are_the_requests_and_every_cited_rows() -> None:
    p = _fast_prec(evidence=Evidence({("ns", "m1"): H1, ("other", "m2"): H2, ("#chunk", "c"): H1}))
    assert pure.fast_namespaces(p, "ns") == ["ns", "other"]


def test_marks_block_when_any_plane_moved_at_or_after_the_retrieval() -> None:
    p = _fast_prec()
    at = p.retrieved_at
    assert at is not None
    before, after = at - timedelta(seconds=1), at + timedelta(seconds=1)
    assert pure.marks_block(p, {}, namespaces=["ns"]) == ""
    assert pure.marks_block(p, {"ns": before}, namespaces=["ns"]) == ""
    assert pure.marks_block(p, {"ns": after}, namespaces=["ns"]) == "moved:ns"
    assert (
        pure.marks_block(p, {"ns": at}, namespaces=["ns"]) == "moved:ns"
    )  # same instant: not proven
    assert pure.marks_block(p, {"*": after}, namespaces=["ns"]) == "moved:*"  # an erasure bump
    assert (
        pure.marks_block(p, {"ns": before}, namespaces=["ns"], blacklist_at=after)
        == "moved:blacklist"
    )
    assert pure.marks_block(p, {"ns": before}, namespaces=["ns"], blacklist_at=before) == ""


def test_store_and_lookup_carry_the_retrieval_time() -> None:
    ch = _StubCH(one=None)
    at = datetime(2026, 9, 12, 8, 0, 0, 250000, tzinfo=UTC)
    asyncio.run(
        repo.store(
            org="o",
            namespace="ns",
            precedent_key="k" * 64,
            question="q",
            shape_sha256="",
            answer="Blue [m1].",
            cited_keys=["m1"],
            evidence=_ev(m1=H1),
            retrieved_at=at,
            ch=ch,
        )
    )
    assert ch.inserts[0][1][0]["retrieved_at"] == datetime(2026, 9, 12, 8, 0, 0, 250000)
    stored = {
        "precedent_key": "k" * 64,
        "question": "q",
        "answer": "Blue [m1].",
        "cited_keys": ["m1"],
        "window_namespaces": ["ns"],
        "window_keys": ["m1"],
        "window_sha256": [H1],
        "synth_model": "",
        "reuse_count": 0,
        "created_at": datetime(2026, 9, 12, 8, 0),
        "retrieved_at": datetime(2026, 9, 12, 8, 0, 0, 250000),
    }
    p = asyncio.run(repo.lookup("o", "ns", "k" * 64, ch=_StubCH(one=stored)))
    assert p is not None and p.retrieved_at == at
    touched = _StubCH(one=None)
    asyncio.run(repo.touch(org="o", namespace="ns", prec=p, ch=touched))
    assert touched.inserts[0][1][0]["retrieved_at"] == datetime(2026, 9, 12, 8, 0, 0, 250000)


def test_evidence_read_treats_a_redacted_row_as_gone() -> None:
    ch = _StubCH(rows=[])
    asyncio.run(repo.evidence_for("o", [("ns", "m1")], ch=ch))
    assert "redacted_at IS NULL" in ch.queries[0][0]


@pytest.fixture
def fast_env(prec_env, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """prec_env plus the write marks: `marks` is namespace → last write,
    `blacklist_at` the blacklist watermark, `searches` counts retrievals."""
    import server.memory.app.search as hsearch
    from server.core import namespace_write_marks as marks_repo
    from tests.unit.test_memory_ask import _search_output

    env = prec_env
    env.marks: dict[str, datetime] = {}
    env.blacklist_at: datetime | None = None
    env.searches = 0

    async def counting_search(search_input: Any):
        env.searches += 1
        return _search_output(list(env.hits))

    async def fake_last_writes(org: str, namespaces: list[str], *, ch: Any = None):
        return {ns: ts for ns, ts in env.marks.items() if ns in {*namespaces, "*"}}

    async def fake_blacklist(*, ch: Any = None):
        return env.blacklist_at

    monkeypatch.setattr(hsearch, "handle_memory_search", counting_search)
    monkeypatch.setattr(marks_repo, "last_writes", fake_last_writes)
    monkeypatch.setattr(marks_repo, "blacklist_updated_at", fake_blacklist)
    return env


@pytest.mark.asyncio
async def test_an_unmoved_namespace_serves_the_precedent_without_retrieving(fast_env) -> None:
    first = await _ask()
    assert first.source == "synthesized" and fast_env.searches == 1
    row = fast_env.inserts[0]
    assert isinstance(row["retrieved_at"], datetime)

    second = await _ask(query="What is my favorite color?")
    assert second.source == "precedent"
    assert second.answer == first.answer and second.memory_keys == ["m1"]
    assert second.route_reason == "precedent:unmoved:reused=1"
    assert second.results == []
    assert fast_env.searches == 1  # no second retrieval
    assert len(fast_env.llm.calls) == 1
    assert fast_env.inserts[-1]["reuse_count"] == 1


@pytest.mark.asyncio
async def test_a_write_since_the_retrieval_falls_back_to_the_retrieving_check(fast_env) -> None:
    await _ask()
    fast_env.marks["ns"] = first_at = fast_env.inserts[0]["retrieved_at"].replace(tzinfo=UTC)
    second = await _ask()
    # The mark is at the retrieval instant: not proven unmoved, so retrieval
    # runs; the window is byte-identical, so the precedent still serves the row.
    assert fast_env.searches == 2
    assert second.source == "precedent" and second.route_reason == "precedent:reused=1"
    fast_env.marks["ns"] = first_at - timedelta(seconds=5)
    third = await _ask()
    assert fast_env.searches == 2 and third.route_reason == "precedent:unmoved:reused=2"


@pytest.mark.asyncio
async def test_an_org_wide_erasure_mark_or_a_blacklist_change_blocks_the_fast_path(
    fast_env,
) -> None:
    await _ask()
    at = fast_env.inserts[0]["retrieved_at"].replace(tzinfo=UTC)
    fast_env.marks["*"] = at + timedelta(seconds=1)
    await _ask()
    assert fast_env.searches == 2
    del fast_env.marks["*"]
    fast_env.blacklist_at = at + timedelta(seconds=1)
    await _ask()
    assert fast_env.searches == 3
    fast_env.blacklist_at = at - timedelta(seconds=1)
    await _ask()
    assert fast_env.searches == 3


@pytest.mark.asyncio
async def test_a_cited_row_that_changed_or_vanished_is_a_fast_miss(fast_env) -> None:
    await _ask()
    fast_env.hashes["m2"] = (
        "c" * 64
    )  # an edit the marks would also see; the row check catches it alone
    out = await _ask()
    assert fast_env.searches == 2 and out.source == "synthesized"
    assert len(fast_env.llm.calls) == 2


@pytest.mark.asyncio
async def test_the_fast_path_is_off_under_the_postgres_authority(monkeypatch) -> None:
    # Its marks and evidence are ClickHouse's, which can trail a delete the authority
    # holds; the ask retrieves, and the precedent is judged against the checked pool.
    from server.core import pg
    from server.memory.app import precedents

    read: list[Any] = []

    async def _lookup(*a: Any, **_k: Any) -> None:
        read.append(a)

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(repo, "lookup", _lookup)
    assert await precedents.fast_reuse("k", org="o", namespace="ns") is None
    assert read == [], "no precedent is read"


@pytest.mark.asyncio
async def test_the_fast_path_is_off_with_precedents_off(fast_env) -> None:
    await _ask()
    out = await _ask(precedents=False)
    assert fast_env.searches == 2 and out.source == "synthesized"


@pytest.mark.asyncio
async def test_user_erasure_bumps_the_org_wide_mark_before_its_first_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core import namespace_write_marks as marks_repo
    from server.erasure.app import saga as h

    bumps: list[tuple[str, str, str]] = []

    async def fake_bump(org: str, namespace: str, plane: str, *, ch: Any = None) -> None:
        bumps.append((org, namespace, plane))

    async def stop_here(**kw: Any):
        raise RuntimeError("stop")

    monkeypatch.setattr(marks_repo, "bump", fake_bump)
    monkeypatch.setattr(h, "_do_step", stop_here)
    with pytest.raises(RuntimeError):
        await h._saga_user_id("o", "u1", completed=set(), row={})
    assert bumps == [("o", "*", "erasure")]


def test_lookup_decodes_a_fixedstring_key_so_the_touch_fits_the_column() -> None:
    stored = {
        "precedent_key": b"k" * 64,
        "question": "q",
        "answer": "Blue [m1].",
        "cited_keys": ["m1"],
        "window_namespaces": ["ns"],
        "window_keys": ["m1"],
        "window_sha256": [H1],
        "synth_model": "",
        "reuse_count": 3,
        "created_at": datetime(2026, 9, 12, 8, 0),
        "retrieved_at": datetime(2026, 9, 12, 8, 0),
    }
    p = asyncio.run(repo.lookup("o", "ns", "k" * 64, ch=_StubCH(one=stored)))
    assert p is not None and p.precedent_key == "k" * 64 and len(p.precedent_key) == 64
    ch = _StubCH(one=None)
    later = datetime(2026, 9, 12, 9, 0, tzinfo=UTC)
    asyncio.run(repo.touch(org="o", namespace="ns", prec=p, retrieved_at=later, ch=ch))
    row = ch.inserts[0][1][0]
    assert row["precedent_key"] == "k" * 64 and row["reuse_count"] == 4
    assert row["retrieved_at"] == datetime(
        2026, 9, 12, 9, 0
    )  # the proof moved up to this retrieval
    plain = _StubCH(one=None)
    asyncio.run(repo.touch(org="o", namespace="ns", prec=p, ch=plain))
    assert plain.inserts[0][1][0]["retrieved_at"] == datetime(
        2026, 9, 12, 8, 0
    )  # untouched when not given


@pytest.mark.asyncio
async def test_a_retrieving_hit_moves_the_retrieval_time_so_the_next_ask_is_fast(fast_env) -> None:
    await _ask()
    first_at = fast_env.inserts[0]["retrieved_at"].replace(tzinfo=UTC)
    fast_env.marks["ns"] = first_at + timedelta(microseconds=1)  # a write after the first retrieval
    second = await _ask()
    assert fast_env.searches == 2 and second.route_reason == "precedent:reused=1"
    touched = fast_env.inserts[-1]
    assert touched["reuse_count"] == 1
    assert touched["retrieved_at"].replace(tzinfo=UTC) > fast_env.marks["ns"]
    third = await _ask()  # the mark now predates the refreshed retrieval time
    assert fast_env.searches == 2 and third.route_reason == "precedent:unmoved:reused=2"


def test_an_operational_setting_leaves_the_shape_as_it_was() -> None:
    """A timeout, a wait or retry bound or a cache lifetime changes no stored answer:
    raising one during an incident keeps every stored precedent."""
    from server.memory.app.ask.handler import ask_shape_sha256
    from server.settings import Settings
    from tests.unit.test_memory_ask import _input

    base = Settings(_env_file=None)  # type: ignore[call-arg]
    for name, value in (
        ("memory_search_rerank_timeout_s", 30.0),
        ("memory_search_hyde_timeout_s", 30.0),
        ("memory_search_page_ttl_s", 60),
        ("memory_search_embed_bound_s", 9.0),
        ("memory_search_embed_bound_retries", 3),
        ("memory_search_fallback_max_wall_ms", 9000),
    ):
        changed = base.model_copy(update={name: value})
        assert ask_shape_sha256(_input(), changed) == ask_shape_sha256(_input(), base), name


def test_ask_shape_hashes_the_identity_the_token_pins() -> None:
    """Two tokens pinned to different end users, one body that names nobody: the read runs
    as each pinned user, so the shapes (and the precedent keys) must differ; a namespace
    token still shares one shape across readers."""
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.app.ask.handler import ask_shape_sha256
    from server.operations.base import Role
    from server.settings import settings
    from tests.unit.test_memory_ask import _input

    def _token(pinned: str) -> AuthInfo:
        return AuthInfo(
            subject="sa:bot",
            org="o",
            namespaces=("n",),
            roles=(Role.VIEWER,),
            is_service_account=True,
            read_user_id=pinned,
        )

    try:
        set_current_auth(_token("alice"))
        alice = ask_shape_sha256(_input(), settings)
        # Under alice's token a body that already names alice hashes as the pinned default does.
        named_alice = ask_shape_sha256(_input(read_scope="own_user", user_id="alice"), settings)
        set_current_auth(_token("bob"))
        bob = ask_shape_sha256(_input(), settings)
        set_current_auth(_token(""))
        shared = ask_shape_sha256(_input(), settings)
    finally:
        set_current_auth(None)
    assert alice != bob
    assert shared == ask_shape_sha256(_input(), settings)
    assert alice == named_alice
    # With no token the same body is served every bot's rules, and the pinned token
    # (which signs no agent) none, so the two never share a shape.
    assert alice != ask_shape_sha256(_input(read_scope="own_user", user_id="alice"), settings)


def test_ask_shape_hashes_the_agent_whose_rules_the_token_is_served() -> None:
    """Two tokens pinned to one end user, signed for different bots, one body under the
    default agent: each is served only its own bot's promoted rules, so the precedent
    keys must differ; a caller with no pin hashes the shape without a rule_agent key."""
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.app.ask.handler import (
        _ASK_SHAPE_EXCLUDE,
        _shape_settings,
        ask_shape_sha256,
    )
    from server.memory.domain.precedents import precedent_key
    from server.operations.base import Role
    from server.settings import settings
    from tests.unit.test_memory_ask import _input

    def _token(agent: str) -> AuthInfo:
        return AuthInfo(
            subject="sa:bot",
            org="o",
            namespaces=("n",),
            roles=(Role.VIEWER,),
            is_service_account=True,
            read_user_id="alice",
            agent_principal=f"agent:{agent}",
        )

    def _key() -> str:
        return precedent_key(_input().query, ask_shape_sha256(_input(), settings))

    unpinned = _key()
    try:
        set_current_auth(_token("support-bot"))
        support = _key()
        set_current_auth(_token("sales-bot"))
        sales = _key()
    finally:
        set_current_auth(None)
    assert support != sales
    # With no pin the shape hashes as a build without the rule_agent key did, so
    # precedents stored before an upgrade still match. That build's shape, by hand:
    body = _input()
    exclude = set(_ASK_SHAPE_EXCLUDE)
    exclude |= {f for f in ("user_id", "session_id") if getattr(body, f) in (None, "-")}
    shape = body.model_dump(exclude=exclude, exclude_defaults=True)
    if shaped := _shape_settings(settings):
        shape["settings"] = shaped
    canonical = json.dumps(shape, sort_keys=True, default=str, separators=(",", ":"))
    older_build = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert unpinned == precedent_key(body.query, older_build)
