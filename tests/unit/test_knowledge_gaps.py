"""Knowledge gaps: the query normalisation, the merge of near-identical groups, the
recording gate (the switch, the invoked op, an asker from the token, a namespace-wide
read), each search op's miss rule, and the worker's build with its erasure fence."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.knowledge.app import gaps as gaps_app
from server.knowledge.domain.gaps import Group, gap_id, merge_groups, normalize_query, ranked
from server.knowledge.store import gaps as gaps_store
from server.knowledge.workers import knowledge_gaps as worker
from server.settings import settings

T0 = datetime(2026, 1, 1, 12, 0, 0)
T1 = datetime(2026, 1, 2, 12, 0, 0)


def _group(head: str, asks: int, askers: set[str], last_at: datetime = T0) -> Group:
    return Group(head=head, asks=asks, last_at=last_at, askers=askers)


def test_normalize_query_folds_case_and_punctuation() -> None:
    assert normalize_query("  What's our ARR?! ") == "what s our arr"
    assert normalize_query("orders_daily — rows") == "orders_daily rows"
    assert normalize_query("?!") == ""
    assert normalize_query("C++ vs C#") == "c++ vs c#"


def test_normalize_query_folds_as_a_name_is() -> None:
    # NFKC (full-width), a byte-order mark dropped, whitespace collapsed
    assert normalize_query("什么是ＭＲＲ\ufeff") == "什么是mrr"
    assert normalize_query("churn  rate") == normalize_query("Churn Rate") == "churn rate"
    assert normalize_query("Straße") == "strasse"
    # a mark stays with its letter; one with no letter before it is punctuation
    assert normalize_query("बिक्री कर") == "बिक्री कर"
    assert normalize_query("\u0301what is arr") == "what is arr"


def test_normalize_query_refuses_invalid_unicode() -> None:
    assert normalize_query("what is \ud800") is None


def test_gap_id_is_per_org_namespace_and_head() -> None:
    assert gap_id("o", "ns", "what is arr") == gap_id("o", "ns", "what is arr")
    assert gap_id("o", "ns", "what is arr") != gap_id("o", "other", "what is arr")
    assert gap_id("o", "ns", "what is arr") != gap_id("o2", "ns", "what is arr")


def test_merge_folds_a_close_group_into_the_more_asked_one() -> None:
    a = _group("what is arr", 5, {"u1", "u2", "u3"})
    b = _group("whats arr", 3, {"u3", "u4"}, last_at=T1)
    c = _group("refund policy", 4, {"u5"})
    merged = merge_groups([a, b, c], [[1.0, 0.0], [0.99, 0.1], [0.0, 1.0]], 0.9)
    assert [g.head for g in merged] == ["what is arr", "refund policy"]
    gap = merged[0]
    assert gap.asks == 8
    assert gap.last_at == T1
    assert gap.samples == ["whats arr"]
    assert gap.askers == {"u1", "u2", "u3", "u4"}


def test_ranked_orders_by_askers_then_asks() -> None:
    few = _group("a", 9, {"u1", "u2", "u3"})
    many = _group("b", 3, {"u1", "u2", "u3", "u4"})
    tie = _group("c", 4, {"u1", "u2", "u3"})
    assert [g.head for g in ranked([few, many, tie])] == ["b", "a", "c"]


class _Auth:
    def __init__(self, subject: str, creator: str = "") -> None:
        self.subject = subject
        self.creator = creator


@pytest.fixture(autouse=True)
def _fresh_recordings(monkeypatch: pytest.MonkeyPatch) -> None:
    # The in-flight count is process-wide; each test starts from none.
    monkeypatch.setattr(gaps_app, "recordings", gaps_app._Recordings())


@pytest.fixture(autouse=True)
def _no_erasures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def none(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr("server.erasure.app.ledger_reads.unfinished_reaching", none)


@pytest.fixture
def scheduled(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The rows record_miss would insert, captured instead of scheduled."""
    rows: list[dict[str, Any]] = []

    async def insert(row: dict[str, Any]) -> None:
        rows.append(row)

    def schedule(coro: Any, *, name: str, scope: Any = None) -> object:
        # Run the insert inline (it awaits nothing): no request loop to outlive.
        with pytest.raises(StopIteration):
            coro.send(None)
        return object()  # a scheduled task

    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)

    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: True)

    async def same(org: str, subject: str) -> str:
        return subject

    monkeypatch.setattr(gaps_app, "current_capture_level", lambda: "query")
    monkeypatch.setattr("server.auth.aliases.login_handle", same)
    monkeypatch.setattr(gaps_store, "insert_miss", insert)
    monkeypatch.setattr("server.memory.app.after_write.schedule", schedule)
    monkeypatch.setattr(gaps_app, "current_operation", lambda: "knowledge.search")
    monkeypatch.setattr("server.auth.context.current_auth", lambda: _Auth("alice"))
    return rows


def _record(**kw: Any) -> None:
    args: dict[str, Any] = {
        "op": "knowledge.search",
        "org": "o",
        "namespace": "ns",
        "query": "What is ARR?",
    }
    args.update(kw)
    gaps_app.record_miss(**args)


def test_a_miss_of_the_invoked_op_is_recorded_under_the_tokens_subject(
    scheduled: list[dict[str, Any]],
) -> None:
    _record()
    assert len(scheduled) == 1
    row = scheduled[0]
    assert row["normalized"] == "what is arr"
    assert "query" not in row  # the raw text is never stored
    assert (row["asker"], row["user_id"]) == ("alice", "-")
    assert "actor" not in row  # the asker is the person; no second principal is kept
    assert "nearest" not in row


