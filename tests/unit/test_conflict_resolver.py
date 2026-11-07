"""The conflict resolver: its fold, its prompt and parse, the cluster read, what one
row's judgement does, and the pass around it. ClickHouse and the LLM are faked;
tests/integration/test_conflict_resolver_ch.py runs it against ClickHouse."""

from __future__ import annotations

import asyncio
import calendar
import json
import time
from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.memory.app import conflict_resolver as resolver
from server.memory.app import intent_marks
from server.memory.domain import conflict_resolution as cr
from server.memory.store import conflict_resolver as queue
from server.settings import Settings, settings

# What decided a retirement, as `_act_on_verdicts` passes it.
_WHY = {
    "judge_verdict": "update",
    "judge_confidence": "0.99",
    "judge_model": "m",
    "judge_prompt_version": "v3",
}


@pytest.fixture(autouse=True)
def _key_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    # A pair is settled under the peer's key write lock (a Keeper lease, faked here). No
    # test here waits on a held key, and a short wait passes its deadline on a loaded
    # runner: the pair then reads busy and the row is left owed instead of acted on.
    from tests.unit.claims_fake import fake_key_locks

    fake_key_locks(monkeypatch, wait_s=10.0)


# --- the fold ------------------------------------------------------------------------


def _v(verdict: str, confidence: float, *, entity: bool = True, attribute: bool = True) -> Any:
    return cr.PeerVerdict("p", verdict, confidence, entity, attribute)


def _action(v: Any, policy: str = "newest_wins") -> str:
    return cr.action_for(v, policy=policy, retire_at=0.98, clear_at=0.90, flag_at=0.95)


@pytest.mark.parametrize("policy", ["newest_wins", "auto_reinforce"])
def test_an_update_retires_only_at_the_retire_confidence(policy: str) -> None:
    assert _action(_v("update", 0.99), policy) == cr.RETIRE
    assert _action(_v("update", 0.98), policy) == cr.RETIRE
    # Between the flag and the retire threshold an update goes to review, never to a
    # retirement; below the flag threshold it changes nothing.
    assert _action(_v("update", 0.97), policy) == cr.FLAG
    assert _action(_v("update", 0.95), policy) == cr.FLAG
    assert _action(_v("update", 0.92), policy) == cr.NOTHING


def test_an_update_under_manual_review_is_a_flag_at_any_confidence() -> None:
    assert _action(_v("update", 0.99), "manual_review") == cr.FLAG


def test_a_contradiction_flags_and_compatible_verdicts_clear() -> None:
    assert _action(_v("contradiction", 0.99)) == cr.FLAG
    for verdict in ("elaboration", "restatement", "related", "unrelated"):
        assert _action(_v(verdict, 0.93)) == cr.CLEAR, verdict


def test_below_the_clear_confidence_nothing_changes() -> None:
    for verdict in cr.VERDICTS:
        assert _action(_v(verdict, 0.89)) == cr.NOTHING, verdict


def test_an_update_about_another_entity_or_attribute_changes_nothing() -> None:
    # A change of value, yet not the same thing: the answer contradicts itself, and a
    # flag between the pair stays.
    assert _action(_v("update", 0.99, entity=False)) == cr.NOTHING
    assert _action(_v("contradiction", 0.99, attribute=False)) == cr.NOTHING
    assert _action(_v("update", 0.5, entity=False)) == cr.NOTHING


# --- the prompt and the parse ----------------------------------------------------------

_PEERS = [
    cr.Note("11111111-aaaa-4aaa-8aaa-111111111111", "office_city", "Office is in Paris", 0.83),
    cr.Note("22222222-bbbb-4bbb-8bbb-222222222222", "team_lead", "Lead is Ana", 0.71),
]


def test_an_update_from_another_session_of_an_agent_changes_nothing() -> None:
    # The anonymous user is one subject per agent, whose sessions are separate tasks: a
    # confident update across them is neither retired nor flagged.
    v = _v("update", 0.99)
    kw = {"policy": "auto_reinforce", "retire_at": 0.95, "clear_at": 0.90, "flag_at": 0.95}
    assert cr.action_for(v, **kw, cross_session=True) == cr.NOTHING
    assert cr.action_for(_v("contradiction", 0.99), **kw, cross_session=True) == cr.NOTHING
    assert cr.action_for(v, **kw) == cr.RETIRE
    assert cr.action_for(_v("related", 0.99), **kw, cross_session=True) == cr.CLEAR


def test_cross_session_is_another_session_of_a_subject_with_no_user() -> None:
    row = NS(user_id="-", agent="a", session_id="s1")
    assert resolver._cross_session(row, {"agent": "a", "session_id": "s2"})
    assert not resolver._cross_session(row, {"agent": "a", "session_id": "s1"})
    other = NS(user_id="u", agent="a", session_id="s1")
    assert not resolver._cross_session(other, {"agent": "a", "session_id": "s2"})
    # A user_id sent empty is stored as sent and is the anonymous user too.
    empty = NS(user_id="", agent="a", session_id="s1")
    assert resolver._cross_session(empty, {"agent": "a", "session_id": "s2"})
    # Whitespace is a user, as `_subject_params` reads it.
    blank = NS(user_id=" ", agent="a", session_id="s1")
    assert not resolver._cross_session(blank, {"agent": "a", "session_id": "s2"})
    # No session is '-' or sent empty: either spelling is the other.
    none = NS(user_id="-", agent="a", session_id="")
    assert not resolver._cross_session(none, {"agent": "a", "session_id": "-"})


@pytest.mark.parametrize(
    ("user_id", "users", "anon"),
    [("-", ["-", ""], 1), ("", ["-", ""], 1), (" ", [" "], 0), ("u1", ["u1"], 0)],
)
def test_the_cluster_subject_of_the_anonymous_user_takes_both_spellings(
    user_id: str, users: list[str], anon: int
) -> None:
    """The anonymous user's rows, '-' or sent empty, are one subject per agent; a named
    user's are that user's, whatever the agent."""
    assert queue._subject_params(user_id) == {"users": users, "anon": anon}


def test_each_note_shows_when_it_holds_and_its_session() -> None:
    row = _mem("m1", agent="a", session_id="s1", valid_at=None, value_text='"Office in Rome"')
    peers = [
        {
            "memory_id": "m2",
            "memory_key": "office_city",
            "value": '"Office in Paris"',
            "similarity": 0.8,
            "agent": "a",
            "session_id": "s2",
            "valid_at": None,
        },
        {
            "memory_id": "m3",
            "memory_key": "team",
            "value": '"Lead is Ana"',
            "similarity": 0.7,
            "agent": "b",
            "session_id": "s1",
            "valid_at": datetime(2026, 1, 2),
        },
    ]
    ns = 1_000_000_000
    holds = {"m1": 1_767_225_600 * ns, "m2": 1_767_139_200 * ns, "m3": 1_767_312_000 * ns}
    new, notes = resolver.notes_for(row, peers, holds)
    text = cr.user_prompt(new, notes)
    # The time a retirement orders the pair by, not the row's first write.
    assert text.startswith("NEW note (key office_city, written 2026-01-01 00:00):")
    assert (
        "(key office_city, cosine 0.80, written 2025-12-31 00:00, other session, same agent)"
        in text
    )
    # A writer-given fact date is shown as a date.
    assert "(key team, cosine 0.70, written 2026-01-02, other agent)" in text
    assert "same session" in cr.session_relation("a", "s1", "a", "s1")


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


def test_a_peer_given_two_different_verdicts_has_none() -> None:
    text = _answer(_entry("p1", "update", 0.97), _entry("p1", "unrelated", 0.97))
    assert cr.parse_verdicts(text, _PEERS) == {}
    same = _answer(_entry("p1", "update", 0.97), _entry("p1", "update", 0.5))
    assert cr.parse_verdicts(same, _PEERS)[_PEERS[0].memory_id].confidence == 0.97
    # The first entry malformed (a confidence past 1), the second another verdict: the
    # model still gave this peer two verdicts.
    torn = _answer(_entry("p1", "update", 1.5), _entry("p1", "unrelated", 0.99))
    assert cr.parse_verdicts(torn, _PEERS) == {}


def test_an_answer_followed_by_a_note_in_braces_still_parses() -> None:
    # A greedy match from the first brace to the last takes the note too and parses nothing.
    text = "Here is the result:\n" + _answer(_entry("p1")) + "\n(see {p1} above)"
    assert list(cr.parse_verdicts(text, _PEERS)) == [_PEERS[0].memory_id]
    assert cr.parse_verdicts("no json here", _PEERS) == {}


def test_an_explicit_key_is_never_read_as_a_short_id() -> None:
    peers = [cr.Note("m1", "p2", "City is Paris", 0.8), cr.Note("m2", "office", "Office", 0.7)]
    assert list(cr.parse_verdicts(_answer(_entry("key p2")), peers)) == ["m1"]
    assert list(cr.parse_verdicts(_answer(_entry("p2")), peers)) == ["m2"]


def test_a_key_two_peers_share_names_neither() -> None:
    peers = [cr.Note("m2", "office_city", "Paris"), cr.Note("m3", "office_city", "Lyon")]
    assert cr.parse_verdicts(_answer(_entry("office_city")), peers) == {}
    assert cr.parse_verdicts(_answer(_entry("key office_city")), peers) == {}
    assert list(cr.parse_verdicts(_answer(_entry("p2")), peers)) == ["m3"]
    assert list(cr.parse_verdicts(_answer(_entry("m3")), peers)) == ["m3"]


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
        # A 0.99 contradiction whose booleans are missing or not booleans: read as
        # False they would make it "different entities", which clears the flag.
        _answer({k: v for k, v in _entry("p1", "contradiction", 0.99).items() if "same" not in k}),
        _answer(_entry("p1", "contradiction", 0.99) | {"same_entity": "true"}),
        _answer(_entry("p1", "contradiction", 0.99) | {"same_attribute": None}),
        _answer(_entry("p1", "contradiction", 0.99) | {"same_entity": 1}),
        _answer(_entry("p1", verdict=["update"])),  # type: ignore[arg-type]
    ],
)
def test_malformed_output_yields_no_verdict(text: str) -> None:
    assert cr.parse_verdicts(text, _PEERS) == {}


def test_a_peer_the_answer_leaves_out_has_no_verdict_and_prose_is_ignored() -> None:
    text = "Here you go:\n" + _answer(_entry("p2", "related", 0.93)) + "\nthanks"
    got = cr.parse_verdicts(text, _PEERS)
    assert list(got) == [_PEERS[1].memory_id]
    # A peer named twice with one verdict keeps its first entry.
    twice = cr.parse_verdicts(_answer(_entry("p1", "related"), _entry("p1", "related")), _PEERS)
    assert twice[_PEERS[0].memory_id].verdict == "related"


# --- the cluster read --------------------------------------------------------------------


class _ClusterCH:
    def __init__(
        self,
        near: list[dict[str, Any]],
        rows: list[dict[str, Any]],
        flagged: list[dict[str, Any]] | None = None,
    ) -> None:
        self.near, self.rows, self.flagged, self.calls = near, rows, flagged or [], []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.calls.append((sql, params))
        if "arrayJoin(conflict_with)" in sql:
            return self.flagged
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
        "embedding_model": "text-embedding-3-large",
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
        "org": "o",
        "namespace": "ns",
        "agent": "a",
        "user_id": "u",
        "session_id": session,
        "memory_key": key,
        "value": value,
        "version": 1,
        "redacted_at": None,
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
    peers = (await queue.cluster_peers(_q(), floor=0.6, limit=8, ch=ch)).peers
    assert [(p["memory_id"], p["value"], round(p["similarity"], 2)) for p in peers] == [
        ("m3", "Lyon", 0.9),
        ("m2", "Paris", 0.7),
    ]
    _, params = ch.calls[-1]
    assert params["ids"] == [("a", "u", "s3", "office_city"), ("a", "u", "s2", "office_city")]


