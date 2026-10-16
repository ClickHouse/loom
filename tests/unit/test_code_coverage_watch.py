"""code.coverage.watch / code.coverage.unwatch and the code_coverage_notify
worker: one `code.coverage.ready` event per watched commit once every index
tier covers it, over a real git history and stub ClickHouse repositories."""

from __future__ import annotations

import dataclasses
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.code.api import models_coverage_watch as ops
from server.code.app import coverage_watch as h
from server.code.app import watches as svc
from server.code.app.index import commit_coverage as cc
from server.code.domain.coverage_watch import CoverageWatch
from server.code.store import coverage_watches as wrepo
from server.code.workers import coverage_notify as code_coverage_notify
from server.core.errors import AuthInsufficientError, NotFoundError, ValidationError
from server.operations.registry import OPERATIONS_BY_NAME

ORG, NS, INBOX = "acme", "code-app", "reviewer-bot"
# The server clock task events are stamped from, far from any host clock.
SERVER_NOW_US = 1_800_000_000_000_000


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    ).stdout.strip()


class Fake:
    """The watch table (replacing by key like FINAL would), task_events, the
    index state and the inbox registry."""

    def __init__(self, git_dir: str) -> None:
        self.git_dir = git_dir
        self.watches: dict[tuple[str, str, str, str], CoverageWatch] = {}
        self.events: list[dict[str, Any]] = []
        self.state: dict[str, dict[str, Any]] = {}
        self.inboxes = {(ORG, INBOX): {"org": ORG, "inbox": INBOX, "owner_namespace": "bots"}}
        self.fail_watch_writes = 0

    def set_heads(self, fast: str, clang: str) -> None:
        self.state = {"git_head": {"value": fast}, "scip_commit": {"value": clang}}

    async def fetch_watch(self, org, ns, sha, inbox):
        row = self.watches.get((org, ns, sha, inbox))
        return None if row is None or row.is_deleted else row

    async def pending_watches(self, *, limit=5000):
        rows = [r for r in self.watches.values() if not r.is_deleted]
        return [r for r in rows if r.delivered_at is None][:limit]

    async def insert_watch_version(self, row):
        if self.fail_watch_writes:
            self.fail_watch_writes -= 1
            raise RuntimeError("process died")
        key = (row.org, row.namespace, row.commit_sha, row.inbox)
        old = self.watches.get(key)
        if old is None or row.version > old.version:
            self.watches[key] = row

    async def ready_event_exists(self, org, inbox, task_id):
        return any(
            e["org"] == org and e["inbox"] == inbox and e["task_id"] == task_id for e in self.events
        )

    async def insert_ready_event(self, row):
        # The task id is the insert's dedup token: ClickHouse drops a repeat.
        if not await self.ready_event_exists(row["org"], row["inbox"], row["task_id"]):
            self.events.append(dict(row))

    async def ready_event_seq(self, org, inbox, task_id):
        for e in self.events:
            if e["org"] == org and e["inbox"] == inbox and e["task_id"] == task_id:
                return e["seq"]
        return None

    async def fetch_inbox(self, org, inbox):
        return self.inboxes.get((org, inbox))


@pytest.fixture()
def history(tmp_path: Path) -> tuple[str, list[str]]:
    _git(tmp_path, "init", "-q", "-b", "master")
    shas = []
    for name in "ABC":
        (tmp_path / "f").write_text(name)
        _git(tmp_path, "add", "f")
        _git(tmp_path, "commit", "-q", "-m", name)
        shas.append(_git(tmp_path, "rev-parse", "HEAD"))
    cc.reset()
    return str(tmp_path / ".git"), shas


@pytest.fixture()
def fake(history: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch) -> Fake:
    from server.code.domain.index import repos
    from server.code.store import index as code_repo
    from server.tasks.store import repo as tasks_repo

    f = Fake(history[0])
    for name in (
        "fetch_watch",
        "pending_watches",
        "insert_watch_version",
        "ready_event_exists",
        "insert_ready_event",
        "ready_event_seq",
    ):
        monkeypatch.setattr(wrepo, name, getattr(f, name))
    monkeypatch.setattr(tasks_repo, "fetch_inbox", f.fetch_inbox)

    async def server_version(floor: int = 0) -> int:
        return SERVER_NOW_US

    monkeypatch.setattr(tasks_repo, "server_version", server_version)

    class _State:
        async def all_state(self, org, ns):
            return f.state

        async def all_state_many(self, pairs):
            return {p: f.state for p in pairs}

    monkeypatch.setattr(code_repo, "CodeRepo", _State)
    cfg = SimpleNamespace(org=ORG, namespace=NS, git_dir=f.git_dir, scip_producer="clang")
    monkeypatch.setattr(
        repos, "repo_for_namespace", lambda o, n: cfg if (o, n) == (ORG, NS) else None
    )
    return f


