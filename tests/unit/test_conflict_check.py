"""The write-time conflict check: deterministic tiers only.

`check_on_commit` answers clear | merge | duplicate | conflict | refines |
rejected from cosine, canonical text and the namespace policy, with no LLM
call. The prefetch folds the namespace's write-time reads into one concurrent
round trip, and the check issues none of them again."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.app import conflicts as cf
from server.memory.domain import conflicts as conflict_rules
from server.memory.domain.conflicts import ConflictPeer
from server.settings import settings

_P1 = "11111111-1111-1111-1111-111111111111"


def _row(mid: str, key: str, value: str, dist: float, pinned: int = 0, run_id: str = "-") -> dict:
    return {
        "memory_id": mid,
        "memory_key": key,
        "value": value,
        "pinned": pinned,
        "agent": "ag",
        "user_id": "u",
        "session_id": "s",
        "run_id": run_id,
        "dist": dist,
        "valid_at": None,
    }


class _RowsCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sql: list[str] = []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append(sql)
        if "loom.conflict_allowlist" in sql:
            return []
        return self.rows

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any] | None:
        self.sql.append(sql)
        return None


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> NS:
    e = NS(rows=[_row(_P1, "old_key", "PR 1 has a bug in the hash join", dist=0.08)])
    e.ch = _RowsCH(e.rows)
    monkeypatch.setattr(cf, "get_client", lambda: e.ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: e.ch)

    async def thr(*a: Any, **k: Any) -> float:
        return 0.85

    async def dthr(*a: Any, **k: Any) -> float:
        return 0.97

    async def allow(*a: Any, **k: Any) -> set[str]:
        return set()

    async def policy(*a: Any, **k: Any) -> str:
        return "auto_reinforce"

    monkeypatch.setattr(cf, "_resolve_threshold", thr)
    monkeypatch.setattr(cf, "_resolve_duplicate_threshold", dthr)
    monkeypatch.setattr(cf, "_allowlisted_keys", allow)
    monkeypatch.setattr(cf, "namespace_conflict_policy", policy)
    return e


async def _check(value: str, **kw: Any) -> Any:
    base: dict[str, Any] = dict(
        org="o",
        namespace="n",
        agent="ag",
        memory_type="semantic",
        memory_key="new_key",
        new_value=value,
        new_embedding=[0.1, 0.2, 0.3],
    )
    base.update(kw)
    return await cf.check_on_commit(**base)


@pytest.mark.asyncio
async def test_a_close_but_different_pair_is_a_conflict(env: NS) -> None:
    res = await _check("PR 1 is about the merge join")
    assert res.verdict == "conflict" and [p.memory_id for p in res.peers] == [_P1]


@pytest.mark.asyncio
async def test_a_one_word_update_is_a_conflict_not_a_duplicate(env: NS) -> None:
    env.rows[:] = [
        _row(_P1, "moshe_kahlon_profession", "Moshe Kahlon works in the field of politician", 0.02)
    ]
    res = await _check("Moshe Kahlon works in the field of soldier", memory_key="kahlon_field")
    assert res.verdict == "conflict"


@pytest.mark.asyncio
async def test_templated_substitution_supersedes_under_newest_wins_only(
    env: NS, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.rows[:] = [
        _row(
            _P1,
            "basketball_creat_in",
            "User read that basketball was created in the country of United States of America.",
            0.03,
        )
    ]
    new = "User read that basketball was created in the country of Soviet Union."

    async def newest(*a: Any, **k: Any) -> str:
        return "newest_wins"

    monkeypatch.setattr(cf, "namespace_conflict_policy", newest)
    res = await _check(new, memory_key="user_basketball_origin")
    assert res.verdict == "refines" and [p.memory_id for p in res.refined_peers] == [_P1]

    async def default(*a: Any, **k: Any) -> str:
        return ""

    monkeypatch.setattr(cf, "namespace_conflict_policy", default)
    assert (await _check(new, memory_key="user_basketball_origin")).verdict == "conflict"


def test_templated_substitution_shape() -> None:
    from server.memory.domain.conflicts import templated_substitution as ts

    assert ts(
        "User read that basketball was created in the country of United States of America.",
        "User read that basketball was created in the country of Soviet Union.",
    )
    assert ts(
        "The chairperson of Fatah is Mahmoud Abbas", "The chairperson of Fatah is Moshe Kahlon"
    )
    assert not ts("User likes the beach.", "This user enjoys spending time by the seashore.")
    assert not ts("PR 1 has a bug", "PR 1 has a bug: the root cause is a missing null check")


def test_duplicate_step_requires_the_same_content_words() -> None:
    from server.memory.domain.conflicts import same_content_words

    assert same_content_words("The user likes hiking", "user likes hiking.")
    assert same_content_words(
        "PR 1 has a bug", "PR 1 has a bug: the root cause is a missing null check"
    )
    assert not same_content_words("Nolan Pierce is 26 years old", "Nolan Pierce is 27 years old")
    assert not same_content_words(
        "Moshe Kahlon works in the field of politician",
        "Moshe Kahlon works in the field of soldier",
    )
    # A new value in new words is an update, not a paraphrase.
    assert not same_content_words("my PB is 27:10", "beat my PB, 26:30")
    assert same_content_words("my PB is 27:10", "my PB is 27:10 in the 5k")
    # A value spelt another way reads as a new one, so the pair goes to the conflict check.
    assert not same_content_words("the plan costs $40", "the plan costs 40 dollars")


def test_later_valid_peers_needs_both_dates() -> None:
    from server.memory.domain.conflicts import later_valid_peers

    def peer(mid: str, valid_at: Any) -> ConflictPeer:
        return ConflictPeer(mid, "k", 0.9, False, "c", valid_at=valid_at)

    d18, d27 = datetime(2022, 12, 18), datetime(2022, 12, 27)
    peers = [peer("later", d27), peer("earlier", d18), peer("undated", None)]
    assert [p.memory_id for p in later_valid_peers(peers, d18)] == ["later"]
    assert later_valid_peers(peers, d27) == []
    assert later_valid_peers(peers, None) == []  # new row undated: arrival order


# --- prefetch: the namespace's write-time reads in one round trip ------------


@pytest.mark.asyncio
async def test_prefetch_reads_type_row_namespace_row_and_allowlist_once(monkeypatch) -> None:
    ch = _RowsCH([])
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _thr(default: float, *a: Any, **k: Any) -> float:
        return default

    monkeypatch.setattr(
        "server.memory.app.embedder_calibration.effective_conflict_threshold_for_ns", _thr
    )
    monkeypatch.setattr(
        "server.memory.app.embedder_calibration.effective_duplicate_threshold_for_ns", _thr
    )
    cfg = await cf.prefetch_write_config("o", "n", "semantic", "k1")
    assert cfg.threshold == settings.conflict_threshold
    assert cfg.duplicate_threshold == settings.duplicate_threshold
    assert cfg.policy == "auto_reinforce" and cfg.allowlisted == set()
    assert cfg.memory_type == "semantic" and cfg.type_config is not None
    tables = [
        t
        for t in ("loom.namespace_types", "loom.namespaces", "loom.conflict_allowlist")
        if any(t in s for s in ch.sql)
    ]
    assert len(ch.sql) == 3 and len(tables) == 3, ch.sql


@pytest.mark.asyncio
async def test_the_check_with_prefetched_config_issues_only_the_peer_statement(monkeypatch) -> None:
    ch = _RowsCH([_row(_P1, "old_key", "PR 1 has a bug in the hash join", dist=0.08)])
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    cfg = conflict_rules.WriteConfig(0.85, 0.97, "auto_reinforce", set(), None, "semantic")
    res = await _check("PR 1 is about the merge join", **cfg.check_kwargs())
    assert res.verdict == "conflict"
    # The peer read is two statements — the vectors, then the candidates' rows.
    assert len(ch.sql) == 2 and "loom.memory_embeddings" in ch.sql[0], ch.sql
    assert "{ids:Array(Tuple" in ch.sql[1], ch.sql


@pytest.mark.asyncio
async def test_a_failed_prefetch_falls_back_to_per_read(monkeypatch) -> None:
    class Boom:
        async def query_one(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

        async def query_all(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    monkeypatch.setattr(cf, "get_client", lambda: Boom())
    monkeypatch.setattr("server.core.ch.get_client", lambda: Boom())
    cfg = await cf.prefetch_write_config("o", "n", "semantic", "k1")
    assert cfg == conflict_rules.WriteConfig()
    assert all(v is None for v in cfg.check_kwargs().values())


# --- an update retires its predecessor even when the changed value is a number ----


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("The office lease ends in 2025", "The office lease ends in 2027"),
        ("The team standup is at 10:00 on Mondays", "The team standup is at 11:30 on Mondays"),
    ],
)
async def test_a_changed_number_or_year_is_an_update_under_newest_wins(
    env: NS, monkeypatch: pytest.MonkeyPatch, old: str, new: str
) -> None:
    env.rows[:] = [_row(_P1, "office_lease_end", old, 0.03)]

    async def newest(*a: Any, **k: Any) -> str:
        return "newest_wins"

    monkeypatch.setattr(cf, "namespace_conflict_policy", newest)
    res = await _check(new, memory_key="lease_end")
    assert res.verdict == "refines" and [p.memory_id for p in res.refined_peers] == [_P1]


@pytest.mark.asyncio
async def test_a_rescheduled_date_retires_the_old_row(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.memory.app.ops as ops
    import server.memory.store as repo

    victims: list[Any] = []

    async def supersede(org: str, v: list[dict[str, Any]], **_kw: Any) -> int:
        victims.extend(v)
        return len(v)

    async def no_row(*a: Any, **k: Any) -> None:
        return None

    async def link(inp: Any) -> Any:
        return NS(linked=True)

    monkeypatch.setattr(repo, "supersede_duplicates", supersede)
    monkeypatch.setattr(repo, "get_by_id", no_row)
    monkeypatch.setattr(ops, "handle_memory_link", link)
    old = ConflictPeer(
        "p1",
        "standup_time",
        0.9,
        False,
        "c",
        agent="ag",
        user_id="u",
        session_id="s",
        value="Standup is on 2026-03-04 at 10:00",
    )
    await cf.apply_refines(
        org="o",
        namespace="ns",
        new_memory_id="n1",
        new_memory_key="standup_day",
        refined_peers=[old],
        ch=object(),
    )
    assert [v["memory_id"] for v in victims] == ["p1"], "the old date does not stay live"
