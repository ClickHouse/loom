"""The first write into an unregistered namespace inserts the registry row
`namespace.register` would have written — owner = subject (none on a name in use),
kind by prefix, retention_days 0 — and nothing else: no ALTER, no TTL."""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from server.identity.app import namespace_autoregister as ar
from server.operations.registry import OPERATIONS_BY_NAME
from server.settings import settings

ROOT = Path(__file__).resolve().parents[2]


class _RecordingCH:
    """Records every statement the path emits, whatever the method."""

    def __init__(self, existing: bool = False, fail: bool = False) -> None:
        self.existing = existing
        self.fail = fail
        self.statements: list[tuple[str, str, Any]] = []
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        if self.fail:
            raise RuntimeError("ch down")
        self.statements.append(("query_one", sql, params))
        return {"present": 1} if self.existing else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.statements.append(("query_all", sql, params))
        return []

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.statements.append(("execute", sql, params))

    async def command(self, sql: str, *a: Any, **kw: Any) -> None:
        self.statements.append(("command", sql, None))

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.statements.append(("insert", table, None))
        self.inserts.append((table, rows))


class _Auth:
    def __init__(self, subject: str = "b5-memo-bench") -> None:
        self.subject = subject


@pytest.fixture(autouse=True)
def _fresh_cache() -> None:
    ar.forget_all()


def _texts(ch: _RecordingCH) -> str:
    return "\n".join(sql for _, sql, _ in ch.statements)


@pytest.mark.asyncio
async def test_first_write_inserts_the_register_row_with_no_ttl_side_effect() -> None:
    ch = _RecordingCH()
    inserted = await ar.ensure_registered(
        "clickhouse", "lme-e1-abc", subject="b5-memo-bench", op_name="memory.set", ch=ch
    )
    assert inserted is True
    tables = [t for t, _ in ch.inserts]
    # The owner row is the only write: no grant (an owner row is not
    # elevation — a shared writer subject would gain admin on every
    # namespace it touched and its wildcard-token searches would fan out
    # across all of them), no DDL.
    assert tables == ["loom.namespaces"]
    row = ch.inserts[0][1][0]
    assert row["owner_email"] == "b5-memo-bench"
    assert row["kind"] == "bench"
    assert row["retention_days"] == 0 and row["warm_days"] == 0
    assert row["active"] is True and row["visibility"] == "private"
    assert row["conversation_logging_enabled"] is False
    assert ar.AUTO_REGISTER_TAG in row["tags"] and "bench" in row["tags"]
    # The gate of the record: zero DDL from this path, of any shape. The in-use read
    # names the grants table; nothing else may.
    text = "\n".join(sql for _, sql, _ in ch.statements if "AS in_use" not in sql).upper()
    for forbidden in ("ALTER", "MODIFY TTL", "MATERIALIZE TTL", "TTL ", "NAMESPACE_GRANTS"):
        assert forbidden not in text, f"auto-register emitted {forbidden!r}"
    kinds = [k for k, _, _ in ch.statements]
    assert kinds == ["query_one", "query_one", "insert"]


@pytest.mark.asyncio
async def test_the_row_is_the_register_handlers_row() -> None:
    """Same builder, same defaults: what a bare write creates is what the
    form would have created for owner/kind/retention 0."""
    from datetime import datetime

    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    ch = _RecordingCH()
    await ar.ensure_registered("o", "team-kb", subject="alice", op_name="memory.set", ch=ch)
    auto = ch.inserts[0][1][0]
    form = registry_row(
        NamespaceRegisterInput(org="o", namespace="team-kb", owner_email="alice"),
        None,
        datetime(2026, 1, 1),
    )
    for key in form:
        if key in ("description", "tags", "created_at", "updated_at"):
            continue
        assert auto[key] == form[key], key
    assert auto["kind"] == "memory"
    assert set(auto) == set(form)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "kind"),
    [
        ("lme-e1-abc", "bench"),
        ("bench-run-7", "bench"),
        ("code-acme", "memory"),
        ("team-kb", "memory"),
    ],
)
async def test_register_and_first_write_classify_a_fresh_name_alike(name: str, kind: str) -> None:
    """`namespace.register` with no kind and a bare first write land the same kind for a
    fresh name, so registering a bench corpus first does not keep it out of the gc and
    the bench traffic class. A code namespace is `code` only when the code plane says so."""
    from datetime import datetime

    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    ch = _RecordingCH()
    await ar.ensure_registered("o", name, subject="alice", op_name="memory.set", ch=ch)
    form = registry_row(
        NamespaceRegisterInput(org="o", namespace=name, owner_email="alice"),
        None,
        datetime(2026, 1, 1),
    )
    assert ch.inserts[0][1][0]["kind"] == form["kind"] == kind