def _watch(commit: str, **over: Any) -> ops.CodeCoverageWatchInput:
    return ops.CodeCoverageWatchInput(
        **{"org": ORG, "namespace": NS, "commit": commit, "inbox": INBOX, **over}
    )


def test_both_ops_are_registered_viewer_ops() -> None:
    for name in ("code.coverage.watch", "code.coverage.unwatch"):
        assert OPERATIONS_BY_NAME[name].required_role.name == "VIEWER"


@pytest.mark.asyncio
async def test_watch_then_the_pass_once_covered_emits_one_event(
    fake: Fake, history: tuple[str, list[str]]
) -> None:
    _, (a, b, c) = history
    fake.set_heads(fast=c, clang=a)  # the compiler tier has not reached B
    out = await h.handle_code_coverage_watch(_watch(b))
    assert not out.ready and not out.emitted and out.delivered_at is None
    assert out.commit_coverage is not None and out.commit_coverage.waiting_on == ["C++"]
    assert out.event_id == wrepo.event_id(ORG, NS, b, INBOX)

    got = await code_coverage_notify.run_pass()
    assert (got.pending, got.delivered, got.emitted) == (1, 0, 0) and not fake.events

    fake.set_heads(fast=c, clang=b)
    got = await code_coverage_notify.run_pass()
    assert (got.delivered, got.emitted) == (1, 1)
    [event] = fake.events
    assert event["event"] == "code.coverage.ready" and event["inbox"] == INBOX
    assert event["task_id"] == out.event_id
    # seq / ts from the server clock, so the task cursors order it across replicas
    assert event["seq"] == event["version"] == SERVER_NOW_US
    assert event["ts"] == datetime.fromtimestamp(SERVER_NOW_US / 1e6, UTC).replace(tzinfo=None)
    payload = json.loads(event["patch"])
    assert payload["namespace"] == NS and payload["commit"] == b
    assert payload["tiers"] == [{"producer": "clang", "label": "C++", "watermark": b}]
    assert fake.watches[(ORG, NS, b, INBOX)].delivered_at is not None

    # a second pass has nothing pending and emits nothing
    got = await code_coverage_notify.run_pass()
    assert (got.pending, got.emitted) == (0, 0) and len(fake.events) == 1


@pytest.mark.asyncio
async def test_a_crash_between_the_event_and_the_watch_update_does_not_duplicate(
    fake: Fake, history: tuple[str, list[str]]
) -> None:
    _, (a, b, c) = history
    fake.set_heads(fast=c, clang=a)
    await h.handle_code_coverage_watch(_watch(b))
    fake.set_heads(fast=c, clang=c)
    fake.fail_watch_writes = 1  # the event lands, the delivered version does not
    got = await code_coverage_notify.run_pass()
    assert got.delivered == 0 and len(fake.events) == 1
    assert fake.watches[(ORG, NS, b, INBOX)].delivered_at is None

    got = await code_coverage_notify.run_pass()
    assert (got.delivered, got.emitted) == (1, 0) and len(fake.events) == 1
    assert fake.watches[(ORG, NS, b, INBOX)].delivered_at is not None


