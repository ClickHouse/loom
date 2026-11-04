"""Task inbox plane against a live server.

The ClickGap shape end to end: a public bot with no access to the private
namespace posts a confidential finding into the private bot's inbox; the
task waits in triage until a person on the private side accepts it; the
private bot pulls it under a lease, heartbeats, completes; the public bot
learns the outcome from its updates cursor; a stranger from another org
sees nothing. Needs LOOM_BASE_URL (skips otherwise).
"""

from __future__ import annotations

import asyncio
import uuid

import httpx
import pytest

from tests.integration._resources import unique_org


def _hdr(tok: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {tok}"}


async def _call(c: httpx.AsyncClient, tok: str, op: str, body: dict, *, expect: int = 200) -> dict:
    r = await c.post(f"/v1/{op}", json=body, headers=_hdr(tok))
    assert r.status_code == expect, (op, r.status_code, r.text[:400])
    return r.json()


@pytest.mark.slow
@pytest.mark.asyncio
async def test_public_bot_hands_a_finding_to_the_private_bot(
    control_plane_url: str, mint_test_token
) -> None:
    org = f"taskq-{uuid.uuid4().hex[:8]}"
    private_ns, public_ns = "private", "public"
    admin = mint_test_token(
        org=org, namespaces=[private_ns, public_ns], roles=["admin"], subject="alice"
    )
    private_bot = mint_test_token(
        org=org, namespaces=[private_ns], roles=["writer"], subject="sa:private-bot"
    )
    public_bot = mint_test_token(
        org=org, namespaces=[public_ns], roles=["writer"], subject="sa:public-bot"
    )
    # same org, an unrelated namespace: the handler answers found=false, never a
    # 403 that confirms the id exists; a token from another org is
    # refused by the transport before any handler runs.
    stranger = mint_test_token(org=org, namespaces=["elsewhere"], roles=["writer"], subject="eve")

    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        # the private inbox: admin registers it, the public bot may post, review by default
        reg = await _call(
            c,
            admin,
            "inbox.register",
            {
                "org": org,
                "namespace": private_ns,
                "agent": "alice",
                "inbox": "private-bot",
                "owner_agent": "private-bot",
                "posters": ["sa:public-bot"],
                "accept_disclosure": ["confidential", "internal"],
                "default_lease_seconds": 60,
            },
        )
        assert reg["created"] and reg["inbox"]["owner_namespace"] == private_ns
        await _call(
            c,
            admin,
            "inbox.policy_set",
            {
                "org": org,
                "namespace": private_ns,
                "agent": "alice",
                "inbox": "private-bot",
                "poster": "sa:public-bot",
                "kind": "security_finding",
                "mode": "review",
                "require_evidence": ["inputs.finding_id"],
            },
        )

        # the poster picker's source: the listed poster shows as listed, the admin as
        # someone who can already post
        who = await _call(
            c,
            admin,
            "inbox.principals",
            {"org": org, "namespace": private_ns, "agent": "alice", "inbox": "private-bot"},
        )
        by_subject = {p["subject"]: p for p in who["principals"]}
        assert (
            by_subject["sa:public-bot"]["listed"]
            and by_subject["sa:public-bot"]["kind"] == "service_account"
        )
        assert all(p["kind"] in ("human", "service_account", "other") for p in who["principals"])
        await _call(
            c,
            public_bot,
            "inbox.principals",
            {"org": org, "namespace": public_ns, "agent": "public-bot", "inbox": "private-bot"},
            expect=403,
        )

        # the public bot cannot read the private namespace at all — the inbox
        # reads as unregistered, never a 403 that confirms the name …
        await _call(
            c,
            public_bot,
            "task.list",
            {"org": org, "namespace": public_ns, "agent": "public-bot", "inbox": "private-bot"},
            expect=404,
        )
        # … but it may post into the inbox; a public_ok body is refused by the disclosure gate
        base = {
            "org": org,
            "namespace": public_ns,
            "agent": "public-bot",
            "inbox": "private-bot",
            "kind": "security_finding",
            "title": "UB in JSON reader",
            "brief": {"objective": "file privately", "inputs": {"finding_id": "f-77"}},
        }
        await _call(c, public_bot, "task.create", {**base, "disclosure": "public_ok"}, expect=422)
        created = await _call(
            c,
            public_bot,
            "task.create",
            {**base, "disclosure": "confidential", "idempotency_key": "f-77"},
        )
        task = created["task"]
        assert created["created"] and task["status"] == "triage"
        assert task["namespace"] == private_ns and task["from_subject"] == "sa:public-bot"
        again = await _call(
            c,
            public_bot,
            "task.create",
            {**base, "disclosure": "confidential", "idempotency_key": "f-77"},
        )
        assert not again["created"] and again["task"]["task_id"] == task["task_id"]

        # the poster can read its own task; a stranger gets found=false, never a 403
        mine = await _call(
            c,
            public_bot,
            "task.get",
            {"org": org, "namespace": public_ns, "task_id": task["task_id"]},
        )
        assert mine["found"]
        gone = await _call(
            c,
            stranger,
            "task.get",
            {"org": org, "namespace": "elsewhere", "task_id": task["task_id"]},
        )
        assert gone == {"found": False, "task": None}

        # the private bot sees it in triage but cannot claim it yet; the poster cannot self-accept
        triage = await _call(
            c,
            private_bot,
            "task.pull",
            {
                "org": org,
                "namespace": private_ns,
                "agent": "private-bot",
                "inbox": "private-bot",
                "status": "triage",
                "limit": 5,
            },
        )
        assert [t["task_id"] for t in triage["tasks"]] == [task["task_id"]]
        empty = await _call(
            c,
            private_bot,
            "task.pull",
            {"org": org, "namespace": private_ns, "agent": "private-bot", "inbox": "private-bot"},
        )
        assert empty["tasks"] == []
        await _call(
            c,
            public_bot,
            "task.accept",
            {"org": org, "namespace": public_ns, "agent": "public-bot", "task_id": task["task_id"]},
            expect=404,
        )
        accepted = await _call(
            c,
            admin,
            "task.accept",
            {"org": org, "namespace": private_ns, "agent": "alice", "task_id": task["task_id"]},
        )
        assert accepted["task"]["status"] == "todo"

        # two pulls race; exactly one owner
        a, b = await asyncio.gather(
            _call(
                c,
                private_bot,
                "task.pull",
                {"org": org, "namespace": private_ns, "agent": "bot-a", "inbox": "private-bot"},
            ),
            _call(
                c,
                private_bot,
                "task.pull",
                {"org": org, "namespace": private_ns, "agent": "bot-b", "inbox": "private-bot"},
            ),
        )
        winners = [t for t in a["tasks"] + b["tasks"]]
        assert len(winners) == 1 and winners[0]["status"] == "in_progress"
        owned = winners[0]
        assert owned["fence"] == 1
        assert a["backend"] in ("keeper", "log")

        # owner writes need the fence; a stale one is refused
        await _call(
            c,
            private_bot,
            "task.heartbeat",
            {
                "org": org,
                "namespace": private_ns,
                "agent": owned["owner"],
                "task_id": task["task_id"],
                "fence": 0,
            },
            expect=409,
        )
        hb = await _call(
            c,
            private_bot,
            "task.heartbeat",
            {
                "org": org,
                "namespace": private_ns,
                "agent": owned["owner"],
                "task_id": task["task_id"],
                "fence": owned["fence"],
            },
        )
        assert hb["task"]["lease_until"] is not None
        await _call(
            c,
            private_bot,
            "task.progress",
            {
                "org": org,
                "namespace": private_ns,
                "agent": owned["owner"],
                "task_id": task["task_id"],
                "fence": owned["fence"],
                "note": "reproduced on 26.8",
            },
        )
        done = await _call(
            c,
            private_bot,
            "task.complete",
            {
                "org": org,
                "namespace": private_ns,
                "agent": owned["owner"],
                "task_id": task["task_id"],
                "fence": owned["fence"],
                "outcome": {"issue_url": "https://example.invalid/private/1"},
            },
        )
        assert done["task"]["status"] == "done"

        # the sender learns the outcome from its cursor, in order, and the cursor
        # advances; rows sit behind a 3 s replication watermark, so a read returns
        # what has crossed it and the next read (long-polling) brings the rest
        events: list[str] = []
        cursor = 0
        for _ in range(6):
            upd = await _call(
                c,
                public_bot,
                "task.updates",
                {
                    "org": org,
                    "namespace": public_ns,
                    "agent": "public-bot",
                    "since_seq": cursor,
                    "wait_seconds": 10,
                },
            )
            events += [e["event"] for e in upd["events"]]
            cursor = upd["next_seq"]
            if events and events[-1] == "done":
                break
        assert events[0] == "created" and events[-1] == "done", events
        assert "accepted" in events and "claimed" in events
        later = await _call(
            c,
            public_bot,
            "task.updates",
            {
                "org": org,
                "namespace": public_ns,
                "agent": "public-bot",
                "since_seq": cursor,
                "wait_seconds": 0,
            },
        )
        assert later["events"] == []

        # the history is the audit trail; counts feed the board
        hist = await _call(
            c,
            admin,
            "task.history",
            {"org": org, "namespace": private_ns, "task_id": task["task_id"]},
        )
        assert [e["event"] for e in hist["events"]] == [
            "created",
            "accepted",
            "claimed",
            "heartbeat",
            "progress",
            "done",
        ]
        actors = {e["actor"] for e in hist["events"]}
        assert any(a.startswith("process:sa:public-bot") for a in actors)
        counts = await _call(
            c, admin, "task.counts", {"org": org, "namespace": private_ns, "inbox": "private-bot"}
        )
        assert [(x["status"], x["n"]) for x in counts["counts"]] == [("done", 1)]

        # a task the bot posts to itself is scheduled, not offered before its time
        own = await _call(
            c,
            private_bot,
            "task.create",
            {
                "org": org,
                "namespace": private_ns,
                "agent": "private-bot",
                "inbox": "private-bot",
                "kind": "retry_gates",
                "title": "retry PR 1 gates",
                "not_before": "2999-01-01T00:00:00Z",
            },
        )
        assert own["task"]["status"] == "todo"
        nothing = await _call(
            c,
            private_bot,
            "task.pull",
            {"org": org, "namespace": private_ns, "agent": "private-bot", "inbox": "private-bot"},
        )
        assert nothing["tasks"] == []


@pytest.mark.asyncio
async def test_long_poll_wakes_on_a_create(control_plane_url: str, mint_test_token) -> None:
    org = f"taskq-{uuid.uuid4().hex[:8]}"
    admin = mint_test_token(org=org, namespaces=["ns"], roles=["admin"], subject="alice")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        await _call(
            c,
            admin,
            "inbox.register",
            {"org": org, "namespace": "ns", "agent": "alice", "inbox": "bot", "owner_agent": "bot"},
        )

        async def _create_later() -> None:
            await asyncio.sleep(1.0)
            await _call(
                c,
                admin,
                "task.create",
                {
                    "org": org,
                    "namespace": "ns",
                    "agent": "bot",
                    "inbox": "bot",
                    "kind": "k",
                    "title": "t",
                },
            )

        pull = asyncio.create_task(
            _call(
                c,
                admin,
                "task.pull",
                {"org": org, "namespace": "ns", "agent": "bot", "inbox": "bot", "wait_seconds": 15},
            )
        )
        await _create_later()
        out = await pull
        assert len(out["tasks"]) == 1 and out["tasks"][0]["status"] == "in_progress"
        # woken well before the 15 s ceiling (an in-process wake-up or the 2 s re-read)
        assert out["waited_ms"] < 8000, out["waited_ms"]
        assert out["backend"] == "keeper"


@pytest.mark.slow
@pytest.mark.asyncio
async def test_watch_stream_delivers_a_create_within_seconds(
    control_plane_url: str, mint_test_token
) -> None:
    """The SSE wake-up: an owner-side stream on the
    inbox sees a task another coroutine creates. The event sits behind the
    3 s cursor watermark, so arrival is bounded by that plus a re-read."""
    import time

    org = f"taskq-{uuid.uuid4().hex[:8]}"
    admin = mint_test_token(org=org, namespaces=["ns"], roles=["admin"], subject="alice")
    stranger = mint_test_token(org=org, namespaces=["elsewhere"], roles=["writer"], subject="eve")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        await _call(
            c,
            admin,
            "inbox.register",
            {"org": org, "namespace": "ns", "agent": "alice", "inbox": "bot", "owner_agent": "bot"},
        )
        # a writer elsewhere in the org gets the same 404 a missing inbox gets
        r = await c.get(
            "/v1/task/watch", params={"org": org, "inbox": "bot"}, headers=_hdr(stranger)
        )
        assert r.status_code == 404, r.text[:200]
        r = await c.get("/v1/task/watch", params={"org": org, "inbox": "nope"}, headers=_hdr(admin))
        assert r.status_code == 404

        created_at: dict[str, float] = {}

        async def _create_later() -> None:
            await asyncio.sleep(0.5)
            created_at["t"] = time.monotonic()
            await _call(
                c,
                admin,
                "task.create",
                {
                    "org": org,
                    "namespace": "ns",
                    "agent": "bot",
                    "inbox": "bot",
                    "kind": "k",
                    "title": "t",
                },
            )

        creator = asyncio.create_task(_create_later())
        frames: list[str] = []
        first_event_ms: int | None = None
        async with c.stream(
            "GET",
            "/v1/task/watch",
            params={"org": org, "inbox": "bot", "max_seconds": 20, "heartbeat_seconds": 2},
            headers=_hdr(admin),
        ) as r:
            assert r.status_code == 200
            assert r.headers["content-type"].startswith("text/event-stream")
            assert r.headers.get("content-encoding") == "identity"
            assert r.headers.get("x-accel-buffering") == "no"
            async for line in r.aiter_lines():
                frames.append(line)
                if line.startswith("data: ") and first_event_ms is None:
                    first_event_ms = int((time.monotonic() - created_at["t"]) * 1000)
                    break
        await creator
        assert frames[0] == "retry: 3000"
        assert first_event_ms is not None and first_event_ms < 8000, (first_event_ms, frames)
        ids = [ln for ln in frames if ln.startswith("id: ")]
        assert len(ids) == 1
        payload = next(ln for ln in frames if ln.startswith("data: "))[len("data: ") :]
        import json

        ev = json.loads(payload)
        assert ev["event"] == "created" and ev["inbox"] == "bot" and ev["to_status"] == "todo"
        print(f"\nSSE: created → first frame {first_event_ms} ms (watermark 3 s + re-read)")

        # a reconnect with Last-Event-ID after the last frame sees nothing new and ends at the bound
        last_id = int(ids[0][len("id: ") :])
        started = time.monotonic()
        async with c.stream(
            "GET",
            "/v1/task/watch",
            params={"org": org, "inbox": "bot", "max_seconds": 1, "heartbeat_seconds": 1},
            headers={**_hdr(admin), "Last-Event-ID": str(last_id)},
        ) as r:
            body = "".join([chunk async for chunk in r.aiter_text()])
        assert "data: " not in body and "retry: 3000" in body
        assert time.monotonic() - started < 5

        # the poster side: the creator's own events, by subject
        async with c.stream(
            "GET",
            "/v1/task/watch",
            params={"org": org, "posted_by_me": 1, "max_seconds": 1},
            headers=_hdr(admin),
        ) as r:
            body = "".join([chunk async for chunk in r.aiter_text()])
        assert f"id: {last_id}" in body


@pytest.mark.asyncio
async def test_a_foreign_poster_naming_the_owner_agent_lands_in_triage(
    control_plane_url: str, mint_test_token
) -> None:
    """Trust follows the token, not the `agent` label. A
    listed poster outside the owner namespace that names the owner's agent is
    reviewed; the owner's own writer bot posting as that agent is not."""
    org = f"taskq-{uuid.uuid4().hex[:8]}"
    admin = mint_test_token(org=org, namespaces=["private"], roles=["admin"], subject="alice")
    private_bot = mint_test_token(
        org=org, namespaces=["private"], roles=["writer"], subject="sa:private-bot"
    )
    public_bot = mint_test_token(
        org=org, namespaces=["public"], roles=["writer"], subject="sa:public-bot"
    )
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        reg = await _call(
            c,
            admin,
            "inbox.register",
            {
                "org": org,
                "namespace": "private",
                "agent": "alice",
                "inbox": "private-bot",
                "owner_agent": "private-bot",
                "posters": ["sa:public-bot"],
            },
        )
        assert reg["inbox"]["owner_subject"] == "alice"
        body = {
            "org": org,
            "inbox": "private-bot",
            "kind": "probe",
            "title": "t",
            "agent": "private-bot",  # the owner's agent label
        }
        spoof = await _call(c, public_bot, "task.create", {**body, "namespace": "public"})
        assert spoof["task"]["status"] == "triage"
        own = await _call(
            c, private_bot, "task.create", {**body, "namespace": "private", "title": "own"}
        )
        assert own["task"]["status"] == "todo"


@pytest.mark.asyncio
async def test_a_poster_reusing_another_posters_key_gets_its_own_task(
    control_plane_url: str, mint_test_token
) -> None:
    # Two listed posters, neither able to read the inbox's namespace, send one key: the
    # second stores its own task and never sees the first one's title or brief.
    org = unique_org("taskkey")
    admin = mint_test_token(org=org, namespaces=["private"], roles=["admin"], subject="alice")
    bots = {
        name: mint_test_token(org=org, namespaces=[name], roles=["writer"], subject=f"sa:{name}")
        for name in ("bot-a", "bot-b")
    }
    secret = uuid.uuid4().hex
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        await _call(
            c,
            admin,
            "inbox.register",
            {
                "org": org,
                "namespace": "private",
                "agent": "alice",
                "inbox": "triage",
                "owner_agent": "triage",
                "posters": ["sa:bot-a", "sa:bot-b"],
                "accept_disclosure": ["confidential"],
            },
        )

        async def post(name: str, title: str, brief: dict) -> dict:
            return await _call(
                c,
                bots[name],
                "task.create",
                {
                    "org": org,
                    "namespace": name,
                    "agent": name,
                    "inbox": "triage",
                    "kind": "finding",
                    "title": title,
                    "brief": brief,
                    "disclosure": "confidential",
                    "idempotency_key": "finding-42",
                },
            )

        a = await post("bot-a", f"title {secret}", {"objective": f"brief {secret}"})
        b = await post("bot-b", "b's finding", {"objective": "b's brief"})
        assert a["created"] and b["created"]
        assert b["task"]["task_id"] != a["task"]["task_id"]
        assert b["task"]["from_subject"] == "sa:bot-b" and secret not in str(b)
        retry = await post("bot-b", "b's finding", {"objective": "b's brief"})
        assert not retry["created"] and retry["task"]["task_id"] == b["task"]["task_id"]


@pytest.mark.asyncio
async def test_a_reassign_into_an_inbox_the_caller_may_not_post_to_is_refused(
    control_plane_url: str, mint_test_token
) -> None:
    # W owns inbox `a` and may not post to inbox `x` nor read its namespace: moving a task
    # there answers as an unregistered name does, and the task stays where it was.
    org = unique_org("taskmove")
    w = mint_test_token(org=org, namespaces=["team-a"], roles=["admin", "writer"], subject="sa:w")
    x_admin = mint_test_token(org=org, namespaces=["team-x"], roles=["admin"], subject="xa")
    async with httpx.AsyncClient(base_url=control_plane_url, timeout=60) as c:
        for tok, ns, inbox in ((w, "team-a", "a"), (x_admin, "team-x", "x")):
            await _call(
                c,
                tok,
                "inbox.register",
                {
                    "org": org,
                    "namespace": ns,
                    "agent": "admin",
                    "inbox": inbox,
                    "owner_agent": inbox,
                    "accept_disclosure": ["internal"],
                },
            )
        ident = {"org": org, "namespace": "team-a", "agent": "w"}
        task = (
            await _call(
                c,
                w,
                "task.create",
                {**ident, "inbox": "a", "kind": "chore", "title": "t", "disclosure": "internal"},
            )
        )["task"]
        move = {**ident, "task_id": task["task_id"], "reason": "elsewhere"}
        refused = await _call(c, w, "task.reassign", {**move, "to_inbox": "x"}, expect=404)
        missing = await _call(c, w, "task.reassign", {**move, "to_inbox": "nope"}, expect=404)
        assert "RESOURCE_NOT_FOUND" in str(refused) and "RESOURCE_NOT_FOUND" in str(missing)
        got = await _call(c, w, "task.get", {**ident, "task_id": task["task_id"]})
        assert got["task"]["status"] == task["status"] and got["task"]["inbox"] == "a"
