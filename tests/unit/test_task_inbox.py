"""Task inbox plane — the state machine over an in-memory
repository, the trust policy, the lease arbitration, and the sweeper.

Every `task.*` / `inbox.*` operation is named here (the registry-coverage
test asks for that): inbox.register, inbox.list, inbox.policy_set,
inbox.policy_list, inbox.principals, task.resume, task.create, task.accept, task.pull, task.claim,
task.heartbeat, task.progress, task.defer, task.block, task.unblock,
task.complete, task.fail, task.wont_do, task.cancel, task.reopen,
task.update, task.reassign, task.get, task.list, task.counts, task.history,
task.updates. Loom stores the trust mode the owner sets and never moves it.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.tasks.api import models_task as wire
from server.tasks.api import ops_task
from server.tasks.app import common, inboxes, reads, sweep, transitions, trust
from server.tasks.store import repo as repo
from server.tasks.store.repo import ClaimResult, KeeperLeases, resolve_policy
from tests.unit.claims_fake import memory_claims

ORG = "acme"

# The modules the handlers live in. A name a test replaces is replaced in
# each module that binds it, so every caller sees the replacement.
HANDLER_MODULES = (common, trust, inboxes, transitions, reads, sweep)


def patch_handlers(monkeypatch: pytest.MonkeyPatch, name: str, value: Any) -> None:
    bound = [m for m in HANDLER_MODULES if hasattr(m, name)]
    assert bound, name
    for m in bound:
        monkeypatch.setattr(m, name, value)


# ------------------------------------------------------------ fakes


class FakeLeases:
    """The arbitration contract: one winner per fence, fence-gated writes."""

    name = "fake"

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.deleted: list[str] = []

    async def ensure_row(
        self, task_id: str, org: str, inbox: str, fence: int, *, reset_idle: bool = False
    ) -> bool:
        if task_id not in self.rows:
            self.rows[task_id] = {"owner": "", "fence": fence, "lease_until": None}
            return True
        row = self.rows[task_id]
        if reset_idle and row["owner"] == "" and row["fence"] < fence:
            row["fence"] = fence
        return False

    async def try_claim(self, task_id, claimant, *, expected_fence, lease_until) -> ClaimResult:
        row = self.rows.get(task_id)
        if row is None:  # no lease row: nothing to win
            return ClaimResult(False, expected_fence, "")
        expired = row.get("lease_until") is not None and row["lease_until"] < datetime.now(
            UTC
        ).replace(tzinfo=None)
        if row["fence"] == expected_fence and (row["owner"] == "" or expired):
            row.update({"owner": claimant, "fence": expected_fence + 1, "lease_until": lease_until})
            return ClaimResult(True, row["fence"], claimant)
        return ClaimResult(False, row["fence"], row["owner"])

    async def give_back(self, task_id, holder, *, held_fence, fence) -> bool:
        row = self.rows.get(task_id)
        if row is not None and row["owner"] == holder and row["fence"] == held_fence:
            row.update({"owner": "", "fence": fence, "lease_until": None})
        return row is not None and row["owner"] == "" and row["fence"] == fence

    async def set_owner(self, task_id, *, fence, owner, lease_until, status) -> bool:
        row = self.rows.get(task_id)
        if row is None or row["fence"] != fence:
            return False
        row.update({"owner": owner, "lease_until": lease_until})
        return True

    async def delete(self, task_id: str) -> None:
        self.rows.pop(task_id, None)
        self.deleted.append(task_id)

    async def delete_held(self, task_id: str, holder: str, *, fence: int) -> bool:
        row = self.rows.get(task_id)
        if row is not None and row["owner"] == holder and row["fence"] == fence:
            await self.delete(task_id)
        return task_id not in self.rows


class FakeRepo:
    """The repository surface the handlers use, over dicts. Version rows
    replace by key like ReplacingMergeTree FINAL would."""

    TASK_FIELDS = repo.TASK_FIELDS
    next_version = staticmethod(repo.next_version)

    async def server_version(self, floor=0):
        return max(repo.next_version(), int(floor) + 1)

    resolve_policy = staticmethod(repo.resolve_policy)
    create_key_digest = staticmethod(repo.create_key_digest)

    def __init__(self) -> None:
        self.inboxes: dict[tuple[str, str], dict[str, Any]] = {}
        self.policies: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        self.tasks: dict[str, dict[str, Any]] = {}
        self.events: list[dict[str, Any]] = []
        self.leases = FakeLeases()

    # inboxes
    async def fetch_inbox_row(self, org, inbox):
        row = self.inboxes.get((org, inbox))
        return None if row is None or row.get("is_deleted") else dict(row)

    async def list_inboxes(self, org, *, namespace=None, owner_agent=None, limit=200):
        rows = [r for (o, _), r in self.inboxes.items() if o == org and not r.get("is_deleted")]
        if namespace:
            rows = [r for r in rows if r["owner_namespace"] == namespace]
        return [dict(r) for r in rows][:limit]

    async def insert_inbox_version(self, row):
        self.inboxes[(row["org"], row["inbox"])] = dict(row)

    # policies
    async def fetch_policies(self, org, inbox):
        return [
            dict(r)
            for (o, i, _, _), r in self.policies.items()
            if o == org and i == inbox and not r.get("is_deleted")
        ]

    async def insert_policy_version(self, row):
        self.policies[(row["org"], row["inbox"], row["poster"], row["kind"])] = dict(row)

    async def count_created_since(self, org, inbox, from_subject, kind, since):
        return sum(
            1
            for e in self.events
            if e["org"] == org
            and e["inbox"] == inbox
            and e["event"] == "created"
            and e["from_subject"] == from_subject
            and json.loads(e["patch"]).get("kind") == kind
        )

    # tasks
    async def fetch_task(self, org, task_id):
        row = self.tasks.get(str(task_id))
        return None if row is None or row["org"] != org or row.get("is_deleted") else dict(row)

    async def fetch_task_by_idempotency_key(self, org, inbox, subject, key):
        await asyncio.sleep(0)  # a racing create runs meanwhile
        for r in self.tasks.values():
            if (r["org"], r["inbox"], r["from_subject"], r.get("idempotency_key")) == (
                org,
                inbox,
                subject,
                key,
            ):
                return dict(r)
        return None

    async def insert_task_version(self, row):
        self.tasks[str(row["task_id"])] = dict(row)

    async def insert_created_task(self, row):
        # Storage dedups the key's create token: a second task under the key is dropped.
        prior = await self.fetch_task_by_idempotency_key(
            row["org"], row["inbox"], row["from_subject"], row["idempotency_key"]
        )
        if prior is not None:
            return prior
        await self.insert_task_version(row)
        return dict(row)

    async def list_tasks(
        self,
        org,
        *,
        inbox=None,
        namespace=None,
        statuses=None,
        kinds=None,
        from_subject=None,
        owner=None,
        limit=100,
        fields=None,
        newest_first=False,
    ):
        rows = [r for r in self.tasks.values() if r["org"] == org and not r.get("is_deleted")]
        if inbox:
            rows = [r for r in rows if r["inbox"] == inbox]
        if statuses:
            rows = [r for r in rows if r["status"] in statuses]
        if kinds:
            rows = [r for r in rows if r["kind"] in kinds]
        if from_subject:
            rows = [r for r in rows if r["from_subject"] == from_subject]
        if owner:
            rows = [r for r in rows if r["owner"] == owner]
        if newest_first:
            rows.sort(key=lambda r: r["created_at"], reverse=True)
        else:
            rows.sort(key=lambda r: (r["priority"], r["created_at"]))
        out = [dict(r) for r in rows[:limit]]
        if fields:
            keep = set(fields) | {"org", "inbox", "task_id", "status", "version"}
            out = [{k: v for k, v in r.items() if k in keep} for r in out]
        return out

    async def claimable_candidates(self, org, inbox, *, kinds, now, limit):
        rows = await self.list_tasks(org, inbox=inbox, statuses=["todo"], kinds=kinds, limit=limit)
        return [r for r in rows if r.get("not_before") is None or r["not_before"] <= now]

    async def sweep_candidates(self, *, now, limit):
        expired = [
            dict(r)
            for r in self.tasks.values()
            if r["status"] == "in_progress" and r.get("lease_until") and r["lease_until"] < now
        ]
        resume = [
            dict(r)
            for r in self.tasks.values()
            if r["status"] == "deferred" and r.get("resume_at") and r["resume_at"] <= now
        ]
        stale = []
        for r in self.tasks.values():
            if r["status"] != "triage":
                continue
            inbox = self.inboxes.get((r["org"], r["inbox"]))
            after = int((inbox or {}).get("triage_escalate_after_seconds") or 14_400)
            if r["created_at"] + timedelta(seconds=after) < now and "escalated_at" not in (
                r.get("outcome") or ""
            ):
                stale.append({**r, "after_s": after})
        return {"expired": expired, "resume": resume, "stale_triage": stale}

    async def lease_strays(self, *, before, terminal, limit):
        # The todo and triage tasks with no lease row; no stray rows.
        missing = [
            {"task_id": tid, "org": r["org"], "inbox": r["inbox"], "fence": int(r["fence"])}
            for tid, r in self.tasks.items()
            if r["status"] in ("todo", "triage") and tid not in self.leases.rows
        ]
        return missing[:limit], []

    async def inbox_counts(self, org, *, namespace=None, inbox=None):
        out: dict[tuple[str, str, str], int] = {}
        for r in self.tasks.values():
            if r["org"] != org or r.get("is_deleted"):
                continue
            if inbox and r["inbox"] != inbox:
                continue
            if namespace and r["namespace"] != namespace:
                continue
            key = (r["inbox"], r["status"], r.get("blocked_on") or "")
            out[key] = out.get(key, 0) + 1
        return [
            {"inbox": i, "status": s, "blocked_on": b, "n": n, "oldest": None}
            for (i, s, b), n in out.items()
        ]

    # events
    async def insert_event(self, row):
        self.events.append(dict(row))

    async def task_history(self, org, task_id, *, limit=500, consistent=False):
        return [e for e in self.events if e["org"] == org and e["task_id"] == str(task_id)][:limit]

    async def events_since(
        self, org, *, since_seq, lag_s, inbox=None, from_subject=None, limit=200
    ):
        rows = [e for e in self.events if e["org"] == org and e["seq"] > since_seq]
        if inbox:
            rows = [e for e in rows if e["inbox"] == inbox]
        if from_subject:
            rows = [e for e in rows if e["from_subject"] == from_subject]
        return rows[:limit]

    # principals
    service_accounts: list[dict[str, Any]] = []
    grants: list[dict[str, Any]] = []
    last_seen: dict[str, Any] = {}

    async def list_service_accounts(self, org):
        return [dict(r) for r in self.service_accounts]

    async def list_grant_subjects(self, org):
        return [dict(r) for r in self.grants]

    async def subjects_last_seen(self, org):
        return dict(self.last_seen)

    async def lease_backend(self):
        return self.leases


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeRepo:
    fr = FakeRepo()
    patch_handlers(monkeypatch, "repo", fr)
    memory_claims(monkeypatch)
    monkeypatch.setattr("server.tasks.domain.cursors.LAG_SECONDS", -1)  # the fake has no replicas
    return fr


def _caller(namespace: str, agent: str = "-") -> dict[str, Any]:
    return {"org": ORG, "namespace": namespace, "agent": agent}


async def _register(fake: FakeRepo, inbox: str = "private-bot", owner_ns: str = "private") -> Any:
    return await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            **_caller(owner_ns, "admin"),
            inbox=inbox,
            owner_agent="private-bot",
            posters=["sa:public-bot"],
            accept_disclosure=["confidential", "internal"],
            default_lease_seconds=60,
            max_attempts=2,
            max_defers=2,
        )
    )


async def _post(fake: FakeRepo, **over: Any) -> Any:
    body: dict[str, Any] = {
        **_caller("public", "public-bot"),
        "inbox": "private-bot",
        "kind": "security_finding",
        "title": "UB in Parser",
        "brief": {"objective": "file it privately", "inputs": {"finding_id": "f-1"}},
        "disclosure": "confidential",
        "from_subject": "sa:public-bot",
    }
    body.update(over)
    return await transitions.handle_task_create(wire.TaskCreateInput(**body))


# ------------------------------------------------------------ registry


def test_every_task_and_inbox_op_is_registered_and_experimental() -> None:
    names = {
        "inbox.register",
        "inbox.list",
        "inbox.policy_set",
        "inbox.policy_list",
        "inbox.principals",
        "task.create",
        "task.accept",
        "task.pull",
        "task.claim",
        "task.heartbeat",
        "task.progress",
        "task.defer",
        "task.block",
        "task.unblock",
        "task.resume",
        "task.complete",
        "task.fail",
        "task.wont_do",
        "task.cancel",
        "task.reopen",
        "task.update",
        "task.reassign",
        "task.get",
        "task.list",
        "task.counts",
        "task.history",
        "task.updates",
    }
    assert names <= set(OPERATIONS_BY_NAME)
    assert {o.name for o in ops_task.OPERATIONS} == names
    for name in names:
        op = OPERATIONS_BY_NAME[name]
        assert op.maturity == "experimental"
        assert op in ALL_OPERATIONS
    # long polls declare their hold in the budget
    assert OPERATIONS_BY_NAME["task.pull"].budget_p99_ms >= wire.MAX_WAIT_SECONDS * 1000
    assert OPERATIONS_BY_NAME["task.updates"].budget_p99_ms >= wire.MAX_WAIT_SECONDS * 1000
    # reads are viewer + idempotent (the readonly toolset rule)
    for name in ("task.get", "task.list", "task.counts", "task.history", "task.updates"):
        assert OPERATIONS_BY_NAME[name].idempotent
        assert OPERATIONS_BY_NAME[name].required_role.name == "VIEWER"


# ------------------------------------------------------------ policy resolution


def test_resolve_policy_prefers_the_most_specific_row() -> None:
    rows = [
        {"poster": "*", "kind": "*", "mode": "review"},
        {"poster": "*", "kind": "gap", "mode": "auto"},
        {"poster": "sa:pub", "kind": "*", "mode": "deny"},
        {"poster": "sa:pub", "kind": "gap", "mode": "review"},
        {"poster": "pub-agent", "kind": "bug", "mode": "auto"},
    ]
    pick = lambda s, a, k: resolve_policy(rows, poster_subject=s, poster_agent=a, kind=k)  # noqa: E731
    assert pick("sa:pub", "pub-agent", "gap")["mode"] == "review"
    assert pick("sa:pub", "pub-agent", "bug")["mode"] == "deny"  # subject row beats agent row
    assert pick("", "pub-agent", "bug")["mode"] == "auto"
    assert pick("nobody", "x", "gap")["mode"] == "auto"
    assert pick("nobody", "x", "other")["mode"] == "review"
    assert resolve_policy([], poster_subject="a", poster_agent="b", kind="c") is None


def test_projection_keeps_identity_columns_and_drops_unknown_names() -> None:
    cols = repo._projection(["title", "drop table", "nope"], repo.TASK_COLS, repo.TASK_FIELDS)
    assert cols == "title, org, inbox, task_id, status, version"
    assert repo._projection(None, repo.TASK_COLS, repo.TASK_FIELDS) == repo.TASK_COLS


# ------------------------------------------------------------ create + trust


@pytest.mark.asyncio
async def test_a_foreign_poster_lands_in_triage_and_the_owner_lands_in_todo(fake: FakeRepo) -> None:
    await _register(fake)
    out = await _post(fake)
    assert out.created and out.task.status == "triage"
    assert out.next.startswith("status=triage") and "task.accept" in out.next
    assert out.task.namespace == "private"  # the inbox's owner namespace, not the poster's
    assert fake.events[-1]["event"] == "created" and fake.events[-1]["to_status"] == "triage"
    own = await transitions.handle_task_create(
        wire.TaskCreateInput(
            **_caller("private", "private-bot"),
            inbox="private-bot",
            kind="retry_gates",
            title="retry later",
            not_before=datetime.now(UTC) + timedelta(hours=2),
        )
    )
    assert own.task.status == "todo" and own.next == ""
    # scheduled for later: not offered to a pull yet
    pulled = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "private-bot"), inbox="private-bot", limit=5)
    )
    assert pulled.tasks == []


@pytest.mark.asyncio
async def test_disclosure_gate_and_deny_policy_refuse_at_create(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(fake)
    with pytest.raises(HTTPException) as e:
        await _post(fake, disclosure="public_ok")
    assert e.value.status_code == 422
    await inboxes.handle_inbox_policy_set(
        wire.InboxPolicySetInput(
            **_caller("private", "admin"), inbox="private-bot", poster="sa:public-bot", mode="deny"
        )
    )
    with pytest.raises(HTTPException) as e:
        await _post(fake)
    assert e.value.detail["code"] == "TASK_POSTER_DENIED"
    # someone not listed as a poster and without the namespace is refused, with the answer
    # an unregistered inbox gets (a 403 would confirm it exists)
    fake.inboxes[(ORG, "private-bot")]["posters"] = []

    async def _no_scope(org, ns, *, write, hides_answer=True):
        return False

    patch_handlers(monkeypatch, "_in_scope", _no_scope)
    with pytest.raises(HTTPException) as e:
        await _post(fake)
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_required_evidence_is_a_shape_check_and_the_mode_is_the_owners(
    fake: FakeRepo,
) -> None:
    await _register(fake)
    await inboxes.handle_inbox_policy_set(
        wire.InboxPolicySetInput(
            **_caller("private", "admin"),
            inbox="private-bot",
            poster="sa:public-bot",
            kind="security_finding",
            mode="auto",
            require_evidence=["inputs.finding_id", "inputs.test"],
        )
    )
    with pytest.raises(HTTPException) as e:
        await _post(fake)
    assert e.value.detail["missing_evidence"] == ["inputs.test"]
    ok_brief = {"inputs": {"finding_id": "f-1", "test": "02999_x.sql"}}
    first = await _post(fake, brief=ok_brief)
    assert first.task.status == "todo"  # the owner said auto; loom does not second-guess it
    for _ in range(5):
        assert (await _post(fake, brief=ok_brief, title="another")).task.status == "todo"
    # and the mode never moves on its own: still exactly what the owner wrote
    policy = fake.policies[(ORG, "private-bot", "sa:public-bot", "security_finding")]
    assert policy["mode"] == "auto" and policy["set_by"] != "policy:promotion"


@pytest.mark.asyncio
async def test_idempotency_key_returns_the_first_task(fake: FakeRepo) -> None:
    await _register(fake)
    a = await _post(fake, idempotency_key="k1")
    b = await _post(fake, idempotency_key="k1", title="retried")
    assert a.created and not b.created and a.task.task_id == b.task.task_id


@pytest.mark.asyncio
async def test_another_posters_idempotency_key_stores_its_own_task(fake: FakeRepo) -> None:
    await _register(fake)
    fake.inboxes[(ORG, "private-bot")]["posters"] = ["sa:public-bot", "sa:other-bot"]
    a = await _post(fake, idempotency_key="k1", brief={"objective": "a's secret"})
    b = await _post(
        fake, idempotency_key="k1", from_subject="sa:other-bot", brief={"objective": "b's"}
    )
    assert a.created and b.created and a.task.task_id != b.task.task_id
    assert b.task.from_subject == "sa:other-bot" and b.task.brief == {"objective": "b's"}
    again = await _post(fake, idempotency_key="k1", from_subject="sa:other-bot")
    assert not again.created and again.task.task_id == b.task.task_id


@pytest.mark.asyncio
async def test_a_create_cut_off_before_its_lease_row_is_claimable_after_one_reconcile(
    fake: FakeRepo,
) -> None:
    """A create cut off after its task row: a retry writes no lease row (it reads a
    status it cannot hold), and the lease reconcile writes the missing one."""
    from server.tasks.app import sweep

    await _register(fake)
    a = await _post(fake, idempotency_key="k1")
    tid = a.task.task_id
    fake.leases.rows.pop(tid)
    await _post(fake, idempotency_key="k1")
    assert tid not in fake.leases.rows
    assert await sweep.reconcile_leases(datetime.now(UTC).replace(tzinfo=None), limit=10) == 1
    assert fake.leases.rows[tid]["owner"] == "" and fake.leases.rows[tid]["fence"] == 0


@pytest.mark.asyncio
async def test_a_retried_create_writes_the_created_event_a_cut_off_create_left_out(
    fake: FakeRepo,
) -> None:
    await _register(fake)
    a = await _post(fake, idempotency_key="k1")
    tid = a.task.task_id
    first = fake.events.pop()
    assert first["event"] == "created"
    # A create cut off after its task row: the retry logs the created event it missed,
    # as the create would have, and only once.
    await _post(fake, idempotency_key="k1")
    await _post(fake, idempotency_key="k1")
    events = [e for e in fake.events if e["task_id"] == tid]
    assert [e["event"] for e in events] == ["created"]
    keep = ("from_status", "to_status", "actor", "agent", "from_subject", "fence")
    assert {k: events[0][k] for k in keep} == {k: first[k] for k in keep}
    # A claimed task is not a create's to finish.
    fake.events.clear()
    fake.tasks[tid].update(status="in_progress", fence=1)
    await _post(fake, idempotency_key="k1")
    assert fake.events == []


@pytest.mark.asyncio
async def test_a_backfilled_created_event_with_nothing_after_it_reaches_cursor_readers(
    fake: FakeRepo,
) -> None:
    # A reader whose cursor already passed the task's version must still be handed the
    # created event the retry writes, so it gets a fresh seq.
    await _register(fake)
    a = await _post(fake, idempotency_key="k1")
    fake.events.clear()
    cursor = await fake.server_version()
    await _post(fake, idempotency_key="k1")
    delivered = await fake.events_since(ORG, since_seq=cursor, lag_s=0)
    assert [(e["event"], e["task_id"]) for e in delivered] == [("created", a.task.task_id)]


@pytest.mark.asyncio
async def test_a_create_that_lost_the_insert_race_leaves_the_event_to_the_winner(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The winner stored its task and is still running: it writes its own created event.
    await _register(fake)
    a = await _post(fake, idempotency_key="k1")
    fake.events.clear()

    async def missed(org, inbox, subject, key):
        return None  # the loser's lease ran out, so its lookup ran before the winner stored

    winner = dict(fake.tasks[a.task.task_id])

    async def lost(row):
        return dict(winner)

    monkeypatch.setattr(fake, "fetch_task_by_idempotency_key", missed)
    monkeypatch.setattr(fake, "insert_created_task", lost)
    b = await _post(fake, idempotency_key="k1", title="raced")
    assert not b.created and b.task.task_id == a.task.task_id
    assert fake.events == []


@pytest.mark.asyncio
async def test_a_backfilled_created_event_lands_before_the_events_logged_since(
    fake: FakeRepo,
) -> None:
    # The triaged task was accepted before the retry came: the created event it gets
    # lands the task in triage, ahead of the accept.
    await _register(fake)
    a = await _post(fake, idempotency_key="k1", title="new kind")
    assert a.task.status == "triage"
    fake.events.clear()
    await transitions.handle_task_accept(
        wire.TaskAcceptInput(**_caller("private", "private-bot"), task_id=a.task.task_id)
    )
    await _post(fake, idempotency_key="k1")
    events = sorted(fake.events, key=lambda e: e["seq"])
    assert [(e["event"], e["from_status"], e["to_status"]) for e in events] == [
        ("created", "", "triage"),
        ("accepted", "triage", "todo"),
    ]


@pytest.mark.asyncio
async def test_racing_creates_with_one_key_answer_with_the_task_that_landed(
    fake: FakeRepo,
) -> None:
    await _register(fake)
    # The key lookup yields, so without the key's lease both creates would
    # miss it before either stored its task.
    a, b = await asyncio.gather(
        _post(fake, idempotency_key="k1"), _post(fake, idempotency_key="k1", title="raced")
    )
    assert a.created and not b.created and b.task.task_id == a.task.task_id
    assert len(fake.tasks) == 1
    assert [e["task_id"] for e in fake.events] == [a.task.task_id]
    assert list(fake.leases.rows) == [a.task.task_id]


@pytest.mark.asyncio
async def test_a_create_whose_key_lease_is_held_or_unavailable_stores_nothing(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core import leases
    from server.core.errors import PreconditionFailedError

    await _register(fake)

    async def still_held(name: str, **_: Any) -> None:
        return None  # another create kept the key past the wait

    monkeypatch.setattr(leases, "take", still_held)
    with pytest.raises(PreconditionFailedError):
        await _post(fake, idempotency_key="k1")

    async def keeper_down(name: str, **_: Any) -> None:
        raise leases.LeaseUnavailable("the lease store did not answer", operation=name)

    monkeypatch.setattr(leases, "take", keeper_down)
    with pytest.raises(leases.LeaseUnavailable) as e:
        await _post(fake, idempotency_key="k1")
    assert e.value.status_code == 503
    assert not fake.tasks and not fake.events
    # a create without a key takes no lease
    assert (await _post(fake)).created


# ------------------------------------------------------------ accept + promotion


@pytest.mark.asyncio
async def test_accept_moves_to_todo_and_a_poster_cannot_self_accept(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(fake)
    t = (await _post(fake)).task
    # the poster's own token: refused
    patch_handlers(monkeypatch, "_caller_subject", lambda fallback="": fallback or "sa:public-bot")
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_accept(
            wire.TaskAcceptInput(**_caller("private", "x"), task_id=t.task_id)
        )
    assert e.value.detail["code"] == "TASK_SELF_ACCEPT"
    # a person on the owner side: accepted, with an edit on the way in
    patch_handlers(monkeypatch, "_caller_subject", lambda fallback="": fallback or "human:alice")
    out = await transitions.handle_task_accept(
        wire.TaskAcceptInput(**_caller("private", "alice"), task_id=t.task_id, priority=1)
    )
    assert out.task.status == "todo" and out.task.priority == 1
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_accept(
            wire.TaskAcceptInput(**_caller("private", "alice"), task_id=t.task_id)
        )
    assert e.value.detail["code"] == "TASK_NOT_IN_TRIAGE"


# ------------------------------------------------------------ claim, fence, lease


@pytest.mark.asyncio
async def test_pull_claims_once_and_owner_writes_need_the_fence(fake: FakeRepo) -> None:
    await _register(fake)
    t = (await _post(fake, **_caller("private", "private-bot"), from_subject="")).task
    assert t.status == "todo"
    a = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot-a"), inbox="private-bot")
    )
    b = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot-b"), inbox="private-bot")
    )
    assert len(a.tasks) == 1 and b.tasks == []
    claimed = a.tasks[0]
    assert claimed.status == "in_progress" and claimed.owner == "bot-a" and claimed.fence == 1
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_heartbeat(
            wire.TaskHeartbeatInput(**_caller("private", "bot-a"), task_id=t.task_id, fence=0)
        )
    assert e.value.detail["code"] == "TASK_FENCE_STALE"
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_progress(
            wire.TaskProgressInput(
                **_caller("private", "bot-b"), task_id=t.task_id, fence=1, note="hi"
            )
        )
    assert e.value.detail["code"] == "TASK_OWNED_ELSEWHERE"
    hb = await transitions.handle_task_heartbeat(
        wire.TaskHeartbeatInput(**_caller("private", "bot-a"), task_id=t.task_id, fence=1)
    )
    assert hb.task.lease_until is not None
    done = await transitions.handle_task_complete(
        wire.TaskCompleteInput(
            **_caller("private", "bot-a"), task_id=t.task_id, fence=1, outcome={"pr": 7}
        )
    )
    assert done.task.status == "done" and done.task.outcome == {"pr": 7}
    assert fake.leases.deleted == [t.task_id]
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_claim(
            wire.TaskClaimInput(**_caller("private", "bot-b"), task_id=t.task_id)
        )
    assert e.value.detail["code"] == "TASK_NOT_CLAIMABLE"


@pytest.mark.asyncio
async def test_a_draining_process_ends_a_long_pull(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stopping pod answers its long polls at once, inside the shutdown
    window, instead of holding them until uvicorn cancels them."""
    import time

    await _register(fake)
    monkeypatch.setattr("server.core.readiness.draining", lambda: True)
    started = time.monotonic()
    out = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot-a"), inbox="private-bot", wait_seconds=30)
    )
    assert out.tasks == [] and time.monotonic() - started < 1