def test_register_kind_explicit_or_stored_beats_the_name() -> None:
    from datetime import datetime

    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row

    now = datetime(2026, 1, 1)
    explicit = NamespaceRegisterInput(org="o", namespace="lme-x", owner_email="a", kind="memory")
    assert registry_row(explicit, None, now)["kind"] == "memory"
    code = NamespaceRegisterInput(org="o", namespace="lme-x", owner_email="a", kind="code")
    stored = registry_row(code, None, now)
    assert stored["kind"] == "code"
    bare = NamespaceRegisterInput(org="o", namespace="lme-x", owner_email="a")
    assert registry_row(bare, stored, now)["kind"] == "code"


def test_owner_of_an_auto_registered_row_may_complete_its_registration() -> None:
    from server.identity.app.namespace import _is_auto_registered_owner

    auto_row = {"owner_email": "bob@x", "tags": [ar.AUTO_REGISTER_TAG, "bench"]}
    assert _is_auto_registered_owner(auto_row, "bob@x")
    assert not _is_auto_registered_owner(auto_row, "eve@x")
    assert not _is_auto_registered_owner(auto_row, "")
    # a row registered through the form keeps the admin-grant rule
    assert not _is_auto_registered_owner({"owner_email": "bob@x", "tags": []}, "bob@x")


class _InUseCH(_RecordingCH):
    """No registry row, but the name already holds state."""

    async def query_one(self, sql: str, params: dict[str, Any] | None = None):
        self.statements.append(("query_one", sql, params))
        return {"in_use": 1} if "AS in_use" in sql else None


@pytest.mark.asyncio
async def test_a_first_write_into_a_name_in_use_records_no_owner() -> None:
    from server.identity.app.namespace import _is_auto_registered_owner

    ch = _InUseCH()
    assert await ar.ensure_registered("o", "legacy", subject="w", op_name="memory.set", ch=ch)
    [(_, [row])] = ch.inserts
    assert row["owner_email"] == ""
    assert not _is_auto_registered_owner(row, "w")
    # A racing first write that found the name free wins under FINAL.
    assert row["updated_at"] < ar.AUTO_ROW_VERSION


@pytest.mark.asyncio
async def test_a_name_postgres_holds_memory_of_records_no_owner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import ledger_reads

    async def held(org: str, namespace: str) -> bool:
        return True

    monkeypatch.setattr(ledger_reads, "pg_holds_namespace", held)
    ch = _RecordingCH()
    assert await ar.ensure_registered("o", "legacy", subject="w", op_name="memory.set", ch=ch)
    [(_, [row])] = ch.inserts
    assert row["owner_email"] == ""


@pytest.mark.asyncio
async def test_rows_a_read_left_do_not_take_the_name_from_its_first_writer() -> None:
    from server.erasure.app import ledger_reads

    class _TracedCH(_RecordingCH):
        """In use when the statement reads any of `held`."""

        def __init__(self, held: set[str]) -> None:
            super().__init__()
            self.held = held

        async def query_one(self, sql: str, params: dict[str, Any] | None = None):
            self.statements.append(("query_one", sql, params))
            found = any(f"FROM {t} WHERE" in sql for t in self.held)
            return {"in_use": 1} if "AS in_use" in sql and found else None

    assert ar.READ_TRACES <= set(ledger_reads.namespace_tables())
    for held, owner in ((set(ar.READ_TRACES), "w"), ({"loom.memory"}, "")):
        ar.forget_all()
        ch = _TracedCH(held)
        assert await ar.ensure_registered("o", "n", subject="w", op_name="memory.set", ch=ch)
        [(_, [row])] = ch.inserts
        assert row["owner_email"] == owner


@pytest.mark.asyncio
async def test_existing_row_is_not_rewritten_and_is_cached() -> None:
    ch = _RecordingCH(existing=True)
    assert await ar.ensure_registered("o", "n", subject="s", op_name="memory.set", ch=ch) is False
    assert ch.inserts == []
    assert await ar.ensure_registered("o", "n", subject="s", op_name="memory.set", ch=ch) is False
    assert len(ch.statements) == 1, "the second call is a set lookup, no read"


@pytest.mark.asyncio
async def test_registry_failure_never_raises_and_does_not_cache() -> None:
    ch = _RecordingCH(fail=True)
    assert await ar.ensure_registered("o", "n", subject="s", op_name="memory.set", ch=ch) is False
    assert ("o", "n") not in ar._known
    ch.fail = False
    assert await ar.ensure_registered("o", "n", subject="s", op_name="memory.set", ch=ch) is True