async def test_the_cluster_keeps_the_floor_the_cap_and_the_rows_flags() -> None:
    near = [_near(f"s{i}", f"k{i}", 0.05 * i) for i in range(1, 12)]  # cosine 0.95 .. 0.45
    rows = [_peer_row(f"m{i}", f"s{i}", f"k{i}", "v") for i in range(1, 12)]
    ch = _ClusterCH(near, rows)
    peers = (
        await queue.cluster_peers(_q(), floor=0.6, limit=3, prefer=frozenset({"m8"}), ch=ch)
    ).peers
    # m8 (cosine 0.60) is the row's flag: it takes a place first; below 0.60 never.
    assert [p["memory_id"] for p in peers] == ["m1", "m2", "m8"]
    sql, params = ch.calls[0]
    own = "NOT (agent = {a:String} AND user_id = {u:String} AND session_id = {s:String}"
    assert own + " AND memory_key = {k:String})" in " ".join(sql.split())
    assert "user_id IN {users:Array(String)} AND ({anon:UInt8} = 0 OR agent = {a:String})" in sql
    assert params["users"] == [str(_q()["user_id"])] and params["anon"] == 0
    assert params["org"] == "o" and params["ns"] == "ns" and params["lim"] == 12
    # The type, the embedding model and the expiry are applied before the vector LIMIT.
    assert "memory_type = {mt:String}" in sql and "expires_at > now()" in sql
    assert "embedding_model = {model:String}" in sql
    assert params["model"] == "text-embedding-3-large"
    assert "valid_to > now()" in sql
    rows_sql, _ = ch.calls[-1]
    assert "sensitivity != 'high'" in rows_sql and "memory_type = {mt:String}" in rows_sql


async def test_peers_are_read_in_the_rows_own_embedding_model_only() -> None:
    # Two models of one dimension: their cosine means nothing, so the vector reads (the
    # nearest rows, the flagged and same-key ones) keep to the queued vector's model.
    ch = _ClusterCH([_near("s2", "k2", 0.1)], [_peer_row("m2", "s2", "k2", "v")])
    await queue.cluster_peers(_q(embedding_model="gemini-embedding-001"), floor=0.6, limit=8, ch=ch)
    vector_reads = [(sql, p) for sql, p in ch.calls if "cosineDistance" in sql]
    assert len(vector_reads) == 2
    for sql, params in vector_reads:
        assert sql.count("embedding_model = {model:String}") >= 1
        assert params["model"] == "gemini-embedding-001"


async def test_a_flagged_peer_joins_the_cluster_whatever_the_cutoff() -> None:
    # m20 is outside the nearest vectors and below the floor, but the row is flagged with it.
    near = [_near("s1", "k1", 0.10)]
    rows = [_peer_row("m1", "s1", "k1", "v"), _peer_row("m20", "s20", "k20", "v")]
    ch = _ClusterCH(near, rows, flagged=[_near("s20", "k20", 0.50)])
    peers = (
        await queue.cluster_peers(_q(), floor=0.6, limit=3, prefer=frozenset({"m20"}), ch=ch)
    ).peers
    assert [p["memory_id"] for p in peers] == ["m1", "m20"]
    flagged_sql, params = ch.calls[1]
    # The flags are read from the row's head in ClickHouse, never sent.
    assert "arrayJoin(conflict_with)" in flagged_sql and "flagged" not in params


async def test_a_key_held_in_many_sessions_leaves_room_for_flags_and_near_peers() -> None:
    # office_city is held in 10 other sessions (cosine 0.5, below the floor); the row is
    # flagged with m20 (0.4) and owed for m30 at 0.9.
    same = [_near(f"s{i}", "office_city", 0.5) for i in range(10)]
    near = [_near("s30", "k30", 0.10)]
    rows = [_peer_row(f"m{i}", f"s{i}", "office_city", "v") for i in range(10)]
    rows += [_peer_row("m20", "s20", "k20", "v"), _peer_row("m30", "s30", "k30", "v")]
    ch = _ClusterCH(near, rows, flagged=[*same, _near("s20", "k20", 0.6)])
    peers = (
        await queue.cluster_peers(_q(), floor=0.6, limit=8, prefer=frozenset({"m20"}), ch=ch)
    ).peers
    ids = [p["memory_id"] for p in peers]
    assert "m20" in ids and "m30" in ids
    assert sum(p["memory_key"] == "office_city" for p in peers) == 4, "half the places"
    anchored_sql, params = ch.calls[1]
    assert "LIMIT {same_lim:UInt32}" in anchored_sql and params["same_lim"] == 4


# --- one row --------------------------------------------------------------------------------


def _mem(mid: str = "m1", **kw: Any) -> Any:
    base = dict(
        memory_id=mid,
        actor="",
        memory_key="office_city",
        namespace="ns",
        agent="a",
        user_id="u",
        session_id="s1",
        status="active",
        superseded_by="",
        pinned=False,
        content_sha256="sha-new",
        conflict_with=[],
        value_text='"Office moved to Rome"',
        value="Office moved to Rome",
        run_id="-",
        valid_at=None,
        created_at=datetime(2026, 1, 2),
        updated_at=datetime(2026, 1, 2),
        tombstoned_at=None,
        expires_at=None,
        valid_to=None,
        sensitivity="",
        version=0,
    )
    return NS(**(base | kw))


# When the judged row's value and its peer's were asserted (ns): a day apart, as the
# prompt shows them (to the minute).
_PEER_AT = 1_767_312_000_000_000_000  # 2026-01-02
_ROW_AT = _PEER_AT + 86_400 * 10**9  # 2026-01-03


