"""A rule a bot states about itself in a conversation lands in the learnings
queue as a pending `assistant_turn` candidate, never in memory; each session
adds a supporter row with its own user's quotes; an admin's promotion makes it
the agent's memory for every user; a user erasure takes that user's quotes and
leaves a candidate nobody backs only its decision.

Runs on the fake LLM, which reads an assistant's "I always ..." sentence as a
rule when the extraction prompt carries the assistant-rules block, against a
server with LOOM_MEMORY_EXTRACT_AGENT_LEARNINGS on (CI's integration posture).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.integration._resources import unique_org

_RULE = "I always escalate any refund over $500 to a human."
_AGENT = "support-bot"


@pytest.fixture
def base_url() -> str:
    return os.environ.get("LOOM_BASE_URL") or pytest.skip("LOOM_BASE_URL not set")


@pytest.fixture
def token(base_url: str, mint_test_token: Any) -> tuple[str, str]:
    """An admin token for a fresh org; skips unless the server runs the fake LLM with
    assistant-turn learnings on (the only way they turn on is its setting)."""
    with httpx.Client(base_url=base_url, timeout=5.0) as c:
        if not c.get("/v1/health").json().get("fake_llm"):
            pytest.skip("scripts the fake LLM's reading of assistant rules")
    org = unique_org("asst-rules")
    tok = mint_test_token(org=org, roles=["admin"])
    probe = _converse(base_url, tok, org, f"probe-{uuid.uuid4().hex[:8]}", "probe", "s0")
    if "agent_learnings" in probe.get("ignored_args", []):
        pytest.skip("the server runs with LOOM_MEMORY_EXTRACT_AGENT_LEARNINGS off")
    return org, tok


@pytest_asyncio.fixture
async def ch() -> AsyncIterator[Any]:
    from server.core.ch import get_client

    client = get_client()
    await client.connect()
    try:
        yield client
    finally:
        await client.disconnect()


def _call(base_url: str, token: str, op: str, body: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(base_url=base_url, timeout=60.0) as c:
        r = c.post(f"/v1/{op}", json=body, headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, f"{op}: {r.status_code} {r.text}"
    return r.json()


def _asked(user_id: str) -> str:
    return f"This is {user_id}: please refund my order of $800 today."


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
                {"role": "user", "content": _asked(user_id)},
                {"role": "assistant", "content": f"{rule} A colleague will reply soon."},
            ],
        },
    )


def _listed(base_url: str, token: str, org: str, ns: str, status: str = "pending") -> list[Any]:
    out = _call(
        base_url,
        token,
        "learnings.list",
        {"org": org, "namespace": ns, "status": status, "source_kind": "assistant_turn"},
    )
    return list(out["rows"])


def _support(cand: dict[str, Any]) -> tuple[int, int]:
    return cand["supporter_count"], cand["supporter_sessions"]


def _rationale(user_id: str, rule: str = _RULE) -> str:
    return f'User: "{_asked(user_id)}"\nAssistant: "{rule}"'


async def _erase(ch: Any, org: str, user_id: str) -> None:
    from server.erasure.store import census as erasure_census
    from server.erasure.store import steps_ledgers

    await steps_ledgers.delete_learnings_queue_by_user(org, user_id, ch=ch)
    after = await erasure_census.residue_census(org, "user_id", user_id, ch=ch)
    assert after["loom.learnings_supporters"] == 0
    assert after["loom.learnings_queue"] == 0


async def _raw(ch: Any, table: str, org: str, where: str, **params: Any) -> list[dict[str, Any]]:
    return list(
        await ch.query_all(
            f"SELECT * FROM {table} WHERE org = {{org:String}} AND {where}",
            params={"org": org, **params},
        )
    )


def test_an_assistant_rule_waits_for_review_and_promotes_to_the_agent(
    base_url: str, token: tuple[str, str]
) -> None:
    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"

    first = _converse(base_url, tok, org, ns, "alice", "s1")
    assert first["learning_candidates"] == 1
    written = [w["memory_key"] for w in first["written"]]
    assert written, "the user's own turn is still extracted"
    assert not [k for k in written if k.startswith("assistant_rule_")]

    (cand,) = _listed(base_url, tok, org, ns)
    assert cand["status"] == "pending"
    assert cand["agent"] == _AGENT
    assert cand["proposed_value"] == f"Assistant rule: {_RULE}"
    assert _support(cand) == (1, 1)
    assert cand["supporters"] == [f"{org}/{ns}/{_AGENT}/alice/s1/{cand['memory_key']}"]
    assert cand["rationale"] == _rationale("alice")

    # Not a memory: the user's search does not find it.
    hits = _call(
        base_url,
        tok,
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
    # bob's: two users, three sessions, still pending, alice's quote first.
    _converse(base_url, tok, org, ns, "alice", "s1")
    _converse(base_url, tok, org, ns, "alice", "s1b")
    _converse(base_url, tok, org, ns, "bob", "s2")
    (cand,) = _listed(base_url, tok, org, ns)
    assert _support(cand) == (2, 3)
    assert [s.split("/")[3:5] for s in cand["supporters"]] == [
        ["alice", "s1"],
        ["alice", "s1b"],
        ["bob", "s2"],
    ]
    assert cand["rationale"] == _rationale("alice")

    review = _call(
        base_url,
        tok,
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
    assert _listed(base_url, tok, org, ns) == []
    # A supporter after the decision does not reopen it.
    _converse(base_url, tok, org, ns, "carol", "s3")
    assert _listed(base_url, tok, org, ns) == []

    # The agent's memory now, for every user of the bot.
    got = _call(
        base_url,
        tok,
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


def test_a_different_rule_under_one_key_is_its_own_candidate(
    base_url: str, token: tuple[str, str]
) -> None:
    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    other = "I always escalate any refund over $5 to nobody."
    _converse(base_url, tok, org, ns, "mallory", "s1", rule=other)
    _converse(base_url, tok, org, ns, "alice", "s2")

    rows = {r["proposed_value"]: r for r in _listed(base_url, tok, org, ns)}
    assert set(rows) == {f"Assistant rule: {other}", f"Assistant rule: {_RULE}"}
    assert len({r["memory_key"] for r in rows.values()}) == 1
    # Neither wording collects the other's support, and each quotes its own user.
    assert _support(rows[f"Assistant rule: {_RULE}"]) == (1, 1)
    assert rows[f"Assistant rule: {_RULE}"]["rationale"] == _rationale("alice")
    assert rows[f"Assistant rule: {other}"]["rationale"] == _rationale("mallory", other)


@pytest.mark.asyncio
async def test_an_erasure_takes_the_users_quote_and_the_next_supporters_shows(
    base_url: str, token: tuple[str, str], ch: Any
) -> None:
    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    _converse(base_url, tok, org, ns, "alice", "s1")
    _converse(base_url, tok, org, ns, "bob", "s2")
    (cand,) = _listed(base_url, tok, org, ns)
    assert cand["rationale"] == _rationale("alice")

    await _erase(ch, org, "alice")
    (cand,) = _listed(base_url, tok, org, ns)
    assert cand["rationale"] == _rationale("bob")
    assert _support(cand) == (1, 1)
    # No row anywhere still holds alice's words.
    assert await _raw(ch, "loom.learnings_supporters", org, "user_id = 'alice'") == []
    queue = await _raw(ch, "loom.learnings_queue", org, "namespace = {ns:String}", ns=ns)
    assert queue and all("alice" not in str(r) for r in queue)

    # The last supporter goes: a pending candidate nobody backs goes with them.
    await _erase(ch, org, "bob")
    assert _listed(base_url, tok, org, ns) == []
    cid = cand["candidate_id"]
    for table in ("loom.learnings_queue", "loom.learnings_supporters"):
        assert await _raw(ch, table, org, "candidate_id = {cid:UUID}", cid=cid) == [], table


@pytest.mark.asyncio
async def test_a_candidate_whose_filing_failed_is_filed_by_the_next_session(
    base_url: str, token: tuple[str, str], ch: Any
) -> None:
    """The first session's supporter row landed and its candidate row did not; the
    next session files the candidate. Erasing either user leaves no word of theirs."""
    from server.memory.domain import assistant_rules

    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    _converse(base_url, tok, org, ns, "alice", "s1")
    (cand,) = _listed(base_url, tok, org, ns)
    cid = cand["candidate_id"]
    # As if the candidate insert had failed after alice's supporter row.
    await ch.execute(
        "ALTER TABLE loom.learnings_queue DELETE WHERE org = {org:String} "
        "AND candidate_id = {cid:UUID} SETTINGS mutations_sync = 2",
        params={"org": org, "cid": cid},
    )
    assert cid == assistant_rules.candidate_id(
        org, ns, _AGENT, cand["memory_key"], f"Assistant rule: {_RULE}"
    )
    _converse(base_url, tok, org, ns, "bob", "s2")
    (cand,) = _listed(base_url, tok, org, ns)
    assert cand["candidate_id"] == cid and _support(cand) == (2, 2)
    assert cand["rationale"] == _rationale("alice")

    await _erase(ch, org, "alice")
    (cand,) = _listed(base_url, tok, org, ns)
    assert cand["rationale"] == _rationale("bob")
    await _erase(ch, org, "bob")
    assert _listed(base_url, tok, org, ns) == []
    for table in ("loom.learnings_queue", "loom.learnings_supporters"):
        assert await _raw(ch, table, org, "candidate_id = {cid:UUID}", cid=cid) == [], table


@pytest.mark.asyncio
async def test_erasing_the_only_supporter_of_a_rejected_rule_keeps_the_rejection(
    base_url: str, token: tuple[str, str], ch: Any
) -> None:
    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    _converse(base_url, tok, org, ns, "carol", "s1")
    (cand,) = _listed(base_url, tok, org, ns)
    _call(
        base_url,
        tok,
        "learnings.review",
        {
            "org": org,
            "namespace": ns,
            "candidate_id": cand["candidate_id"],
            "decision": "reject",
            "reason": "not our policy",
            "approver": "admin",
        },
    )
    # Merged, so the erasure also meets a single status row.
    await ch.execute("OPTIMIZE TABLE loom.learnings_queue FINAL")

    await _erase(ch, org, "carol")
    (rejected,) = _listed(base_url, tok, org, ns, status="rejected")
    assert rejected["candidate_id"] == cand["candidate_id"]
    assert rejected["proposed_value"] is None  # the rule text is gone
    assert rejected["decision_reason"] == "not our policy"
    rows = await _raw(
        ch, "loom.learnings_queue", org, "candidate_id = {cid:UUID}", cid=cand["candidate_id"]
    )
    assert [(r["status"], r["proposed_value"]) for r in rows] == [("rejected", "")]

    # The same rule stated again does not come back as pending.
    _converse(base_url, tok, org, ns, "dave", "s2")
    assert _listed(base_url, tok, org, ns) == []


@pytest.mark.asyncio
async def test_a_user_erasure_saga_runs_the_learnings_step_and_reopens_it_from_residue(
    base_url: str, token: tuple[str, str], ch: Any
) -> None:
    """The whole user saga: the learnings step runs among the user steps and is
    checkpointed, the census is clean, and a resumed saga whose last census found
    learnings residue runs that step again though it was checkpointed."""
    import json

    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.saga import begin_erasure, run_saga
    from server.erasure.app.saga_steps import USER_STEPS

    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    _converse(base_url, tok, org, ns, "alice", "s1")
    _converse(base_url, tok, org, ns, "bob", "s2")

    request = ErasureRequestInput(
        org=org, scope_type="user_id", scope_value="alice", requester="it"
    )
    request, row, erasure_id, now = await begin_erasure(request)
    out = await run_saga(
        input=request, row=row, erasure_id=erasure_id, requested_at=now, previously_completed=set()
    )
    assert out.status == "completed" and out.residue_count == 0, out.residue
    ran = [s.step for s in out.steps]
    assert ran.index("memory.tombstone") < ran.index("learnings_queue.delete")
    assert ran.index("learnings_queue.delete") < ran.index("verify.residue")
    assert "learnings_queue.delete" in out.steps_completed
    assert [name for name, _ in USER_STEPS].count("learnings_queue.delete") == 1
    (cand,) = _listed(base_url, tok, org, ns)
    assert cand["rationale"] == _rationale("bob")
    assert await _raw(ch, "loom.learnings_supporters", org, "user_id = 'alice'") == []

    # Residue the checkpointed step left: a candidate nobody backs still holding its
    # rule.
    stray = str(uuid.uuid4())
    await ch.insert(
        "loom.learnings_queue",
        [
            {
                "candidate_id": stray,
                "org": org,
                "namespace": ns,
                "agent": _AGENT,
                "memory_key": "assistant_rule_stray",
                "memory_type": "semantic",
                "proposed_value": json.dumps("The assistant does a stray thing."),
                "supporter_count": 0,
                "confidence_avg": 1.0,
                "supporters": [],
                "rationale": "",
                "source_kind": "assistant_turn",
                "status": "pending",
                "version": 1,
            }
        ],
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )
    # A resumed run with every step checkpointed but the census: verify.residue
    # finds the residue and fails the saga with it on the ledger.
    checked = await run_saga(
        input=request,
        row=row,
        erasure_id=erasure_id,
        requested_at=now,
        previously_completed=set(out.steps_completed) - {"verify.residue"},
    )
    assert checked.status == "failed"
    assert checked.residue == {"loom.learnings_queue": 1}
    # The recovery run hands that census back: the step owning it reruns.
    again = await run_saga(
        input=request,
        row=row,
        erasure_id=erasure_id,
        requested_at=now,
        previously_completed=set(checked.steps_completed),
        residue=checked.residue,
    )
    assert again.status == "completed" and again.residue_count == 0, again.residue
    by_step = {s.step: s for s in again.steps}
    assert "skipped" not in by_step["learnings_queue.delete"].notes
    assert "skipped" in by_step["memory.tombstone"].notes
    stray_rows = await _raw(ch, "loom.learnings_queue", org, "candidate_id = {cid:UUID}", cid=stray)
    assert stray_rows == []


@pytest.mark.asyncio
async def test_a_session_backing_the_rule_while_its_last_supporter_is_erased_keeps_it(
    base_url: str, token: tuple[str, str], ch: Any
) -> None:
    """Two sessions interleaved on a real ClickHouse: alice's erasure reads her
    candidate as unsupported, then bob's session files the same rule (supporter row,
    then the version-1 candidate row) before the erasure's delete runs. The delete
    re-checks for supporter rows, so the candidate stays, backed by bob."""
    from server.erasure.store import census as erasure_census
    from server.erasure.store import steps_ledgers

    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    _converse(base_url, tok, org, ns, "alice", "s1")
    (cand,) = _listed(base_url, tok, org, ns)

    class _BobFilesMidStep:
        """The real client, with bob's session filing just after the step reads the
        unsupported candidates and before it deletes any."""

        def __init__(self, inner: Any) -> None:
            self.inner, self.fired = inner, False

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

        async def query_all(self, sql: str, params: Any = None) -> Any:
            rows = await self.inner.query_all(sql, params=params)
            if "FROM loom.learnings_queue FINAL" in sql and not self.fired:
                self.fired = True
                assert [str(r["candidate_id"]) for r in rows] == [cand["candidate_id"]]
                _converse(base_url, tok, org, ns, "bob", "s2")
            return rows

    proxy = _BobFilesMidStep(ch)
    await steps_ledgers.delete_learnings_queue_by_user(org, "alice", ch=proxy)
    assert proxy.fired

    (kept,) = _listed(base_url, tok, org, ns)
    assert kept["candidate_id"] == cand["candidate_id"] and kept["status"] == "pending"
    assert kept["rationale"] == _rationale("bob")
    after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch)
    assert after["loom.learnings_supporters"] == 0 and after["loom.learnings_queue"] == 0