@pytest.mark.asyncio
async def test_two_racing_deliveries_report_one_emitted_event(
    fake: Fake, history: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.code.domain.index import repos

    _, (a, b, c) = history
    fake.set_heads(fast=c, clang=a)
    await h.handle_code_coverage_watch(_watch(b))
    fake.set_heads(fast=c, clang=c)
    watch = fake.watches[(ORG, NS, b, INBOX)]
    coverage = await svc.coverage_for(repos.repo_for_namespace(ORG, NS), b, fake.state)

    # Both deliverers checked for the event before either posted it.
    async def no_event_yet(org: str, inbox: str, task_id: str) -> bool:
        return False

    seqs = iter([SERVER_NOW_US, SERVER_NOW_US + 1])

    async def server_version(floor: int = 0) -> int:
        return next(seqs)

    monkeypatch.setattr(wrepo, "ready_event_exists", no_event_yet)
    monkeypatch.setattr(svc, "server_version", server_version)
    first, _ = await svc.deliver(watch, coverage)
    second, _ = await svc.deliver(watch, coverage)
    assert (first, second) == (True, False) and len(fake.events) == 1


@pytest.mark.asyncio
async def test_watching_a_covered_commit_emits_at_once_and_a_rewatch_does_not(
    fake: Fake, history: tuple[str, list[str]]
) -> None:
    _, (a, b, c) = history
    fake.set_heads(fast=c, clang=c)
    out = await h.handle_code_coverage_watch(_watch(a[:10]))  # an abbreviation resolves
    assert out.commit == a and out.ready and out.emitted and out.delivered_at is not None
    assert len(fake.events) == 1
    again = await h.handle_code_coverage_watch(_watch(a))
    assert again.ready and not again.emitted and len(fake.events) == 1
    assert (await code_coverage_notify.run_pass()).pending == 0


@pytest.mark.asyncio
async def test_unwatch_drops_the_watch(fake: Fake, history: tuple[str, list[str]]) -> None:
    _, (a, b, c) = history
    fake.set_heads(fast=a, clang=a)
    await h.handle_code_coverage_watch(_watch(c))
    args = {"org": ORG, "namespace": NS, "commit": c, "inbox": INBOX}
    out = await h.handle_code_coverage_unwatch(ops.CodeCoverageUnwatchInput(**args))
    assert out.removed
    fake.set_heads(fast=c, clang=c)
    got = await code_coverage_notify.run_pass()
    assert got.pending == 0 and not fake.events
    out = await h.handle_code_coverage_unwatch(ops.CodeCoverageUnwatchInput(**args))
    assert not out.removed


@pytest.mark.asyncio
async def test_refusals(
    fake: Fake, history: tuple[str, list[str]], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.auth import authz

    _, (a, _b, _c) = history
    fake.set_heads(fast=a, clang=a)
    with pytest.raises(NotFoundError):
        await h.handle_code_coverage_watch(_watch(a, inbox="nobody"))
    with pytest.raises(NotFoundError):
        await h.handle_code_coverage_watch(_watch(a, namespace="code-unknown"))
    with pytest.raises(ValidationError):
        await h.handle_code_coverage_watch(_watch("0123456789abcdef"))  # not in the mirror
    with pytest.raises(ValidationError):
        await h.handle_code_coverage_watch(_watch("zzzzzzzz"))

    async def _viewer_only(org, ns, role):
        return role.name == "VIEWER"

    monkeypatch.setattr(authz, "row_in_scope", _viewer_only)
    with pytest.raises(AuthInsufficientError, match="inbox"):
        await h.handle_code_coverage_watch(_watch(a))

    async def _no_inbox_access(org, ns, role):
        return ns == NS  # the code namespace only: the inbox's owner is unreadable

    monkeypatch.setattr(authz, "row_in_scope", _no_inbox_access)
    with pytest.raises(NotFoundError, match="not registered"):
        await h.handle_code_coverage_watch(_watch(a))  # never confirms the inbox exists

    async def _no_code_access(org, ns, role):
        return ns != NS

    monkeypatch.setattr(authz, "row_in_scope", _no_code_access)
    with pytest.raises(AuthInsufficientError, match="code namespace"):
        await h.handle_code_coverage_watch(_watch(a))
    with pytest.raises(AuthInsufficientError):
        await h.handle_code_coverage_unwatch(
            ops.CodeCoverageUnwatchInput(org=ORG, namespace=NS, commit=a, inbox=INBOX)
        )
    assert not fake.watches and not fake.events


def test_the_event_id_is_fixed_per_commit_and_inbox() -> None:
    one = wrepo.event_id(ORG, NS, "a" * 40, INBOX)
    assert one == wrepo.event_id(ORG, NS, "a" * 40, INBOX)
    assert one != wrepo.event_id(ORG, NS, "a" * 40, "other-inbox")
    assert svc.ACTOR.startswith("process:")


def test_a_watch_carries_every_column_of_the_table() -> None:
    assert tuple(f.name for f in dataclasses.fields(CoverageWatch)) == wrepo.WATCH_FIELDS
