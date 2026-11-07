"""Proposals are served only when asked, never retire or flag
an established fact, and are decided on graded counts only under an
owner-set policy."""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import pytest
from pydantic import ValidationError

from server.identity.domain.read_scope import WHOLE_NAMESPACE, ReadFilters
from server.memory.api.models_proposals import MemoryProposalsInput
from server.memory.app import proposals as p
from server.memory.app import proposals_ops
from server.memory.domain import proposal_policy as pp
from server.memory.domain import temporal_sql as ts
from server.memory.domain.proposal_policy import ProposalPolicy
from server.memory.store import Identity, get_many_by_keys
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl


def test_served_statuses_admit_proposed_only_when_asked() -> None:
    assert "proposed" not in ts.served_statuses()
    assert "proposed" not in ts.served_statuses(include_duplicates=True, include_superseded=True)
    assert ts.served_statuses(include_proposed=True)[-1] == "proposed"
    assert "'proposed'" in ts.served_status_sql(include_proposed=True)
    assert "'proposed'" not in ts.status_predicate()
    assert "'proposed'" in ts.status_predicate(include_proposed=True)


def test_policy_parses_defaults_and_never_turns_auto_on_by_accident() -> None:
    assert pp.parse_policy("") == pp.ProposalPolicy()
    assert pp.parse_policy("{not json").mode == "manual"
    assert (
        pp.parse_policy('{"mode": "auto", "min_passes": 0}').mode == "manual"
    )  # invalid → default
    auto = pp.parse_policy('{"mode": "auto", "min_passes": 3, "reject_after_fails": 2}')
    assert (auto.mode, auto.min_passes, auto.reject_after_fails) == ("auto", 3, 2)
    assert pp.serialize_policy(pp.ProposalPolicy()) == ""
    assert pp.parse_policy(pp.serialize_policy(auto)) == auto


@pytest.mark.parametrize(
    ("passes", "fails", "want"),
    [
        (0, 0, None),
        (1, 0, None),
        (2, 0, "accept"),
        (5, 1, None),
        (2, 2, "reject"),
        (9, 2, "reject"),
    ],
)
def test_policy_verdict_checks_rejection_first(passes: int, fails: int, want: str | None) -> None:
    policy = pp.ProposalPolicy(mode="auto", min_passes=2, max_fails=0, reject_after_fails=2)
    assert p.policy_verdict(policy, p.GradedCounts(passes=passes, fails=fails)) == want


def test_graded_counts_statement_counts_graded_runs_by_relied_on_key() -> None:
    sql = p._GRADED_COUNTS_SQL
    assert "ARRAY JOIN memories_used AS key" in sql
    assert "grade IN ('pass', 'fail')" in sql and "FROM loom.agent_runs FINAL" in sql


class _Row:
    def __init__(self, key: str, status: str, conflict_with: list[str] | None = None) -> None:
        self.memory_key, self.status = key, status
        self.memory_id = f"id-{key}"
        self.conflict_with = conflict_with or []
        self.content_sha256 = "sha"


