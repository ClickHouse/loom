"""Proposals are served only when asked, never retire or flag
an established fact, and are decided on graded counts only under an
owner-set policy."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.app import proposals as p
from server.memory.domain import proposal_policy as pp
from server.memory.domain import temporal_sql as ts


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
    seen: dict[str, Any] = {
        "mirror": [],
        "audit": [],
        "subjects": [],
        "flagged": [],
        "announced": [],
    }

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
        seen["subjects"].append(kw["subject"])
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


def test_the_reject_audit_row_holds_the_reasons_hash_not_its_text(repo: dict[str, Any]) -> None:
    """The reason is kept on the rejected row, which erasure reaches; the audit row,
    which it does not, keeps its hash and length."""
    from server.core.hashing import sha256_value

    reason = "Dana Reyes says the vault code 4417 is wrong"
    asyncio.run(p.decide("o", "n", ["p1"], "reject", actor="human:t", reason=reason, ch=_CH()))
    (subject,) = repo["subjects"]
    assert "reason" not in subject and "4417" not in str(subject)
    assert subject["reason_sha256"] == sha256_value(reason)
    assert subject["reason_chars"] == len(reason)


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