@pytest.mark.asyncio
async def test_a_pull_cancelled_after_its_claim_puts_the_task_back(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claim won before the cancel is returned, so the task is not leased
    to a caller that never got the answer."""
    import asyncio

    await _register(fake)
    t = (await _post(fake, **_caller("private", "private-bot"), from_subject="")).task
    claim_one = transitions._claim_one
    won = asyncio.Event()

    async def slow_claim(*args: Any, **kwargs: Any) -> Any:
        got = await claim_one(*args, **kwargs)
        won.set()
        await asyncio.sleep(0.05)
        return got

    monkeypatch.setattr(transitions, "_claim_one", slow_claim)
    pull = asyncio.ensure_future(
        transitions.handle_task_pull(
            wire.TaskPullInput(**_caller("private", "bot-a"), inbox="private-bot")
        )
    )
    await won.wait()
    pull.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pull
    assert [e["event"] for e in fake.events][-2:] == ["claimed", "claim_returned"]
    monkeypatch.setattr(transitions, "_claim_one", claim_one)
    again = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot-b"), inbox="private-bot")
    )
    assert [x.task_id for x in again.tasks] == [t.task_id] and again.tasks[0].owner == "bot-b"


@pytest.mark.asyncio
async def test_defer_block_unblock_wont_do_reopen_carry_reasons(fake: FakeRepo) -> None:
    await _register(fake)
    t = (await _post(fake, **_caller("private", "private-bot"), from_subject="")).task
    got = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot"), inbox="private-bot")
    )
    fence = got.tasks[0].fence
    resume = datetime.now(UTC) + timedelta(hours=2)
    d = await transitions.handle_task_defer(
        wire.TaskDeferInput(
            **_caller("private", "bot"),
            task_id=t.task_id,
            fence=fence,
            reason="binary missing",
            resume_at=resume,
        )
    )
    assert d.task.status == "deferred" and d.task.reason == "binary missing" and d.task.owner == ""
    # a person resumes it ahead of resume_at; then park it again for the defer-cap check
    r0 = await transitions.handle_task_resume(
        wire.TaskResumeInput(
            **_caller("private", "alice"), task_id=t.task_id, note="binary is here"
        )
    )
    assert r0.task.status == "todo" and r0.task.resume_at is None and r0.task.reason == ""
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_resume(
            wire.TaskResumeInput(**_caller("private", "alice"), task_id=t.task_id)
        )
    assert e.value.detail["code"] == "TASK_NOT_DEFERRED"
    fake.tasks[t.task_id]["status"] = "deferred"
    # a second defer beyond max_defers (2) blocks on a human instead
    fake.tasks[t.task_id]["status"] = "todo"
    fake.tasks[t.task_id]["defer_count"] = 2
    d2 = await transitions.handle_task_defer(
        wire.TaskDeferInput(
            **_caller("private", "bot"), task_id=t.task_id, fence=0, reason="still missing"
        )
    )
    assert d2.task.status == "blocked" and d2.task.blocked_on == "human"
    u = await transitions.handle_task_unblock(
        wire.TaskUnblockInput(**_caller("private", "alice"), task_id=t.task_id, note="built it")
    )
    assert u.task.status == "todo" and u.task.blocked_on == ""
    b = await transitions.handle_task_block(
        wire.TaskBlockInput(
            **_caller("private", "bot"),
            task_id=t.task_id,
            fence=0,
            reason="need repro",
            blocked_on="sender",
        )
    )
    assert b.task.status == "blocked" and b.task.blocked_on == "sender"
    w = await transitions.handle_task_wont_do(
        wire.TaskReasonInput(**_caller("private", "alice"), task_id=t.task_id, reason="not a bug")
    )
    assert w.task.status == "wont_do" and w.task.reason == "not a bug"
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_cancel(
            wire.TaskReasonInput(**_caller("private", "alice"), task_id=t.task_id, reason="x")
        )
    assert e.value.detail["code"] == "TASK_TERMINAL"
    r = await transitions.handle_task_reopen(
        wire.TaskReasonInput(**_caller("private", "alice"), task_id=t.task_id, reason="it is a bug")
    )
    assert r.task.status == "todo"
    events = [e["event"] for e in fake.events if e["task_id"] == t.task_id]
    assert events == [
        "created",
        "claimed",
        "deferred",
        "resumed",
        "blocked",
        "unblocked",
        "blocked",
        "wont_do",
        "reopened",
    ]


@pytest.mark.asyncio
async def test_update_needs_the_current_version_and_answers_with_the_row(fake: FakeRepo) -> None:
    await _register(fake)
    t = (await _post(fake)).task
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_update(
            wire.TaskUpdateInput(
                **_caller("private", "alice"), task_id=t.task_id, if_match_version=1, title="x"
            )
        )
    assert e.value.detail["code"] == "PRECONDITION_FAILED"
    assert e.value.detail["current_version"] == t.version
    out = await transitions.handle_task_update(
        wire.TaskUpdateInput(
            **_caller("private", "alice"),
            task_id=t.task_id,
            if_match_version=t.version,
            title="UB in Parser (confirmed)",
            brief={"objective": "file it", "constraints": "no public repro"},
        )
    )
    assert (
        out.task.title.endswith("(confirmed)")
        and out.task.brief["constraints"] == "no public repro"
    )
    assert out.task.version > t.version


@pytest.mark.asyncio
async def test_reassign_cancels_here_and_posts_a_child_there(fake: FakeRepo) -> None:
    await _register(fake)
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            **_caller("public", "admin"),
            inbox="public-bot",
            owner_agent="public-reviewer",
            accept_disclosure=["public_ok", "internal", "confidential"],
        )
    )
    t = (await _post(fake)).task
    out = await transitions.handle_task_reassign(
        wire.TaskReassignInput(
            **_caller("private", "alice"),
            task_id=t.task_id,
            to_inbox="public-bot",
            reason="upstream",
        )
    )
    assert out.task.status == "cancelled"
    assert out.replacement.inbox == "public-bot" and out.replacement.parent_task_id == t.task_id
    assert out.replacement.status == "triage"  # the original poster is foreign there too


@pytest.mark.asyncio
async def test_a_reassign_into_an_inbox_the_caller_may_not_post_to_is_refused(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(fake)
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            **_caller("public", "admin"),
            inbox="public-bot",
            owner_agent="public-reviewer",
            accept_disclosure=["public_ok", "internal", "confidential"],
        )
    )
    t = (await _post(fake)).task
    readable = {"private"}

    async def scoped(org, ns, *, write, hides_answer=True):
        return ns in readable and (not write or ns == "private")

    patch_handlers(monkeypatch, "_in_scope", scoped)

    async def reassign() -> None:
        await transitions.handle_task_reassign(
            wire.TaskReassignInput(
                **_caller("private", "alice"), task_id=t.task_id, to_inbox="public-bot", reason="r"
            )
        )

    # Read but no write there: refused as a poster, and the task stays where it is.
    readable.add("public")
    with pytest.raises(HTTPException) as e:
        await reassign()
    assert e.value.status_code == 403
    assert [r["task_id"] for r in fake.tasks.values()] == [t.task_id]
    assert fake.tasks[t.task_id]["status"] == t.status
    # Not a poster there and no read on its namespace: the unregistered-name answer.
    readable.discard("public")
    with pytest.raises(HTTPException) as e:
        await reassign()
    del fake.inboxes[(ORG, "public-bot")]
    with pytest.raises(HTTPException) as unregistered:
        await reassign()
    assert e.value.status_code == unregistered.value.status_code == 404
    assert e.value.detail == unregistered.value.detail
    assert [r["task_id"] for r in fake.tasks.values()] == [t.task_id]
    assert fake.tasks[t.task_id]["status"] == t.status


@pytest.mark.asyncio
async def test_a_reassign_the_destination_refuses_leaves_the_task_in_place(
    fake: FakeRepo,
) -> None:
    await _register(fake)
    await inboxes.handle_inbox_register(
        wire.InboxRegisterInput(
            **_caller("public", "admin"),
            inbox="public-bot",
            owner_agent="public-reviewer",
            accept_disclosure=["public_ok", "internal", "confidential"],
        )
    )
    await inboxes.handle_inbox_policy_set(
        wire.InboxPolicySetInput(
            **_caller("public", "admin"), inbox="public-bot", poster="sa:public-bot", mode="deny"
        )
    )
    t = (await _post(fake)).task
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_reassign(
            wire.TaskReassignInput(
                **_caller("private", "alice"), task_id=t.task_id, to_inbox="public-bot", reason="r"
            )
        )
    assert e.value.detail["code"] == "TASK_POSTER_DENIED"
    got = await reads.handle_task_get(wire.TaskGetInput(**_caller("private"), task_id=t.task_id))
    assert got.task is not None and got.task.status == t.status  # not cancelled
    hist = await reads.handle_task_history(
        wire.TaskHistoryInput(**_caller("private"), task_id=t.task_id)
    )
    assert [ev.event for ev in hist.events] == ["created"]


# ------------------------------------------------------------ reads + cursor


@pytest.mark.asyncio
async def test_reads_answer_found_false_out_of_scope_and_the_cursor_advances(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(fake)
    t = (await _post(fake)).task
    got = await reads.handle_task_get(wire.TaskGetInput(**_caller("private"), task_id=t.task_id))
    assert got.found and got.task is not None
    hist = await reads.handle_task_history(
        wire.TaskHistoryInput(**_caller("private"), task_id=t.task_id)
    )
    assert hist.found and [e.event for e in hist.events] == ["created"]
    counts = await reads.handle_task_counts(
        wire.TaskCountsInput(**_caller("private"), inbox="private-bot")
    )
    assert [(c.inbox, c.status, c.n) for c in counts.counts] == [("private-bot", "triage", 1)]
    listed = await reads.handle_task_list(
        wire.TaskListInput(**_caller("private"), inbox="private-bot", fields=["title"])
    )
    assert listed.tasks[0]["title"] == "UB in Parser" and "brief" not in listed.tasks[0]
    record = await reads.handle_task_list(
        wire.TaskListInput(**_caller("private"), inbox="private-bot", posted_by="sa:public-bot")
    )
    assert [r["from_subject"] for r in record.tasks] == ["sa:public-bot"]
    none = await reads.handle_task_list(
        wire.TaskListInput(**_caller("private"), inbox="private-bot", posted_by="nobody")
    )
    assert none.tasks == []
    # the sender's cursor sees its own task's events and nothing else
    upd = await reads.handle_task_updates(
        wire.TaskUpdatesInput(**_caller("public", "public-bot"), from_subject="sa:public-bot")
    )
    assert [e.event for e in upd.events] == ["created"] and upd.next_seq == upd.events[-1].seq
    again = await reads.handle_task_updates(
        wire.TaskUpdatesInput(
            **_caller("public", "public-bot"), from_subject="sa:public-bot", since_seq=upd.next_seq
        )
    )
    assert again.events == [] and again.next_seq == upd.next_seq
    # out of scope: found=false, never a 403 that confirms the id

    async def _no_scope(org, ns, *, write, hides_answer=True):
        return False

    patch_handlers(monkeypatch, "_in_scope", _no_scope)
    got = await reads.handle_task_get(wire.TaskGetInput(**_caller("other"), task_id=t.task_id))
    assert not got.found and got.task is None
    hist = await reads.handle_task_history(
        wire.TaskHistoryInput(**_caller("other"), task_id=t.task_id)
    )
    assert not hist.found
    with pytest.raises(HTTPException) as e:
        await transitions.handle_task_accept(
            wire.TaskAcceptInput(**_caller("other", "x"), task_id=t.task_id)
        )
    assert e.value.status_code == 404


@pytest.mark.asyncio
async def test_inbox_list_and_policy_list(fake: FakeRepo) -> None:
    await _register(fake)
    await inboxes.handle_inbox_policy_set(
        wire.InboxPolicySetInput(
            **_caller("private", "admin"),
            inbox="private-bot",
            poster="sa:public-bot",
            mode="review",
        )
    )
    listed = await inboxes.handle_inbox_list(wire.InboxListInput(**_caller("private")))
    assert [i.inbox for i in listed.inboxes] == ["private-bot"]
    pol = await inboxes.handle_inbox_policy_list(
        wire.InboxPolicyListInput(**_caller("private"), inbox="private-bot")
    )
    assert [(p.poster, p.mode) for p in pol.policies] == [("sa:public-bot", "review")]
    removed = await inboxes.handle_inbox_policy_set(
        wire.InboxPolicySetInput(
            **_caller("private", "admin"),
            inbox="private-bot",
            poster="sa:public-bot",
            mode="review",
            remove=True,
        )
    )
    assert removed.removed
    pol = await inboxes.handle_inbox_policy_list(
        wire.InboxPolicyListInput(**_caller("private"), inbox="private-bot")
    )
    assert pol.policies == []


# ------------------------------------------------------------ principals (the poster picker)


@pytest.mark.asyncio
async def test_principals_come_from_the_directory_not_from_typing(fake: FakeRepo) -> None:
    await _register(fake)  # posters: ["sa:public-bot"]
    fake.service_accounts = [
        {
            "name": "public-bot",
            "sa_subject": "sa:public-bot",
            "namespaces": ["public"],
            "roles": ["writer"],
            "ns_roles": "{}",
            "kind": "service_account",
            "client": "clickgap",
            "created_by": "alice@example.com",
            "created_at": datetime(2026, 9, 1),
        },
        {
            "name": "private-bot",
            "sa_subject": "sa:private-bot-1a2b",
            "namespaces": [],
            "roles": [],
            "ns_roles": '{"private": "admin"}',
            "kind": "service_account",
            "client": "",
            "created_by": "alice@example.com",
            "created_at": datetime(2026, 9, 1),
        },
    ]
    fake.grants = [
        {"subject": "alice@example.com", "namespace": "private", "role": "admin"},
        {"subject": "alice@example.com", "namespace": "public", "role": "viewer"},
        {"subject": "bob@example.com", "namespace": "elsewhere", "role": "writer"},
    ]
    fake.last_seen = {"sa:public-bot": datetime(2026, 9, 20, 7, 0), "ghost": datetime(2026, 9, 1)}
    fake.inboxes[(ORG, "private-bot")]["posters"] = ["sa:public-bot", "forgotten@example.com"]

    out = await inboxes.handle_inbox_principals(
        wire.InboxPrincipalsInput(**_caller("private", "alice"), inbox="private-bot")
    )
    by = {p.subject: p for p in out.principals}
    # people first (by label), then service accounts, then the rest
    assert [p.kind for p in out.principals] == sorted(
        [p.kind for p in out.principals], key={"human": 0, "service_account": 1, "other": 2}.get
    )
    alice = by["alice@example.com"]
    assert (
        alice.kind == "human"
        and alice.can_already_post
        and alice.namespaces == ["private", "public"]
    )
    bob = by["bob@example.com"]
    assert bob.kind == "human" and not bob.can_already_post and not bob.listed
    pub = by["sa:public-bot"]
    assert pub.kind == "service_account" and pub.label == "public-bot" and pub.listed
    assert pub.last_seen == datetime(2026, 9, 20, 7, 0) and not pub.can_already_post
    prv = by["sa:private-bot-1a2b"]
    assert prv.can_already_post and prv.namespaces == ["private"]  # ns_roles admin on the owner ns
    assert by["ghost"].kind == "human" and by["ghost"].last_seen is not None
    assert by["forgotten@example.com"].listed  # a listed poster the directory forgot still shows
    # a query narrows by subject or label
    narrowed = await inboxes.handle_inbox_principals(
        wire.InboxPrincipalsInput(**_caller("private", "alice"), inbox="private-bot", query="bot")
    )
    assert {p.subject for p in narrowed.principals} == {"sa:public-bot", "sa:private-bot-1a2b"}
    assert not narrowed.truncated


@pytest.mark.asyncio
async def test_principals_work_for_an_inbox_not_registered_yet(fake: FakeRepo) -> None:
    fake.grants = [{"subject": "alice@example.com", "namespace": "private", "role": "admin"}]
    out = await inboxes.handle_inbox_principals(
        wire.InboxPrincipalsInput(**_caller("private", "alice"), inbox="brand-new")
    )
    assert [p.subject for p in out.principals] == ["alice@example.com"]
    assert out.principals[0].can_already_post and not out.principals[0].listed


@pytest.mark.asyncio
async def test_principals_need_the_admin_side(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _register(fake)

    async def _refuse(org, ns, role, *, hides_answer=True):
        return False

    from server.auth import authz

    monkeypatch.setattr(authz, "row_in_scope", _refuse)
    with pytest.raises(HTTPException) as e:
        await inboxes.handle_inbox_principals(
            wire.InboxPrincipalsInput(**_caller("private", "alice"), inbox="private-bot")
        )
    assert e.value.status_code == 403


# ------------------------------------------------------------ sweeper


@pytest.mark.asyncio
async def test_sweep_returns_expired_leases_resumes_deferred_and_escalates_triage(
    fake: FakeRepo,
) -> None:
    await _register(fake)
    t = (await _post(fake, **_caller("private", "private-bot"), from_subject="")).task
    got = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot"), inbox="private-bot")
    )
    assert got.tasks[0].attempt == 1
    fake.tasks[t.task_id]["lease_until"] = datetime(2000, 1, 1)
    counts = await sweep.sweep_once()
    assert counts["expired"] == 1 and fake.tasks[t.task_id]["status"] == "todo"
    # attempt 2 of max_attempts 2 → blocked on a human when it expires again
    got = await transitions.handle_task_pull(
        wire.TaskPullInput(**_caller("private", "bot"), inbox="private-bot")
    )
    assert got.tasks[0].attempt == 2 and got.tasks[0].fence == 2
    fake.tasks[t.task_id]["lease_until"] = datetime(2000, 1, 1)
    counts = await sweep.sweep_once()
    assert counts["blocked"] == 1 and fake.tasks[t.task_id]["blocked_on"] == "human"
    # deferred past resume_at → todo
    d = (await _post(fake, **_caller("private", "private-bot"), from_subject="", title="d")).task
    fake.tasks[d.task_id].update({"status": "deferred", "resume_at": datetime(2000, 1, 1)})
    counts = await sweep.sweep_once()
    assert counts["resumed"] == 1 and fake.tasks[d.task_id]["status"] == "todo"
    # stale triage escalated exactly once
    s = (await _post(fake, title="s")).task
    fake.tasks[s.task_id]["created_at"] = datetime(2000, 1, 1)
    assert (await sweep.sweep_once())["escalated"] == 1
    assert (await sweep.sweep_once())["escalated"] == 0
    assert fake.events[-1]["event"] == "triage_escalated"


# ------------------------------------------------------------ lease backends


class StubCH:
    def __init__(self) -> None:
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_one_returns: list[dict[str, Any] | None] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.raise_on_execute: Exception | None = None

    async def query_one(self, sql, params=None):
        self.query_one_calls.append((sql, params or {}))
        return self.query_one_returns.pop(0) if self.query_one_returns else None

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql, params=None):
        self.execute_calls.append((sql, params or {}))
        if self.raise_on_execute is not None:
            raise self.raise_on_execute

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


@pytest.mark.asyncio
async def test_keeper_leases_are_a_strict_mode_cas_with_read_back() -> None:
    ch = StubCH()
    leases = KeeperLeases(ch)
    until = datetime(2030, 1, 1)
    ch.query_one_returns = [{"owner": "a", "fence": 1}]
    won = await leases.try_claim("t1", "a", expected_fence=0, lease_until=until)
    assert won.won and won.fence == 1
    sql, params = ch.execute_calls[-1]
    assert "keeper_map_strict_mode = 1" in sql and "fence = fence + 1" in sql
    assert params["fence"] == 0 and "owner = ''" in sql
    # the loser: Keeper says Bad version → lost, no raise
    ch.raise_on_execute = RuntimeError(
        "Code: 999. Coordination::Exception: Transaction failed (Bad version)"
    )
    ch.query_one_returns = [{"owner": "a", "fence": 1}]
    lost = await leases.try_claim("t1", "b", expected_fence=0, lease_until=until)
    assert not lost.won and lost.owner == "a"
    # the silent loser: no error, but the row says someone else
    ch.raise_on_execute = None
    ch.query_one_returns = [{"owner": "a", "fence": 1}]
    lost = await leases.try_claim("t1", "b", expected_fence=0, lease_until=until)
    assert not lost.won
    # any other error propagates
    ch.raise_on_execute = RuntimeError("Code: 60. DB::Exception: Table does not exist")
    with pytest.raises(RuntimeError):
        await leases.try_claim("t1", "b", expected_fence=0, lease_until=until)
    ch.raise_on_execute = None
    ch.query_one_returns = [{"owner": "a", "fence": 1}]
    assert await leases.set_owner("t1", fence=1, owner="a", lease_until=until, status="in_progress")
    await leases.delete("t1")
    assert "DELETE WHERE task_id" in ch.execute_calls[-1][0]


@pytest.mark.asyncio
async def test_lease_backend_is_keeper() -> None:
    repo.reset_lease_backend()
    try:
        assert (await repo.lease_backend(ch=StubCH())).name == "keeper"
    finally:
        repo.reset_lease_backend()


def test_a_made_up_task_id_reads_as_not_found_not_as_a_parse_error() -> None:
    assert repo.is_uuid("3862f1e8-b0aa-485f-aa34-1dabf2668fc8")
    assert not repo.is_uuid("x") and not repo.is_uuid("") and not repo.is_uuid(None)


@pytest.mark.asyncio
async def test_fetch_task_and_history_skip_clickhouse_for_a_non_uuid() -> None:
    ch = StubCH()
    assert await repo.fetch_task(ORG, "nope", ch=ch) is None
    assert await repo.task_history(ORG, "nope", ch=ch) == []
    assert ch.execute_calls == [] and ch.insert_calls == []


@pytest.mark.asyncio
async def test_inbox_and_policy_reads_are_sequentially_consistent() -> None:
    """An admin registers an inbox and sets a policy in the next request; the
    read must see the write on any replica (Cloud can answer 404 otherwise)."""
    ch = StubCH()
    ch.query_all_returns = [[]]
    await repo.fetch_inbox(ORG, "x", ch=ch)
    await repo.list_inboxes(ORG, ch=ch)
    await repo.fetch_policies(ORG, "x", ch=ch)
    reads = ch.query_one_calls + ch.query_all_calls
    assert len(reads) == 3, reads
    for sql, _ in reads:
        assert "select_sequential_consistency = 1" in sql, sql


def test_next_version_is_strictly_increasing_and_json_safe() -> None:
    a, b, c = repo.next_version(), repo.next_version(), repo.next_version()
    assert a < b < c
    # rides the wire as a JSON number: a JavaScript client must not round it
    assert c < repo.JS_SAFE_INTEGER
    assert c > 1_700_000_000_000_000  # microseconds, not seconds or millis


# ------------------------------------------------------------ inbox existence


@pytest.mark.asyncio
async def test_a_foreign_inbox_reads_exactly_like_an_unknown_one(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reads addressed by inbox name answer a caller who may not read the
    owner namespace with the same 404 an unregistered name gets — a 403
    there would confirm the inbox exists."""
    await _register(fake)

    async def _no_scope(org, ns, *, write, hides_answer=True):
        return False

    patch_handlers(monkeypatch, "_in_scope", _no_scope)
    who = _caller("public", "eve")
    calls = {
        "task.list": lambda name: reads.handle_task_list(wire.TaskListInput(**who, inbox=name)),
        "task.counts": lambda name: reads.handle_task_counts(
            wire.TaskCountsInput(**who, inbox=name)
        ),
        "task.updates": lambda name: reads.handle_task_updates(
            wire.TaskUpdatesInput(**who, inbox=name)
        ),
        "inbox.policy_list": lambda name: inboxes.handle_inbox_policy_list(
            wire.InboxPolicyListInput(**who, inbox=name)
        ),
    }
    for op, call in calls.items():
        seen = []
        for name in ("private-bot", "no-such-inbox"):
            with pytest.raises(HTTPException) as e:
                await call(name)
            detail = dict(e.value.detail)
            detail["message"] = detail["message"].replace(repr(name), "<inbox>")
            seen.append((e.value.status_code, detail))
        assert seen[0] == seen[1], op
        assert seen[0][0] == 404, op


@pytest.mark.asyncio
async def test_an_inbox_the_caller_cannot_read_answers_as_unregistered(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """policy_set, pull and principals address an inbox by its guessable name: a caller who
    cannot read its owner namespace gets the unregistered answer, never a 403 that says the
    inbox exists."""
    await _register(fake)

    async def _foreign(org, ns, *, write, hides_answer=True):
        return ns != "private"

    async def _admin_elsewhere(org, ns, role, *, hides_answer=True):
        return ns != "private"

    from server.auth import authz

    patch_handlers(monkeypatch, "_in_scope", _foreign)
    monkeypatch.setattr(authz, "row_in_scope", _admin_elsewhere)
    for call in (
        inboxes.handle_inbox_policy_set(
            wire.InboxPolicySetInput(
                **_caller("other", "x"), inbox="private-bot", poster="sa:x", mode="auto"
            )
        ),
        transitions.handle_task_pull(
            wire.TaskPullInput(**_caller("other", "x"), inbox="private-bot")
        ),
    ):
        with pytest.raises(HTTPException) as e:
            await call
        assert e.value.status_code == 404
    out = await inboxes.handle_inbox_principals(
        wire.InboxPrincipalsInput(**_caller("other", "x"), inbox="private-bot")
    )
    assert not any(p.listed for p in out.principals)


@pytest.mark.asyncio
async def test_a_stranger_posting_to_an_inbox_is_told_it_does_not_exist(
    fake: FakeRepo, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller who may neither post to an inbox nor read its owner namespace gets the
    answer an unregistered name gets; one who can read it but not post gets the 403."""
    await _register(fake)

    async def _no_scope(org, ns, *, write, hides_answer=True):
        return False

    patch_handlers(monkeypatch, "_in_scope", _no_scope)
    with pytest.raises(HTTPException) as stranger:
        await _post(fake, from_subject="sa:stranger", **_caller("other", "stranger"))
    with pytest.raises(HTTPException) as absent:
        await _post(fake, inbox="no-such-inbox", from_subject="sa:stranger")
    assert stranger.value.status_code == 404 == absent.value.status_code
