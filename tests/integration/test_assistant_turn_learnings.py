"""A rule a bot states about itself in a conversation lands in the learnings
queue as a pending `assistant_turn` candidate, never in memory; a second
session adds a supporter; an admin's promotion makes it the agent's memory for
every user; a user erasure removes the candidates that user backs.

Runs on the fake LLM, which reads an assistant's "I always ..." sentence as a
rule when the extraction prompt carries the assistant-rules block.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

import httpx
import pytest

from tests.integration._resources import unique_org

_RULE = "I always escalate any refund over $500 to a human."
_AGENT = "support-bot"


@pytest.fixture
def base_url() -> str:
    return os.environ.get("LOOM_BASE_URL") or pytest.skip("LOOM_BASE_URL not set")


@pytest.fixture
def fake_llm(base_url: str) -> None:
    with httpx.Client(base_url=base_url, timeout=5.0) as c:
        if not c.get("/v1/health").json().get("fake_llm"):
            pytest.skip("scripts the fake LLM's reading of assistant rules")


def _call(base_url: str, token: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=60.0) as c:
        r = c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text}"
    return r.json()


def _converse(
    base_url: str,
    token: str,
    org: str,
    ns: str,
    user_id: str,
    session_id: str,
    rule: str = _RULE,
) -> dict[str, Any]:
    return _call(
        base_url,
        token,
        "memory.set_from_messages",
        {
            "org": org,
            "namespace": ns,
            "agent": _AGENT,
            "user_id": user_id,
            "session_id": session_id,
            "agent_learnings": True,
            "messages": [
                {"role": "user", "content": "Please refund my order of $800 today."},
                {"role": "assistant", "content": f"{rule} A colleague will reply soon."},
            ],
        },
    )


def _pending(base_url: str, token: str, org: str, ns: str) -> list[dict[str, Any]]:
    out = _call(
        base_url,
        token,
        "learnings.list",
        {"org": org, "namespace": ns, "source_kind": "assistant_turn"},
    )
    return list(out["rows"])


def _support(cand: dict[str, Any]) -> tuple[int, int, int]:
    return cand["supporter_count"], cand["supporter_users"], cand["supporter_sessions"]


@pytest.mark.usefixtures("fake_llm")
def test_an_assistant_rule_waits_for_review_and_promotes_to_the_agent(
    base_url: str, mint_test_token: Any
) -> None:
    org = unique_org("asst-rules")
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    token = mint_test_token(org=org, roles=["admin"])

    first = _converse(base_url, token, org, ns, "alice", "s1")
    assert first["learning_candidates"] == 1
    written = [w["memory_key"] for w in first["written"]]
    assert written, "the user's own turn is still extracted"
    assert not [k for k in written if k.startswith("assistant_rule_")]

    (cand,) = _pending(base_url, token, org, ns)
    assert cand["status"] == "pending"
    assert cand["agent"] == _AGENT
    assert cand["proposed_value"] == f"Assistant rule: {_RULE}"
    assert _support(cand) == (1, 1, 1)
    assert cand["supporters"] == [f"{org}/{ns}/{_AGENT}/alice/s1/{cand['memory_key']}"]
    assert cand["rationale"] == (
        f'User: "Please refund my order of $800 today."\nAssistant: "{_RULE}"'
    )

    # Not a memory: the user's search does not find it.
    hits = _call(
        base_url,
        token,
        "memory.search",
        {
            "org": org,
            "namespace": ns,
            "agent": _AGENT,
            "user_id": "alice",
            "query": "escalate any refund over $500 to a human",
        },
    )
    assert not [h for h in hits["results"] if "escalate any refund" in str(h.get("value"))]

    # The same session again changes nothing; another session of alice's and one of
    # bob's: two users, three sessions, still pending.
    _converse(base_url, token, org, ns, "alice", "s1")
    _converse(base_url, token, org, ns, "alice", "s1b")
    _converse(base_url, token, org, ns, "bob", "s2")
    (cand,) = _pending(base_url, token, org, ns)
    assert _support(cand) == (2, 2, 3)
    assert [s.split("/")[3:5] for s in cand["supporters"]] == [
        ["alice", "s1"],
        ["alice", "s1b"],
        ["bob", "s2"],
    ]

    review = _call(
        base_url,
        token,
        "learnings.review",
        {
            "org": org,
            "namespace": ns,
            "candidate_id": cand["candidate_id"],
            "decision": "promote",
            "reason": "matches the refund policy",
            "approver": "admin",
        },
    )
    assert review["new_status"] == "promoted"
    assert review["promoted_to_shared"] is True
    assert _pending(base_url, token, org, ns) == []
    # A supporter after the decision does not reopen it.
    _converse(base_url, token, org, ns, "carol", "s3")
    assert _pending(base_url, token, org, ns) == []

    # The agent's memory now, for every user of the bot.
    got = _call(
        base_url,
        token,
        "memory.get",
        {
            "org": org,
            "namespace": ns,
            "agent": _AGENT,
            "user_id": "-",
            "session_id": "-",
            "memory_key": cand["memory_key"],
        },
    )
    assert got["found"] is True
    assert got["value"] == f"Assistant rule: {_RULE}"
    assert "source-kind:assistant_turn" in got["tags"]


@pytest.mark.usefixtures("fake_llm")
def test_a_different_rule_under_one_key_is_its_own_candidate(
    base_url: str, mint_test_token: Any
) -> None:
    org = unique_org("asst-sibling")
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    token = mint_test_token(org=org, roles=["admin"])
    other = "I always escalate any refund over $5 to nobody."
    _converse(base_url, token, org, ns, "mallory", "s1", rule=other)
    _converse(base_url, token, org, ns, "alice", "s2")

    rows = {r["proposed_value"]: r for r in _pending(base_url, token, org, ns)}
    assert set(rows) == {f"Assistant rule: {other}", f"Assistant rule: {_RULE}"}
    assert len({r["memory_key"] for r in rows.values()}) == 1
    # Neither wording collects the other's support.
    assert _support(rows[f"Assistant rule: {_RULE}"]) == (1, 1, 1)
    assert _support(rows[f"Assistant rule: {other}"]) == (1, 1, 1)


@pytest.mark.usefixtures("fake_llm")
@pytest.mark.asyncio
async def test_a_user_erasure_removes_the_users_support_and_filings(
    base_url: str, mint_test_token: Any
) -> None:
    from server.core.ch import get_client
    from server.erasure.store import census as erasure_census
    from server.erasure.store import steps_ledgers

    org = unique_org("asst-erase")
    ns_a = f"bot-{uuid.uuid4().hex[:8]}"
    ns_b = f"bot-{uuid.uuid4().hex[:8]}"
    token = mint_test_token(org=org, roles=["admin"])
    # alice files in A, bob supports it; bob files in B, alice supports it.
    _converse(base_url, token, org, ns_a, "alice", "s1")
    _converse(base_url, token, org, ns_a, "bob", "s2")
    _converse(base_url, token, org, ns_b, "bob", "s3")
    _converse(base_url, token, org, ns_b, "alice", "s4")
    (filed,) = _pending(base_url, token, org, ns_a)

    ch = get_client()
    await ch.connect()
    try:
        # A review written by a build a release behind: no agent, no filed_by_user.
        await ch.insert(
            "loom.learnings_queue",
            [
                {
                    "candidate_id": filed["candidate_id"],
                    "org": org,
                    "namespace": ns_a,
                    "memory_key": filed["memory_key"],
                    "memory_type": "semantic",
                    "proposed_value": '"x"',
                    "supporter_count": 0,
                    "confidence_avg": 1.0,
                    "supporters": [],
                    "rationale": filed["rationale"],
                    "source_kind": "assistant_turn",
                    "status": "rejected",
                    "version": 2**62,
                }
            ],
            settings={"async_insert": 0, "wait_for_async_insert": 1},
        )
        # Merged away: only the review row, which carries nothing of the filer.
        await ch.execute("OPTIMIZE TABLE loom.learnings_queue FINAL")
        before = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert before["loom.learnings_queue"] > 0 and before["loom.learnings_supporters"] > 0
        await steps_ledgers.delete_learnings_queue_by_user(org, "alice", ch=ch)
        after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert after["loom.learnings_queue"] == 0
        assert after["loom.learnings_supporters"] == 0
        # Not one physical row of alice's candidate is left in either table.
        for table in ("loom.learnings_queue", "loom.learnings_supporters"):
            row = await ch.query_one(
                f"SELECT count() AS n FROM {table} "
                "WHERE org = {org:String} AND candidate_id = {cid:UUID}",
                params={"org": org, "cid": filed["candidate_id"]},
            )
            assert row is not None and int(row["n"]) == 0, table
        # Nor of alice's support of bob's candidate.
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.learnings_supporters "
            "WHERE org = {org:String} AND user_id = 'alice'",
            params={"org": org},
        )
        assert row is not None and int(row["n"]) == 0
    finally:
        await ch.disconnect()

    # alice's filing is gone with bob's support of it; bob's candidate stays, backed
    # by bob alone.
    assert _pending(base_url, token, org, ns_a) == []
    (kept,) = _pending(base_url, token, org, ns_b)
    assert _support(kept) == (1, 1, 1)
    assert kept["supporters"][0].split("/")[3] == "bob"
