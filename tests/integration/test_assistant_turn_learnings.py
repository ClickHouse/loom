"""A rule a bot states about itself in a conversation lands in the learnings
queue as a pending `assistant_turn` candidate, never in memory; each session
adds a supporter row with its own user's quotes; an admin's promotion publishes
it to the namespace's knowledge, which serves it to that bot alone; a user
erasure takes that user's quotes and leaves a candidate nobody backs only its
decision.

Runs on the fake LLM, which reads an assistant's "I always ..." sentence as a
rule when the extraction prompt carries the assistant-rules block, which
memory_extract_agent_learnings adds. The setting is off by default, so the module
turns it on through a runtime override and clears it after.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from tests.integration._eventually import overrides_adopted
from tests.integration._resources import unique_org

_RULE = "I always escalate any refund over $500 to a human."
_AGENT = "support-bot"


def _set_learnings(c: httpx.Client, body: dict[str, Any]) -> None:
    r = c.post("/v1/admin/settings.set", json={"name": "memory_extract_agent_learnings", **body})
    assert r.status_code == 200, r.text
    asyncio.run(overrides_adopted())


@pytest.fixture(scope="module")
def base_url(control_plane_url: str) -> Iterator[str]:
    """The server under test with assistant-turn learnings on; skips unless it runs
    the fake LLM."""
    tok = os.environ.get("LOOM_API_KEY")
    headers = {"Authorization": f"Bearer {tok}"} if tok else {}
    with httpx.Client(base_url=control_plane_url, timeout=10.0, headers=headers) as c:
        if not c.get("/v1/health").json().get("fake_llm"):
            pytest.skip("scripts the fake LLM's reading of assistant rules")
        _set_learnings(c, {"value": True})
        try:
            yield control_plane_url
        finally:
            _set_learnings(c, {"clear": True})


@pytest.fixture
def token(mint_test_token: Any) -> tuple[str, str]:
    """An admin token for a fresh org."""
    org = unique_org("asst-rules")
    return org, mint_test_token(org=org, roles=["admin"])


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
    **extra: Any,
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
            "messages": [
                {"role": "user", "content": _asked(user_id)},
                {"role": "assistant", "content": f"{rule} A colleague will reply soon."},
            ],
            **extra,
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
    after = await erasure_census.residue_census(
        org, "user_id", user_id, ch=ch, spellings=[user_id.lower()]
    )
    assert after["loom.learnings_supporters"] == 0
    assert after["loom.learnings_queue"] == 0


async def _raw(ch: Any, table: str, org: str, where: str, **params: Any) -> list[dict[str, Any]]:
    return list(
        await ch.query_all(
            f"SELECT * FROM {table} WHERE org = {{org:String}} AND {where}",
            params={"org": org, **params},
        )
    )


def test_a_request_turns_learnings_off_for_its_own_call(
    base_url: str, token: tuple[str, str]
) -> None:
    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"

    out = _converse(base_url, tok, org, ns, "alice", "s1", agent_learnings=False)
    assert out["learning_candidates"] == 0
    assert _listed(base_url, tok, org, ns) == []


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
    assert review["promoted_to_knowledge"] is True
    assert _listed(base_url, tok, org, ns) == []
    # A supporter after the decision does not reopen it.
    _converse(base_url, tok, org, ns, "carol", "s3")
    assert _listed(base_url, tok, org, ns) == []

    # Published to the namespace's knowledge: every user's reads of this bot find it,
    # a user bounded to their own rows (read_scope=own_user) included, and a bot reads
    # its own rules by the agent tag.
    rule = f"Assistant rule: {_RULE}"
    query = "escalate any refund over $500 to a human"
    for user_id, read_scope in (("alice", "own_user"), ("dave", "namespace")):
        pack = _call(
            base_url,
            tok,
            "context.pack",
            {
                "org": org,
                "namespace": ns,
                "agent": _AGENT,
                "user_id": user_id,
                "query": query,
                "read_scope": read_scope,
                "include_knowledge": True,
            },
        )
        assert rule in pack["sections"].get("knowledge", ""), (user_id, pack["sections"])

    # Another bot in the namespace is not served the rule as one about itself.
    other = _call(
        base_url,
        tok,
        "context.pack",
        {
            "org": org,
            "namespace": ns,
            "agent": "other-bot",
            "user_id": "dave",
            "query": query,
            "include_knowledge": True,
        },
    )
    assert not [name for name, text in other["sections"].items() if _RULE in str(text)]

    def by_tag(agent: str) -> list[str]:
        out = _call(
            base_url,
            tok,
            "knowledge.search",
            {"org": org, "namespace": ns, "query": query, "tags": [f"agent:{agent}"]},
        )
        return [h["content"] for h in out["results"]]

    assert by_tag(_AGENT) == [rule]
    assert by_tag("other-bot") == []


def test_a_loom_minted_read_user_id_token_names_no_agent(
    base_url: str, token: tuple[str, str]
) -> None:
    """A token `loom mint-token --read-user-id` makes carries no agent: it files no
    bot's rule and is served none, while the bot's unpinned reads are."""
    from server.auth.jwt import mint_token

    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    _converse(base_url, tok, org, ns, "alice", "s1")
    (cand,) = _listed(base_url, tok, org, ns)
    _call(
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
    pinned = mint_token(
        subject="support-bot-for-alice",
        org=org,
        namespaces=["*"],
        roles=["writer"],
        read_user_id="alice",
        ttl_seconds=600,
    )

    def knowledge(read_token: str) -> str:
        pack = _call(
            base_url,
            read_token,
            "context.pack",
            {
                "org": org,
                "namespace": ns,
                "agent": _AGENT,
                "user_id": "alice",
                "query": "escalate any refund over $500 to a human",
                "include_knowledge": True,
            },
        )
        return str(pack["sections"].get("knowledge", ""))

    rule = f"Assistant rule: {_RULE}"
    assert rule in knowledge(tok)
    assert rule not in knowledge(pinned)

    other = "I always escalate any refund over $900 to a manager."
    filed = _converse(base_url, pinned, org, ns, "alice", "s2", rule=other)
    assert filed["learning_candidates"] == 0
    assert _listed(base_url, tok, org, ns) == []


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


@pytest.mark.destructive
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
    after = await erasure_census.residue_census(org, "user_id", "alice", ch=ch, spellings=["alice"])
    assert after["loom.learnings_supporters"] == 0 and after["loom.learnings_queue"] == 0


def test_the_coverage_sweep_does_not_write_the_rule_back_as_a_user_fact(
    base_url: str, token: tuple[str, str]
) -> None:
    """A second reading of the turns the first pass cited nowhere (coverage_sweep)
    reads them with the same rules: the assistant's rule is still not a memory."""
    org, tok = token
    ns = f"bot-{uuid.uuid4().hex[:8]}"
    out = _call(
        base_url,
        tok,
        "memory.set_from_messages",
        {
            "org": org,
            "namespace": ns,
            "agent": _AGENT,
            "user_id": "alice",
            "session_id": "s1",
            "coverage_sweep": True,
            "messages": [
                {"role": "user", "content": _asked("alice")},
                {"role": "assistant", "content": f"{_RULE} A colleague will reply soon."},
                {"role": "user", "content": "My order number is 4417 and it shipped Monday."},
                {"role": "user", "content": "The parcel came damaged and the box was wet."},
                {"role": "user", "content": "I live in Porto and work from home on Fridays."},
                {"role": "user", "content": "My sister also ordered the same blender last week."},
            ],
        },
    )
    assert out["learning_candidates"] == 1
    written = [w["memory_key"] for w in out["written"]]
    assert written, out
    for key in written:
        got = _call(
            base_url,
            tok,
            "memory.get",
            {
                "org": org,
                "namespace": ns,
                "agent": _AGENT,
                "user_id": "alice",
                "session_id": "s1",
                "memory_key": key,
            },
        )
        assert got["found"] is True, key
        assert "escalate any refund" not in str(got["value"]), (key, got)
    (cand,) = _listed(base_url, tok, org, ns)
    assert cand["proposed_value"] == f"Assistant rule: {_RULE}"
