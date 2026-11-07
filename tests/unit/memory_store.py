"""An in-process loom.memory for handler tests that need the table to keep state.

`MemoryStore` models the storage contract the write paths rely on:
ReplacingMergeTree(version) over the sort key
(org, namespace, agent, user_id, session_id, memory_key), where FINAL serves
the highest version, plus the memory_versions mirror, which records every
inserted row. `wire_memory_store` installs it behind the repository reads and
the ClickHouse client that memory.set, memory.update, memory.pin and
memory.delete use. Every collaborator a test does not name is a fake that
records its calls. A query the store does not model answers empty, the way a
table with no matching rows would.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Any

import pytest

from server.memory import store as mr
from tests.unit.schema_ddl import ddl

SortKey = tuple[str, str, str, str, str, str]


@cache
def _ddl_defaults() -> dict[str, str]:
    """Column -> DEFAULT expression of loom.memory ('NULL' for a bare Nullable)."""
    out: dict[str, str] = {}
    for line in ddl("memory").splitlines():
        m = re.match(r"\s+`([a-z_0-9]+)`\s+(.+?),?$", line)
        if not m or " MATERIALIZED " in f" {m.group(2)} ":
            continue
        d = re.search(r" DEFAULT (.+)$", m.group(2))
        if d:
            out[m.group(1)] = d.group(1)
        elif m.group(2).startswith("Nullable"):
            out[m.group(1)] = "NULL"
    return out


def _default_value(expr: str) -> Any:
    if expr == "now()":
        return datetime.now(UTC).replace(tzinfo=None)
    if expr == "generateUUIDv4()":
        return str(uuid.uuid4())
    if expr == "NULL":
        return None
    if expr == "[]":
        return []
    if expr.startswith("'"):
        return expr.strip("'")
    return float(expr) if "." in expr else int(expr)


def sort_key(row: dict[str, Any]) -> SortKey:
    return (
        row["org"],
        row["namespace"],
        row["agent"],
        row["user_id"],
        row["session_id"],
        row["memory_key"],
    )


@dataclass
class MemoryStore:
    """Rows as ClickHouse holds them, before and after FINAL."""

    versions: list[dict[str, Any]] = field(default_factory=list)
    blacklist: list[dict[str, Any]] = field(default_factory=list)
    blacklist_versions: list[int] = field(default_factory=list)
    embeddings: list[dict[str, Any]] = field(default_factory=list)
    other_inserts: list[tuple[str, list[dict[str, Any]]]] = field(default_factory=list)
    # (memory_key, columns the insert left to their DDL default, the key had a live head)
    partial_inserts: list[tuple[str, tuple[str, ...], bool]] = field(default_factory=list)

    def insert(self, table: str, rows: list[dict[str, Any]]) -> None:
        name = table.removeprefix("loom.")
        if name == "memory":
            for row in rows:
                self._insert_memory_row(dict(row))
        elif name == "blacklist":
            self.blacklist.extend(dict(r) for r in rows)
        elif name == "blacklist_version":
            self.blacklist_versions.extend(int(r["version"]) for r in rows)
        elif name == "memory_embeddings":
            self.embeddings.extend(dict(r) for r in rows)
        else:
            self.other_inserts.append((name, [dict(r) for r in rows]))

    def _insert_memory_row(self, row: dict[str, Any]) -> None:
        """ClickHouse fills an omitted column with its DDL default; an unknown
        column is an insert error."""
        defaults = _ddl_defaults()
        unknown = sorted(set(row) - set(defaults) - set(mr.MEMORY_PERSISTED_COLS))
        assert not unknown, f"loom.memory has no columns {unknown}"
        missing = tuple(sorted(c for c in mr.MEMORY_PERSISTED_COLS if c not in row))
        if missing:
            head = self.final().get(sort_key(row))
            live = head is not None and head["tombstoned_at"] is None
            self.partial_inserts.append((row["memory_key"], missing, live))
            for c in missing:
                row[c] = _default_value(defaults[c])
        self.versions.append(row)

    def final(self) -> dict[SortKey, dict[str, Any]]:
        """The collapsed table: the highest version per sort key; on a tie the
        later insert wins, as a merge keeps the last-inserted duplicate."""
        heads: dict[SortKey, dict[str, Any]] = {}
        for row in self.versions:
            k = sort_key(row)
            if k not in heads or int(row["version"]) >= int(heads[k]["version"]):
                heads[k] = row
        return heads

    def head(self, *, memory_key: str, **ident: str) -> dict[str, Any] | None:
        """The FINAL row of one key, narrowed by whichever identity fields are given."""
        rows = [
            r
            for r in self.final().values()
            if r["memory_key"] == memory_key and all(r[f] == v for f, v in ident.items())
        ]
        assert len(rows) <= 1, f"{memory_key!r} is live under {len(rows)} identities"
        return rows[0] if rows else None

    def history(self, memory_id: str) -> list[dict[str, Any]]:
        """memory_versions for one memory, oldest first."""
        return sorted(
            (r for r in self.versions if str(r["memory_id"]) == memory_id),
            key=lambda r: int(r["version"]),
        )

    def _hydrate(self, row: dict[str, Any]) -> mr.MemoryRow:
        return mr._hydrate({**row, "value_text": row["value"]})

    async def get_by_key(
        self,
        identity: mr.Identity,
        memory_key: str,
        *,
        include_tombstoned: bool = False,
        **_: Any,
    ) -> mr.MemoryRow | None:
        rows = [
            r
            for r in self.final().values()
            if r["org"] == identity.org
            and r["namespace"] == identity.namespace
            and r["memory_key"] == memory_key
            and (
                identity.agent == "*"
                or (r["agent"], r["user_id"], r["session_id"])
                == (identity.agent, identity.user_id, identity.session_id)
            )
            and (include_tombstoned or r["tombstoned_at"] is None)
        ]
        if not rows:
            return None
        return self._hydrate(max(rows, key=lambda r: int(r["version"])))

    async def get_by_id(
        self, memory_id: str, *, org: str, namespace: str, **_: Any
    ) -> mr.MemoryRow | None:
        for r in self.final().values():
            if str(r["memory_id"]) == str(memory_id) and (r["org"], r["namespace"]) == (
                org,
                namespace,
            ):
                return self._hydrate(r) if r["tombstoned_at"] is None else None
        return None

    async def namespaces_of_id(self, org: str, memory_id: str, **_: Any) -> list[str]:
        return sorted(
            {
                str(r["namespace"])
                for r in self.final().values()
                if str(r["memory_id"]) == str(memory_id)
                and r["org"] == org
                and r["tombstoned_at"] is None
            }
        )

    async def live_rows_for_key(self, input: Any, _ch: Any) -> list[dict[str, Any]]:
        """memory.update's namespace-wide key resolution."""
        rows = [
            r
            for r in self.final().values()
            if r["org"] == input.org
            and r["namespace"] == input.namespace
            and r["memory_key"] == input.memory_key
            and r["tombstoned_at"] is None
            and r["status"] in ("active", "duplicate", "conflicted")
            and getattr(input, "agent", None) in (None, "", "-", "*", r["agent"])
            and all(
                getattr(input, f, None) in (None, "", "-", r[f]) for f in ("user_id", "session_id")
            )
        ]
        return sorted(rows, key=lambda r: -int(r["version"]))