def test_kind_follows_the_bench_prefix_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    assert ar.classify_kind("lme-9f2a") == "bench"
    assert ar.classify_kind("mab-cr-7") == "bench"
    assert ar.classify_kind("ma-verbatim-3") == "bench"
    assert ar.classify_kind("ma-other") == "memory"
    assert ar.classify_kind("demo-full") == "memory"
    monkeypatch.setattr(settings, "namespace_bench_prefixes", "zz-")
    assert ar.classify_kind("lme-9f2a") == "memory" and ar.classify_kind("zz-1") == "bench"


def test_should_autoregister_is_write_shaped_and_skips_the_operator_planes() -> None:
    ops = OPERATIONS_BY_NAME
    assert ar.should_autoregister(ops["memory.set"], "o", "n")
    assert ar.should_autoregister(ops["memory.bulk_set"], "o", "n")
    assert ar.should_autoregister(ops["knowledge.add"], "o", "n")
    # reads never register
    assert not ar.should_autoregister(ops["memory.search"], "o", "n")
    assert not ar.should_autoregister(ops["memory.get"], "o", "n")
    # ops ON a namespace, an org admin's ops, the code plane and the operator planes
    # are skipped
    for name in (
        "extraction_policy.set",
        "extraction_policy.clear",
        "org.set_model_calls",
        "namespace.register",
        "namespace.delete",
        "code.reindex",
        "blacklist.add",
        "grants.create",
        "policy.set",
    ):
        assert not ar.should_autoregister(ops[name], "o", "n"), name
    # a missing or wildcard identity registers nothing
    for org, ns in ((None, "n"), ("o", None), ("-", "n"), ("o", "-"), ("o", "*"), ("", "n")):
        assert not ar.should_autoregister(ops["memory.set"], org, ns)


def test_disabled_by_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "namespace_autoregister_enabled", False)
    assert not ar.should_autoregister(OPERATIONS_BY_NAME["memory.set"], "o", "n")


@pytest.mark.asyncio
async def test_hook_requires_an_authenticated_subject(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, str]] = []

    async def _fake(org, ns, *, subject, op_name, ch=None):
        calls.append((org, ns, subject))
        return True

    monkeypatch.setattr(ar, "ensure_registered", _fake)
    op = OPERATIONS_BY_NAME["memory.set"]
    assert await ar.maybe_autoregister(op, org="o", namespace="n", auth=None) is False
    assert await ar.maybe_autoregister(op, org="o", namespace="n", auth=_Auth("")) is False
    assert await ar.maybe_autoregister(op, org="o", namespace="n", auth=_Auth("bob")) is True
    assert calls == [("o", "n", "bob")]


def test_both_transports_call_the_hook_after_authorization() -> None:
    """Both transports run the shared gate (`gates.authorize`), which calls
    the post-gate step (`_admitted`) after `authorize_op` (which runs
    `check_authorized`), and that step is what calls the hook."""
    for rel in ("server/transport/http.py", "server/transport/mcp_call.py"):
        src = (ROOT / rel).read_text()
        assert "await gates.authorize(" in src, rel
    gate = (ROOT / "server/transport/gates.py").read_text()
    assert gate.index("await authorize_op(") < gate.index("await _admitted(")
    body = gate.split("async def _admitted(", 1)[1]
    assert "maybe_autoregister(" in body
    assert body.index("note_restricted_access(") < body.index("maybe_autoregister(")


def test_a_retention_zero_row_changes_no_ttl_clause() -> None:
    """The reconcile worker recomposes table TTLs from registry rows with a
    non-zero retention; a row the hook writes carries 0 and is not even
    fetched."""
    from server.storage import retention_overrides as ro

    src = inspect.getsource(ro._fetch_namespace_overrides)
    assert "retention_days > 0 OR warm_days > 0" in src
    auto_row = {"org": "o", "namespace": "lme-x", "retention_days": 0, "warm_days": 0}
    with_row = ro._retention_clause(ro.TARGETS[0], global_days=0, namespace_overrides=[auto_row])
    without = ro._retention_clause(ro.TARGETS[0], global_days=0, namespace_overrides=[])
    assert with_row == without is None


def test_bench_kind_is_a_valid_register_kind() -> None:
    from server.identity.api.models import NamespaceRegisterInput

    inp = NamespaceRegisterInput(org="o", namespace="lme-1", owner_email="x", kind="bench")
    assert inp.kind == "bench"


@pytest.mark.asyncio
async def test_a_personal_collection_registers_on_its_first_write() -> None:
    from server.core.principals import personal_name

    ch = _RecordingCH()
    name = personal_name("o", "user:a")
    assert await ar.ensure_registered(
        "o", name, subject="user:a", op_name="knowledge.upsert", ch=ch
    )
    [(table, [row])] = ch.inserts
    assert (table, row["namespace"], row["kind"], row["owner_email"]) == (
        "loom.namespaces",
        name,
        "personal",
        "user:a",
    )