@pytest.fixture
def row_env(monkeypatch: pytest.MonkeyPatch) -> NS:
    e = NS(
        mem=_mem(),
        heads={},
        flags_left=set(),
        peers=[_peer_row("m2", "s2", "office_city", '"Office is in Paris"') | {"similarity": 0.8}],
        answer=_answer(_entry("p1", "update", 0.99)),
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

    async def cluster(q: Any, **kw: Any) -> Any:
        e.cluster_kw = kw
        return queue.Cluster(e.peers, frozenset(e.flags_left))

    async def judge(new: Any, notes: list[Any]) -> dict[str, Any]:
        e.llm_calls += 1
        e.notes = notes
        return cr.parse_verdicts(e.answer, notes), "gpt-6.1-sol-2026-09-01"

    async def stamp(q: Any, **kw: Any) -> int:
        if e.erasing:  # as the store's stamp: nothing under erasure
            return 0
        e.stamped.append(q["memory_key"])
        return 1

    # When each row's value was first asserted (ns): the peer a moment before the row.
    e.asserted = {"m1": _ROW_AT, "m2": _PEER_AT}

    e.reads = 0
    e.undone_from_read = 1  # the read (1-based) from which the Undos are seen

    async def asserted(org: str, namespace: str, rows: Any, **kw: Any) -> Any:
        # An Undo restored now, after both statements, of the value its row holds.
        e.reads += 1
        undos = [
            queue.Undo(lo, w, str(e.heads[lo].content_sha256 or ""), time.time_ns())
            for lo, w in e.undone
            if e.reads >= e.undone_from_read
        ]
        return queue.Asserted({i: e.asserted[i] for i, _ in rows if i in e.asserted}, undos)

    async def head(org: str, namespace: str, mid: str) -> Any:
        return e.heads.get(mid)

    async def retire(org: str, namespace: str, row: Any, peer: Any, *, why: Any) -> Any:
        e.acted.append((cr.RETIRE, row.memory_id, peer.memory_id))
        e.why = why
        return resolver.Retired("f1", [], "a")

    def acts(action: str) -> Any:
        async def act(org: str, row: Any, peer: Any) -> bool:
            e.acted.append((action, row.memory_id, peer.memory_id))
            return True

        return act

    async def audit(org: str, namespace: str, row: Any, acted: list[Any], model: str) -> None:
        e.audits.append(acted)
        e.audit_model = model

    monkeypatch.setattr(resolver, "_current", current)
    monkeypatch.setattr(queue, "cluster_peers", cluster)
    monkeypatch.setattr(resolver, "judge", judge)
    monkeypatch.setattr(queue, "stamp_judged", stamp)
    e.protected = set()

    async def protected(org: str, namespace: str, ids: list[str], **kw: Any) -> set[str]:
        return e.protected & set(ids)

    e.erasing = False

    async def under_erasure(org: str, **kw: Any) -> bool:
        return e.erasing

    e.dead: set[str] = set()

    async def dead_flags(org: str, namespace: str, identity: Any, **kw: Any) -> list[str]:
        return [str(x) for x in e.mem.conflict_with if str(x) in e.dead]

    e.unlinked: list[tuple[str, tuple[str, ...]]] = []

    async def unlink(org: str, row: Any, *ids: str) -> bool:
        e.unlinked.append((row.memory_id, ids))
        return True

    e.undone: set[tuple[str, str]] = set()  # (loser, winner) pairs a person undid
    monkeypatch.setattr(queue, "dead_flags", dead_flags)
    monkeypatch.setattr(resolver, "_unlink", unlink)
    monkeypatch.setattr(queue, "asserted_versions", asserted)
    monkeypatch.setattr(queue, "protected_ids", protected)
    monkeypatch.setattr(queue, "under_erasure", under_erasure)
    monkeypatch.setattr(resolver, "_head", head)

    async def after_retire(org: str, namespace: str, done: Any) -> None:
        e.after_retire = done

    monkeypatch.setattr(resolver, "_retire", retire)
    monkeypatch.setattr(resolver, "_flag", acts(cr.FLAG))
    monkeypatch.setattr(resolver, "_clear", acts(cr.CLEAR))
    monkeypatch.setattr(resolver, "_after_retire", after_retire)
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


async def test_the_record_and_follow_run_after_the_keys_locks_are_released(
    row_env: NS, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.store import key_write_lock as kl

    taken: list[str] = []

    async def after_retire(org: str, namespace: str, done: Any) -> None:
        # Both keys are free again: a revision of the peer does not wait for the graph.
        for session in ("s1", "s2"):
            async with kl.key_write_locks.hold("o", "ns", "a", "u", session, "office_city"):
                taken.append(session)

    monkeypatch.setattr(resolver, "_after_retire", after_retire)
    await _resolve()
    assert taken == ["s1", "s2"]


async def test_an_erasure_requested_during_the_call_leaves_everything_owed(row_env: NS) -> None:
    row_env.erasing = True
    counts = await _resolve()
    assert row_env.llm_calls == 1 and row_env.acted == [] and row_env.audits == []
    assert row_env.stamped == [], "the row stays owed; the queue leaves it out meanwhile"
    assert counts.retired == 0


async def test_the_stamp_writes_nothing_under_erasure() -> None:
    class CH:
        def __init__(self) -> None:
            self.sql: list[str] = []

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.sql.append(sql)
            return [{"n": 1}]

    ch = CH()
    assert await queue.stamp_judged(_q(), ch=ch) == 0
    assert len(ch.sql) == 1 and "erasure_requests" in ch.sql[0], "no vector read, no write"


@pytest.mark.parametrize(
    ("mark", "stamped"),
    [
        (queue.OWED_MARK, False),
        (datetime(1970, 1, 1), False),
        (None, True),
        (datetime(2026, 1, 1), True),
    ],
)
async def test_a_peers_stamp_keeps_a_mark_still_owed(monkeypatch, mark: Any, stamped: bool) -> None:
    async def rows(ch: Any, **kw: Any) -> list[dict[str, Any]]:
        return [{"scope": "private", "content_sha256": "sha-new", "conflict_checked_at": mark}]

    written: list[Any] = []

    async def reemit(rows: list[Any], overrides: Any, **kw: Any) -> int:
        written.extend(rows)
        return len(rows)

    async def no_erasure(org: str, **kw: Any) -> bool:
        return False

    monkeypatch.setattr(queue, "live_embedding_rows_for_key", rows)
    monkeypatch.setattr(queue, "reemit_rows", reemit)
    monkeypatch.setattr(queue, "under_erasure", no_erasure)
    assert await queue.stamp_judged(_q(), keep_owed=True, ch=object()) == int(stamped)
    assert await queue.stamp_judged(_q(), ch=object()) == 1, "its own turn stamps it"


async def test_a_pair_that_fails_leaves_the_audit_row_of_the_pairs_done_before_it(
    row_env: NS, monkeypatch: pytest.MonkeyPatch
) -> None:
    row_env.peers.append(
        _peer_row("m3", "s3", "office_town", '"Office is in Lyon"') | {"similarity": 0.7}
    )
    row_env.heads["m3"] = _mem("m3", session_id="s3", content_sha256="sha-m3")
    row_env.asserted["m3"] = _PEER_AT
    row_env.answer = _answer(
        _entry("p1", "contradiction", 0.95), _entry("p2", "contradiction", 0.95)
    )

    async def flag(org: str, row: Any, peer: Any) -> bool:
        if peer.memory_id == "m3":
            raise RuntimeError("ClickHouse went away")
        row_env.acted.append((cr.FLAG, row.memory_id, peer.memory_id))
        return True

    monkeypatch.setattr(resolver, "_flag", flag)
    with pytest.raises(RuntimeError):
        await _resolve()
    assert [p["peer_id"] for p in row_env.audits[0]] == ["m2"]


def test_a_value_its_writer_restated_after_a_retirement_is_asserted_then() -> None:
    """Versions (version, holds the hash, superseded): the row held the value at 10, was
    retired at 20, and holds it again at 30, written by an Undo or by its writer."""
    run = [(10, True, False), (20, True, True), (30, True, False)]
    assert queue._run_start(run, undo_versions=set()) == 30, "restated: a new statement"
    assert queue._run_start(run, undo_versions={30}) == 10, "the Undo's restore: still 10"
    assert queue._run_start(run, undo_versions={25}) == 30, "another Undo's version: restated"
    # A copy written while still retired (an attest of the retired row), then the Undo.
    still = [(1, True, False), (2, True, True), (3, True, True), (4, True, False)]
    assert queue._run_start(still, undo_versions={4}) == 1
    assert queue._run_start([(10, True, False), (15, True, False)], undo_versions=set()) == 10
    assert queue._run_start([(10, True, False), (15, False, False)], undo_versions=set()) is None


async def test_a_retirement_a_person_undid_is_not_made_again(row_env: NS) -> None:
    # m2 was retired behind m1 and restored: the same update leaves the pair alone,
    # neither retired again nor flagged.
    row_env.undone = {("m2", "m1")}
    counts = await _resolve()
    assert row_env.acted == [] and counts.retired == counts.flagged == 0
    assert counts.skipped_no_action == 1 and row_env.stamped == ["office_city"]
    assert row_env.llm_calls == 0, "a pair an Undo settled is not paid for"


async def test_an_undo_made_during_the_call_is_honoured_under_the_locks(row_env: NS) -> None:
    # The Undo lands after the read before the call: the re-read under the locks sees it.
    row_env.undone = {("m2", "m1")}
    row_env.undone_from_read = 2
    counts = await _resolve()
    assert row_env.llm_calls == 1 and row_env.acted == []
    assert counts.skipped_no_action == 1


async def test_a_failed_audit_read_after_a_failed_pair_raises_the_pairs_error(
    row_env: NS, monkeypatch
) -> None:
    # The erasure read before the audit fails too: the error raised is still the pair's.
    row_env.peers.append(
        _peer_row("m3", "s3", "office_town", '"Office is in Lyon"') | {"similarity": 0.7}
    )
    row_env.heads["m3"] = _mem("m3", session_id="s3", content_sha256="sha-m3")
    row_env.asserted["m3"] = _PEER_AT
    row_env.answer = _answer(
        _entry("p1", "contradiction", 0.95), _entry("p2", "contradiction", 0.95)
    )
    failed = False

    async def flag(org: str, row: Any, peer: Any) -> bool:
        nonlocal failed
        if peer.memory_id == "m3":
            failed = True
            raise RuntimeError("ClickHouse went away")
        return True

    async def under_erasure(org: str, **kw: Any) -> bool:
        if failed:
            raise ConnectionError("erasure read failed")
        return False

    monkeypatch.setattr(resolver, "_flag", flag)
    monkeypatch.setattr(queue, "under_erasure", under_erasure)
    with pytest.raises(RuntimeError, match="ClickHouse went away"):
        await _resolve()


@pytest.mark.parametrize("peer_at", [_ROW_AT, _ROW_AT + 10**9])
async def test_an_update_whose_peer_was_not_asserted_first_is_flagged(
    row_env: NS, peer_at: int
) -> None:
    # Asserted at the same ns as the judged row, or later: no order, so no retire.
    row_env.asserted["m2"] = peer_at
    counts = await _resolve()
    assert row_env.acted == [(cr.FLAG, "m1", "m2")]
    assert counts.flagged == 1 and counts.retired == 0
    assert row_env.audits[0][0]["action"] == cr.FLAG


@pytest.mark.parametrize(
    ("answer", "policy", "want"),
    [
        (_entry("p1", "update", 0.96), "newest_wins", cr.FLAG),
        (_entry("p1", "update", 0.99), "auto_reinforce", cr.RETIRE),
        (_entry("p1", "update", 0.95), "auto_reinforce", cr.FLAG),
        (_entry("p1", "update", 0.99), "manual_review", cr.FLAG),
        (_entry("p1", "contradiction", 0.95), "newest_wins", cr.FLAG),
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
    [
        _answer(_entry("p1", "update", 0.85)),
        _answer({**_entry("p1", "update", 0.99), "same_entity": False}),
    ],
)
async def test_an_unsure_or_self_contradicting_answer_changes_nothing_but_stamps(
    row_env: NS, answer: str
) -> None:
    row_env.answer = answer
    counts = await _resolve()
    assert row_env.acted == [] and row_env.audits == []
    assert row_env.stamped == ["office_city"], "judged once: the row leaves the queue"
    assert counts.skipped_no_action == 1


@pytest.mark.parametrize("bad", [_answer(), "not json", ""])
async def test_the_judge_asks_once_more_after_an_answer_with_nothing_usable(
    monkeypatch, bad: str
) -> None:
    import server.llm.client as llm_client

    answers = [bad, _answer(_entry("p1")), bad, bad]

    class LLM:
        async def complete(self, *a: Any, **k: Any) -> Any:
            return NS(text=answers.pop(0), model="m")

    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: LLM())
    verdicts, _ = await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
    assert list(verdicts) == [_PEERS[0].memory_id]
    with pytest.raises(resolver.Unanswered):
        await resolver.judge(cr.Note("n", "k", "v"), _PEERS)


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


async def test_a_key_held_past_the_locks_wait_leaves_the_row_owed(
    row_env: NS, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The holder may fail its own if_match or change only metadata: no new mark brings
    # the pair back, so the row is judged again. The call was paid: it counts as an
    # attempt (JudgedThenFailed), so a key held on every pass ends the row's attempts.
    from contextlib import asynccontextmanager

    from server.core.errors import PreconditionFailedError

    @asynccontextmanager
    async def busy(*a: Any) -> Any:
        raise PreconditionFailedError("busy")
        yield

    monkeypatch.setattr(resolver, "_pair_locks", busy)
    with pytest.raises(resolver.JudgedThenFailed):
        await _resolve()
    assert row_env.llm_calls == 1 and row_env.acted == [] and row_env.stamped == []


async def test_a_row_gone_or_under_append_only_is_stamped_without_a_call(row_env: NS) -> None:
    row_env.mem.status = "superseded"
    await _resolve()
    row_env.mem.status = "active"
    await _resolve(policy="append_only")
    assert row_env.llm_calls == 0 and row_env.stamped == ["office_city", "office_city"]


@pytest.mark.parametrize("field", ["expires_at", "valid_to"])
async def test_an_expired_or_ended_queued_row_is_stamped_without_a_call(
    row_env: NS, field: str
) -> None:
    setattr(row_env.mem, field, datetime(2020, 1, 1))
    await _resolve()
    assert row_env.llm_calls == 0 and row_env.stamped == ["office_city"]


@pytest.mark.parametrize("who", ["m1", "m2"])
async def test_a_pair_is_not_acted_on_once_either_row_has_expired(row_env: NS, who: str) -> None:
    # Judged while live; expired by the time the pair is re-read: neither row moves.
    import copy

    expired = copy.copy(row_env.heads[who])
    expired.expires_at = datetime(2020, 1, 1)
    row_env.heads[who] = expired
    counts = await _resolve()
    assert row_env.acted == [] and counts.retired == 0
    if who == "m1":
        # The judged row is re-read right before its prompt: no call at all.
        assert row_env.llm_calls == 0 and row_env.stamped == ["office_city"]
    else:
        assert row_env.llm_calls == 1 and counts.skipped_changed == 1


async def test_a_row_or_peer_labelled_high_before_the_prompt_is_never_sent(row_env: NS) -> None:
    import copy

    row_env.protected = {"m2"}
    counts = await _resolve()
    assert row_env.llm_calls == 0 and counts.judged == 0, "the only peer is protected"
    row_env.protected = set()
    high = copy.copy(row_env.mem)
    high.sensitivity = "high"
    row_env.heads["m1"] = high
    await _resolve()
    assert row_env.llm_calls == 0 and row_env.acted == []


async def test_a_peer_the_authority_no_longer_serves_is_never_sent(
    row_env: NS, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Under Postgres ClickHouse's copy of a peer can trail a delete, a rewrite, a status
    # or a label: the peer goes to the model only while its head is the version read.
    from server.core import pg
    from server.memory.store import pg_memory

    key = ("o", "ns", "a", "u", "s2", "office_city")
    heads: dict[Any, Any] = {}

    async def _heads(keys: list[Any], **_k: Any) -> dict[Any, Any]:
        return {k: heads[k] for k in keys if k in heads}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    counts = await _resolve()
    assert row_env.llm_calls == 0 and counts.judged == 0, "the only peer is gone"
    assert row_env.stamped == ["office_city"], "an unflagged peer leaves nothing owed"
    heads[key] = {"version": 2, "redacted_at": None}
    await _resolve()
    assert row_env.llm_calls == 0, "a version since the read is not what was read"
    heads[key] = {"version": 1, "redacted_at": None}
    await _resolve()
    assert row_env.llm_calls == 1 and row_env.notes[0].value == "Office is in Paris"


@pytest.mark.parametrize(
    ("flags", "beside_a_served_peer", "held"),
    [
        pytest.param([], True, False, id="behind-unflagged-other-served"),
        pytest.param(["m2"], True, False, id="behind-unflagged-other-flagged"),
        pytest.param(["m3"], True, True, id="behind-flagged-other-served"),
        pytest.param(["m3"], False, True, id="behind-flagged-other-behind"),
    ],
)
async def test_a_peer_behind_its_delivery_holds_the_row_back_only_when_flagged(
    row_env: NS,
    monkeypatch: pytest.MonkeyPatch,
    flags: list[str],
    beside_a_served_peer: bool,
    held: bool,
) -> None:
    # m3 is behind. Unflagged, it is left out and the row is judged against the rest; flagged,
    # the row waits with no call, whatever else the cluster holds: judging the other peers
    # now would be paid again once ClickHouse holds m3's change.
    from server.core import pg
    from server.memory.store import pg_memory

    row_env.peers.append(
        _peer_row("m3", "s3", "office_city", '"Office is in Lyon"') | {"similarity": 0.7}
    )
    row_env.heads["m3"] = _mem("m3", session_id="s3", content_sha256="sha-m3")
    served = {"s2"} if beside_a_served_peer else set()

    async def _heads(keys: list[Any], **_k: Any) -> dict[Any, Any]:
        return {k: {"version": 1, "redacted_at": None} for k in keys if k[4] in served}

    monkeypatch.setattr(pg, "enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    row_env.mem.conflict_with = flags
    counts = resolver.Counts()
    if held:
        with pytest.raises(resolver.PeerBehind):
            await resolver.resolve_row(_q(), policy="newest_wins", counts=counts)
        assert row_env.llm_calls == 0 and row_env.stamped == [] and counts.judged == 0
    else:
        assert await resolver.resolve_row(_q(), policy="newest_wins", counts=counts) is None
        assert [n.memory_id for n in row_env.notes] == ["m2"] and counts.judged == 1
        assert row_env.stamped == ["office_city"]


async def test_a_cancellation_while_acting_lets_the_act_and_its_audit_finish(
    row_env: NS, monkeypatch
) -> None:
    acting = asyncio.Event()

    async def slow_retire(org: str, namespace: str, row: Any, peer: Any, **kw: Any) -> Any:
        acting.set()
        await asyncio.sleep(0.05)
        row_env.acted.append((cr.RETIRE, row.memory_id, peer.memory_id))
        return resolver.Retired("f1", [], "a")

    monkeypatch.setattr(resolver, "_retire", slow_retire)
    task = asyncio.ensure_future(
        resolver.resolve_row(_q(), policy="newest_wins", counts=resolver.Counts())
    )
    await acting.wait()  # cancelled while it acts, however slow the judge before it
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    # Done by the time the cancellation returns: the next pass never overlaps it.
    assert row_env.acted == [(cr.RETIRE, "m1", "m2")]
    assert row_env.audits and row_env.audits[0][0]["action"] == cr.RETIRE
    assert row_env.stamped == [], "cut short before its stamp: judged again next pass"


async def test_the_recheck_clears_a_false_flag_and_judges_the_flagged_peer(row_env: NS) -> None:
    row_env.mem.status, row_env.mem.conflict_with = "conflicted", ["m2"]
    row_env.answer = _answer(_entry("p1", "unrelated", 0.98))
    counts = await _resolve()
    assert row_env.cluster_kw["prefer"] == frozenset({"m2"})
    assert row_env.acted == [(cr.CLEAR, "m1", "m2")] and counts.cleared == 1


async def test_a_flagged_peer_the_answer_leaves_out_keeps_the_row_owed(row_env: NS) -> None:
    # The answer judges nothing about m2, the peer the row is flagged against: stamped,
    # that flag would never be judged again.
    row_env.mem.status, row_env.mem.conflict_with = "conflicted", ["m2"]
    row_env.answer = _answer()
    await _resolve()
    assert row_env.stamped == []


async def test_a_row_flagged_against_more_peers_than_places_stays_owed(
    row_env: NS, monkeypatch
) -> None:
    """Flagged against 3 rows with 2 places: the judged two act, the third waits."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_conflict_resolver_max_peers", 2)
    row_env.peers = [
        _peer_row(f"m{i}", f"s{i}", "office_city", f'"v{i}"') | {"similarity": 0.8} for i in (2, 3)
    ]
    for i in (2, 3):
        row_env.heads[f"m{i}"] = _mem(f"m{i}", session_id=f"s{i}", content_sha256=f"sha-m{i}")
    row_env.mem.status, row_env.mem.conflict_with = "conflicted", ["m2", "m3", "m4", "m9"]
    row_env.flags_left = {"m4"}  # joinable, past the places (m9 never joins: no wait for it)
    row_env.answer = _answer(_entry("p1", "related", 0.95), _entry("p2", "related", 0.95))
    judged = await resolver.resolve_row(_q(), policy="newest_wins", counts=resolver.Counts())
    assert judged == {"m2", "m3"} and row_env.stamped == []
    row_env.flags_left = set()
    judged = await resolver.resolve_row(_q(), policy="newest_wins", counts=resolver.Counts())
    assert judged is None and row_env.stamped == ["office_city"], "only m9 left: stamped"


async def test_flagged_peers_not_judged_before_take_the_places_first() -> None:
    """Three flagged peers, two places: once m1 and m2 were judged, m3 gets a place."""
    flagged = [_near(f"s{i}", f"k{i}", 0.1 * i) for i in (1, 2, 3)]
    rows = [_peer_row(f"m{i}", f"s{i}", f"k{i}", "v") for i in (1, 2, 3)]
    prefer = frozenset({"m1", "m2", "m3"})
    first = await queue.cluster_peers(
        _q(), floor=0.6, limit=2, prefer=prefer, ch=_ClusterCH([], rows, flagged=flagged)
    )
    assert {p["memory_id"] for p in first.peers} == {"m1", "m2"} and first.flags_left == {"m3"}
    again = await queue.cluster_peers(
        _q(),
        floor=0.6,
        limit=2,
        prefer=prefer,
        shown=frozenset({"m1", "m2"}),
        ch=_ClusterCH([], rows, flagged=flagged),
    )
    assert "m3" in {p["memory_id"] for p in again.peers}


async def test_an_unflagged_peer_left_out_still_lets_the_row_be_stamped(row_env: NS) -> None:
    row_env.answer = _answer()
    await _resolve()
    assert row_env.stamped == ["office_city"]


async def test_a_flag_against_a_row_no_longer_live_is_cleared_with_its_audit(
    row_env: NS,
) -> None:
    # m9 was retired since it flagged the row: it never joins a cluster, so only this
    # release clears it. The live flag (m2) is judged as before.
    row_env.mem.status, row_env.mem.conflict_with = "conflicted", ["m2", "m9"]
    row_env.dead = {"m9"}
    row_env.answer = _answer(_entry("p1", "unrelated", 0.98))
    counts = await _resolve()
    assert row_env.unlinked == [("m1", ("m9",))]
    assert row_env.audits[0] == [{"peer_id": "m9", "action": cr.CLEAR, "reason": "peer_not_live"}]
    assert row_env.acted == [(cr.CLEAR, "m1", "m2")] and counts.cleared == 2
    assert row_env.stamped == ["office_city"]


async def test_a_row_whose_flags_are_all_dead_is_released_and_never_left_conflicted(
    row_env: NS,
) -> None:
    row_env.mem.status, row_env.mem.conflict_with = "conflicted", ["m8", "m9"]
    row_env.dead = {"m8", "m9"}
    row_env.peers = []
    counts = await _resolve()
    assert row_env.unlinked == [("m1", ("m8", "m9"))] and counts.cleared == 2
    assert row_env.llm_calls == 0 and row_env.stamped == ["office_city"]


async def test_dead_flags_stay_on_a_pinned_row_and_under_an_erasure(row_env: NS) -> None:
    row_env.mem.status, row_env.mem.conflict_with = "conflicted", ["m9"]
    row_env.dead = {"m9"}
    row_env.peers = []
    row_env.heads["m1"].pinned = True
    await _resolve()
    assert row_env.unlinked == []
    row_env.heads["m1"].pinned = False
    row_env.erasing = True
    await _resolve()
    assert row_env.unlinked == [] and row_env.audits == []


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

    stamped: list[dict[str, Any]] = []

    stamp_kw: list[dict[str, Any]] = []

    async def stamp(q: Any, **kw: Any) -> int:
        stamped.append(dict(q))
        stamp_kw.append(kw)
        return 1

    monkeypatch.setattr(conflicts, "mark_peers_conflicted", mark)
    monkeypatch.setattr(queue, "stamp_judged", stamp)
    a = _mem("m1", status="conflicted", conflict_with=["m2"])
    b = _mem("m2", org="o", status="conflicted", conflict_with=["m1"], session_id="s3")
    assert await resolver._flag("o", a, b) is False and called == []
    assert stamped == [], "confirmed, not written: the peer keeps its own mark"
    peer = _mem("m2", org="o", session_id="s2", content_sha256="sha-m2", version=7)
    assert await resolver._flag("o", _mem("m1"), peer) is True
    assert called[0]["expected_sha256"] == "sha-new", "only the judged write is flagged"
    # The peer's side of the pair is judged too: its flag is not re-checked reversed.
    assert [(q["session_id"], q["content_sha256"], q["version"]) for q in stamped] == [
        ("s2", "sha-m2", 7)
    ]
    assert stamp_kw == [{"keep_owed": True}], "unless the peer is owed its own turn"


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


@pytest.fixture
def retire_env(monkeypatch: pytest.MonkeyPatch) -> NS:
    from server.memory.app import auto_resolution, conflicts, intent_marks, projection_failures

    e = NS(seen={}, records=[], followed=[], log=[], follow_ok=True, retires=True, record_ok=True)
    # The table's one batcher, empty and with no flusher of an earlier test's loop.
    monkeypatch.setattr(intent_marks, "_pending", {})
    monkeypatch.setattr(intent_marks, "_flusher", None)

    async def refines(**kw: Any) -> int:
        e.log.append("supersede")
        e.seen.update(kw)
        if not e.retires:
            return 0
        loser = kw["refined_peers"][0]
        kw["record_into"].setdefault(kw["reason"], []).append(
            {"memory_id": loser.memory_id, "superseded_by": kw["new_memory_id"]}
        )
        return 1

    async def record(**kw: Any) -> bool:
        e.records.append(kw)
        return e.record_ok

    async def follow(scope, pairs, op):
        e.followed.extend(pairs)
        return [] if e.follow_ok else pairs

    async def intent(**kw: Any) -> str:
        e.intent_payload = kw["payload"]
        e.log.append(("intent", kw["projection"], kw["payload"]["pairs"]))
        return f"f{sum(1 for x in e.log if x[0] == 'intent')}"

    async def resolved(*, org: str, failure_ids: list[str], **kw: Any) -> int:
        e.log.append(("resolved", failure_ids))
        return 1

    async def no_erasure(org: str, **kw: Any) -> bool:
        return False

    e.heads = {}  # the loser's head, read when nothing was tracked

    async def head(org: str, namespace: str, mid: str) -> Any:
        return e.heads.get(mid)

    monkeypatch.setattr(resolver, "_head", head)
    monkeypatch.setattr(queue, "under_erasure", no_erasure)
    monkeypatch.setattr(conflicts, "apply_refines", refines)
    monkeypatch.setattr(auto_resolution, "record_auto_supersessions", record)
    monkeypatch.setattr(auto_resolution, "follow_pairs", follow)
    monkeypatch.setattr(projection_failures, "record_projection_failure", intent)
    monkeypatch.setattr(projection_failures, "mark_projection_failures_resolved", resolved)
    return e


@pytest.mark.parametrize(("retires", "follow_ok"), [(True, True), (True, False), (False, True)])
async def test_the_follow_is_owed_before_the_supersede_and_settled_after_it(
    retire_env: NS, retires: bool, follow_ok: bool
) -> None:
    retire_env.retires, retire_env.follow_ok = retires, follow_ok
    late = _mem("m1", content_sha256="sha-late")
    current = _mem("m2", session_id="s2", content_sha256="sha-cur")
    retire_env.heads["m1"] = late  # not retired: still live as judged
    done = await resolver._retire("o", "ns", current, late, why=_WHY)
    assert (done is not None) is retires
    if done is not None:
        assert done.retired and all(e.items() >= _WHY.items() for e in done.retired)
        await resolver._after_retire("o", "ns", done)
    await intent_marks.drain()  # the batched mark
    assert retire_env.log[0] == ("intent", "triples_follow_owed", [["m1", "m2"]])
    assert retire_env.log[1] == "supersede"
    # A follow that failed stays owed to the repair; nothing retired, nothing owed.
    assert (("resolved", ["f1"]) in retire_env.log) is (follow_ok or not retires)


async def test_retirements_intents_are_resolved_in_one_batched_mark(retire_env: NS) -> None:
    for loser, winner in (("m1", "m2"), ("m3", "m4"), ("m5", "m6")):
        late = _mem(loser, content_sha256=f"sha-{loser}")
        current = _mem(winner, session_id="s2", content_sha256=f"sha-{winner}")
        await resolver._after_retire(
            "o", "ns", await resolver._retire("o", "ns", current, late, why=_WHY)
        )
    assert not [x for x in retire_env.log if x[0] == "resolved"], "none before the flush"
    await intent_marks.drain()
    resolved = [x for x in retire_env.log if x[0] == "resolved"]
    assert resolved == [("resolved", ["f1", "f2", "f3"])]


async def test_a_record_that_failed_leaves_the_intent_with_its_entry(retire_env: NS) -> None:
    retire_env.record_ok = False
    late = _mem("m1", content_sha256="sha-late")
    current = _mem("m2", session_id="s2", content_sha256="sha-cur", memory_key="office_town")
    await resolver._after_retire(
        "o", "ns", await resolver._retire("o", "ns", current, late, why=_WHY)
    )
    assert ("resolved", ["f1"]) not in retire_env.log, "the repair writes the record"
    assert retire_env.intent_payload["reason"] == "conflict_resolver"
    (entry,) = retire_env.intent_payload["retired"]
    assert (entry["memory_id"], entry["superseded_by"], entry["superseded_by_key"]) == (
        "m1",
        "m2",
        "office_town",
    )


async def test_a_retirement_behind_the_newer_row_of_the_pair(retire_env: NS) -> None:
    seen, records, followed = retire_env.seen, retire_env.records, retire_env.followed
    # The peer, an older statement, retires behind the judged row.
    late = _mem("m1", valid_at=datetime(2024, 1, 1), content_sha256="sha-late")
    current = _mem("m2", session_id="s2", content_sha256="sha-cur")
    await resolver._after_retire(
        "o", "ns", await resolver._retire("o", "ns", current, late, why=_WHY)
    )
    assert seen["new_memory_id"] == "m2" and seen["expected_sha256"] == "sha-cur"
    assert [p.memory_id for p in seen["refined_peers"]] == ["m1"]
    assert seen["skip_peer"](NS(content_sha256="sha-late")) is False
    assert seen["skip_peer"](NS(content_sha256="other")) is True, "a revised loser stays"
    assert records[0]["reason"] == "conflict_resolver"
    assert records[0]["retired"][0]["label"] == "update"
    assert followed == [("m1", "m2")]


async def test_the_order_is_finer_than_the_second_and_ignores_metadata_writes(
    monkeypatch,
) -> None:
    # Both written in one second; the peer's value 5 ms earlier. A later touch of the
    # peer moved its version and updated_at, not when its value was first asserted.
    second = datetime(2026, 1, 2)
    row = _mem("m1", version=1_767_312_000_010_000_000, created_at=second, updated_at=second)
    touched = datetime(2026, 1, 3)
    peer = _mem("m2", version=1_767_398_400_000_000_001, created_at=second, updated_at=touched)

    async def asserted(org: str, namespace: str, rows: Any, **kw: Any) -> Any:
        return queue.Asserted(
            {"m1": 1_767_312_000_010_000_000, "m2": 1_767_312_000_005_000_000}, []
        )

    monkeypatch.setattr(queue, "asserted_versions", asserted)
    order = (await resolver._assertion_order("o", "ns", [row, peer])).when
    assert order["m2"] < order["m1"]
    # An explicit valid_at earlier than the assertion moves the row back.
    peer.valid_at = datetime(2020, 1, 1)
    row.valid_at = datetime(2019, 1, 1)
    order = (await resolver._assertion_order("o", "ns", [row, peer])).when
    assert order["m1"] < order["m2"]


async def test_the_order_reads_valid_time_in_utc_caps_it_at_the_assertion_and_falls_back(
    monkeypatch,
) -> None:
    from datetime import timedelta, timezone

    ns = 1_000_000_000
    asserted_at = {"m1": 1_800_000_000 * ns}

    async def asserted(org: str, namespace: str, rows: Any, **kw: Any) -> Any:
        return queue.Asserted(dict(asserted_at), [])

    monkeypatch.setattr(queue, "asserted_versions", asserted)
    # 01:00 at -07:00 is 08:00 UTC.
    row = _mem("m1", valid_at=datetime(2023, 5, 30, 1, 0, tzinfo=timezone(timedelta(hours=-7))))
    order = (await resolver._assertion_order("o", "ns", [row])).when
    assert order["m1"][0] == calendar.timegm(datetime(2023, 5, 30, 8, 0).utctimetuple()) * ns
    # A valid time after the assertion (a fact dated ahead) holds from the assertion.
    row.valid_at = datetime(2030, 1, 1)
    assert (await resolver._assertion_order("o", "ns", [row])).when["m1"] == (
        asserted_at["m1"],
    ) * 2
    # A row the history does not hold: its current version.
    other = _mem("m9", version=42)
    assert (await resolver._assertion_order("o", "ns", [other])).when["m9"] == (42, 42)


async def test_a_row_with_a_newer_flagged_peer_is_judged_as_itself(row_env: NS) -> None:
    # The flagged peer is the newer statement: the row is NEW all the same, and only its
    # own mark is stamped (the peer comes to its own turn from its own mark).
    row_env.mem.conflict_with = ["m2"]
    row_env.asserted = {"m1": _PEER_AT, "m2": _ROW_AT}
    await _resolve()
    assert row_env.notes[0].memory_id == "m2" and row_env.stamped == ["office_city"]
    assert row_env.acted == [(cr.FLAG, "m1", "m2")], "an update not stated later flags"


# --- the pass ---------------------------------------------------------------------------------


@pytest.fixture
def pass_env(monkeypatch: pytest.MonkeyPatch) -> NS:
    from server.memory.workers import conflict_resolver as worker
    from server.observability import budget

    e = NS(owed=[], recheck=[], resolved=[], over=set(), raise_for={}, slow=0.0, stamped=[])
    e.left_owed = set()
    monkeypatch.setattr(worker, "_state", worker._State())
    e.in_flight = e.most_in_flight = 0
    e.recheck_reads = e.queue_reads = 0
    e.missing = None
    monkeypatch.setattr(worker, "_state", worker._State())
    monkeypatch.setattr(resolver, "model_missing", lambda: e.missing)

    def _ns(r: dict[str, Any]) -> tuple[str, str]:
        return (r["org"], r["namespace"])

    async def namespaces(**kw: Any) -> list[tuple[str, str]]:
        e.queue_reads += 1
        return sorted({_ns(r) for r in e.owed + e.recheck})

    e.seeds = []

    async def owed(*, limit: int, capped: Any = (), **kw: Any) -> list[dict[str, Any]]:
        e.capped = list(capped)
        e.seeds.append(("owed", kw.get("seed")))
        return [r for r in e.owed if _ns(r) not in capped][:limit]

    async def recheck(*, limit: int, capped: Any = (), **kw: Any) -> list[dict[str, Any]]:
        e.recheck_reads += 1
        e.seeds.append(("recheck", kw.get("seed")))
        return [r for r in e.recheck if _ns(r) not in capped][:limit]

    e.cap_reads = []

    async def over_all(pairs, **kw):
        e.cap_reads.append(kw)
        return {p: p in e.over for p in pairs}

    async def over(org, namespace, **kw):
        e.cap_reads.append(kw)
        return (org, namespace) in e.over

    async def resolve(q, *, policy, counts, shown=frozenset()):
        e.in_flight += 1
        e.most_in_flight = max(e.most_in_flight, e.in_flight)
        await asyncio.sleep(e.slow)
        e.in_flight -= 1
        if q["memory_key"] in e.raise_for:
            raise e.raise_for[q["memory_key"]]
        e.resolved.append((q["org"], q["namespace"], q["memory_key"], policy))
        counts.judged += 1
        return frozenset({"m2"}) if q["memory_key"] in e.left_owed else None

    async def stamp(q, **kw):
        e.stamped.append(q["memory_key"])

    async def policy(org, namespace, ch=None):
        return "newest_wins"

    monkeypatch.setattr(queue, "owed_rows", owed)
    monkeypatch.setattr(queue, "unjudged_conflicted_rows", recheck)
    monkeypatch.setattr(queue, "queued_namespaces", namespaces)
    monkeypatch.setattr(queue, "stamp_judged", stamp)
    monkeypatch.setattr(budget, "namespaces_over_cap", over_all)
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


async def test_the_same_refusal_row_after_row_stops_the_pass_and_leaves_the_rest_owed(
    pass_env: NS, monkeypatch
) -> None:
    """A request the provider refuses for every row (a bad model id, a prompt it rejects):
    three in a row stop the pass, and none of them is stamped, so the backlog is judged
    once the request is fixed. One slot, so the rows run in order."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    keys = [f"k{i}" for i in range(6)]
    pass_env.owed = [_q(memory_key=k) for k in keys]
    pass_env.raise_for = {k: resolver.Unjudgeable("400 Bad Request") for k in keys}
    out = await pass_env.run()
    assert pass_env.stamped == [], "the request was at fault: every row stays owed"
    assert out["errored"] == 3


async def test_a_row_stamped_after_an_attempt_left_it_owed_forgets_the_attempt(
    pass_env: NS,
) -> None:
    """Left owed once, then judged and stamped: its attempt count goes, so the leader's
    table holds only rows still owed."""
    from server.memory.workers import conflict_resolver as worker

    row = _q(memory_key="k1")
    pass_env.owed = [row]
    pass_env.left_owed = {"k1"}
    await pass_env.run()
    assert worker._attempt_key(row) in worker._state.attempts
    pass_env.left_owed = set()
    await pass_env.run()
    assert worker._state.attempts == {}


async def test_an_answer_whose_action_failed_breaks_a_refusal_streak(
    pass_env: NS, monkeypatch
) -> None:
    """Two refusals, then a row the judge answered whose write failed, then a refusal:
    the answer ends the streak, so the namespace is not stopped and its refused rows
    are stamped. One slot, so the rows run in order."""
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)

    async def resolve(q: Any, *, policy: str, counts: Any, shown: Any = frozenset()) -> None:
        if q["memory_key"] == "answered":
            counts.judged += 1
            raise resolver.JudgedThenFailed("RuntimeError: insert failed")
        raise resolver.Unjudgeable("400 Bad Request")

    monkeypatch.setattr(resolver, "resolve_row", resolve)
    keys = ["first", "second", "answered", "third"]
    pass_env.owed = [_q(memory_key=k) for k in keys]
    await pass_env.run()
    assert sorted(pass_env.stamped) == ["first", "second", "third"]


async def test_another_namespaces_answer_breaks_no_refusal_streak(
    pass_env: NS, monkeypatch
) -> None:
    """Rows of two namespaces judged at once: one refused row after row, with a row
    between that calls no judge, while the other's row is answered. The answer is the
    other namespace's alone, so the streak runs on and stops the refused namespace,
    whose rows stay owed instead of being stamped."""
    order = {k: asyncio.Event() for k in ("answered", "quiet")}

    async def resolve(q: Any, *, policy: str, counts: Any, shown: Any = frozenset()) -> None:
        key = q["memory_key"]
        if key == "b-answered":
            counts.judged += 1
            order["answered"].set()
        elif key == "a-quiet":  # no peers: stamped without a call
            await order["answered"].wait()
            order["quiet"].set()
        else:
            if key == "a-last":
                await order["quiet"].wait()
            raise resolver.Unjudgeable("400 Bad Request")

    monkeypatch.setattr(resolver, "resolve_row", resolve)
    pass_env.owed = [
        _q(namespace="a", memory_key=k) for k in ("a-first", "a-second", "a-quiet", "a-last")
    ]
    pass_env.owed.append(_q(namespace="b", memory_key="b-answered"))
    out = await pass_env.run()
    assert out["judged"] == 1 and out["errored"] == 3
    assert pass_env.stamped == [], "the refusals stopped namespace a: its rows stay owed"


async def test_a_row_judged_and_left_owed_is_stamped_on_its_third_attempt(
    pass_env: NS,
) -> None:
    """Every pass pays for it again (a flagged peer the answer leaves out): the third
    attempt stamps it, errored, so it stops costing calls and holding a place."""
    import httpx

    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2")]
    pass_env.left_owed = {"k1"}
    req = httpx.Request("POST", "https://llm.example/v1")
    pass_env.raise_for = {
        "k2": httpx.HTTPStatusError("503", request=req, response=httpx.Response(503, request=req))
    }
    outs = [await pass_env.run() for _ in range(3)]
    assert pass_env.stamped == ["k1"], "a transient failure pays nothing and is not counted"
    assert [o["errored"] for o in outs] == [1, 1, 2]


async def test_answers_cut_for_every_row_stop_the_pass_whatever_their_cap(
    pass_env: NS, monkeypatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    keys = [f"k{i}" for i in range(5)]
    pass_env.owed = [_q(memory_key=k) for k in keys]
    caps = [600, 900, 1200, 600, 900]
    pass_env.raise_for = {
        k: resolver.Unanswered(f"nothing usable in two answers (output cap {c} tokens)")
        for k, c in zip(keys, caps, strict=True)
    }
    out = await pass_env.run()
    assert out["errored"] == 3 and pass_env.stamped == []


async def test_an_answered_failure_keeps_the_peers_judged_before(monkeypatch) -> None:
    from server.memory.workers import conflict_resolver as worker

    monkeypatch.setattr(worker, "_state", worker._State())
    monkeypatch.setattr(queue, "stamp_judged", lambda q, **kw: asyncio.sleep(0))
    row = _q(memory_key="k1")
    await worker._out_of_attempts(row, frozenset({"m2"}))
    await worker._out_of_attempts(row)  # a failure after the answer: no peers of its own
    assert worker._state.attempts[worker._attempt_key(row)] == (2, frozenset({"m2"}))


async def test_only_a_failure_after_the_answer_counts_as_an_attempt(pass_env: NS) -> None:
    """A ClickHouse error before the call paid for nothing: the row stays owed however
    often it fails. One after the judge answered was paid: the third stamps it."""
    pass_env.owed = [_q(memory_key="before"), _q(memory_key="after")]
    pass_env.raise_for = {
        "before": RuntimeError("Code: 202. TOO_MANY_SIMULTANEOUS_QUERIES"),
        "after": resolver.JudgedThenFailed("RuntimeError: insert failed"),
    }
    for _ in range(4):
        await pass_env.run()
    assert pass_env.stamped == ["after"]


async def test_a_judged_row_between_refusals_keeps_the_pass_going(pass_env: NS) -> None:
    keys = ["k0", "k1", "ok", "k2", "k3"]
    pass_env.owed = [_q(memory_key=k) for k in keys]
    pass_env.raise_for = {k: resolver.Unjudgeable("400 Bad Request") for k in keys if k != "ok"}
    out = await pass_env.run()
    assert sorted(pass_env.stamped) == ["k0", "k1", "k2", "k3"] and out["judged"] == 1


async def test_each_pass_draws_a_new_namespace_order_for_both_reads(
    pass_env: NS, monkeypatch
) -> None:
    from server.memory.workers import conflict_resolver as worker

    draws = iter([11, 22])
    monkeypatch.setattr(worker.random, "getrandbits", lambda n: next(draws))
    pass_env.owed = [_q(memory_key="k1")]
    await pass_env.run()
    monkeypatch.setattr(worker._state, "recheck_after", 0.0)
    await pass_env.run()
    assert pass_env.seeds == [("owed", 11), ("recheck", 11), ("owed", 22), ("recheck", 22)]


async def test_the_resolver_reads_its_own_spend_against_its_share_of_the_cap(
    pass_env: NS,
) -> None:
    """The rest of a namespace's daily cap stays for the requests (memory.ask)."""
    pass_env.owed = [_q(memory_key="k1")]
    await pass_env.run()
    own = {"operation": "worker.conflict_resolver", "share": 0.25, "fail_open": False}
    assert pass_env.cap_reads == [own, own], "the pass's read and the read before the call"


async def test_a_namespace_over_its_cap_is_left_out_of_the_read(pass_env: NS) -> None:
    pass_env.owed = [_q(org="o1", memory_key="k1"), _q(org="o2", memory_key="k2")]
    pass_env.over = {("o2", "ns")}
    out = await pass_env.run()
    assert pass_env.capped == [("o2", "ns")]
    assert [k for _, _, k, _ in pass_env.resolved] == ["k1"]
    assert out["capped_namespaces"] == 1 and out["skipped_budget"] == 0


async def test_a_refused_request_is_stamped_and_a_transient_failure_stays_owed(
    pass_env: NS,
) -> None:
    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2"), _q(memory_key="k3")]
    pass_env.owed.append(_q(memory_key="k4"))
    pass_env.raise_for = {
        "k1": resolver.Unjudgeable("400 Bad Request"),
        "k2": RuntimeError("503 Service Unavailable"),
        "k4": resolver.Unanswered("cut twice"),
    }
    out = await pass_env.run()
    assert sorted(pass_env.stamped) == ["k1", "k4"], "a refusal or a cut answer holds no place"
    assert [k for _, _, k, _ in pass_env.resolved] == ["k3"]
    assert out["errored"] == 3


async def test_a_failed_cap_read_stays_inside_its_row(pass_env: NS, monkeypatch) -> None:
    from server.observability import budget

    async def over(org: str, namespace: str, **kw: Any) -> bool:
        if org == "o1":
            raise RuntimeError("ClickHouse timed out")
        return False

    monkeypatch.setattr(budget, "namespace_over_cap", over)
    pass_env.owed = [_q(org="o1", memory_key="k1"), _q(org="o2", memory_key="k2")]
    out = await pass_env.run()
    assert [k for _, _, k, _ in pass_env.resolved] == ["k2"] and out["errored"] == 1


async def test_a_burst_ending_on_an_empty_read_is_not_idle(
    pass_env: NS, monkeypatch, caplog
) -> None:
    from server.workers import pass_context

    idle: list[bool] = []
    monkeypatch.setattr(pass_context, "mark_idle", lambda: idle.append(True))
    monkeypatch.setattr(settings, "memory_conflict_resolver_batch", 1)
    pass_env.owed = [_q(memory_key="k1")]
    real = queue.owed_rows

    async def once(**kw: Any) -> list[dict[str, Any]]:
        rows = await real(**kw)
        pass_env.owed = []  # judged: the next read of the burst finds nothing
        return rows

    monkeypatch.setattr(queue, "owed_rows", once)
    with caplog.at_level("INFO"):
        out = await pass_env.run()
    assert out["judged"] == 1 and idle == []
    assert any("judged=1" in r.message for r in caplog.records)


async def test_a_failed_stamp_stays_inside_its_row(pass_env: NS, monkeypatch) -> None:
    async def stamp(q: Any, **kw: Any) -> int:
        raise RuntimeError("ClickHouse blip")

    monkeypatch.setattr(queue, "stamp_judged", stamp)
    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2")]
    pass_env.raise_for = {"k1": resolver.Unjudgeable("400 Bad Request")}
    pass_env.slow = 0.05
    out = await pass_env.run()
    assert [k for _, _, k, _ in pass_env.resolved] == ["k2"], "resolved before the pass ended"
    assert out["errored"] == 1


async def test_a_failure_that_escapes_a_row_ends_the_pass_with_no_row_running(
    pass_env: NS, monkeypatch
) -> None:
    import server.workers.cost_event as cost_event

    running: set[str] = set()

    async def resolve(q, *, policy, counts, shown=frozenset()):
        running.add(q["memory_key"])
        try:
            await asyncio.sleep(0 if q["memory_key"] == "k1" else 0.2)
        finally:
            running.discard(q["memory_key"])

    def emit(**kw: Any) -> None:
        if not running:
            return
        raise RuntimeError("the cost event failed")

    monkeypatch.setattr(resolver, "resolve_row", resolve)
    monkeypatch.setattr(cost_event, "emit_worker_cost_event_for", emit)
    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2")]
    with pytest.raises(ExceptionGroup):
        await pass_env.run()
    assert running == set(), "the other row was cancelled and awaited"


async def test_a_lease_outage_stops_the_pass_with_the_rows_owed(
    pass_env: NS, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.leases import LeaseUnavailable

    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2"), _q(memory_key="k3")]
    pass_env.raise_for = {"k1": LeaseUnavailable("keeper down", operation="memory_key_lock")}
    out = await pass_env.run()
    assert pass_env.resolved == [] and pass_env.stamped == [], "no further call, rows owed"
    assert out["errored"] == 0


async def test_an_unknown_model_warns_once_and_stops_the_pass(
    pass_env: NS, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "memory_conflict_resolver_concurrency", 1)
    pass_env.owed = [_q(memory_key="k1"), _q(memory_key="k2"), _q(memory_key="k3")]
    pass_env.raise_for = {"k1": resolver.ModelUnavailable("404 model_not_found")}
    with caplog.at_level("WARNING"):
        out = await pass_env.run()
    assert pass_env.resolved == [] and out["errored"] == 0
    assert sum("unavailable" in r.message for r in caplog.records) == 1


async def test_with_no_key_a_pass_reads_nothing_and_warns_once(pass_env: NS, caplog) -> None:
    pass_env.owed = [_q(memory_key="k1")]
    pass_env.missing = "no key for gpt-6.1-sol"
    with caplog.at_level("INFO"):
        await pass_env.run()
        await pass_env.run()
        assert pass_env.queue_reads == 0 and pass_env.resolved == []
        assert sum("unavailable" in r.message for r in caplog.records) == 1
        pass_env.missing = None
        await pass_env.run()
    assert [k for _, _, k, _ in pass_env.resolved] == ["k1"]
    assert sum("available again" in r.message for r in caplog.records) == 1


async def test_the_key_coming_back_is_noted_with_nothing_queued(pass_env: NS, caplog) -> None:
    # No key; the key is set while nothing is queued; then the provider refuses it.
    pass_env.missing = "no key for gpt-6.1-sol"
    with caplog.at_level("INFO"):
        await pass_env.run()
        pass_env.missing = None
        await pass_env.run()
        pass_env.owed = [_q(memory_key="k1")]
        pass_env.raise_for = {"k1": resolver.ModelUnavailable("401 invalid key")}
        await pass_env.run()
    assert sum("unavailable" in r.message for r in caplog.records) == 2
    assert sum("available again" in r.message for r in caplog.records) == 1


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

    async def over(org, namespace, **kw):
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


@pytest.mark.parametrize(
    ("status", "raised"),
    [
        (401, resolver.ModelUnavailable),
        (403, resolver.ModelUnavailable),
        (400, resolver.Unjudgeable),
        (422, resolver.Unjudgeable),
        (408, "transient"),
        (429, "transient"),
        (503, "transient"),
    ],
)
async def test_the_judge_tells_a_refusal_from_a_transient_failure(
    monkeypatch, status: int, raised: Any
) -> None:
    import httpx

    import server.llm.client as llm_client

    class Failing:
        async def complete(self, *a: Any, **k: Any) -> Any:
            req = httpx.Request("POST", "https://llm.example/v1")
            raise httpx.HTTPStatusError(
                f"{status} error", request=req, response=httpx.Response(status, request=req)
            )

    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: Failing())
    want = httpx.HTTPStatusError if raised == "transient" else raised
    with pytest.raises(want) as caught:
        await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
    assert type(caught.value) is want


async def test_the_judge_sends_the_schema_and_the_model(monkeypatch) -> None:
    import server.llm.client as llm_client

    seen: dict[str, Any] = {}

    class LLM:
        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            seen.update(system=system, user=user, **kw)
            text = _answer(_entry("p1", "related", 0.9), _entry("p2", "related", 0.9))
            return NS(text=text, model="gpt-6.1-sol-2026-09-01")

    models: list[str] = []
    monkeypatch.setattr(
        llm_client,
        "get_llm_client_without_fallback",
        lambda m, **kw: models.append((m, kw["reasoning_effort"])) or LLM(),
    )
    got, answered = await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
    assert models == [(settings.conflict_resolver_model, "low")] == [("gpt-6.1-sol", "low")]
    assert seen["system"] == cr.SYSTEM_PROMPT and seen["response_format"] == cr.RESPONSE_FORMAT
    assert seen["max_tokens"] == 120 + 90 * 2
    assert list(got) == [_PEERS[0].memory_id, _PEERS[1].memory_id]
    assert answered == "gpt-6.1-sol-2026-09-01", "the audit names the model that answered"


async def test_an_answer_that_leaves_a_peer_out_is_asked_again_with_room(monkeypatch) -> None:
    import server.llm.client as llm_client

    answers = [
        _answer(_entry("p2", "related", 0.9)),
        _answer(_entry("p1", "update", 0.97), _entry("p2", "related", 0.9)),
    ]
    caps: list[int] = []

    class LLM:
        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            caps.append(kw["max_tokens"])
            return NS(text=answers[len(caps) - 1], model="m")

    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: LLM())
    got, _ = await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
    assert caps == [300, 600] and set(got) == {p.memory_id for p in _PEERS}


@pytest.mark.parametrize("later", ["http", "refused", "deferred", "disabled", "unread"])
@pytest.mark.parametrize("usable", [False, True])
async def test_a_reask_that_fails_keeps_the_first_usable_answer(
    monkeypatch, later: str, usable: bool
) -> None:
    import httpx

    import server.llm.client as llm_client
    from server.core.errors import ModelCallDeferred, ModelCallRefused
    from server.llm import customer

    req = httpx.Request("POST", "https://llm.example/v1")
    errors = {
        "http": httpx.HTTPStatusError(
            "503", request=req, response=httpx.Response(503, request=req)
        ),
        "refused": ModelCallRefused("budget", hold_s=600),
        "deferred": ModelCallDeferred("wait", hold_s=120),
        "disabled": customer.ModelCallsDisabled("disabled"),
        "unread": customer.SwitchUnread("unread"),
    }
    error = errors[later]
    calls = 0

    class LLM:
        async def complete(self, system: str, user: str, **kw: Any) -> Any:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise error
            text = _answer(_entry("p1", "update", 0.97)) if usable else _answer()
            return NS(text=text, model="m")

    monkeypatch.setattr(llm_client, "get_llm_client_without_fallback", lambda m, **kw: LLM())
    if usable and later in {"http", "refused", "deferred"}:
        got, model = await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
        assert list(got) == [_PEERS[0].memory_id] and model == "m"
    else:
        with pytest.raises(type(error)):
            await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
    assert calls == 2


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


def test_unset_the_resolver_follows_its_models_provider(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    # A stack pinned to the fake provider stays free, whatever keys the shell holds.
    assert Settings(_env_file=None, llm_provider="fake").memory_conflict_resolver_enabled is False
    fake_judge = Settings(_env_file=None, llm_provider="openai", conflict_resolver_model="fake-j")
    assert fake_judge.memory_conflict_resolver_enabled is False
    assert Settings(_env_file=None, llm_provider="openai").memory_conflict_resolver_enabled is True
    # Without the key it stays on: writes are marked and wait for it.
    monkeypatch.delenv("OPENAI_API_KEY")
    assert Settings(_env_file=None, llm_provider="openai").memory_conflict_resolver_enabled is True
    off = Settings(_env_file=None, llm_provider="openai", memory_conflict_resolver_enabled=False)
    assert off.memory_conflict_resolver_enabled is False


def test_a_resolver_without_its_key_warns_once_at_boot(monkeypatch, caplog) -> None:
    import server.llm.client as llm_client
    import server.settings as settings_mod

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(settings_mod, "settings", Settings(_env_file=None, llm_provider="openai"))
    with caplog.at_level("WARNING"):
        llm_client.log_conflict_resolver_resolution()
    assert [r.message for r in caplog.records if "judged once the key is set" in r.message]
    caplog.clear()
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    with caplog.at_level("WARNING"):
        llm_client.log_conflict_resolver_resolution()
    assert caplog.records == []


async def test_the_judge_refuses_the_fake_stand_in_for_a_missing_key(monkeypatch) -> None:
    import server.llm.client as llm_client
    from server.llm.fake import FakeLLMClient

    monkeypatch.setattr(
        llm_client, "get_llm_client_without_fallback", lambda m, **kw: FakeLLMClient(model=m)
    )
    with pytest.raises(resolver.ModelUnavailable):
        await resolver.judge(cr.Note("n", "k", "v"), _PEERS)
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
    own = "NOT (agent = {own_agent:String} AND session_id IN {own_sessions:Array(String)})"
    assert own in " ".join(ch.sql[0].split())
    # Under newest_wins the insert retires the older row itself: nothing is read.
    current["policy"], ch.sql = "newest_wins", []
    await bulk_set._bulk_mark_same_key(ch, NS(org="o", namespace="ns"), state)
    assert ch.sql == []
    # The anonymous user's other sessions are other tasks: nothing is read or owed.
    current["policy"] = "auto_reinforce"
    for row in state.embedding_rows:
        row["conflict_checked_at"] = None
    for row in state.memory_rows:
        row["user_id"] = "-"
    await bulk_set._bulk_mark_same_key(ch, NS(org="o", namespace="ns"), state)
    assert ch.sql == [] and [e["conflict_checked_at"] for e in state.embedding_rows] == [None, None]


def test_a_later_close_sibling_of_its_type_is_marked() -> None:
    paris, rome, car = [1.0, 0.0, 0.0], [0.8, 0.6, 0.0], [0.0, 0.0, 1.0]
    kinds = ["semantic", "semantic", "semantic"]
    assert cr.later_close_siblings([paris, rome, car], kinds, 0.60) == {1}
    assert cr.later_close_siblings([rome, paris], kinds[:2], 0.60) == {1}, "the later one"
    assert cr.later_close_siblings([paris, rome], ["semantic", "preference"], 0.60) == set()
    assert cr.later_close_siblings([paris, [], rome], kinds, 0.85) == set()


async def test_a_bulk_update_inside_the_batch_is_owed_under_every_policy_but_append_only(
    monkeypatch,
) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    class CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return []

    current = {"policy": "newest_wins"}

    async def policy(org: str, namespace: str, ch: Any = None) -> str:
        return current["policy"]

    monkeypatch.setattr("server.memory.app.conflicts.namespace_conflict_policy", policy)
    from server.identity.domain.read_scope import WHOLE_NAMESPACE

    monkeypatch.setattr("server.memory.app.key_supersession.request_scope", lambda: WHOLE_NAMESPACE)
    monkeypatch.setattr(settings, "memory_conflict_resolver_enabled", True)
    for name, want in (("newest_wins", 1), ("auto_reinforce", 1), ("append_only", None)):
        current["policy"] = name
        state = _BulkWriteState()
        ident = {"agent": "a", "user_id": "u", "session_id": "s1", "memory_type": "semantic"}
        state.memory_rows = [{**ident, "memory_key": k} for k in ("office", "office_now")]
        state.embedding_rows = [
            {**ident, "memory_key": "office", "embedding": [1.0, 0.0], "conflict_checked_at": None},
            {**ident, "memory_key": "office_now", "embedding": [0.8, 0.6]}
            | {"conflict_checked_at": None},
        ]
        await bulk_set._bulk_mark_same_key(CH(), NS(org="o", namespace="ns"), state)
        marks = [r["conflict_checked_at"] for r in state.embedding_rows]
        assert marks == [None, queue.OWED_MARK if want else None], name


def test_the_defaults_are_the_benched_operating_point() -> None:
    # gpt-6.1-sol at low effort: update precision 0.99 or more at >= 0.98 on held-out pairs
    # (below it, the wrong retirements sat at 0.96), clear precision 0.96-0.98 at >= 0.90;
    # a 0.60 floor flagged nothing it should not, and 0.70 missed updates at cosine 0.696.
    defaults = {n: f.default for n, f in Settings.model_fields.items()}
    assert defaults["conflict_resolver_model"] == "gpt-6.1-sol"
    assert defaults["memory_conflict_resolver_min_similarity"] == 0.60
    assert defaults["memory_conflict_resolver_max_peers"] == 8
    assert defaults["memory_conflict_resolver_retire_confidence"] == 0.98
    assert defaults["memory_conflict_resolver_clear_confidence"] == 0.90
    # On a drained chat-memory run, flags at 0.90-0.95 were right 11 times in 26.
    assert defaults["memory_conflict_resolver_flag_confidence"] == 0.95
    # 48 rows a pass, 8 at once at ~5 s a call, then 10 s: ~70 rows a minute.
    assert defaults["memory_conflict_resolver_batch"] == 40
    assert defaults["memory_conflict_resolver_recheck_batch"] == 8
    assert defaults["memory_conflict_resolver_concurrency"] == 8
    assert defaults["memory_conflict_resolver_interval_seconds"] == 10.0


def test_the_queue_knows_an_abandoned_erasure_as_the_ledger_writes_it() -> None:
    from server.erasure.store.ledger import ABANDONED_REASON_PREFIX

    assert queue.ERASURE_ABANDONED_PREFIX == ABANDONED_REASON_PREFIX


@pytest.mark.parametrize(("owed", "reads"), [(3, 2), (1, 1)])
async def test_a_full_batch_takes_the_next_one_at_once_and_a_short_one_ends_the_pass(
    pass_env: NS, monkeypatch: pytest.MonkeyPatch, owed: int, reads: int
) -> None:
    monkeypatch.setattr(settings, "memory_conflict_resolver_batch", 2)
    pass_env.owed = [_q(memory_key=f"k{i}") for i in range(owed)]
    read: list[int] = []

    async def unjudged(*, limit: int, capped: Any = (), **kw: Any) -> list[dict[str, Any]]:
        done = {k for _, _, k, _ in pass_env.resolved}
        rows = [r for r in pass_env.owed if r["memory_key"] not in done][:limit]
        read.append(len(rows))
        return rows

    monkeypatch.setattr(queue, "owed_rows", unjudged)
    out = await pass_env.run()
    assert len(read) == reads and out["judged"] == owed


async def test_the_recheck_waits_once_it_comes_back_short(
    pass_env: NS, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A full batch: read again next pass. Short: not again for memory_conflict_resolver_recheck_idle_seconds.
    monkeypatch.setattr(settings, "memory_conflict_resolver_recheck_batch", 2)
    pass_env.recheck = [_q(memory_key=f"k{i}") for i in range(3)]
    await pass_env.run()
    pass_env.recheck = pass_env.recheck[2:]
    await pass_env.run()
    await pass_env.run()
    assert pass_env.recheck_reads == 2


# --- the claims each pinned on its own ---------------------------------------------------


def test_the_thresholds_themselves_change_the_pair() -> None:
    """At exactly 0.90 a compatible verdict clears; at exactly 0.95 a contradiction flags."""
    assert _action(_v("restatement", 0.90)) == cr.CLEAR
    assert _action(_v("contradiction", 0.95)) == cr.FLAG
    assert _action(_v("contradiction", 0.9499)) == cr.NOTHING


async def test_at_most_half_the_places_go_to_rows_of_the_key_in_other_sessions() -> None:
    """Six other sessions hold the key, all below the floor: four of them join."""
    sessions = [f"s{i}" for i in range(2, 8)]
    same_key = [_near(s, "office_city", 0.5) for s in sessions]
    rows = [_peer_row(f"m{s}", s, "office_city", "v") for s in sessions]
    ch = _ClusterCH([], rows, flagged=same_key)  # the anchored read: flagged and same-key
    peers = (await queue.cluster_peers(_q(), floor=0.6, limit=8, ch=ch)).peers
    assert len(peers) == 4
    anchored = [p for sql, p in ch.calls if "same_lim" in (p or {})]
    assert anchored and anchored[0]["same_lim"] == 4


@pytest.mark.parametrize(
    "predicate",
    [
        "org = {org:String}",
        "namespace = {ns:String}",
        "user_id IN {users:Array(String)} AND ({anon:UInt8} = 0 OR agent = {a:String})",
    ],
)
def test_every_cluster_read_keeps_to_the_tenant_and_the_subject(predicate: str) -> None:
    """Each filter on its own: a vector read or the rows' read without one of them would
    put another tenant's or another user's text in the prompt."""
    for sql in (queue._LIVE_VECTORS, queue._PEER_ROWS_SQL):
        assert predicate in sql, (predicate, sql)


async def test_a_full_batch_past_the_burst_window_ends_the_pass(pass_env: NS, monkeypatch) -> None:
    from server.memory.workers import conflict_resolver as worker
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_conflict_resolver_batch", 2)
    monkeypatch.setattr(worker.settings, "memory_conflict_resolver_burst_seconds", 0.0)
    pass_env.owed = [_q(memory_key=f"k{i}") for i in range(10)]  # every read comes back full
    await pass_env.run()
    assert [s for s in pass_env.seeds if s[0] == "owed"].__len__() == 1


async def test_a_pass_tags_its_queries_as_the_resolver(pass_env: NS, monkeypatch) -> None:
    from server.memory.workers import conflict_resolver as worker

    tags: list[str] = []
    monkeypatch.setattr(worker, "set_query_tag", tags.append)
    await pass_env.run()
    assert tags == ["worker.conflict_resolver"]


def test_a_resolver_burst_ends_within_half_the_pass_deadline() -> None:
    from pydantic import ValidationError

    from server.settings.catalog import MemorySettings

    with pytest.raises(ValidationError, match="BURST_SECONDS must be at most half"):
        MemorySettings(memory_conflict_resolver_max_pass_seconds=200.0)
    # 300 s, the default, is exactly half of a 600 s deadline.
    MemorySettings(
        memory_conflict_resolver_burst_seconds=300.0,
        memory_conflict_resolver_max_pass_seconds=600.0,
    )


def test_the_resolver_interval_is_at_least_a_second() -> None:
    from pydantic import ValidationError

    from server.settings.catalog import MemorySettings

    with pytest.raises(ValidationError, match="memory_conflict_resolver_interval_seconds"):
        MemorySettings(memory_conflict_resolver_interval_seconds=0.5)
    assert MemorySettings().memory_conflict_resolver_interval_seconds == 10.0


def test_an_update_between_two_facts_of_one_day_goes_to_review() -> None:
    sure = _v("update", 0.99)
    kw = {"policy": "newest_wins", "retire_at": 0.98, "clear_at": 0.90, "flag_at": 0.95}
    assert cr.action_for(sure, **kw, same_written=True) == cr.FLAG
    assert cr.action_for(sure, **kw) == cr.RETIRE


@pytest.mark.parametrize(("peer_day", "want"), [("2026-01-03", cr.FLAG), ("2026-01-02", cr.RETIRE)])
async def test_an_update_retires_only_a_fact_dated_an_earlier_day(
    row_env: NS, peer_day: str, want: str
) -> None:
    # Both writers gave the fact's date: the prompt shows the day alone. The same day has
    # no order between the two, so the update is flagged; the day before retires.
    row_env.mem.valid_at = datetime(2026, 1, 3)
    row_env.peers[0]["valid_at"] = datetime.fromisoformat(peer_day)
    row_env.asserted = {"m1": _ROW_AT + 10**9, "m2": _ROW_AT if want == cr.FLAG else _PEER_AT}
    await _resolve()
    assert row_env.acted == [(want, "m1", "m2")]


async def test_a_date_corrected_during_the_call_onto_the_same_day_flags(row_env: NS) -> None:
    # The prompt showed the peer a day earlier; by the re-read under the locks its writer
    # had dated it the judged row's day: the update goes to review.
    row_env.mem.valid_at = datetime(2026, 1, 3)
    row_env.peers[0]["valid_at"] = datetime(2026, 1, 2)
    row_env.heads["m2"].valid_at = datetime(2026, 1, 3)
    row_env.asserted = {"m1": _ROW_AT + 10**9, "m2": _ROW_AT}
    await _resolve()
    assert row_env.acted == [(cr.FLAG, "m1", "m2")]


def test_same_fact_day_needs_both_dates() -> None:
    day = cr.Note("a", "k", "v", written="2026-01-03", dated=True)
    assert cr.same_fact_day(day, cr.Note("b", "k", "v", written="2026-01-03", dated=True))
    assert not cr.same_fact_day(day, cr.Note("b", "k", "v", written="2026-01-03"))
    assert not cr.same_fact_day(day, cr.Note("b", "k", "v", written="2026-01-02", dated=True))


@pytest.mark.parametrize("verdict", ["update", "contradiction"])
def test_a_flag_takes_the_flag_confidence(verdict: str) -> None:
    # Below it an update or a contradiction changes nothing; at it, the pair is flagged.
    assert _action(_v(verdict, 0.94)) == cr.NOTHING
    assert _action(_v(verdict, 0.95), "manual_review") == cr.FLAG


def test_a_compatible_verdict_still_clears_at_the_clear_confidence() -> None:
    assert _action(_v("related", 0.90)) == cr.CLEAR
    assert _action(_v("related", 0.89)) == cr.NOTHING


async def test_another_anonymous_session_still_flagged_is_judged_and_cleared(row_env: NS) -> None:
    row_env.mem = _mem(user_id="-", conflict_with=["m2"])
    row_env.heads["m1"] = row_env.mem
    row_env.peers = [{**row_env.peers[0], "user_id": "-"}]
    row_env.answer = _answer(_entry("p1", "related", 0.99))
    counts = await _resolve(policy="auto_reinforce")
    assert row_env.llm_calls == 1 and counts.cleared == 1
    assert row_env.acted == [(cr.CLEAR, "m1", "m2")]


async def test_the_audit_row_names_the_prompt_version(monkeypatch) -> None:
    from server.observability import audit

    emitted: list[dict[str, Any]] = []

    async def emit(event: str, **kw: Any) -> None:
        emitted.append(kw["subject"])

    monkeypatch.setattr(audit, "emit", emit)
    await resolver._audit("o", "ns", _mem(), [{"action": cr.FLAG}], "gpt-6.1")
    assert emitted[0]["prompt_version"] == cr.PROMPT_VERSION == "v3"


async def test_another_anonymous_session_still_flagging_the_row_is_judged(row_env: NS) -> None:
    """The row's own flags went with a revision, but the peer still flags it: the pair
    is judged and the peer's stale flag cleared."""
    row_env.mem = _mem(user_id="-", conflict_with=[])
    row_env.heads["m1"] = row_env.mem
    row_env.heads["m2"].conflict_with = ["m1"]
    row_env.heads["m2"].status = "conflicted"
    peer = {**row_env.peers[0], "user_id": "-", "conflict_with": ["m1"], "status": "conflicted"}
    row_env.peers = [peer]
    row_env.answer = _answer(_entry("p1", "related", 0.99))
    counts = await _resolve(policy="auto_reinforce")
    assert row_env.llm_calls == 1 and counts.cleared == 1


async def test_a_row_held_back_by_a_peer_behind_is_held_with_no_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from server.memory.workers import conflict_resolver as w
    from server.workers import holds

    async def behind(*a: Any, **k: Any) -> Any:
        raise resolver.PeerBehind()

    async def over_cap(*a: Any, **k: Any) -> bool:
        return False

    async def flush() -> None:
        return None

    monkeypatch.setattr(resolver, "resolve_row", behind)
    monkeypatch.setattr("server.observability.budget.namespace_over_cap", over_cap)
    monkeypatch.setattr(
        "server.observability.events.get_emitter", lambda: SimpleNamespace(flush_now=flush)
    )
    monkeypatch.setattr("server.workers.cost_event.emit_worker_cost_event_for", lambda **k: None)
    monkeypatch.setattr(w, "_state", w._State())
    monkeypatch.setattr(w, "_HELD", holds.Held())
    lease = 45
    monkeypatch.setattr(w.settings, "pg_delivery_lease_seconds", lease)
    row = {"org": "o", "namespace": "ns", "agent": "a", "user_id": "u", "session_id": "s1"}
    row |= {"memory_key": "office_city", "content_sha256": "sha"}
    earlier = {w._attempt_key(row): (1, frozenset({"m2"}))}
    w._state.attempts.update(earlier)
    p = w._Pass()
    await w._resolve_one(p, row, "newest_wins")
    assert p.counts.skipped_behind == 1 and p.counts.errored == 0 and p.errors == []
    assert w._state.attempts == earlier, "no attempt spent, the peers shown before kept"
    assert w._HELD.current() == frozenset({w._identity(row)})
    assert holds.UNREAD_HOLD_S < w._HELD.left(w._identity(row)) <= lease, "held for a lease"
