"""The registry reads of document sources, run on clickhouse-local against the real
`loom.knowledge_sources`."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.knowledge.store import sources as store
from tests.unit.clickhouse_local import CLICKHOUSE, LocalCH, ddl

pytestmark = pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")


def _row(namespace: str, name: str, active: bool, version: int, updated: str = "now()") -> str:
    return (
        "INSERT INTO loom.knowledge_sources (org, namespace, name, source_id, kind, active,"
        f" updated_at, version) VALUES ('o', '{namespace}', '{name}', generateUUIDv4(),"
        f" 'gdrive', {str(active).lower()}, {updated}, {version})"
    )


@pytest.mark.asyncio
async def test_every_source_of_the_namespaces_is_named_active_or_not() -> None:
    ch = LocalCH(
        ddl("knowledge_sources"),
        _row("team-a", "decks", True, 1),
        # Removed: its newest version is inactive.
        _row("team-b", "decks", True, 1),
        _row("team-b", "decks", False, 2),
        _row("team-c", "notes", True, 1),
    )
    names = ["team-a", "team-b", "team-d"]
    assert await store.sources_in("o", names, ch=ch) == [
        ("team-a", "decks"),
        ("team-b", "decks"),
    ]
    assert await store.sources_in("other", names, ch=ch) == []
    assert await store.sources_in("o", [], ch=ch) == []


@pytest.mark.asyncio
async def test_a_namespace_claimed_at_signup_is_told_from_a_shared_one() -> None:
    def _ns(name: str, owner: str, tags: str = "[]") -> str:
        return (
            "INSERT INTO loom.namespaces (org, namespace, owner_email, active, tags)"
            f" VALUES ('o', '{name}', '{owner}', true, {tags})"
        )

    ch = LocalCH(
        ddl("namespaces"),
        _ns("alice-notes", "alice-notes"),
        _ns("team", "bob@x.com"),
        _ns("agent-7", "agent-7", "['agent']"),
        "INSERT INTO loom.namespaces (org, namespace, owner_email, active, tags)"
        " VALUES ('o', 'gone', 'gone', false, [])",
    )
    assert await store.directory_row("o", "alice-notes", ch=ch) == (False, True)
    assert await store.directory_row("o", "team", ch=ch) == (False, False)
    # An agent sign-up's namespace (its own owner) is its own too.
    assert await store.directory_row("o", "agent-7", ch=ch) == (False, True)
    assert await store.directory_row("o", "gone", ch=ch) == (True, False)
    assert await store.directory_row("o", "unregistered", ch=ch) == (False, False)


def _step_13_queries() -> list[str]:
    """The two SELECTs upgrade step 13 tells an operator to run."""
    page = (Path(__file__).resolve().parents[2] / "docs/operations/upgrades.md").read_text()
    step = page.split("13. Moving to a build that keeps document sources", 1)[1]
    blocks = step.split("```sql")[1:3]
    return [b.split("```", 1)[0].strip() for b in blocks]


def _erasure(scope_type: str, scope_value: str, step: str, completed: str = "+") -> str:
    """A completed erasure; `completed` "+" an hour after the sources were added, "-" before."""
    value = scope_value.replace("'", "\\'")
    return (
        "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, status,"
        f" steps_completed, requested_at, completed_at) VALUES (generateUUIDv4(), 'o',"
        f" '{scope_type}', '{value}', 'completed', ['{step}'], now(),"
        f" now() {completed} INTERVAL 1 HOUR)"
    )


def _namespace(name: str, active: bool) -> str:
    return (
        "INSERT INTO loom.namespaces (org, namespace, owner_email, active)"
        f" VALUES ('o', '{name}', 'a@x', {str(active).lower()})"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("seeded", [False, True])
async def test_upgrade_step_13_lists_the_sources_an_older_build_left(seeded: bool) -> None:
    rows = [
        _row("team-gone", "decks", True, 1),
        _row("team-live", "notes", True, 1),
        _namespace("team-gone", False),
        _namespace("team-live", True),
        _erasure("namespace_list", '{"namespaces": ["team-live"]}', "memory.tombstone"),
        _row("team-array", "decks", True, 1),
        _erasure("namespace_list", '["team-array"]', "memory.tombstone"),
        _row("team-one", "decks", True, 1),
        _erasure("namespace", "team-one", "memory.tombstone"),
        # Retired since (its namespace deleted on the newer build): still listed.
        _row("team-retired", "decks", True, 1),
        _row("team-retired", "decks", False, 2, updated="now() + INTERVAL 2 HOUR"),
        # Retired before the older build's erasure: it wrote nothing back, so not listed.
        _row("team-before", "decks", True, 1),
        _row("team-before", "decks", False, 2),
        _erasure("namespace", "team-before", "memory.tombstone"),
        _erasure("namespace", "team-retired", "memory.tombstone"),
        # This build's erasure retired its sources itself: not listed.
        _row("team-new", "decks", True, 1),
        _row("team-new", "decks", False, 2),
        _erasure("namespace", "team-new", "knowledge_sources.retire"),
        # An older build added it during this build's erasure, after the retirement.
        _row("team-mixed", "decks", True, 1),
        _erasure("namespace", "team-mixed", "knowledge_sources.retire"),
        # Erased before its source was added: the erasure never covered it.
        _row("team-later", "decks", True, 1),
        _erasure("namespace", "team-later", "memory.tombstone", completed="-"),
    ]
    ch = LocalCH(
        ddl("knowledge_sources"),
        ddl("namespaces"),
        ddl("erasure_requests"),
        *(rows if seeded else []),
    )
    deleted, erased = _step_13_queries()
    on_deleted = await ch.query_all(deleted)
    covered = await ch.query_all(erased)
    if seeded:
        assert [(r["namespace"], r["name"]) for r in on_deleted] == [("team-gone", "decks")]
        listed = sorted((r["namespace"], r["name"]) for r in covered)
        assert listed == [
            ("team-array", "decks"),
            ("team-live", "notes"),
            ("team-mixed", "decks"),
            ("team-one", "decks"),
            ("team-retired", "decks"),
        ]
    else:
        assert on_deleted == [] and covered == []


@pytest.mark.asyncio
async def test_a_source_add_waits_only_for_namespace_erasures_that_will_still_run() -> None:
    from server.erasure.domain.failure_reason import ABANDONED_REASON_PREFIX, REPLAY_PENDING_REASON
    from server.erasure.store import ledger

    def _row(scope_type: str, value: str, status: str, reason: str = "") -> str:
        return (
            "INSERT INTO loom.erasure_requests (id, org, scope_type, scope_value, status,"
            f" failure_reason, requested_at) VALUES (generateUUIDv4(), 'o', '{scope_type}',"
            f" '{value}', '{status}', '{reason}', now())"
        )

    ch = LocalCH(
        ddl("erasure_requests"),
        _row("namespace", "running", "in_progress"),
        _row("namespace_list", '["queued"]', "queued"),
        _row("namespace", "retrying", "failed", "backend down"),
        _row("namespace", "abandoned", "failed", f"{ABANDONED_REASON_PREFIX} after 5 attempts"),
        _row("namespace", "replay", "failed", REPLAY_PENDING_REASON),
        _row("namespace", "done", "completed"),
        _row("user_id", "alice", "in_progress"),
    )
    rows = await ledger.open_namespace_erasures("o", ch=ch)
    assert sorted(r["scope_value"] for r in rows) == ['["queued"]', "retrying", "running"]
