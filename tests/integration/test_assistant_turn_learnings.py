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
    base_url: str, token: str, org: str, ns: str, user_id: str, session_id: str
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
                {"role": "assistant", "content": f"{_RULE} A colleague will reply soon."},
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
    assert cand["supporter_count"] == 1
    assert cand["supporters"] == [f"{org}/{ns}/{_AGENT}/alice/s1/{cand['memory_key']}"]
    assert _RULE in cand["rationale"]

    # Not a memory: neither the user's search nor the agent-level read finds it.
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

    # Another user's session states the same rule: one candidate, two supporters,
    # still pending.
    second = _converse(base_url, token, org, ns, "bob", "s2")
    assert second["learning_candidates"] == 1
    (cand,) = _pending(base_url, token, org, ns)
    assert cand["supporter_count"] == 2
    assert [s.split("/")[3] for s in cand["supporters"]] == ["alice", "bob"]
    assert cand["status"] == "pending"

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
@pytest.mark.asyncio
async def test_a_user_erasure_removes_the_candidates_that_user_backs(
    base_url: str, mint_test_token: Any
) -> None:
    from server.core.ch import get_client
    from server.erasure.store import census as erasure_census
    from server.erasure.store import steps_ledgers

    org = unique_org("asst-erase")
    ns_a = f"bot-{uuid.uuid4().hex[:8]}"
    ns_b = f"bot-{uuid.uuid4().hex[:8]}"
    token = mint_test_token(org=org, roles=["admin"])
    _converse(base_url, token, org, ns_a, "alice", "s1")
    _converse(base_url, token, org, ns_a, "bob", "s2")
    _converse(base_url, token, org, ns_b, "bob", "s3")

    ch = get_client()
    await ch.connect()
    try:
        before = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        # The filing and bob's support: two versions, one once a merge collapses them.
        assert before["loom.learnings_queue"] > 0
        deleted = await steps_ledgers.delete_learnings_queue_by_user(org, "alice", ch=ch)
        assert deleted == before["loom.learnings_queue"]
        after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
        assert after["loom.learnings_queue"] == 0
    finally:
        await ch.disconnect()

    # The candidate alice backed is gone; bob's own one in the other namespace stays.
    assert _pending(base_url, token, org, ns_a) == []
    (kept,) = _pending(base_url, token, org, ns_b)
    assert kept["supporters"][0].split("/")[3] == "bob"
