"""The conflict resolver: its fold, its prompt and parse, the cluster read, what one
row's judgement does, and the pass around it. ClickHouse and the LLM are faked;
tests/integration/test_conflict_resolver_ch.py runs it against ClickHouse."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.app import conflict_resolver as resolver
from server.memory.domain import conflict_resolution as cr
from server.memory.store import conflict_resolver as queue
from server.settings import Settings, settings

# --- the fold ------------------------------------------------------------------------


def _v(verdict: str, confidence: float, *, entity: bool = True, attribute: bool = True) -> Any:
    return cr.PeerVerdict("p", verdict, confidence, entity, attribute)


def _action(v: Any, policy: str = "newest_wins") -> str:
    return cr.action_for(v, policy=policy, retire_at=0.95, clear_at=0.90)


def test_an_update_retires_only_at_the_retire_confidence_under_newest_wins() -> None:
    assert _action(_v("update", 0.97)) == cr.RETIRE
    assert _action(_v("update", 0.95)) == cr.RETIRE
    # Between the two thresholds an update goes to review, never to a retirement.
    assert _action(_v("update", 0.92)) == cr.FLAG
    # Under any other policy an update is a flag at any confidence.
    assert _action(_v("update", 0.99), "auto_reinforce") == cr.FLAG


def test_a_contradiction_flags_and_compatible_verdicts_clear() -> None:
    assert _action(_v("contradiction", 0.99)) == cr.FLAG
    for verdict in ("elaboration", "restatement", "related", "unrelated"):
        assert _action(_v(verdict, 0.93)) == cr.CLEAR, verdict


def test_below_the_clear_confidence_nothing_changes() -> None:
    for verdict in cr.VERDICTS:
        assert _action(_v(verdict, 0.89)) == cr.NOTHING, verdict


def test_an_update_about_another_entity_or_attribute_reads_as_related() -> None:
    assert _action(_v("update", 0.99, entity=False)) == cr.CLEAR
    assert _action(_v("contradiction", 0.99, attribute=False)) == cr.CLEAR
    assert _action(_v("update", 0.5, entity=False)) == cr.NOTHING


# --- the prompt and the parse ----------------------------------------------------------

_PEERS = [
    cr.Note("11111111-aaaa-4aaa-8aaa-111111111111", "office_city", "Office is in Paris", 0.83),
    cr.Note("22222222-bbbb-4bbb-8bbb-222222222222", "team_lead", "Lead is Ana", 0.71),
]


def test_the_prompt_names_each_peer_by_a_short_id_with_its_key_and_cosine() -> None:
    text = cr.user_prompt(cr.Note("n", "office_city_now", "Office moved to Rome"), _PEERS)
    assert text.startswith(
        "NEW note (key office_city_now):\nOffice moved to Rome\n\nEXISTING notes:"
    )
    assert "[p1] (key office_city, cosine 0.83)\nOffice is in Paris" in text
    assert "[p2] (key team_lead, cosine 0.71)\nLead is Ana" in text
    assert _PEERS[0].memory_id not in text
    long = cr.Note("n", "k", "x" * 2000)
    assert "x" * 701 not in cr.user_prompt(long, [])
    assert cr.max_output_tokens(8) == 120 + 90 * 8


def _answer(*entries: dict[str, Any]) -> str:
    return json.dumps({"peers": list(entries)})


def _entry(peer_id: str, verdict: str = "update", confidence: Any = 0.97) -> dict[str, Any]:
    return {
        "peer_id": peer_id,
        "reason": "r",
        "same_entity": True,
        "same_attribute": True,
        "verdict": verdict,
        "confidence": confidence,
    }


@pytest.mark.parametrize(
    "spelled",
    ["p1", "[p1]", "P1", "office_city", "key office_city", _PEERS[0].memory_id],
)
def test_a_peer_id_is_accepted_in_every_spelling_a_model_uses(spelled: str) -> None:
    got = cr.parse_verdicts(_answer(_entry(spelled)), _PEERS)
    assert list(got) == [_PEERS[0].memory_id] and got[_PEERS[0].memory_id].verdict == "update"


@pytest.mark.parametrize(
    "text",
    [
        "",
        "no json here",
        '{"peers": "nope"}',
        _answer(_entry("p9")),  # names no peer
        _answer(_entry("p1", verdict="supersedes")),  # not a verdict
        _answer(_entry("p1", confidence="high")),
        _answer(_entry("p1", confidence=1.4)),
        _answer(_entry("p1", confidence=True)),
    ],
)
def test_malformed_output_yields_no_verdict(text: str) -> None:
    assert cr.parse_verdicts(text, _PEERS) == {}


def test_a_peer_the_answer_leaves_out_has_no_verdict_and_prose_is_ignored() -> None:
    text = "Here you go:\n" + _answer(_entry("p2", "related", 0.93)) + "\nthanks"
    got = cr.parse_verdicts(text, _PEERS)
    assert list(got) == [_PEERS[1].memory_id]
    # A peer named twice keeps its first entry.
    twice = cr.parse_verdicts(_answer(_entry("p1", "related"), _entry("p1", "update")), _PEERS)
    assert twice[_PEERS[0].memory_id].verdict == "related"


# --- the cluster read --------------------------------------------------------------------


class _ClusterCH:
    def __init__(self, near: list[dict[str, Any]], rows: list[dict[str, Any]]) -> None:
        self.near, self.rows, self.calls = near, rows, []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        return self.near if "cosineDistance" in sql else self.rows


def _q(**kw: Any) -> dict[str, Any]:
    base = {
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "user_id": "u",
        "session_id": "s1",
        "memory_key": "office_city",
        "memory_type": "semantic",
        "embedding": [0.1, 0.2],
        "version": 5,
        "content_sha256": "sha-new",
        "status": "active",
    }
    return base | kw


def _near(session: str, key: str, dist: float) -> dict[str, Any]:
    return {"agent": "a", "user_id": "u", "session_id": session, "memory_key": key, "dist": dist}


def _peer_row(mid: str, session: str, key: str, value: str) -> dict[str, Any]:
    return {
        "memory_id": mid,
        "agent": "a",
        "user_id": "u",
        "session_id": session,
        "memory_key": key,
        "value": value,
        "pinned": 0,
        "content_sha256": f"sha-{mid}",
        "status": "active",
        "conflict_with": [],
    }


async def test_two_sessions_that_share_a_key_each_get_their_own_row_and_vector() -> None:
    # s2 and s3 both hold `office_city`: the rows join to their vectors by the full
    # identity, so each peer carries its own value and its own cosine.
    ch = _ClusterCH(
        near=[_near("s3", "office_city", 0.10), _near("s2", "office_city", 0.30)],
        rows=[
            _peer_row("m2", "s2", "office_city", "Paris"),
            _peer_row("m3", "s3", "office_city", "Lyon"),
        ],
    )
    peers = await queue.cluster_peers(_q(), floor=0.6, limit=8, ch=ch)
    assert [(p["memory_id"], p["value"], round(p["similarity"], 2)) for p in peers] == [
        ("m3", "Lyon", 0.9),
        ("m2", "Paris", 0.7),
    ]
    _, params = ch.calls[1]
    assert params["ids"] == [("a", "u", "s3", "office_city"), ("a", "u", "s2", "office_city")]


async def test_the_cluster_keeps_the_floor_the_cap_and_the_rows_flags() -> None:
    near = [_near(f"s{i}", f"k{i}", 0.05 * i) for i in range(1, 12)]  # cosine 0.95 .. 0.45
    rows = [_peer_row(f"m{i}", f"s{i}", f"k{i}", "v") for i in range(1, 12)]
    ch = _ClusterCH(near, rows)
    peers = await queue.cluster_peers(_q(), floor=0.6, limit=3, prefer=frozenset({"m8"}), ch=ch)
    # m8 (cosine 0.60) is the row's flag: it takes a place first; below 0.60 never.
    assert [p["memory_id"] for p in peers] == ["m1", "m2", "m8"]
    sql, params = ch.calls[0]
    assert "NOT (agent = {a:String} AND session_id = {s:String} AND memory_key = {k:String})" in sql
    assert "user_id = {u:String} AND (user_id != '-' OR agent = {a:String})" in sql
    assert params["org"] == "o" and params["ns"] == "ns" and params["lim"] == 12
    rows_sql, _ = ch.calls[1]
    assert "sensitivity != 'high'" in rows_sql and "memory_type = {mt:String}" in rows_sql


# --- one row --------------------------------------------------------------------------------


def _mem(mid: str = "m1", **kw: Any) -> Any:
    base = dict(
        memory_id=mid,
        memory_key="office_city",
        namespace="ns",
        agent="a",
        user_id="u",
        session_id="s1",
        status="active",
        pinned=False,
        content_sha256="sha-new",
        conflict_with=[],
        value_text='"Office moved to Rome"',
        value="Office moved to Rome",
        run_id="-",
        valid_at=None,
        created_at=datetime(2026, 1, 2),
        updated_at=datetime(2026, 1, 2),
    )
    return NS(**(base | kw))


@pytest.fixture
def row_env(monkeypatch: pytest.MonkeyPatch) -> NS:
    e = NS(
        mem=_mem(),
        heads={},
        peers=[_peer_row("m2", "s2", "office_city", '"Office is in Paris"') | {"similarity": 0.8}],
        answer=_answer(_entry("p1", "update", 0.97)),
        stamped=[],
        acted=[],
        audits=[],
        llm_calls=0,
    )
    # The peer was stated a day before the judged row.
    day_before = datetime(2026, 1, 1)
    e.heads = {
        "m1": e.mem,
        "m2": _mem(
            "m2",
            session_id="s2",
            content_sha256="sha-m2",
            created_at=day_before,
            updated_at=day_before,
        ),
    }

    async def current(q: Any) -> Any:
        return e.mem

    async def cluster(q: Any, **kw: Any) -> list[dict[str, Any]]:
        e.cluster_kw = kw
        return e.peers

    async def judge(new: Any, notes: list[Any]) -> dict[str, Any]:
        e.llm_calls += 1
        e.notes = notes
        return cr.parse_verdicts(e.answer, notes), "gpt-6.1-sol-2026-09-01"

    async def stamp(q: Any, **kw: Any) -> int:
        e.stamped.append(q["memory_key"])
        return 1

    async def head(org: str, mid: str) -> Any:
        return e.heads.get(mid)

    async def act(action: str, org: str, namespace: str, row: Any, peer: Any) -> bool:
        e.acted.append((action, row.memory_id, peer.memory_id))
        return True

    async def audit(org: str, namespace: str, row: Any, acted: list[Any], model: str) -> None:
        e.audits.append(acted)
        e.audit_model = model

    monkeypatch.setattr(resolver, "_current", current)
    monkeypatch.setattr(queue, "cluster_peers", cluster)
    monkeypatch.setattr(resolver, "judge", judge)
    monkeypatch.setattr(queue, "stamp_judged", stamp)
    monkeypatch.setattr(resolver, "_head", head)
    monkeypatch.setattr(resolver, "_act", act)
    monkeypatch.setattr(resolver, "_audit", audit)
    return e


async def _resolve(policy: str = "newest_wins") -> resolver.Counts:
    counts = resolver.Counts()
    await resolver.resolve_row(_q(), policy=policy, counts=counts)
    return counts


async def test_a_confident_update_under_newest_wins_retires_and_stamps(row_env: NS) -> None:
    counts = await _resolve()
    assert row_env.acted == [(cr.RETIRE, "m1", "m2")] and counts.retired == 1
    assert counts.judged == 1 and row_env.stamped == ["office_city"]
    assert row_env.audits[0][0]["action"] == cr.RETIRE
    assert row_env.notes[0].value == "Office is in Paris", "the prompt shows text, not JSON"
    assert row_env.audit_model == "gpt-6.1-sol-2026-09-01"


@pytest.mark.parametrize("peer_at", [datetime(2026, 1, 2), datetime(2026, 1, 3)])
async def test_an_update_whose_peer_was_not_stated_first_is_flagged(
    row_env: NS, peer_at: datetime
) -> None:
    # The same second as the judged row, or later: the order is not proven, so no retire.
    row_env.heads["m2"].created_at = row_env.heads["m2"].updated_at = peer_at
    counts = await _resolve()
    assert row_env.acted == [(cr.FLAG, "m1", "m2")]
    assert counts.flagged == 1 and counts.retired == 0
    assert row_env.audits[0][0]["action"] == cr.FLAG


@pytest.mark.parametrize(
    ("answer", "policy", "want"),
    [
        (_entry("p1", "update", 0.93), "newest_wins", cr.FLAG),
        (_entry("p1", "update", 0.99), "auto_reinforce", cr.FLAG),
        (_entry("p1", "contradiction", 0.91), "newest_wins", cr.FLAG),
        (_entry("p1", "elaboration", 0.95), "newest_wins", cr.CLEAR),
    ],
)
async def test_each_verdict_branch_acts_on_the_pair(
    row_env: NS, answer: dict[str, Any], policy: str, want: str
) -> None:
    row_env.answer = _answer(answer)
    if want == cr.CLEAR:  # a clear needs a flag between the pair
        row_env.mem.conflict_with = ["m2"]
    await _resolve(policy)
    assert row_env.acted == [(want, "m1", "m2")]


@pytest.mark.parametrize(
    "answer",
    [_answer(_entry("p1", "update", 0.85)), _answer(), "not json", ""],
)
async def test_unsure_missing_or_malformed_output_changes_nothing_but_stamps(
    row_env: NS, answer: str
) -> None:
    row_env.answer = answer
    counts = await _resolve()
    assert row_env.acted == [] and row_env.audits == []
    assert row_env.stamped == ["office_city"], "judged once: the row leaves the queue"
    assert counts.skipped_low_confidence + counts.errored == 1


async def test_a_failed_call_leaves_the_row_owed(row_env: NS, monkeypatch) -> None:
    async def boom(new: Any, notes: list[Any]) -> Any:
        raise RuntimeError("provider down")

    monkeypatch.setattr(resolver, "judge", boom)
    with pytest.raises(RuntimeError):
        await _resolve()
    assert row_env.stamped == [] and row_env.acted == []


async def test_a_pinned_row_or_peer_is_never_touched(row_env: NS) -> None:
    row_env.heads["m2"].pinned = True
    counts = await _resolve()
    assert row_env.acted == [] and counts.skipped_pinned == 1
    row_env.mem.pinned = True
    row_env.llm_calls, counts = 0, await _resolve()
    assert row_env.llm_calls == 0 and counts.skipped_pinned == 1


async def test_a_row_changed_since_it_was_queued_or_judged_is_skipped(row_env: NS) -> None:
    # Revised after the queue read: unstamped, its new write carries its own mark.
    row_env.mem.content_sha256 = "sha-revised"
    counts = await _resolve()
    assert counts.skipped_changed == 1 and row_env.stamped == [] and row_env.llm_calls == 0
    # The peer revised while the call ran: the pair is left alone.
    row_env.mem.content_sha256 = "sha-new"
    row_env.heads["m2"].content_sha256 = "sha-m2-revised"
    counts = await _resolve()
    assert counts.skipped_changed == 1 and row_env.acted == []


async def test_a_row_gone_or_under_append_only_is_stamped_without_a_call(row_env: NS) -> None:
    row_env.mem.status = "superseded"
    await _resolve()
    row_env.mem.status = "active"
    await _resolve(policy="append_only")
    assert row_env.llm_calls == 0 and row_env.stamped == ["office_city", "office_city"]


async def test_the_recheck_clears_a_false_flag_and_judges_the_flagged_peer(row_env: NS) -> None:
    row_env.mem.status, row_env.mem.conflict_with = "conflicted", ["m2"]
    row_env.answer = _answer(_entry("p1", "unrelated", 0.98))
    counts = await _resolve()
    assert row_env.cluster_kw["prefer"] == frozenset({"m2"})
    assert row_env.acted == [(cr.CLEAR, "m1", "m2")] and counts.cleared == 1


async def test_a_clear_with_no_flag_between_the_pair_does_nothing(row_env: NS) -> None:
    row_env.answer = _answer(_entry("p1", "related", 0.99))
    counts = await _resolve()
    assert row_env.acted == [] and counts.cleared == 0 and row_env.stamped == ["office_city"]


# --- acting twice is a no-op ------------------------------------------------------------------


async def test_a_flag_already_raised_is_not_written_again(monkeypatch) -> None:
    from server.memory.app import conflicts

    called: list[Any] = []

    async def mark(**kw: Any) -> str:
        called.append(kw)
        return "conflict"

    monkeypatch.setattr(conflicts, "mark_peers_conflicted", mark)
    a = _mem("m1", status="conflicted", conflict_with=["m2"])
    b = _mem("m2", status="conflicted", conflict_with=["m1"])
    assert await resolver._flag("o", a, b) is False and called == []
    assert await resolver._flag("o", _mem("m1"), _mem("m2")) is True
    assert called[0]["expected_sha256"] == "sha-new", "only the judged write is flagged"


async def test_unlinking_a_pair_leaves_other_flags_and_reactivates_a_row_left_with_none(
    monkeypatch,
) -> None:
    import server.memory.store as memory_repo

    stored = {
        "m1": _mem("m1", status="conflicted", conflict_with=["m2", "m9"], version=4),
        "m2": _mem("m2", status="conflicted", conflict_with=["m1"], version=7),
    }
    written: list[dict[str, Any]] = []
    mirrored: list[Any] = []

    async def write(org, ids, update, *, skip=None, on_written=None, **kw):
        rows = []
        for mid in ids:
            r = stored[mid]
            if skip is not None and skip(r):
                continue
            rows.append({"memory_id": mid, "status": r.status, **update(r)})
        written.extend(rows)
        on_written(rows)
        return len(rows)

    async def mirror(rows, **kw):
        mirrored.extend(r["memory_id"] for r in rows)

    monkeypatch.setattr(memory_repo, "write_versions_by_ids", write)
    monkeypatch.setattr(memory_repo, "mirror_status_to_embeddings", mirror)
    assert await resolver._clear("o", stored["m1"], stored["m2"]) is True
    by_id = {w["memory_id"]: w for w in written}
    assert by_id["m1"]["conflict_with"] == ["m9"] and by_id["m1"]["status"] == "conflicted"
    assert by_id["m2"]["conflict_with"] == [] and by_id["m2"]["status"] == "active"
    assert by_id["m1"]["version"] == 5, "a revision after the read stays newest"
    assert mirrored == ["m2"], "the search row learns the row is active again"
    # Again: nothing is flagged between them any more, so nothing is written.
    stored["m1"].conflict_with, stored["m2"].conflict_with = ["m9"], []
    written.clear()
    assert await resolver._clear("o", stored["m1"], stored["m2"]) is False and written == []


async def test_a_retirement_behind_the_newer_row_of_the_pair(monkeypatch) -> None:
    from server.memory.app import auto_resolution, conflicts

    seen: dict[str, Any] = {}

    async def refines(**kw: Any) -> int:
        seen.update(kw)
        loser = kw["refined_peers"][0]
        kw["record_into"].setdefault(kw["reason"], []).append(
            {"memory_id": loser.memory_id, "superseded_by": kw["new_memory_id"]}
        )
        return 1

    records: list[Any] = []

    async def record(**kw: Any) -> None:
        records.append(kw)

    followed: list[Any] = []

    async def follow(org, namespace, agent, pairs):
        followed.extend(pairs)

    monkeypatch.setattr(conflicts, "apply_refines", refines)
    monkeypatch.setattr(auto_resolution, "record_auto_supersessions", record)
    monkeypatch.setattr(resolver, "_follow", follow)
    # The peer, an older statement, retires behind the judged row.
    late = _mem("m1", valid_at=datetime(2024, 1, 1), content_sha256="sha-late")
    current = _mem("m2", session_id="s2", content_sha256="sha-cur")
    assert await resolver._retire("o", "ns", current, late) is True
    assert seen["new_memory_id"] == "m2" and seen["expected_sha256"] == "sha-cur"
    assert [p.memory_id for p in seen["refined_peers"]] == ["m1"]
    assert seen["skip_peer"](NS(content_sha256="sha-late")) is False
    assert seen["skip_peer"](NS(content_sha256="other")) is True, "a revised loser stays"
    assert records[0]["reason"] == "conflict_resolver"
    assert records[0]["retired"][0]["label"] == "update"
    assert followed == [("m1", "m2")]


async def test_a_recheck_of_the_older_side_of_a_flag_judges_the_newer_as_new(
    monkeypatch,
) -> None:
    day_before = datetime(2026, 1, 1)
    older = _mem(
        "m1",
        status="conflicted",
        conflict_with=["m2"],
        created_at=day_before,
        updated_at=day_before,
    )
    newer = _mem("m2", content_sha256="sha-m2", value_text='"Office is in Rome"')
    peers = [
        _peer_row("m2", "s2", "office_city", "Rome")
        | {"similarity": 0.9, "created_at": newer.created_at, "updated_at": newer.updated_at},
        _peer_row("m3", "s3", "office_city", "Lyon") | {"similarity": 0.7},
    ]

    async def head(org: str, mid: str) -> Any:
        return newer

    monkeypatch.setattr(resolver, "_head", head)
    new, rest = await resolver._newest_as_new("o", older, peers)
    assert new is newer
    assert [p["memory_id"] for p in rest] == ["m1", "m3"]
    assert rest[0]["content_sha256"] == "sha-new" and rest[0]["similarity"] == 0.9
    # The newer side, or a row with no later flagged peer, is judged as queued.
    assert await resolver._newest_as_new("o", newer, peers[1:]) == (newer, peers[1:])


# --- the pass ---------------------------------------------------------------------------------


@pytest.fixture
def pass_env(monkeypatch: pytest.MonkeyPatch) -> NS:
    from server.memory.workers import conflict_resolver as worker
    from server.observability import budget

    e = NS(owed=[], recheck=[], resolved=[], erasing=set(), over=set(), raise_for=set(), slow=0.0)
    e.in_flight = e.most_in_flight = 0

    async def owed(*, limit: int, **kw: Any) -> list[dict[str, Any]]:
        return e.owed[:limit]

    async def recheck(*, limit: int, **kw: Any) -> list[dict[str, Any]]:
        return e.recheck[:limit]

    async def erasing(org: str, **kw: Any) -> bool:
        return org in e.erasing

    async def over(org, namespace):
        return (org, namespace) in e.over

    async def resolve(q, *, policy, counts, acting):
        e.in_flight += 1
        e.most_in_flight = max(e.most_in_flight, e.in_flight)
        await asyncio.sleep(e.slow)
        e.in_flight -= 1
        if q["memory_key"] in e.raise_for:
            raise resolver.ModelUnavailable("404 model_not_found")
        e.resolved.append((q["org"], q["namespace"], q["memory_key"], policy))
        counts.judged += 1

    async def policy(org, namespace, ch=None):
        return "newest_wins"

    monkeypatch.setattr(queue, "owed_rows", owed)
    monkeypatch.setattr(queue, "unjudged_conflicted_rows", recheck)
    monkeypatch.setattr(queue, "erasure_underway", erasing)
    monkeypatch.setattr(budget, "namespace_over_cap", over)
    monkeypatch.setattr(resolver, "resolve_row", resolve)
    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", policy)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    e.run = worker.run_pass
    return e


async def test_a_pass_handles_each_tenant_on_its_own_and_each_row_once(pass_env: NS) -> None:
    pass_env.owed = [
        _q(org="o1", namespace="a", memory_key="k1"),
        _q(org="o2", namespace="a", memory_key="k2"),
    ]
    # Already flagged and also owed: judged once.
    pass_env.recheck = [_q(org="o1", namespace="a", memory_key="k1"), _q(org="o1", memory_key="k3")]
    out = await pass_env.run()
    assert sorted(pass_env.resolved) == [
        ("o1", "a", "k1", "newest_wins"),
        ("o1", "ns", "k3", "newest_wins"),
        ("o2", "a", "k2", "newest_wins"),
    ]
    assert out["judged"] == 3


async def test_an_erasure_or_a_spent_budget_skips_the_namespace(pass_env: NS) -> None:
    pass_env.owed = [_q(org="o1", memory_key="k1"), _q(org="o2", memory_key="k2")]
    pass_env.erasing, pass_env.over = {"o1"}, {("o2", "ns")}
    out = await pass_env.run()
    assert pass_env.resolved == []
    assert out["skipped_erasure"] == 1 and out["skipped_budget"] == 1


async def test_an_unknown_model_warns_once_and_stops_the_pass(
    pass_env: NS, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2"), _q(memory_key="k3")]
    pass_env.raise_for = {"k1"}
    with caplog.at_level("WARNING"):
        out = await pass_env.run()
    assert pass_env.resolved == [] and out["errored"] == 0
    assert sum("unavailable" in r.message for r in caplog.records) == 1


async def test_a_pass_judges_its_rows_at_the_configured_concurrency(
    pass_env: NS, monkeypatch
) -> None:
    pass_env.owed = [_q(memory_key=f"k{i}") for i in range(12)]
    pass_env.slow = 0.05
    for limit in (8, 3):
        monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", limit)
        pass_env.resolved, pass_env.most_in_flight = [], 0
        out = await pass_env.run()
        assert pass_env.most_in_flight == limit and out["judged"] == 12


async def test_the_cost_cap_is_read_before_every_call(pass_env: NS, monkeypatch) -> None:
    # The namespace crosses its cap after the first judgement: the rest wait.
    from server.observability import budget

    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)

    async def over(org, namespace):
        return len(pass_env.resolved) >= 1

    monkeypatch.setattr(budget, "namespace_over_cap", over)
    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2"), _q(memory_key="k3")]
    out = await pass_env.run()
    assert [k for _, _, k, _ in pass_env.resolved] == ["k1"]
    assert out["skipped_budget"] == 2


async def test_the_judge_maps_a_404_to_model_unavailable(monkeypatch) -> None:
    import httpx

    import server.llm.client as llm_client

    class Missing:
        async def complete(self, *a: Any, **k: Any) -> Any:
            req = httpx.Request("POST", "https://llm.example/v1")
            raise httpx.HTTPStatusError(
                "404 model_not_found", request=req, response=httpx.Response(404, request=req)
            )

    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: Missing())
    with pytest.raises(resolver.ModelUnavailable):
        await resolver.judge(cr.Note("n", "k", "v"), _PEERS)


async def test_the_judge_sends_the_schema_and_the_model(monkeypatch) -> None:
    import server.llm.client as llm_client

    seen: dict[str, Any] = {}

    class LLM:
        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            seen.update(system=system, user=user, **kw)
            return NS(text=_answer(_entry("p2", "related", 0.9)), model="gpt-6.1-sol-2026-09-01")

    models: list[str] = []
    monkeypatch.setattr(
        llm_client,
        "get_llm_client_without_fallback",
        lambda m, **kw: models.append((m, kw["reasoning_effort"])) or LLM(),
    )
    got, answered = await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
    assert models == [(settings.conflict_resolver_model, "low")] == [("gpt-6.1-sol", "low")]
    assert seen["system"] == cr.SYSTEM_PROMPT and seen["response_format"] == cr.RESPONSE_FORMAT
    assert seen["max_tokens"] == 120 + 90 * 2 and list(got) == [_PEERS[1].memory_id]
    assert answered == "gpt-6.1-sol-2026-09-01", "the audit names the model that answered"


def test_the_resolver_client_never_falls_back_to_another_model(monkeypatch) -> None:
    import server.llm.client as llm_client
    from server.llm.fallback import FallbackLLMClient

    monkeypatch.setattr(settings, "llm_fallback_model", "gpt-6-luna")
    monkeypatch.setattr(settings, "llm_provider", "fake")
    llm_client.reset()
    try:
        assert isinstance(llm_client.get_llm_client_for_model("gpt-6.1-sol"), FallbackLLMClient)
        plain = llm_client.get_llm_client_without_fallback("gpt-6.1-sol")
        assert not isinstance(plain, FallbackLLMClient)
    finally:
        llm_client.reset()


# --- the switch and the write-time mark --------------------------------------------------------


def test_unset_the_resolver_follows_the_llm_provider() -> None:
    assert Settings(_env_file=None, llm_provider="fake").memory_conflict_resolver_enabled is False
    assert Settings(_env_file=None, llm_provider="openai").memory_conflict_resolver_enabled is True
    off = Settings(_env_file=None, llm_provider="openai", memory_conflict_resolver_enabled=False)
    assert off.memory_conflict_resolver_enabled is False
    on = Settings(_env_file=None, llm_provider="fake", memory_conflict_resolver_enabled=True)
    assert on.memory_conflict_resolver_enabled is True


def test_a_write_is_marked_only_when_the_check_asks_and_the_resolver_is_on(monkeypatch) -> None:
    from server.memory.app.conflicts import resolver_mark
    from server.memory.domain.conflicts import ConflictResult

    asks = ConflictResult(verdict="clear", peers=[], review=True)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", True)
    assert resolver_mark(asks) == queue.OWED_MARK
    assert resolver_mark(ConflictResult(verdict="clear", peers=[])) is None
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", False)
    assert resolver_mark(asks) is None


async def test_a_peer_below_the_threshold_marks_the_write_but_never_flags_it(monkeypatch) -> None:
    from server.memory.app import conflicts as cf
    from tests.unit.test_conflict_check import _row, _RowsCH

    ch = _RowsCH([_row("p", "old", "Office is in Paris", dist=0.30)])  # cosine 0.70
    monkeypatch.setattr(cf, "get_client", lambda: ch)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", True)
    res = await cf.check_on_commit(
        org="o",
        namespace="n",
        agent="ag",
        memory_type="semantic",
        memory_key="new",
        new_value="Office moved to Rome",
        new_embedding=[0.1, 0.2],
        candidate_rows=list(ch.rows),
        resolved_threshold=0.85,
        resolved_duplicate_threshold=0.97,
        resolved_policy="auto_reinforce",
        allowlisted_keys=set(),
    )
    assert res.verdict == "clear" and res.peers == [] and res.review is True
    monkeypatch.setattr(settings, "memory_conflict_resolver_min_similarity", 0.60)
    assert cf._peer_query_distance(0.85) == pytest.approx(0.40)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", False)
    assert cf._peer_query_distance(0.85) == pytest.approx(0.15)


def test_the_marks_are_never_the_older_builds_pending_mark() -> None:
    # An older build reads only the epoch itself as its own pending judge.
    assert int(queue.OWED_MARK.timestamp()) == 1 and int(queue.JUDGED_MARK.timestamp()) == 2


async def test_a_bulk_row_whose_key_another_session_holds_is_owed(monkeypatch) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    class CH:
        def __init__(self) -> None:
            self.sql: list[str] = []

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.sql.append(sql)
            return [{"memory_key": "office_city"}]

    current = {"policy": "auto_reinforce"}

    async def policy(org: str, namespace: str, ch: Any = None) -> str:
        return current["policy"]

    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", policy)
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    monkeypatch.setattr("server.memory.app.key_supersession.request_scope", lambda: WHOLE_NAMESPACE)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", True)
    state = _BulkWriteState()
    ident = {"agent": "a", "user_id": "u", "session_id": "s1"}
    state.memory_rows = [{**ident, "memory_key": k, "memory_id": k} for k in ("office_city", "pet")]
    state.embedding_rows = [
        {**ident, "memory_key": k, "conflict_checked_at": None} for k in ("office_city", "pet")
    ]
    ch = CH()
    await bulk_set._bulk_mark_same_key(ch, NS(org="o", namespace="ns"), state)
    assert [e["conflict_checked_at"] for e in state.embedding_rows] == [queue.OWED_MARK, None]
    assert "NOT (agent = {own_agent:String} AND session_id = {own_session:String})" in ch.sql[0]
    # Under newest_wins the insert retires the older row itself: nothing is read.
    current["policy"], ch.sql = "newest_wins", []
    await bulk_set._bulk_mark_same_key(ch, NS(org="o", namespace="ns"), state)
    assert ch.sql == []


def test_the_defaults_are_the_benched_operating_point() -> None:
    # gpt-6.1-sol at low effort: update precision 0.98-1.00 at >= 0.95 on held-out pairs,
    # clear precision 0.96-0.98 at >= 0.90; a 0.60 floor flagged nothing it should not.
    defaults = {n: f.default for n, f in Settings.model_fields.items()}
    assert defaults["conflict_resolver_model"] == "gpt-6.1-sol"
    assert defaults["memory_conflict_resolver_min_similarity"] == 0.60
    assert defaults["memory_conflict_resolver_max_peers"] == 8
    assert defaults["memory_conflict_resolver_retire_confidence"] == 0.95
    assert defaults["memory_conflict_resolver_clear_confidence"] == 0.90
    # 48 rows a pass, 8 at once at ~5 s a call, then 10 s: ~70 rows a minute.
    assert defaults["memory_conflict_resolver_batch"] == 40
    assert defaults["memory_conflict_resolver_recheck_batch"] == 8
    assert defaults["memory_conflict_resolver_concurrency"] == 8
    assert defaults["memory_conflict_resolver_interval_seconds"] == 10.0
