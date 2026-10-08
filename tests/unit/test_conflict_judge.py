"""A cosine hit is a question; the judge answers it.

Pure parts (label parsing, the fold precedence), the judge's fail-open, the
conflict check's mapping of a judgement onto verdicts with a stubbed
ClickHouse and LLM, the bulk fold of a `refines` verdict, and apply_refines
superseding the peers and writing the edge."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.app import conflict_judge as cj
from server.memory.domain.conflicts import ConflictPeer

# ---------------------------------------------------------------- pure parts


def test_parse_labels_keeps_known_ids_and_labels_only() -> None:
    text = 'sure {"labels": {"a": "Elaboration", "b": "nope", "zz": "unrelated", "c": "contradiction"}}'
    assert cj.parse_labels(text, ["a", "b", "c"]) == {"a": "elaboration", "c": "contradiction"}
    assert cj.parse_labels("prose", ["a"]) == {} and cj.parse_labels('{"labels": []}', ["a"]) == {}


def test_fold_precedence_contradiction_over_elaboration_over_restatement_over_unrelated() -> None:
    ids = ["a", "b", "c"]
    j = cj.fold({"a": "unrelated", "b": "elaboration", "c": "contradiction"}, ids)
    assert j.verdict == "conflict" and j.keep == ["c"]
    j = cj.fold({"a": "unrelated", "b": "elaboration", "c": "restatement"}, ids)
    assert j.verdict == "refines" and j.keep == ["b"]
    j = cj.fold({"a": "unrelated", "b": "restatement", "c": "restatement"}, ids)
    assert j.verdict == "merge" and j.keep == ["b"]
    j = cj.fold({"a": "unrelated", "b": "unrelated", "c": "unrelated"}, ids)
    assert j.verdict == "clear" and j.keep == []
    # An unlabelled peer reads as contradiction — the safe side.
    j = cj.fold({"a": "unrelated"}, ["a", "b"])
    assert j.verdict == "conflict" and j.keep == ["b"] and j.ran


@pytest.mark.asyncio
async def test_judge_fails_open_to_the_cosine_verdict() -> None:
    class Boom:
        async def complete(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("down")

    peers = [NS(memory_id="a", value="x")]
    j = await cj.judge(Boom(), "new", peers)
    assert not j.ran and j.verdict == "conflict"
    assert not (await cj.judge(None, "new", peers)).ran
    assert not (await cj.judge(Boom(), "", peers)).ran


# ------------------------------------------------- check_on_commit mapping


def _peer_row(
    mid: str, key: str, value: str, dist: float = 0.1, pinned: int = 0, valid_at: Any = None
) -> dict[str, Any]:
    return {
        "memory_id": mid,
        "memory_key": key,
        "value": value,
        "pinned": pinned,
        "agent": "ag",
        "user_id": "u",
        "session_id": "s",
        "dist": dist,
        "valid_at": valid_at,
    }


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "loom.conflict_allowlist" in sql:
            return []
        return self.rows


class _LLM:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    async def complete(self, system: str, user: str, **kw: Any) -> Any:
        self.calls += 1
        return NS(text=self.text)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> NS:
    import server.llm.client as llm_client
    import server.memory.app.conflicts as conflicts
    from server.settings import settings

    e = NS(llm=_LLM('{"labels": {"p1": "elaboration"}}'))
    e.rows = [_peer_row("p1", "old_key", "PR 1 has a bug", dist=0.08)]
    monkeypatch.setattr(conflicts, "get_client", lambda: _CH(e.rows))

    async def thr(*a: Any, **k: Any) -> float:
        return 0.85

    async def dthr(*a: Any, **k: Any) -> float:
        return 0.97

    async def allow(*a: Any, **k: Any) -> set[str]:
        return set()

    monkeypatch.setattr(conflicts, "_resolve_threshold", thr)
    monkeypatch.setattr(conflicts, "_resolve_duplicate_threshold", dthr)
    monkeypatch.setattr(conflicts, "_allowlisted_keys", allow)
    monkeypatch.setattr(llm_client, "get_llm_client", lambda: e.llm)
    monkeypatch.setattr(llm_client, "get_llm_client_for_model", lambda m: e.llm)
    monkeypatch.setattr(settings, "memory_conflict_judge", True)
    monkeypatch.setattr(settings, "memory_ask_synth_model_cheap", "")
    return e


async def _check(**kw: Any) -> Any:
    from server.memory.app.conflicts import check_on_commit

    base = dict(
        org="o",
        namespace="ns",
        agent="ag",
        memory_type="semantic",
        memory_key="new_key",
        new_value="PR 1 has a bug: the root cause is a missing null check",
        new_embedding=[0.1, 0.2],
    )
    base.update(kw)
    return await check_on_commit(**base)


@pytest.mark.asyncio
async def test_elaboration_becomes_refines_with_the_peer_identity(env) -> None:
    r = await _check()
    assert r.verdict == "refines" and [p.memory_id for p in r.refined_peers] == ["p1"]
    assert r.refined_peers[0].agent == "ag" and r.refined_peers[0].value == "PR 1 has a bug"
    assert r.judged == {"p1": "elaboration"} and env.llm.calls == 1


@pytest.mark.asyncio
async def test_unrelated_clears_and_restatement_merges_and_contradiction_stays(env) -> None:
    env.llm.text = '{"labels": {"p1": "unrelated"}}'
    r = await _check()
    assert r.verdict == "clear" and r.peers == []
    env.llm.text = '{"labels": {"p1": "restatement"}}'
    r = await _check()
    assert r.verdict == "merge" and r.existing_memory_id == "p1"
    env.llm.text = '{"labels": {"p1": "contradiction"}}'
    r = await _check()
    assert r.verdict == "conflict" and [p.memory_id for p in r.peers] == ["p1"]


@pytest.mark.asyncio
async def test_mixed_peers_keep_only_the_contradicted_one(env) -> None:
    env.rows = [
        _peer_row("p1", "k1", "PR 1 has a bug", 0.05),
        _peer_row("p2", "k2", "PR 2 has a bug", 0.09),
    ]
    env.llm.text = '{"labels": {"p1": "unrelated", "p2": "contradiction"}}'
    r = await _check()
    assert r.verdict == "conflict" and [p.memory_id for p in r.peers] == ["p2"]


@pytest.mark.asyncio
async def test_judge_off_or_unparseable_keeps_the_cosine_conflict(env, monkeypatch) -> None:
    from server.settings import settings

    env.llm.text = "not json"
    r = await _check()
    assert r.verdict == "conflict" and r.judged == {} and env.llm.calls == 1
    monkeypatch.setattr(settings, "memory_conflict_judge", False)
    r = await _check()
    assert r.verdict == "conflict" and env.llm.calls == 1  # no second call


@pytest.mark.asyncio
async def test_pinned_and_duplicate_still_win_before_the_judge(env) -> None:
    env.rows = [_peer_row("p1", "k1", "PR 1 has a bug", 0.08, pinned=1)]
    assert (await _check()).verdict == "rejected" and env.llm.calls == 0
    env.rows = [_peer_row("p1", "k1", "PR 1 has a bug", 0.01)]
    assert (await _check()).verdict == "duplicate" and env.llm.calls == 0


# ------------------------------------------------------ bulk fold + apply


def test_bulk_fold_records_refines_and_keeps_the_row() -> None:
    from server.memory.app.bulk_verdicts import apply_bulk_verdicts

    memory_rows = [{"memory_key": "k0", "memory_id": "id0", "status": "active"}]
    emb = [{"memory_key": "k0", "status": "active"}]
    results = [NS(memory_key="k0", version=7)]
    peer = ConflictPeer("p1", "old", 0.9, False, "c", agent="ag", user_id="u", session_id="s")
    plan = apply_bulk_verdicts(
        memory_rows,
        emb,
        [0],
        results,
        {0: NS(verdict="refines", refined_peers=[peer], peers=[peer])},
    )
    assert plan.memory_rows == memory_rows and plan.refined == ["k0"]
    assert (
        plan.refines == [("id0", "k0", [peer], "refines", plan.refines[0][4])]
        and not plan.conflict_marks
    )
    assert results[0].version == 7


@pytest.mark.asyncio
async def test_apply_refines_supersedes_unpinned_peers_and_links(monkeypatch) -> None:
    import server.memory.app.ops as ops
    import server.memory.store as repo
    from server.memory.app.conflicts import apply_refines

    captured: dict[str, Any] = {}

    async def fake_supersede(
        org: str, victims: list[dict[str, Any]], *, ch: Any = None, skip: Any = None
    ) -> int:
        captured["victims"] = victims
        return len(victims)

    links: list[Any] = []

    async def fake_link(inp: Any) -> Any:
        links.append(inp)
        return NS(linked=True)

    monkeypatch.setattr(repo, "supersede_duplicates", fake_supersede)
    monkeypatch.setattr(ops, "handle_memory_link", fake_link)
    peers = [
        ConflictPeer("p1", "old_a", 0.9, False, "c", agent="ag", user_id="u", session_id="s"),
        ConflictPeer(
            "p2", "old_b", 0.9, True, "c", agent="ag", user_id="u", session_id="s"
        ),  # pinned
    ]
    n = await apply_refines(
        org="o",
        namespace="ns",
        new_memory_id="new1",
        new_memory_key="new_key",
        refined_peers=peers,
        ch=object(),
    )
    assert n == 1
    assert captured["victims"] == [
        {
            "memory_id": "p1",
            "namespace": "ns",
            "agent": "ag",
            "user_id": "u",
            "session_id": "s",
            "memory_key": "old_a",
            "superseded_by": "new1",
        }
    ]
    assert [(lk.from_key, lk.to_key, lk.relation) for lk in links] == [
        ("new_key", "old_a", "refines"),
        ("new_key", "old_b", "refines"),
    ]


# --------------------------------------------------- judge-only candidate band


@pytest.mark.asyncio
async def test_band_peer_below_the_flag_is_judged_but_never_flagged_on_cosine(
    env, monkeypatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_conflict_judge_min_similarity", 0.70)
    # dist 0.22 → similarity 0.78: below the 0.85 flag, inside the 0.70 band.
    env.rows = [_peer_row("p1", "old_key", "The deploy window is Tuesday 14:00 UTC.", dist=0.22)]
    env.llm.text = '{"labels": {"p1": "contradiction"}}'
    r = await _check(new_value="The deploy window moved to Thursday 09:00 UTC.")
    assert r.verdict == "conflict" and [p.memory_id for p in r.peers] == ["p1"]
    env.llm.text = '{"labels": {"p1": "elaboration"}}'
    r = await _check(new_value="The deploy window is Tuesday 14:00 UTC, announced in #deploys.")
    assert r.verdict == "refines" and [p.memory_id for p in r.refined_peers] == ["p1"]
    # Judge fails on a band-only candidate → clear, not conflict.
    env.llm.text = "garbage"
    r = await _check()
    assert r.verdict == "clear" and r.peers == []
    # Judge off → the band is never even queried for a verdict.
    monkeypatch.setattr(settings, "memory_conflict_judge", False)
    r = await _check()
    assert r.verdict == "clear"


@pytest.mark.asyncio
async def test_flag_and_band_peers_fold_together_but_fallback_names_flagged_only(env) -> None:
    env.rows = [
        _peer_row("f1", "k1", "PR 1 has a bug", 0.08),
        _peer_row("b1", "k2", "PR 1 has a bug in the join", 0.22),
    ]
    env.llm.text = '{"labels": {"f1": "unrelated", "b1": "elaboration"}}'
    r = await _check()
    assert r.verdict == "refines" and [p.memory_id for p in r.refined_peers] == ["b1"]
    # Unparseable judge with a flagged peer present → cosine conflict on the flagged peer only.
    env.llm.text = "??"
    r = await _check()
    assert r.verdict == "conflict" and [p.memory_id for p in r.peers] == ["f1"]


# ------------------------------------------ newest_wins: contradiction supersedes


@pytest.mark.asyncio
async def test_newest_wins_folds_a_judged_contradiction_to_supersession(env, monkeypatch) -> None:
    import server.memory.app.conflicts as conflicts

    async def policy(org: str, ns: str, judged: Any) -> str:
        return "newest_wins"

    monkeypatch.setattr(conflicts, "_policy_for", policy)
    env.rows = [_peer_row("p1", "x_assigned_module", "quorvex0's assigned module is Alpha.", 0.10)]
    env.llm.text = '{"labels": {"p1": "contradiction"}}'
    r = await _check(
        memory_key="x_module_assignment", new_value="quorvex0's assigned module is now Beta."
    )
    assert r.verdict == "refines" and r.refine_relation == "supersedes"
    assert [p.memory_id for p in r.refined_peers] == ["p1"]


@pytest.mark.asyncio
async def test_other_policies_keep_the_contradiction_as_a_conflict(env, monkeypatch) -> None:
    import server.memory.app.conflicts as conflicts

    async def policy(org: str, ns: str, judged: Any) -> str:
        return "auto_reinforce"

    monkeypatch.setattr(conflicts, "_policy_for", policy)
    env.rows = [_peer_row("p1", "x_assigned_module", "module is Alpha", 0.10)]
    env.llm.text = '{"labels": {"p1": "contradiction"}}'
    r = await _check()
    assert r.verdict == "conflict" and r.refine_relation == "refines"


@pytest.mark.asyncio
async def test_apply_refines_writes_the_supersedes_relation(monkeypatch) -> None:
    import server.memory.app.ops as ops
    import server.memory.store as repo
    from server.memory.app.conflicts import apply_refines

    async def fake_supersede(
        org: str, victims: list[dict[str, Any]], *, ch: Any = None, skip: Any = None
    ) -> int:
        return len(victims)

    links: list[Any] = []

    async def fake_link(inp: Any) -> Any:
        links.append(inp)
        return NS(linked=True)

    monkeypatch.setattr(repo, "supersede_duplicates", fake_supersede)
    monkeypatch.setattr(ops, "handle_memory_link", fake_link)
    peer = ConflictPeer("p1", "old", 0.9, False, "c", agent="ag", user_id="u", session_id="s")
    await apply_refines(
        org="o",
        namespace="ns",
        new_memory_id="n",
        new_memory_key="new",
        refined_peers=[peer],
        ch=object(),
        relation="supersedes",
    )
    assert [(lk.relation, "contradicts" in lk.explanation) for lk in links] == [
        ("supersedes", True)
    ]


def test_bulk_fold_carries_the_relation() -> None:
    from server.memory.app.bulk_verdicts import apply_bulk_verdicts

    rows = [{"memory_key": "k0", "memory_id": "id0", "status": "active"}]
    peer = ConflictPeer("p1", "old", 0.9, False, "c")
    plan = apply_bulk_verdicts(
        rows,
        [{"memory_key": "k0", "status": "active"}],
        [0],
        [NS(memory_key="k0", version=1)],
        {
            0: NS(
                verdict="refines", refined_peers=[peer], peers=[peer], refine_relation="supersedes"
            )
        },
    )
    assert plan.refines == [("id0", "k0", [peer], "supersedes", plan.refines[0][4])]


# --------------------------------------------------------- related, and the clock


def test_related_is_a_label_that_folds_to_clear_below_every_other() -> None:
    """Most rows a newest_wins namespace would supersede on the
    knowledge-update slice are the next step of the same story, not an
    elaboration. `related` keeps both rows."""
    assert cj.parse_labels('{"labels": {"a": "related"}}', ["a"]) == {"a": "related"}
    j = cj.fold({"a": "related", "b": "unrelated"}, ["a", "b"])
    assert j.verdict == "clear" and j.keep == [] and j.ran
    # An elaboration beside a related peer still refines — only that peer.
    j = cj.fold({"a": "related", "b": "elaboration"}, ["a", "b"])
    assert j.verdict == "refines" and j.keep == ["b"]
    # A restatement outranks related: merge to it.
    j = cj.fold({"a": "related", "b": "restatement"}, ["a", "b"])
    assert j.verdict == "merge" and j.keep == ["b"]


def test_prompt_asks_for_containment_and_offers_related() -> None:
    assert "EVERYTHING the existing one says" in cj._SYSTEM
    assert "- related:" in cj._SYSTEM and '"related"' in cj._SYSTEM
    assert "the existing memory remains true" not in cj._SYSTEM


def test_later_valid_peers_needs_both_dates() -> None:
    from datetime import datetime

    from server.memory.domain.conflicts import later_valid_peers

    def peer(mid: str, valid_at: Any) -> ConflictPeer:
        return ConflictPeer(mid, "k", 0.9, False, "c", valid_at=valid_at)

    d18, d27 = datetime(2022, 12, 18), datetime(2022, 12, 27)
    peers = [peer("later", d27), peer("earlier", d18), peer("undated", None)]
    assert [p.memory_id for p in later_valid_peers(peers, d18)] == ["later"]
    assert later_valid_peers(peers, d27) == []
    assert later_valid_peers(peers, None) == []  # new row undated: arrival order


@pytest.mark.asyncio
async def test_newest_wins_keeps_a_later_valid_peer_as_a_conflict(env, monkeypatch) -> None:
    """The row being written held on 12-18, the contradicted peer on 12-27.
    Arrival order would retire the later fact; the calendar says the new row
    is the older statement."""
    from datetime import datetime

    import server.memory.app.conflicts as conflicts

    async def policy(org: str, ns: str, judged: Any) -> str:
        return "newest_wins"

    monkeypatch.setattr(conflicts, "_policy_for", policy)
    env.rows = [
        _peer_row(
            "p1",
            "user_guitar",
            "exploring new guitar techniques",
            0.10,
            valid_at=datetime(2022, 12, 27),
        ),
    ]
    env.llm.text = '{"labels": {"p1": "contradiction"}}'
    r = await _check(new_value="inspired to write music again", new_valid_at=datetime(2022, 12, 18))
    assert r.verdict == "conflict" and [p.memory_id for p in r.peers] == ["p1"]
    assert r.refined_peers == []
    # Same pair with the dates the other way round, or with either date
    # missing, folds to supersession as before.
    r = await _check(new_value="inspired to write music again", new_valid_at=datetime(2023, 1, 5))
    assert r.verdict == "refines" and r.refine_relation == "supersedes"
    r = await _check(new_value="inspired to write music again")
    assert r.verdict == "refines" and r.refine_relation == "supersedes"
    env.rows = [_peer_row("p1", "user_guitar", "exploring new guitar techniques", 0.10)]
    r = await _check(new_value="inspired to write music again", new_valid_at=datetime(2022, 12, 18))
    assert r.verdict == "refines" and r.refine_relation == "supersedes"


@pytest.mark.asyncio
async def test_a_related_peer_clears_without_a_retire(env) -> None:
    env.rows = [
        _peer_row(
            "p1", "user_trip_sf", "planning a trip to San Francisco, considering United", 0.10
        )
    ]
    env.llm.text = '{"labels": {"p1": "related"}}'
    r = await _check(new_value="planning to visit Alcatraz Island during the San Francisco trip")
    assert r.verdict == "clear" and r.refined_peers == [] and r.peers == []
    assert r.judged == {"p1": "related"}


@pytest.mark.asyncio
async def test_an_elaboration_dated_before_its_peer_keeps_the_peer(env) -> None:
    """A row saying "considering attending a book reading this weekend" (held
    05-20) arrives after "attended the reading, got a signed copy" (held
    05-21) and is judged an elaboration: the later-held fact stays."""
    from datetime import datetime

    env.rows = [
        _peer_row(
            "p1",
            "user_book_reading_event",
            "attended the reading",
            0.10,
            valid_at=datetime(2023, 5, 21),
        ),
    ]
    env.llm.text = '{"labels": {"p1": "elaboration"}}'
    r = await _check(
        new_value="considering attending a reading", new_valid_at=datetime(2023, 5, 20)
    )
    assert r.verdict == "clear" and r.refined_peers == [] and r.judged == {"p1": "elaboration"}
    # Dated after the peer, the elaboration refines as before.
    r = await _check(
        new_value="considering attending a reading", new_valid_at=datetime(2023, 5, 22)
    )
    assert r.verdict == "refines" and [p.memory_id for p in r.refined_peers] == ["p1"]


def test_duplicate_step_requires_the_same_content_words() -> None:
    """A one-word update above the duplicate gate is not a restatement
    ('works in the field of soldier' must not be filed as a duplicate of
    'works in the field of politician', losing the update)."""
    from server.memory.domain.conflicts import same_content_words

    assert same_content_words(
        "Moshe Kahlon works in the field of politician.",
        "Moshe Kahlon works in the field of politician",
    )
    assert same_content_words("The user likes hiking", "user likes hiking.")
    assert same_content_words(
        "PR 1 has a bug", "PR 1 has a bug: the root cause is a missing null check"
    )
    # a paraphrase shares few words: the cosine gate decides, still a duplicate
    assert same_content_words(
        "User likes the beach.", "This user enjoys spending time by the seashore."
    )
    assert not same_content_words("Nolan Pierce is 26 years old", "Nolan Pierce is 27 years old")
    assert not same_content_words(
        "Moshe Kahlon works in the field of politician",
        "Moshe Kahlon works in the field of soldier",
    )
    assert not same_content_words(
        "Israel is located in the continent of Asia",
        "Israel is located in the continent of North America",
    )


async def test_a_one_word_update_goes_to_the_judge_not_the_duplicate_bin(env, monkeypatch) -> None:
    import server.memory.app.conflicts as conflicts

    env.rows = [
        _peer_row(
            "p1",
            "moshe_kahlon_profession",
            "Moshe Kahlon works in the field of politician",
            dist=0.02,
        )
    ]
    env.llm = _LLM('{"labels": {"p1": "contradiction"}}')
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: env.llm)
    res = await conflicts.check_on_commit(
        org="o",
        namespace="ns",
        agent="ag",
        memory_type="semantic",
        memory_key="moshe_kahlon_field",
        new_value="Moshe Kahlon works in the field of soldier",
        new_embedding=[0.1] * 4,
    )
    assert res.verdict != "duplicate"
    assert env.llm.calls == 1  # the judge saw the pair


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


async def test_templated_substitution_supersedes_without_a_judge_under_newest_wins(
    env, monkeypatch
) -> None:
    import server.memory.app.conflicts as conflicts

    async def newest(*a, **k):
        return "newest_wins"

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", newest)
    env.rows = [
        _peer_row(
            "p1",
            "basketball_creat_in",
            "User read that basketball was created in the country of United States of America.",
            dist=0.03,
        )
    ]
    res = await conflicts.check_on_commit(
        org="o",
        namespace="ns",
        agent="ag",
        memory_type="semantic",
        memory_key="user_basketball_origin",
        new_value="User read that basketball was created in the country of Soviet Union.",
        new_embedding=[0.1] * 4,
    )
    assert res.verdict == "refines" and res.refine_relation == "supersedes"
    assert [p.memory_id for p in res.refined_peers] == ["p1"] and env.llm.calls == 0

    async def default(*a, **k):
        return ""

    monkeypatch.setattr(conflicts, "namespace_conflict_policy", default)
    env.llm = _LLM('{"labels": {"p1": "contradiction"}}')
    monkeypatch.setattr("server.llm.client.get_llm_client", lambda: env.llm)
    res2 = await conflicts.check_on_commit(
        org="o",
        namespace="ns",
        agent="ag",
        memory_type="semantic",
        memory_key="user_basketball_origin",
        new_value="User read that basketball was created in the country of Soviet Union.",
        new_embedding=[0.1] * 4,
    )
    assert (
        res2.verdict != "duplicate" and env.llm.calls == 1
    )  # without the policy the judge decides