class StoreClient:
    """The ClickHouse client the handlers get: inserts land in the store. It
    answers the blacklist cache's two reads (patterns, version watermark); any
    other read returns no rows."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store
        self.queries: list[str] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.store.insert(table, rows)

    async def query_one(self, sql: str, *_: Any, **__: Any) -> dict[str, Any] | None:
        self.queries.append(sql)
        if "FROM loom.blacklist_version" in sql:
            v = self.store.blacklist_versions
            return {"n": len(v), "s": sum(v)}
        return None

    async def query_all(self, sql: str, *_: Any, **__: Any) -> list[dict[str, Any]]:
        self.queries.append(sql)
        if "FROM loom.blacklist FINAL" in sql:
            return [r for r in self.store.blacklist if r["tombstoned_at"] is None]
        return []

    async def execute(self, sql: str, *_: Any, **__: Any) -> None:
        self.queries.append(sql)


class FixedEmbedder:
    """One vector for every text: the conflict check is faked, so similarity
    never decides anything here."""

    model_name = "fake-fixed"
    dim = 4

    async def embed(self, _text: str) -> list[float]:
        return [0.5, 0.5, 0.5, 0.5]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [[0.5, 0.5, 0.5, 0.5] for _ in texts]


@dataclass
class Recorded:
    """Side calls that leave loom.memory, in call order."""

    audit: list[dict[str, Any]] = field(default_factory=list)
    conflict_checks: list[dict[str, Any]] = field(default_factory=list)


def wire_memory_store(monkeypatch: pytest.MonkeyPatch, store: MemoryStore) -> Recorded:
    """Route the memory write paths through `store`; returns the side-call log.

    The conflict check answers `clear` (a lifecycle test is about versions and
    status, not similarity), and the entity projection, same-key supersession,
    usage events and audit rows become no-ops or recorders.
    """
    from server.memory.app import conflicts as cf
    from server.memory.app import handlers as mem
    from server.memory.app import restore as _restore_mod
    from server.memory.app import set_commit as set_commit
    from server.memory.app import stack as _stack_mod
    from server.memory.app import update as update_mod
    from server.memory.domain.conflicts import ConflictResult

    client = StoreClient(store)
    recorded = Recorded()
    for target in (
        "server.core.ch.get_client",
        "server.memory.app.handlers.get_client",
        "server.memory.app.write.get_client",
        "server.memory.app.update.get_client",
        "server.core.repo_base._default_client",
        "server.memory.app.blacklist_guard.get_client",
        "server.memory.app.conflicts.get_client",
        "server.memory.app.verify.get_client",
    ):
        monkeypatch.setattr(target, lambda: client, raising=False)
    for target in (
        "server.memory.app.handlers.get_embedder",
        "server.memory.app.set_commit.get_embedder",
        "server.memory.app.update.get_embedder",
    ):
        monkeypatch.setattr(target, lambda: FixedEmbedder(), raising=False)
    monkeypatch.setattr(mr, "get_by_key", store.get_by_key)
    monkeypatch.setattr(mr, "get_by_id", store.get_by_id)
    monkeypatch.setattr(mr, "namespaces_of_id", store.namespaces_of_id)
    monkeypatch.setattr(update_mod, "_live_rows_for_key", store.live_rows_for_key)

    async def _check(**kw: Any) -> ConflictResult:
        recorded.conflict_checks.append(kw)
        return ConflictResult(verdict="clear", peers=[])

    async def _none(*_: Any, **__: Any) -> None:
        return None

    async def _zero(*_: Any, **__: Any) -> int:
        return 0

    async def _audit(event_type: str, **kw: Any) -> str:
        recorded.audit.append({"event_type": event_type, **kw})
        return "hash"

    monkeypatch.setattr(cf, "check_on_commit", _check)
    monkeypatch.setattr(cf, "mark_peers_conflicted", _none)
    monkeypatch.setattr("server.observability.audit.emit", _audit)
    monkeypatch.setattr("server.observability.audit.emit_strict", _audit)
    monkeypatch.setattr(mr, "record_usage_event", _none)
    monkeypatch.setattr("server.memory.app.key_supersession.supersede_same_key_rows", _zero)
    monkeypatch.setattr(set_commit, "_project_set_entities_fail_open", _none)
    monkeypatch.setattr(mem, "get_client", lambda: client)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: client)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: client)
    # The blacklist cache is process state: start it dirty and put it back after.
    import server.memory.app.blacklist_guard as bl

    for name, value in (("_cache", []), ("_dirty", True), ("_cached_version", -1)):
        monkeypatch.setattr(bl, name, value)
    monkeypatch.setattr(bl, "_pending_hits", {})
    return recorded


async def one_holder(*_: Any, **__: Any) -> list[str]:
    """`namespaces_of_id` for a test whose stubbed `get_by_id` answers from one namespace."""
    return ["ns"]


def stub_status_before(monkeypatch: pytest.MonkeyPatch, status: str = "") -> list[mr.MemoryRow]:
    """Replaces the status-before read an Undo makes: each row it asks about is recorded and
    answered `status`."""
    asked: list[mr.MemoryRow] = []

    async def before(row: mr.MemoryRow, *, ch: Any = None) -> str:
        asked.append(row)
        return status

    monkeypatch.setattr(mr, "status_before", before)
    return asked