def test_an_end_user_token_names_the_end_user(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("server.auth.context.current_auth", lambda: _Auth("user:u9"))
    _record()
    assert [(r["asker"], r["user_id"]) for r in scheduled] == [("u9", "u9")]


async def test_a_service_accounts_token_without_the_claim_counts_as_its_creator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.auth.service_accounts import SACreator

    inserted: list[dict[str, Any]] = []

    async def insert(row: dict[str, Any]) -> None:
        inserted.append(row)

    async def creator(org: str, sub: str) -> SACreator | None:
        chain = {
            "sa:bot": "alice",
            "sa:child": "sa:bot",
            "sa:loop": "sa:loop",
            "sa:agent-x-1a2b": "agent-x",  # an agent sign-up: its namespace made it
            "sa:alice-ci-9f8e": "alice",  # a person's account whose name starts with theirs
        }
        kind = "agent" if sub == "sa:agent-x-1a2b" else "service_account"
        return SACreator(subject=chain[sub], inherits=True, kind=kind) if sub in chain else None

    monkeypatch.setattr(gaps_store, "insert_miss", insert)

    async def canonical(org: str, subject: str) -> str | None:
        # A contact email folds onto its handle, which may itself be an email; a spelling
        # two accounts share names no one person.
        ties = {
            "alice@x.io": "alice",
            "al@home.io": "alice@corp.io",
            "shared@x.io": None,
            "sam@x.io": "asmith",
        }
        return ties.get(subject, subject.lower())

    monkeypatch.setattr("server.auth.service_accounts.sa_creator", creator)
    monkeypatch.setattr("server.auth.aliases.login_handle", canonical)
    for asker in (
        "sa:bot",
        "sa:child",
        "bob",
        "sa:unknown",
        "sa:loop",
        "alice@x.io",
        "sa:agent-x-1a2b",
        "sa:alice-ci-9f8e",
        "Carol@X.io",
        "al@home.io",
        "shared@x.io",
        "ASmith",  # one person's handle token and email token count once
        "sam@x.io",
    ):
        await gaps_app._insert({"org": "o", "namespace": "ns", "asker": asker, "user_id": "-"}, 0)
    # A chain climbs to its person, an email folds onto its login handle, and the rest is
    # lower-cased; an unreadable or personless chain and a shared email record nothing.
    assert [r["asker"] for r in inserted] == [
        "alice",
        "alice",
        "bob",
        "alice",
        "alice",
        "carol@x.io",
        "alice@corp.io",
        "asmith",
        "asmith",
    ]


async def test_login_handle_returns_the_tied_accounts_handle() -> None:
    from server.auth.aliases import login_handle

    class _CH:
        def __init__(self, rows: list[dict[str, Any]]) -> None:
            self.rows = rows

        async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
            return self.rows

    one = [{"owner_email": "Alice@Corp.io", "contact_email": "al@home.io"}]
    two = [*one, {"owner_email": "bob", "contact_email": "al@home.io"}]
    assert await login_handle("o", "al@home.io", ch=_CH(one)) == "alice@corp.io"
    assert await login_handle("o", "al@home.io", ch=_CH([])) == "al@home.io"
    assert await login_handle("o", "al@home.io", ch=_CH(two)) is None
    assert await login_handle("o", "alice", ch=_CH(two)) == "alice"
    # Every answer is lower-cased, as a user erasure compares spellings.
    assert await login_handle("o", "ASmith", ch=_CH([])) == "asmith"
    assert await login_handle("o", "Al@Home.io", ch=_CH([])) == "al@home.io"


def test_a_query_holding_a_high_or_medium_shape_records_nothing(
    scheduled: list[dict[str, Any]],
) -> None:
    _record(query="what is password: hunter2hunter2")
    _record(query="who owns billing, alice@example.com")
    assert scheduled == []
    _record(query="who owns billing")
    assert len(scheduled) == 1


def test_tokens_a_person_minted_count_as_that_person(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    for auth in (_Auth("sa:1", creator="alice"), _Auth("sa:2", creator="alice")):
        monkeypatch.setattr("server.auth.context.current_auth", lambda a=auth: a)
        _record()
    assert [r["asker"] for r in scheduled] == ["alice", "alice"]


def test_no_token_records_nothing(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("server.auth.context.current_auth", lambda: None)
    _record()
    assert scheduled == []


def test_a_search_another_op_runs_records_nothing(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gaps_app, "current_operation", lambda: "search.query")
    _record()
    monkeypatch.setattr(gaps_app, "current_operation", lambda: "")
    _record()
    assert scheduled == []


def test_single_user_mode_records_nothing(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "identity_provider", "single-user")
    _record()
    assert scheduled == []


def test_gaps_off_records_nothing(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", False)
    _record()
    assert scheduled == []


def test_misses_past_the_in_flight_bound_are_dropped_and_counted(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "knowledge_gaps_max_in_flight", 1)
    monkeypatch.setattr(gaps_app.recordings, "in_flight", 1)
    monkeypatch.setattr(gaps_app.recordings, "dropped", 0)
    _record()
    assert scheduled == [] and gaps_app.recordings.dropped == 1
    monkeypatch.setattr(gaps_app.recordings, "in_flight", 0)
    _record()
    assert len(scheduled) == 1 and gaps_app.recordings.in_flight == 0


def test_a_query_of_punctuation_records_nothing(scheduled: list[dict[str, Any]]) -> None:
    _record(query="??")
    assert scheduled == []


def test_nothing_is_recorded_while_an_older_replica_serves(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: False)
    _record()
    assert scheduled == []


def _cap(name: str) -> int:
    field = type(settings).model_fields[name]
    [cap] = [m.le for m in field.metadata if getattr(m, "le", None) is not None]
    return int(cap)


def test_two_intervals_and_two_passes_fit_in_a_builds_life() -> None:
    # A restart may delay the first pass by up to one more interval, and a namespace may
    # be written early in one pass and late in the next.
    longest = 2 * _cap("knowledge_gaps_interval_seconds") + 2 * _cap(
        "knowledge_gaps_max_pass_seconds"
    )
    assert longest < gaps_store.BUILD_DAYS * 86_400


def test_the_rollout_gate_reads_the_fleets_schema(monkeypatch: pytest.MonkeyPatch) -> None:
    from pathlib import Path

    from server.core import instances

    migrations = Path(__file__).resolve().parents[2] / "schemas" / "migrations"
    assert (migrations / f"{gaps_app.GAPS_MIGRATION:03d}_knowledge_gaps.sql").exists()
    at = [{"schema_version": gaps_app.GAPS_MIGRATION}]
    monkeypatch.setattr(instances, "live_rows", lambda: at)
    assert gaps_app.every_replica_erases_gaps()
    monkeypatch.setattr(instances, "live_rows", lambda: [*at, {"schema_version": 94}])
    assert not gaps_app.every_replica_erases_gaps()
    monkeypatch.setattr(instances, "live_rows", lambda: [])
    assert not gaps_app.every_replica_erases_gaps()


async def test_the_list_answers_503_while_an_older_replica_serves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.core.errors import OverloadedError
    from server.knowledge.api.models_gaps import KnowledgeGapsListInput

    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: False)
    with pytest.raises(OverloadedError):
        await gaps_app.handle_knowledge_gaps_list(KnowledgeGapsListInput(org="o", namespace="ns"))


async def test_gaps_off_lists_nothing_though_builds_are_stored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_gaps import KnowledgeGapsListInput

    async def stored(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        raise AssertionError("the list reads no build while gaps are off")

    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: True)
    monkeypatch.setattr(gaps_store, "latest_gaps", stored)
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", False)
    out = await gaps_app.handle_knowledge_gaps_list(KnowledgeGapsListInput(org="o", namespace="ns"))
    assert (out.gaps, out.enabled, out.capturing) == ([], False, None)


async def test_a_namespace_that_withdrew_consent_lists_nothing_before_the_next_pass(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_gaps import KnowledgeGapsListInput

    async def stored(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        raise AssertionError("the list reads no build once consent is off")

    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: True)
    monkeypatch.setattr(gaps_store, "latest_gaps", stored)
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(gaps_app, "current_capture_level", lambda: "off")
    out = await gaps_app.handle_knowledge_gaps_list(KnowledgeGapsListInput(org="o", namespace="ns"))
    assert (out.gaps, out.built_at, out.capturing) == ([], None, False)


async def test_a_capturing_namespace_lists_its_latest_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.knowledge.api.models_gaps import KnowledgeGapsListInput

    built = datetime(2026, 1, 2, 13, 0)
    base = {"built_at": built, "last_at": built, "samples": [], "askers": 3, "asks": 4}
    rows = [{**base, "gap_id": "", "head": ""}, {**base, "gap_id": "g1", "head": "what is arr"}]

    async def stored(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return rows

    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: True)
    monkeypatch.setattr(gaps_store, "latest_gaps", stored)
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(gaps_app, "current_capture_level", lambda: "query")
    out = await gaps_app.handle_knowledge_gaps_list(KnowledgeGapsListInput(org="o", namespace="ns"))
    assert (out.enabled, out.capturing, out.built_at) == (True, True, built)
    assert [g.head for g in out.gaps] == ["what is arr"]


def test_a_read_narrowed_token_is_refused_the_gap_ops() -> None:
    from server.identity.domain.read_scope import honours_read_scope

    assert honours_read_scope("knowledge.search")
    assert not honours_read_scope("knowledge.gaps.list")


@pytest.fixture
def misses(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """What each search op hands record_miss, from a token that reads the namespace."""
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(gaps_app, "record_miss", lambda **kw: calls.append(kw))
    monkeypatch.setattr(gaps_app, "reads_whole_namespace", lambda: True)
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)
    return calls


_EMPTY: dict[str, Any] = {"memory": {"results": []}, "knowledge": {"results": []}}


@pytest.mark.parametrize(
    ("extra", "payload", "repo_namespace", "recorded"),
    [
        ({}, _EMPTY, "", True),
        ({}, {"memory": {"results": [{}]}, "knowledge": {"results": []}}, "", False),
        ({}, {"memory": {"results": []}, "knowledge": {"results": [{}]}}, "", False),
        ({}, {"memory": {"error": "X", "results": []}, "knowledge": {}}, "", False),
        ({}, {**_EMPTY, "conversations": {"results": [{}]}}, "", False),
        ({}, {**_EMPTY, "profile": {"results": [{}]}}, "", False),
        # Tags narrow the search; a later page was judged with the first.
        ({"tags": ["x"]}, _EMPTY, "", False),
        ({"page": "p2"}, _EMPTY, "", False),
        ({}, {**_EMPTY, "tracker": {"verdict": "duplicate"}}, "", False),
        ({}, {**_EMPTY, "namespaces_skipped": ["other"]}, "", False),
        ({}, {**_EMPTY, "memory": {"results": [], "pending_extraction": 2}}, "", False),
        ({}, {**_EMPTY, "memory": {"results": [], "stale_dropped": 1}}, "", False),
        ({}, {**_EMPTY, "memory": {"results": [], "degraded": ["x"]}}, "", False),
        # An answer that led withholds the rest: not a gap.
        ({}, {**_EMPTY, "withheld": {"memory": 2, "reason": "home_fact_leads"}}, "", False),
        # Rows dropped below the precision floor are nothing found: a gap.
        ({}, {**_EMPTY, "withheld": {"memory": 3, "reason": "below_precision_floor"}}, "", True),
        ({}, {**_EMPTY, "withheld": {"memory": 3, "reason": "loom_output"}}, "", True),
        # A memory leg that abstained keeps its flagged hits: it missed, as memory.search does.
        (
            {},
            {**_EMPTY, "memory": {"results": [{}], "abstain": True, "retrieval_risk": "abstain"}},
            "",
            True,
        ),
        ({}, {**_EMPTY, "memory": {"results": [{}], "retrieval_risk": "high"}}, "", True),
        # A decision the repository pass merged in answers, though the home search came back weak.
        (
            {},
            {**_EMPTY, "memory": {"results": [{"namespace": "code-kj"}], "retrieval_risk": "high"}},
            "code-kj",
            False,
        ),
        # Not when the repository is the home namespace, or the weak hit is another namespace's.
        (
            {},
            {**_EMPTY, "memory": {"results": [{"namespace": "ns"}], "retrieval_risk": "high"}},
            "ns",
            True,
        ),
        (
            {},
            {**_EMPTY, "memory": {"results": [{"namespace": "else"}], "retrieval_risk": "high"}},
            "code-kj",
            True,
        ),
    ],
)
def test_loom_search_records_a_miss_only_when_every_section_missed(
    misses: list[dict[str, Any]],
    extra: dict[str, Any],
    payload: dict[str, Any],
    repo_namespace: str,
    recorded: bool,
) -> None:
    from server.memory.api.models_loom_search import SearchInput
    from server.memory.app.loom_search import handler

    q = SearchInput(org="o", namespace="ns", query="what is arr", **extra)
    handler._record_gap_miss(q, payload, repo_namespace)
    assert [c["op"] for c in misses] == (["search.query"] if recorded else [])


def test_loom_search_records_no_miss_under_a_narrowed_token_or_while_disabled(
    misses: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.api.models_loom_search import SearchInput
    from server.memory.app.loom_search import handler

    q = SearchInput(org="o", namespace="ns", query="what is arr")
    monkeypatch.setattr(gaps_app, "reads_whole_namespace", lambda: False)
    handler._record_gap_miss(q, _EMPTY, "")
    monkeypatch.setattr(gaps_app, "reads_whole_namespace", lambda: True)
    monkeypatch.setattr(settings, "knowledge_gaps_enabled", False)
    handler._record_gap_miss(q, _EMPTY, "")
    assert misses == []


def test_knowledge_search_misses_only_with_no_close_keyed_or_lexical_hit(
    misses: list[dict[str, Any]],
) -> None:
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput, _SearchHit
    from server.knowledge.app import knowledge_search

    def hit(uri: str, ns: str) -> _SearchHit:
        return _SearchHit(
            source_uri=uri,
            chunk_idx=0,
            content="",
            tags=[],
            distance=1.5,
            similarity=-0.5,
            namespace=ns,
        )

    q = KnowledgeSearchInput(org="o", namespace="ns", query="what is arr")
    far = [hit("doc://a", ""), hit("doc://b", "ns"), hit("doc://c", "x")]
    assert knowledge_search._missed(q, far, skipped=False, dropped=False)
    keyed = hit("doc://k", "ns")
    keyed.keys_matched = ["arr"]
    assert not knowledge_search._missed(q, [keyed], skipped=False, dropped=False)
    tagged = KnowledgeSearchInput(org="o", namespace="ns", query="what is arr", tags=["x"])
    assert not knowledge_search._missed(tagged, far, skipped=False, dropped=False)
    lexical = KnowledgeSearchInput(org="o", namespace="ns", query="arr", search_mode="lexical")
    assert not knowledge_search._missed(lexical, far, skipped=False, dropped=False)
    assert knowledge_search._missed(lexical, [], skipped=False, dropped=False)
    # A fan-out that stopped short, or hits the authority dropped, is no gap.
    assert not knowledge_search._missed(q, far, skipped=True, dropped=False)
    assert not knowledge_search._missed(q, far, skipped=False, dropped=True)


def test_memory_search_records_a_miss_only_of_a_namespace_wide_read(
    misses: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app.search import output

    def out(abstain: bool, risk: str, **kw: Any) -> Any:
        from server.memory.api.models_search import MemorySearchOutput

        return MemorySearchOutput(
            results=[], embedding_model="m", abstain=abstain, retrieval_risk=risk, **kw
        )

    def q(**kw: Any) -> Any:
        from server.memory.api.models_search import MemorySearchInput

        return MemorySearchInput(org="o", namespace="ns", query="what is arr", **kw)

    for abstain, risk in ((True, "abstain"), (False, "high"), (False, "low")):
        output._record_gap_miss(q(), out(abstain, risk))
    assert [m["op"] for m in misses] == ["memory.search", "memory.search"]
    # A filtered or bounded read and a degraded answer are not gaps.
    output._record_gap_miss(q(user_id="u1"), out(True, "abstain"))
    output._record_gap_miss(q(session_id="s1"), out(True, "abstain"))
    output._record_gap_miss(q(tags=["billing"]), out(True, "abstain"))
    output._record_gap_miss(q(agent="planner"), out(True, "abstain"))
    output._record_gap_miss(q(legs=["memory"]), out(True, "abstain"))  # the Memories page
    output._record_gap_miss(q(), out(True, "abstain", pending_extraction=2))
    output._record_gap_miss(q(), out(True, "abstain", namespaces_skipped=["x"]))
    output._record_gap_miss(q(), out(True, "abstain", stale_dropped=1))
    output._record_gap_miss(q(), out(True, "abstain", degraded=["rerank: timeout"]))
    monkeypatch.setattr(gaps_app, "reads_whole_namespace", lambda: False)
    output._record_gap_miss(q(), out(True, "abstain"))
    assert len(misses) == 2


class _Embedder:
    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] if "arr" in t else [0.0, 1.0] for t in texts]


_ROWS = [
    {
        "head": "what is arr",
        "asks": 4,
        "last_at": T0,
        "askers": ["u1", "u2", "u3"],
    },
    {
        "head": "arr definition",
        "asks": 3,
        "last_at": T1,
        "askers": ["u3", "u4", "u5"],
    },
]


@pytest.fixture
def build(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A build over `_ROWS` with no erasure open; what it wrote and deleted."""
    out: dict[str, Any] = {"calls": {}, "written": [], "deleted": [], "erasures": []}

    async def miss_groups(org: str, ns: str, **kw: Any) -> list[dict[str, Any]]:
        out["calls"].update(kw)
        # As groupUniqArray(cap) does: at most max_askers of each group's askers.
        rows = out.get("rows", _ROWS)
        return [{**r, "askers": list(r["askers"])[: kw["max_askers"]]} for r in rows]

    async def insert_build(rows: list[dict[str, Any]]) -> None:
        out["written"].extend(rows)

    async def delete_build(org: str, ns: str, b: int) -> None:
        out["deleted"].append(b)

    async def unfinished_reaching(
        org: str, ns: str, user_id: str | None, since_s: int, margin_s: int, **kw: Any
    ) -> list[dict[str, Any]]:
        assert user_id is None and kw == {"skip_abandoned": True}
        return out["erasures"]

    monkeypatch.setattr(gaps_store, "miss_groups", miss_groups)
    monkeypatch.setattr(gaps_store, "insert_build", insert_build)

    async def delete_misses(org: str, ns: str) -> None:
        out["misses_deleted"] = True

    async def delete_builds(org: str, ns: str) -> None:
        out["builds_deleted"] = True

    monkeypatch.setattr(gaps_store, "delete_builds", delete_builds)
    monkeypatch.setattr(gaps_store, "delete_build", delete_build)
    monkeypatch.setattr(gaps_store, "delete_misses", delete_misses)
    monkeypatch.setattr("server.erasure.app.ledger_reads.unfinished_reaching", unfinished_reaching)

    async def spellings(org: str, subject: str) -> list[str]:
        return [subject]

    monkeypatch.setattr("server.auth.aliases.spellings", spellings)

    async def over_cap(pairs: Any, **kw: Any) -> dict[tuple[str, str], bool]:
        assert kw == {"fail_open": False}
        return {p: out.get("over", False) for p in pairs}

    async def level(org: str, ns: str) -> str:
        if out.get("refused") == "unreadable":
            raise RuntimeError("down")
        return "off" if out.get("refused") else "query"

    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Embedder())
    monkeypatch.setattr("server.observability.budget.namespaces_over_cap", over_cap)
    monkeypatch.setattr("server.observability.capture_consent.level_uncached", level)
    return out


async def test_a_build_merges_groups_and_writes_a_marker(build: dict[str, Any]) -> None:
    assert await worker.build_namespace("o", "ns") == 1
    assert build["calls"]["min_askers"] == settings.knowledge_gaps_min_askers
    gap, marker = build["written"]
    assert marker["gap_id"] == "" and marker["build"] == gap["build"]
    assert gap["head"] == "what is arr"
    assert gap["samples"] == ["arr definition"]
    assert (gap["askers"], gap["asks"]) == (5, 7)
    assert gap["gap_id"] == gap_id("o", "ns", "what is arr")
    assert build["deleted"] == []


async def test_a_build_with_no_group_writes_only_the_marker(build: dict[str, Any]) -> None:
    build["rows"] = []
    assert await worker.build_namespace("o", "ns") == 0
    assert [r["gap_id"] for r in build["written"]] == [""]


async def test_a_user_erasure_open_since_the_read_drops_the_build(build: dict[str, Any]) -> None:
    build["erasures"] = [{"scope_type": "user_id", "scope_value": "u1"}]
    assert await worker.build_namespace("o", "ns") is None
    # The gaps were written and dropped, and no marker made the build readable.
    assert build["deleted"] == [build["written"][0]["build"]]
    assert all(r["gap_id"] for r in build["written"])


async def test_any_users_erasure_drops_the_build(build: dict[str, Any]) -> None:
    # A group holds only some of its askers, so the pass cannot tell who was among the rest.
    build["erasures"] = [{"scope_type": "user_id", "scope_value": "u9"}]
    assert await worker.build_namespace("o", "ns") is None
    assert len(build["deleted"]) == 1


async def test_an_erasure_of_the_namespace_drops_the_build(build: dict[str, Any]) -> None:
    build["erasures"] = [{"scope_type": "namespace", "scope_value": "ns"}]
    assert await worker.build_namespace("o", "ns") is None
    assert len(build["deleted"]) == 1


async def test_an_over_cap_namespace_keeps_its_last_build(build: dict[str, Any]) -> None:
    build["over"] = True
    assert await worker.build_namespace("o", "ns") is None
    assert build["written"] == []


async def test_a_namespace_list_not_naming_it_keeps_the_build(build: dict[str, Any]) -> None:
    build["erasures"] = [{"scope_type": "namespace_list", "scope_value": '["other"]'}]
    assert await worker.build_namespace("o", "ns") == 1
    assert build["deleted"] == []


async def test_a_pass_reads_its_window_bounds_and_cadence_from_settings(
    build: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def namespaces_to_build(days: int, limit: int) -> list[tuple[str, str]]:
        seen.update(days=days, limit=limit)
        return [("o", "ns")]

    monkeypatch.setattr(gaps_store, "namespaces_to_build", namespaces_to_build)
    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: True)
    monkeypatch.setattr(settings, "knowledge_gaps_max_namespaces", 7)
    monkeypatch.setattr(settings, "knowledge_gaps_max_groups", 11)
    monkeypatch.setattr(settings, "knowledge_gaps_max_askers_per_group", 5)
    # The two heads embed alike, so they merge even at the strictest threshold.
    monkeypatch.setattr(settings, "knowledge_gaps_merge_similarity", 1.0)
    result = await worker.run_pass()
    assert seen == {"days": gaps_store.MISS_DAYS, "limit": 7}
    calls = build["calls"]
    assert (calls["days"], calls["limit"], calls["max_askers"]) == (gaps_store.MISS_DAYS, 11, 5)
    assert (result.namespaces, result.gaps) == (1, 1)

    from server.workers.registry import enabled_workers

    monkeypatch.setattr(settings, "knowledge_gaps_enabled", True)
    monkeypatch.setattr(settings, "knowledge_gaps_interval_seconds", 3600)
    spec = next(w for w in enabled_workers() if w.name == "knowledge_gaps")
    assert spec.interval_seconds == 3600


async def test_a_pass_writes_nothing_while_an_older_replica_serves(
    build: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: False)
    result = await worker.run_pass()
    assert (result.namespaces, build["written"]) == (0, [])


def test_a_miss_is_recorded_only_where_the_capture_level_is_not_off(
    scheduled: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    # The answer the trace log reads for its query column, narrowed for a fan-out.
    for level, kept in (("off", 0), ("query", 1), ("all", 1)):
        monkeypatch.setattr(gaps_app, "current_capture_level", lambda lv=level: lv)
        scheduled.clear()
        _record()
        assert len(scheduled) == kept, level


async def test_a_namespace_that_now_refuses_capture_gets_an_empty_build(
    build: dict[str, Any],
) -> None:
    build["refused"] = True
    assert await worker.build_namespace("o", "ns") == 0
    assert [r["gap_id"] for r in build["written"]] == [""]
    assert build["calls"] == {}
    assert build.get("misses_deleted") and build.get("builds_deleted")


async def test_an_unreadable_consent_skips_the_pass_and_keeps_the_misses(
    build: dict[str, Any],
) -> None:
    build["refused"] = "unreadable"
    assert await worker.build_namespace("o", "ns") is None
    assert build["written"] == [] and build["calls"] == {}
    assert not build.get("misses_deleted") and not build.get("builds_deleted")


async def test_a_user_erasure_named_by_email_reaches_misses_under_the_login_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import saga_steps

    deleted: list[list[str]] = []

    async def spellings(org: str, subject: str) -> list[str]:
        return {"Alice@x.io": ["Alice@x.io", "alice"]}.get(subject, [subject])

    async def delete(org: str, spellings: list[str]) -> int:
        deleted.append(spellings)
        return 2

    gaps: list[list[str]] = []

    async def delete_gaps(org: str, spellings: list[str]) -> int:
        gaps.append(spellings)
        return 0

    monkeypatch.setattr("server.auth.aliases.spellings", spellings)
    monkeypatch.setattr(saga_steps.ledger_steps, "delete_search_misses_by_user", delete)
    monkeypatch.setattr(saga_steps.ledger_steps, "delete_knowledge_gaps_by_user", delete_gaps)
    assert await saga_steps._step_delete_search_misses_by_user("o", "Alice@x.io") == 2
    # The builds of the user's namespaces go first, found through every spelling's misses,
    # each compared lower-cased.
    assert gaps == deleted == [["alice", "alice@x.io"]]


async def test_the_residue_census_counts_misses_under_every_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import saga_steps
    from server.erasure.store import census

    seen: dict[str, Any] = {}

    async def residue(org: str, scope_type: str, value: Any, **kw: Any) -> dict[str, int]:
        seen.update(kw)
        return {}

    async def spellings(org: str, subject: str) -> list[str]:
        return [subject, "alice"]

    monkeypatch.setattr("server.auth.aliases.spellings", spellings)
    monkeypatch.setattr(saga_steps.erasure_census, "residue_census", residue)
    await saga_steps._step_verify_residue("o", "user_id", "Alice@x.io", {})
    assert seen["spellings"] == ["alice", "alice@x.io"]

    class _CH:
        def __init__(self) -> None:
            self.params: list[dict[str, Any]] = []

        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, int]:
            if "search_misses" in sql:
                self.params.append(params)
            return {"n": 0}

    async def no_copies(*a: Any, **kw: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(census, "copy_residue", no_copies)
    ch = _CH()
    # The caller folds them (census_spellings): the census counts exactly those.
    await census.table_residue(
        "o", "user_id", "Alice@x.io", ch=ch, spellings=["alice", "alice@x.io"]
    )
    assert ch.params[0]["spellings"] == ["alice", "alice@x.io"]


async def test_a_miss_an_erasure_reaches_is_not_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict[str, Any]] = {}
    reads: list[list[dict[str, Any]]] = []

    async def insert(row: dict[str, Any]) -> None:
        rows[row["miss_id"]] = row

    async def delete(org: str, ns: str, miss_id: str) -> None:
        assert miss_id in rows  # the second read runs once the row is stored
        del rows[miss_id]

    async def same(org: str, subject: str) -> str:
        return subject

    async def spellings(org: str, subject: str) -> list[str]:
        # The directory ties both ways: alice's spellings name her email.
        return {"alice": ["alice", "Alice@x.io"]}.get(subject, [subject])

    async def unfinished(
        org: str, ns: str, uid: str | None, since_s: int, margin: int, **kw: Any
    ) -> Any:
        assert since_s == 1234  # when the search ran, not when the insert runs
        return reads.pop(0)

    monkeypatch.setattr(gaps_store, "insert_miss", insert)
    monkeypatch.setattr(gaps_store, "delete_miss", delete)
    monkeypatch.setattr("server.auth.aliases.login_handle", same)
    monkeypatch.setattr("server.auth.aliases.spellings", spellings)
    monkeypatch.setattr("server.erasure.app.ledger_reads.unfinished_reaching", unfinished)
    row = {"org": "o", "namespace": "ns", "asker": "alice", "user_id": "-"}
    asker = [{"scope_type": "user_id", "scope_value": "Alice@x.io"}]  # a spelling of alice
    other = [{"scope_type": "user_id", "scope_value": "bob"}]
    ns = [{"scope_type": "namespace", "scope_value": "ns"}]
    for first, second, kept in (
        (asker, None, 0),  # open before the insert: nothing is written
        (ns, None, 0),
        (other, other, 1),  # another user's erasure does not reach it
        ([], asker, 0),  # began meanwhile: the stored row is deleted again
        ([], [], 1),
    ):
        reads[:] = [first] + ([second] if second is not None else [])
        rows.clear()
        await gaps_app._insert(row, 1234)
        assert len(rows) == kept, (first, second)

    async def broken(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("down")

    # An unreadable ledger fails closed.
    monkeypatch.setattr("server.erasure.app.ledger_reads.unfinished_reaching", broken)
    rows.clear()
    await gaps_app._insert(row, 1234)
    assert rows == {}


async def test_the_embed_is_a_call_made_for_the_org(monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import contextmanager

    from server.llm import customer

    seen: list[tuple[str, str]] = []

    @contextmanager
    def for_org(org: str, *, namespace: str = ""):
        seen.append((org, namespace))
        yield

    emitted: list[dict[str, Any]] = []
    monkeypatch.setattr(customer, "for_org", for_org)
    monkeypatch.setattr("server.embedding.get_embedder", lambda: _Embedder())
    monkeypatch.setattr(worker, "emit_worker_cost_event_for", lambda **kw: emitted.append(kw))
    assert await worker._embed("o", "ns", ["what is arr"]) == [[1.0, 0.0]]
    assert seen == [("o", "ns")]
    assert emitted[0]["operation"] == "worker.knowledge_gaps"


async def test_an_org_with_model_calls_off_keeps_its_last_build(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.llm import customer

    async def held(org: str, namespace: str) -> int:
        raise customer.ModelCallsDisabled("model calls are off")

    async def namespaces_to_build(days: int, limit: int) -> list[tuple[str, str]]:
        return [("o", "ns")]

    monkeypatch.setattr(gaps_app, "every_replica_erases_gaps", lambda: True)
    monkeypatch.setattr(gaps_store, "namespaces_to_build", namespaces_to_build)
    monkeypatch.setattr(worker, "build_namespace", held)
    result = await worker.run_pass()
    assert (result.namespaces, result.gaps, result.kept) == (0, 0, 1)


async def test_a_restart_runs_no_pass_right_after_a_build(monkeypatch: pytest.MonkeyPatch) -> None:
    async def namespaces_to_build(days: int, limit: int) -> list[tuple[str, str]]:
        return [("o", "ns")]

    ages: list[int | None] = [60, None, settings.knowledge_gaps_interval_seconds]

    async def seconds_since_last_build() -> int | None:
        return ages.pop(0)

    monkeypatch.setattr(gaps_store, "namespaces_to_build", namespaces_to_build)
    monkeypatch.setattr(gaps_store, "seconds_since_last_build", seconds_since_last_build)
    assert [await worker.first_pass_due() for _ in range(3)] == [False, True, True]


def test_namespace_defaults_and_soft_tags_still_record_a_memory_miss(
    misses: list[dict[str, Any]],
) -> None:
    from server.memory.api.models_search import MemorySearchInput, MemorySearchOutput
    from server.memory.app import namespace_retrieval_defaults
    from server.memory.app.search import output
    from server.memory.app.search.request import _apply_role_tags

    out = MemorySearchOutput(
        results=[], embedding_model="m", abstain=True, retrieval_risk="abstain"
    )
    plain = MemorySearchInput(org="o", namespace="ns", query="what is arr")
    defaulted = namespace_retrieval_defaults.apply(plain, {"min_similarity": 0.4})
    output._record_gap_miss(defaulted, out)
    output._record_gap_miss(_apply_role_tags(plain.model_copy(update={"role": "planner"})), out)
    output._record_gap_miss(plain.model_copy(update={"category": "x", "category_soft": True}), out)
    assert len(misses) == 3


def test_every_hard_filter_names_a_field_of_its_input() -> None:
    # A renamed field would otherwise stop counting as a filter without a word.
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput
    from server.knowledge.app import knowledge_search
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search import output

    assert set(output._HARD_FILTERS) <= set(MemorySearchInput.model_fields)
    assert set(knowledge_search._HARD_FILTERS) <= set(KnowledgeSearchInput.model_fields)


def test_the_list_is_an_admin_op() -> None:
    # A gap's queries are other people's questions.
    from server.knowledge.api.ops_gaps import knowledge_gaps_list
    from server.operations.base import Role

    assert knowledge_gaps_list.required_role is Role.ADMIN


def test_a_pass_gets_its_own_deadline() -> None:
    # Up to knowledge_gaps_max_namespaces embeds: more than the default cap of a daily worker.
    from server.workers.registry import pass_deadline_seconds

    assert pass_deadline_seconds("knowledge_gaps") == settings.knowledge_gaps_max_pass_seconds


def test_a_query_over_the_length_bound_records_no_miss(scheduled: list[dict[str, Any]]) -> None:
    # A long paste records nothing.
    long = "what is arr " * 50
    assert len(long) > gaps_app._MAX_QUERY_CHARS
    _record(query=long)
    assert scheduled == []
    _record()
    assert len(scheduled) == 1


async def test_a_cap_below_the_threshold_is_raised_to_it(
    build: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set live, the cap could fall below the threshold: a gap would show too few askers.
    monkeypatch.setattr(settings, "knowledge_gaps_min_askers", 5)
    monkeypatch.setattr(settings, "knowledge_gaps_max_askers_per_group", 3)
    six = [f"u{i}" for i in range(6)]
    build["rows"] = [
        {"head": "what is arr", "asks": 6, "last_at": datetime(2026, 1, 2), "askers": six}
    ]
    assert await worker.build_namespace("o", "ns") == 1
    assert [r["askers"] for r in build["written"] if r["gap_id"]] == [5]


async def test_a_refused_namespace_whose_build_an_erasure_raced_counts_as_built(
    build: dict[str, Any],
) -> None:
    # Its builds are already gone, so it keeps none: 0, not "kept".
    build["refused"] = True
    build["erasures"] = [{"scope_type": "user_id", "scope_value": "u1"}]
    assert await worker.build_namespace("o", "ns") == 0
    # An empty build wrote nothing, so there is nothing to drop and no marker.
    assert build["written"] == [] and build["deleted"] == []


async def test_the_fence_reads_the_askers_spellings_once_however_many_erasures_are_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A bulk deprovisioning leaves many user erasures open: the cost stays one read per asker.
    looked_up: list[str] = []

    async def spellings(org: str, subject: str) -> list[str]:
        looked_up.append(subject)
        return [subject]

    async def unfinished(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"scope_type": "user_id", "scope_value": f"user-{i}"} for i in range(500)]

    monkeypatch.setattr("server.auth.aliases.spellings", spellings)
    monkeypatch.setattr("server.erasure.app.ledger_reads.unfinished_reaching", unfinished)
    assert not await gaps_app.erasure_since("o", "ns", 0, {"alice"})
    assert looked_up == ["alice"]
    # An end user's asker and user_id differ only in case: one read, not two.
    looked_up.clear()
    assert not await gaps_app.erasure_since("o", "ns", 0, {"bob", "Bob"})
    assert looked_up == ["bob"]


async def test_an_unreadable_directory_during_the_fence_keeps_no_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inserted: list[dict[str, Any]] = []

    async def insert(row: dict[str, Any]) -> None:
        inserted.append(row)

    async def same(org: str, subject: str) -> str:
        return subject

    async def broken(org: str, subject: str) -> list[str]:
        raise RuntimeError("directory down")

    async def open_erasure(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"scope_type": "user_id", "scope_value": "bob"}]

    monkeypatch.setattr(gaps_store, "insert_miss", insert)
    monkeypatch.setattr("server.auth.aliases.login_handle", same)
    monkeypatch.setattr("server.auth.aliases.spellings", broken)
    monkeypatch.setattr("server.erasure.app.ledger_reads.unfinished_reaching", open_erasure)
    await gaps_app._insert({"org": "o", "namespace": "ns", "asker": "alice", "user_id": "-"}, 0)
    assert inserted == []


async def test_the_fence_counts_an_unreadable_directory_as_reaching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Fail closed in the fence itself, so the read after the insert deletes the stored row.
    async def broken(org: str, subject: str) -> list[str]:
        raise RuntimeError("directory down")

    async def open_erasure(*a: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"scope_type": "user_id", "scope_value": "bob"}]

    monkeypatch.setattr("server.auth.aliases.spellings", broken)
    monkeypatch.setattr("server.erasure.app.ledger_reads.unfinished_reaching", open_erasure)
    assert await gaps_app.erasure_since("o", "ns", 0, {"alice"}) is True