class _CH:
    def __init__(self) -> None:
        self.inserts: list[list[dict[str, Any]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.memory"
        self.inserts.append(rows)


@pytest.fixture()
def repo(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from server.memory import store as memory_repo
    from server.observability import audit

    rows = {
        "p1": _Row("p1", "proposed"),
        "p2": _Row("p2", "proposed", conflict_with=["id-fact"]),
        "fact": _Row("fact", "active"),
    }
    seen: dict[str, Any] = {"mirror": [], "audit": [], "flagged": [], "announced": []}

    async def _many(
        ident: Any, keys: list[str], *, ch: Any = None, strong: bool = False, scope: Any = None
    ) -> list[Any]:
        assert strong and ident.agent == "*"
        return [rows[k] for k in keys if k in rows]

    async def _mirror(batch: list[dict[str, Any]], *, ch: Any = None) -> int:
        seen["mirror"].extend(batch)
        return len(batch)

    async def _emit(event_type: str, **kw: Any) -> str:
        seen["audit"].append((event_type, kw["actor"], kw["subject"]["memory_key"]))
        return ""

    async def _flag(org: str, ids: list[str], update: Any, *, ch: Any = None) -> int:
        seen["flagged"].append((ids, update(_Row("fact", "active"))))
        return len(ids)

    monkeypatch.setattr(memory_repo, "get_many_by_keys", _many)
    monkeypatch.setattr(memory_repo, "mirror_status_to_embeddings", _mirror)
    monkeypatch.setattr(memory_repo, "write_versions_by_ids", _flag)
    monkeypatch.setattr(
        memory_repo,
        "build_memory_version_row",
        lambda row, **ov: {"memory_key": row.memory_key, **ov},
    )
    # The stub builder returns partial rows; the funnel's full-row check is
    # pinned in test_rmt_write_is_typed.
    monkeypatch.setattr("server.memory.store.inserts.check_memory_rows", lambda rows: None)

    async def _announce(changes: Any) -> None:
        seen["announced"].extend((event, row["memory_key"]) for event, row in changes)

    monkeypatch.setattr(audit, "emit", _emit)
    monkeypatch.setattr(p.webhooks, "announce", _announce)
    return seen


def test_decide_changes_only_proposals_and_mirrors_every_decided_row(repo: dict[str, Any]) -> None:
    ch = _CH()
    out = asyncio.run(p.decide("o", "n", ["p1", "fact", "gone"], "accept", actor="human:t", ch=ch))
    assert [(r.memory_key, r.decided, r.status) for r in out] == [
        ("p1", True, "active"),
        ("fact", False, "active"),
        ("gone", False, ""),
    ]
    assert [r["status"] for r in ch.inserts[0]] == ["active"]
    # an `active` flip is mirrored too (write_versions_by_ids would skip it)
    assert [r["memory_key"] for r in repo["mirror"]] == ["p1"]
    assert repo["audit"] == [("memory.proposal_accept", "human:t", "p1")]


def test_an_accepted_proposal_that_met_a_contradiction_lands_conflicted(
    repo: dict[str, Any],
) -> None:
    ch = _CH()
    asyncio.run(p.decide("o", "n", ["p2"], "accept", actor="human:t", ch=ch))
    assert ch.inserts[0][0]["status"] == "conflicted"
    ids, update = repo["flagged"][0]
    assert (
        ids == ["id-fact"]
        and update["status"] == "conflicted"
        and "id-p2" in update["conflict_with"]
    )


def test_reject_records_the_reason(repo: dict[str, Any]) -> None:
    ch = _CH()
    asyncio.run(
        p.decide(
            "o", "n", ["p1"], "reject", actor="process:proposal_policy", reason="3 fails", ch=ch
        )
    )
    row = ch.inserts[0][0]
    assert (row["status"], row["rejection_reason"]) == ("rejected", "3 fails")


def test_an_accepted_proposal_is_announced_as_written(repo: dict[str, Any]) -> None:
    """The proposal was never served: accepting it is the key's first served write."""
    asyncio.run(p.decide("o", "n", ["p1", "fact"], "accept", actor="human:t", ch=_CH()))
    assert repo["announced"] == [("memory.written", "p1")]


def test_a_rejected_proposal_is_not_announced(repo: dict[str, Any]) -> None:
    """A rejected proposal was never served or announced: nothing changed for a reader."""
    asyncio.run(p.decide("o", "n", ["p1"], "reject", actor="human:t", ch=_CH()))
    assert repo["announced"] == []


def test_apply_policy_does_nothing_under_manual(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _policy(*a: Any, **k: Any) -> pp.ProposalPolicy:
        return pp.ProposalPolicy()

    async def _boom(*a: Any, **k: Any) -> Any:
        raise AssertionError("manual mode must not read or decide")

    monkeypatch.setattr(p, "policy_for", _policy)
    monkeypatch.setattr(p, "proposal_keys_among", _boom)
    assert asyncio.run(p.apply_policy("o", "n", ["p1"], ch=object())) == []


def test_set_input_takes_proposed_and_the_output_names_the_status() -> None:
    from server.memory.api.models_crud import MemorySetInput, MemorySetOutput

    inp = MemorySetInput(
        org="o", namespace="n", agent="a", memory_key="k", value="v", proposed=True
    )
    assert inp.proposed is True
    assert "status" in MemorySetOutput.model_fields


def test_mark_peers_keeps_a_proposal_on_trial(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import conflicts

    writes: list[tuple[list[str], dict[str, Any]]] = []

    async def _write(org: str, ids: list[str], update: Any, *, ch: Any = None, **_: Any) -> int:
        row = _Row("new", "proposed") if ids == ["new"] else _Row("peer", "active")
        writes.append((ids, update(row)))
        return 1

    async def _get(mid: str, **kw: Any) -> Any:
        return _Row(mid, "active")

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", _write)
    monkeypatch.setattr(memory_repo, "get_by_id", _get)
    asyncio.run(conflicts.mark_peers_conflicted(org="o", new_memory_id="new", peer_ids=["peer"]))
    assert writes == [
        (["new"], {"status": "proposed", "conflict_with": ["peer"]})
    ]  # peers untouched


def test_decide_input_bounds() -> None:
    from server.memory.api.models_proposals import MemoryProposalDecideInput

    with pytest.raises(ValidationError):
        MemoryProposalDecideInput(org="o", namespace="n", memory_keys=[], decision="accept")
    with pytest.raises(ValidationError):
        MemoryProposalDecideInput(org="o", namespace="n", memory_keys=["k"], decision="maybe")  # type: ignore[arg-type]


def test_a_proposal_supersedes_nothing_on_any_refines_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory import store as memory_repo
    from server.memory.app import conflicts
    from server.memory.domain import conflicts as conflict_rules

    async def _get(mid: str, **kw: Any) -> Any:
        return _Row("new", "proposed")

    async def _never(*a: Any, **k: Any) -> Any:
        raise AssertionError("a proposal must not supersede its peers")

    monkeypatch.setattr(memory_repo, "get_by_id", _get)
    monkeypatch.setattr(memory_repo, "supersede_duplicates", _never)
    peer = conflict_rules.ConflictPeer(
        memory_id="p",
        memory_key="old",
        agent="a",
        user_id="-",
        session_id="-",
        similarity=0.9,
        pinned=False,
        canonical="old",
    )
    n = asyncio.run(
        conflicts.apply_refines(
            org="o",
            namespace="n",
            new_memory_id="new",
            new_memory_key="k",
            refined_peers=[peer],
            ch=object(),
        )
    )
    assert n == 0


_ROW = (
    "('o', 'n', '{agent}', '{user}', '-', '00000000-0000-0000-0000-00000000000{mid}', 'k', "
    "'semantic', '{value}', '{status}', {version}, '2026-01-0{version} 00:00:00')"
)
_INSERT = (
    "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_id, "
    "memory_key, memory_type, value, status, version, created_at) VALUES "
)


def _listing(monkeypatch: pytest.MonkeyPatch, insert: str) -> Any:
    """`memory.proposals` over the rows `insert` writes, policy and counters stubbed."""
    ch = LocalCH(ddl("memory"), insert)

    async def _policy(*_a: Any, **_k: Any) -> Any:
        return ProposalPolicy()

    async def _none(*_a: Any, **_k: Any) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(proposals_ops, "get_client", lambda: ch)
    monkeypatch.setattr(p, "policy_for", _policy)
    monkeypatch.setattr(p, "graded_counts", _none)
    monkeypatch.setattr(proposals_ops, "_usage_counts", _none)
    return proposals_ops.handle_memory_proposals


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(
    ("b_status", "b_user", "scope_user", "listed"),
    [
        # a proposed `k`, then b: b's row is the head.
        ("proposed", "-", "", [("b", "b value")]),
        # b's row was accepted: a's older proposal is past deciding.
        ("active", "-", "", []),
        # A caller bounded to user u1 sees a's proposal, but the head is u2's row.
        ("active", "u2", "u1", []),
        ("proposed", "u2", "u1", []),
        # The head is the bounded caller's own proposal.
        ("proposed", "u1", "u1", [("b", "b value")]),
    ],
)
async def test_the_listing_offers_only_the_row_a_decision_takes(
    monkeypatch: pytest.MonkeyPatch,
    b_status: str,
    b_user: str,
    scope_user: str,
    listed: list[tuple[str, str]],
) -> None:
    a_user = "u1" if scope_user else "-"
    rows = [
        _ROW.format(agent="a", mid=1, user=a_user, value="a value", status="proposed", version=1),
        _ROW.format(agent="b", mid=2, user=b_user, value="b value", status=b_status, version=2),
    ]
    scope = (
        ReadFilters(scope="own_user", user_id=scope_user, session_id=None)
        if scope_user
        else WHOLE_NAMESPACE
    )
    list_ = _listing(monkeypatch, _INSERT + ", ".join(rows))
    monkeypatch.setattr(proposals_ops, "request_scope", lambda: scope)
    out = await list_(MemoryProposalsInput(org="o", namespace="n"))
    assert [(r.agent, r.value_preview) for r in out.proposals] == listed
    assert out.total == len(listed)
    for r in out.proposals:  # what the dashboard opens and filters by
        assert (r.user_id, r.session_id, r.version) == (b_user, "-", 2)
    # The row the decision reads is the one listed.
    ident = Identity(org="o", namespace="n", agent="*", user_id="-", session_id="-")
    ch = proposals_ops.get_client()
    head = await get_many_by_keys(ident, ["k"], scope=WHOLE_NAMESPACE, ch=ch, strong=True)
    assert [(r.agent, r.status) for r in head] == [("b", b_status)]
    if listed:
        assert [str(r.memory_id) for r in head] == [out.proposals[0].memory_id]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(("only", "graded", "returned"), [("", 1, 1), ("ready", 2, 0)])
async def test_proposals_grade_only_the_rows_they_can_return(
    monkeypatch: pytest.MonkeyPatch, only: Literal["", "ready"], graded: int, returned: int
) -> None:
    """A listing without `only` grades just the rows within `limit` (a badge's `limit=1`
    reads one row's grades); `only="ready"` grades every row to choose."""
    rows = [
        "('o', 'n', 'a', '-', '-', '00000000-0000-0000-0000-00000000000a', 'trial/tea', "
        "'semantic', 'likes tea', 'proposed', 1, '2026-01-01 00:00:00')",
        "('o', 'n', 'a', '-', '-', '00000000-0000-0000-0000-00000000000b', 'trial/door', "
        "'semantic', 'opens inward', 'proposed', 2, '2026-01-02 00:00:00')",
    ]
    list_ = _listing(monkeypatch, _INSERT + ", ".join(rows))
    asked: list[int] = []
    used: list[int] = []

    async def _counts(_ch: Any, _o: str, _n: str, keys: list[str]) -> dict[str, Any]:
        asked.append(len(keys))
        return {}

    async def _usage(_ch: Any, _o: str, _n: str, ids: list[str]) -> dict[str, Any]:
        used.append(len(ids))
        return {}

    monkeypatch.setattr(p, "graded_counts", _counts)
    monkeypatch.setattr(proposals_ops, "_usage_counts", _usage)
    out = await list_(MemoryProposalsInput(org="o", namespace="n", limit=1, only=only))
    assert asked == used == [graded]
    assert out.total == 2
    # The newest row; the default policy decides neither, so `ready` returns none.
    assert [r.memory_key for r in out.proposals] == ["trial/door"][:returned]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_listing_filters_by_prefix_and_tags_and_cuts_by_character(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = _INSERT.replace("created_at)", "created_at, tags)") + (
        "('o', 'n', 'a', '-', '-', '00000000-0000-0000-0000-000000000001', 'lesson/tea', "
        f"'semantic', '{'é' * 401}', 'proposed', 1, '2026-01-01 00:00:00', []), "
        "('o', 'n', 'a', '-', '-', '00000000-0000-0000-0000-000000000002', 'trial/door', "
        "'semantic', 'opens inward', 'proposed', 2, '2026-01-02 00:00:00', ['ops'])"
    )
    list_ = _listing(monkeypatch, insert)
    by_prefix = await list_(MemoryProposalsInput(org="o", namespace="n", prefix="lesson/"))
    assert [r.memory_key for r in by_prefix.proposals] == ["lesson/tea"]
    assert by_prefix.proposals[0].value_preview == "é" * 400
    assert by_prefix.proposals[0].preview_cut
    by_tag = await list_(MemoryProposalsInput(org="o", namespace="n", tags=["ops"]))
    assert [r.memory_key for r in by_tag.proposals] == ["trial/door"]
    assert not by_tag.proposals[0].preview_cut


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_tombstoned_newest_row_leaves_the_proposal_below_it_listed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    insert = _INSERT.replace("created_at)", "created_at, tombstoned_at)") + (
        "('o', 'n', 'a', '-', '-', '00000000-0000-0000-0000-000000000001', 'k', 'semantic', "
        "'a value', 'proposed', 1, '2026-01-01 00:00:00', NULL), "
        "('o', 'n', 'b', '-', '-', '00000000-0000-0000-0000-000000000002', 'k', 'semantic', "
        "'b value', 'active', 2, '2026-01-02 00:00:00', '2026-01-03 00:00:00')"
    )
    out = await _listing(monkeypatch, insert)(MemoryProposalsInput(org="o", namespace="n"))
    assert [r.value_preview for r in out.proposals] == ["a value"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_a_high_proposal_is_listed_without_its_value(monkeypatch: pytest.MonkeyPatch) -> None:
    insert = _INSERT.replace("created_at)", "created_at, sensitivity)") + (
        "('o', 'n', 'a', '-', '-', '00000000-0000-0000-0000-000000000001', 'k', 'semantic', "
        "'the vault code', 'proposed', 1, '2026-01-01 00:00:00', 'high')"
    )
    out = await _listing(monkeypatch, insert)(MemoryProposalsInput(org="o", namespace="n"))
    assert [(r.memory_key, r.sensitivity, r.value_preview) for r in out.proposals] == [
        ("k", "high", "")
    ]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_listing_orders_by_version_not_by_creation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `a` was created first but written last: its newer version lists first.
    insert = _INSERT + (
        "('o', 'n', 'x', '-', '-', '00000000-0000-0000-0000-000000000001', 'a', 'semantic', "
        "'a value', 'proposed', 2, '2026-01-01 00:00:00'), "
        "('o', 'n', 'x', '-', '-', '00000000-0000-0000-0000-000000000002', 'b', 'semantic', "
        "'b value', 'proposed', 1, '2026-01-02 00:00:00')"
    )
    out = await _listing(monkeypatch, insert)(MemoryProposalsInput(org="o", namespace="n"))
    assert [r.memory_key for r in out.proposals] == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize(("session", "listed"), [("s1", []), ("s2", ["b value"])])
async def test_a_session_bounded_caller_sees_a_key_only_when_its_session_holds_the_head(
    monkeypatch: pytest.MonkeyPatch, session: str, listed: list[str]
) -> None:
    insert = _INSERT + (
        "('o', 'n', 'a', 'u1', 's1', '00000000-0000-0000-0000-000000000001', 'k', 'semantic', "
        "'a value', 'proposed', 1, '2026-01-01 00:00:00'), "
        "('o', 'n', 'b', 'u1', 's2', '00000000-0000-0000-0000-000000000002', 'k', 'semantic', "
        "'b value', 'proposed', 2, '2026-01-02 00:00:00')"
    )
    list_ = _listing(monkeypatch, insert)
    scope = ReadFilters(scope="own_session", user_id="u1", session_id=session)
    monkeypatch.setattr(proposals_ops, "request_scope", lambda: scope)
    out = await list_(MemoryProposalsInput(org="o", namespace="n"))
    assert [r.value_preview for r in out.proposals] == listed


@pytest.mark.asyncio
@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
async def test_the_listing_and_the_decision_break_a_version_tie_alike(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One write stamped one version on two identities' rows of `k`.
    insert = _INSERT + ", ".join(
        f"('o', 'n', '{agent}', '-', '-', '00000000-0000-0000-0000-00000000000{mid}', 'k', "
        f"'semantic', '{agent} value', 'proposed', 5, '2026-01-01 00:00:00')"
        for agent, mid in (("b", 2), ("a", 1))
    )
    out = await _listing(monkeypatch, insert)(MemoryProposalsInput(org="o", namespace="n"))
    ident = Identity(org="o", namespace="n", agent="*", user_id="-", session_id="-")
    ch = proposals_ops.get_client()
    head = await get_many_by_keys(ident, ["k"], scope=WHOLE_NAMESPACE, ch=ch, strong=True)
    assert [r.memory_id for r in out.proposals] == [str(r.memory_id) for r in head]
