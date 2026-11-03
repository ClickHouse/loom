"""Namespace-scoped ClickStack erasure must match the TENANT namespace key.

Loom's exporter sets `service.namespace` to the constant "loom" (the
deployment) and carries the tenant namespace as the `loom.namespace`
span/metric attribute. Matching only service.namespace deleted zero rows
while reporting success. The WHERE clause must OR both keys.
"""

from __future__ import annotations

import pytest

from server.erasure.app.backends import ClickStackBackend
from tests.unit.clickhouse_local import CLICKHOUSE, run


@pytest.mark.asyncio
async def test_namespace_where_matches_tenant_attribute(monkeypatch):
    captured: list[tuple[str, str]] = []

    backend = ClickStackBackend(database="default", timeout_s=1.0)

    async def fake_delete(fq, where, params):
        captured.append((fq, where))
        return 0

    monkeypatch.setattr(backend, "_delete_where", fake_delete)
    await backend.delete_namespace("org", "code-clickhouse")

    assert captured, "no tables visited"
    for fq, where in captured:
        assert "'loom.namespace'" in where, (
            f"{fq}: WHERE must match the tenant attribute key: {where}"
        )
        assert "'service.namespace'" in where, (
            f"{fq}: legacy resource key must stay ORed in: {where}"
        )


@pytest.mark.asyncio
async def test_traces_use_span_attributes_for_tenant_key(monkeypatch):
    backend = ClickStackBackend(database="default", timeout_s=1.0)
    captured: dict[str, str] = {}

    async def fake_delete(fq, where, params):
        captured[fq] = where
        return 0

    monkeypatch.setattr(backend, "_delete_where", fake_delete)
    await backend.delete_namespace("org", "ns1")

    traces = captured["default.otel_traces"]
    # tenant key lives in the span ATTRIBUTE column, not ResourceAttributes
    assert "SpanAttributes['loom.namespace']" in traces


# ---------------------------------------------------------------------------
# A namespace_list erasure is one pass per backend.
# ---------------------------------------------------------------------------


async def test_clickstack_erases_a_list_with_one_predicate_per_table(monkeypatch):
    from server.erasure.app.backends import ClickStackBackend, delete_namespaces

    backend = ClickStackBackend(database="default", timeout_s=5.0)
    calls: list[tuple[str, str, dict]] = []

    async def fake_delete(fq_table, where, params):
        calls.append((fq_table, where, params))
        return 2

    monkeypatch.setattr(backend, "_delete_where", fake_delete)
    n = await delete_namespaces(backend, "org", ["lme-a", "lme-b", "lme-c"])
    assert n == 2 * len(ClickStackBackend.DEFAULT_TABLES)
    assert len(calls) == len(ClickStackBackend.DEFAULT_TABLES)  # one per table, not per namespace
    fq, where, params = calls[0]
    assert fq == "default.otel_logs"
    assert "ResourceAttributes['service.namespace'] IN {u:Array(String)}" in where
    assert "LogAttributes['loom.namespace'] IN {u:Array(String)}" in where
    assert params == {"u": ["lme-a", "lme-b", "lme-c"], "org": "org"}


async def test_a_single_namespace_keeps_the_equality_predicate(monkeypatch):
    from server.erasure.app.backends import ClickStackBackend, delete_namespaces

    backend = ClickStackBackend(database="default", timeout_s=5.0)
    calls: list[tuple[str, str, dict]] = []

    async def fake_delete(fq_table, where, params):
        calls.append((fq_table, where, params))
        return 0

    monkeypatch.setattr(backend, "_delete_where", fake_delete)
    await delete_namespaces(backend, "org", ["only"])
    assert calls and "= {u:String}" in calls[0][1] and calls[0][2] == {"u": "only", "org": "org"}


async def test_a_backend_without_a_list_method_is_called_per_namespace():
    from server.erasure.app.backends import delete_namespaces

    class _PerNs:
        name = "x"

        def __init__(self) -> None:
            self.seen: list[str] = []

        async def delete_namespace(self, org: str, ns: str) -> int:
            self.seen.append(ns)
            return 1

    b = _PerNs()
    assert await delete_namespaces(b, "org", ["a", "b", "c"]) == 3
    assert b.seen == ["a", "b", "c"]


async def test_langfuse_list_runs_lookups_concurrently_and_sums(monkeypatch):
    import asyncio

    from server.erasure.app.backends import LangfuseBackend

    b = LangfuseBackend("https://lf.example", "tok", 5.0)
    active = 0
    peak = 0

    async def fake_one(org: str, ns: str) -> int:
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return 1

    monkeypatch.setattr(b, "delete_namespace", fake_one)
    assert await b.delete_namespaces("org", [f"ns{i}" for i in range(10)]) == 10
    assert 1 < peak <= b._LIST_CONCURRENCY


async def test_clickstack_delete_is_awaited_by_state(monkeypatch):
    """The OTel DELETE goes through mutations.run_lightweight_delete
    (the `_row_exists` mask, not a part rewrite;
    lightweight_deletes_sync=0 + tagged system.mutations poll), not a
    synchronous ALTER."""
    from server.erasure.app import backends as eb
    from server.erasure.store import mutations as erasure_mutations

    class _CH:
        def __init__(self) -> None:
            self.commands: list[tuple[str, dict]] = []

        async def query_one(self, sql, params=None):
            return {"n": 3}

        async def execute(self, sql, params=None):
            self.commands.append((" ".join(sql.split()), dict(params or {})))

        async def query_all(self, sql, params=None):
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    ch = _CH()
    import server.core.ch as chmod

    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    monkeypatch.setattr(erasure_mutations, "_default_client", lambda: ch)
    backend = eb.ClickStackBackend(
        database="default", timeout_s=5.0, tables=[("otel_logs", "LogAttributes")]
    )
    n = await backend.delete_namespaces("org", ["a", "b"])
    assert n == 3 and len(ch.commands) == 1
    sql, params = ch.commands[0]
    assert (
        sql.startswith("DELETE FROM default.otel_logs WHERE")
        and "lightweight_deletes_sync = 0" in sql
    )
    assert "ALTER TABLE" not in sql
    assert params["u"] == ["a", "b"] and params["erasure_mutation_tag"].startswith("erasure:")


class _PrecountFails:
    def __init__(self, error: Exception) -> None:
        self.error = error
        self.commands: list[str] = []

    async def query_one(self, sql, params=None):
        raise self.error

    async def execute(self, sql, params=None):  # pragma: no cover - must not run
        self.commands.append(sql)


async def test_clickstack_precount_on_a_missing_table_is_nothing_to_erase(monkeypatch):
    from server.erasure.app import backends as eb

    for msg in (
        "Code: 60. DB::Exception: Unknown table expression identifier 'default.otel_logs'",
        "Code: 81. DB::Exception: Database otel does not exist. (UNKNOWN_DATABASE)",
    ):
        ch = _PrecountFails(RuntimeError(msg))
        monkeypatch.setattr("server.core.ch.get_client", lambda ch=ch: ch)
        backend = eb.ClickStackBackend(
            database="default", timeout_s=5.0, tables=[("otel_logs", "LogAttributes")]
        )
        assert await backend.delete_user_id("org", "u1") == 0
        assert ch.commands == []


async def test_clickstack_precount_that_fails_otherwise_fails_the_step(monkeypatch):
    """A precount that times out or is refused has not shown the table is
    empty: returning 0 would record the step completed with the rows still
    there, and the recovery worker would never retry it."""
    import pytest

    from server.erasure.app import backends as eb

    for err in (
        TimeoutError(),
        RuntimeError("Code: 241. DB::Exception: Memory limit exceeded (MEMORY_LIMIT_EXCEEDED)"),
        RuntimeError("Code: 497. DB::Exception: Not enough privileges (ACCESS_DENIED)"),
    ):
        ch = _PrecountFails(err)
        monkeypatch.setattr("server.core.ch.get_client", lambda ch=ch: ch)
        backend = eb.ClickStackBackend(
            database="default", timeout_s=5.0, tables=[("otel_logs", "LogAttributes")]
        )
        with pytest.raises(eb.ErasureBackendError, match="precount"):
            await backend.delete_namespace("org", "ns")
        assert ch.commands == []


async def test_clickstack_user_erasure_never_matches_another_org(monkeypatch):
    """A subject name is unique per org only: a row carrying another org's
    `loom.org` is never matched; a row with no org tag (a non-Loom emitter)
    still is."""

    backend = ClickStackBackend(database="default", timeout_s=5.0)
    calls: list[tuple[str, str, dict]] = []

    async def fake_delete(fq_table, where, params):
        calls.append((fq_table, where, params))
        return 0

    monkeypatch.setattr(backend, "_delete_where", fake_delete)
    await backend.delete_user_id("acme", "bob")
    for fq, where, params in calls:
        col = dict(ClickStackBackend.DEFAULT_TABLES)[fq.split(".", 1)[1]]
        assert f"AND {col}['loom.org'] IN ({{org:String}}, '')" in where, where
        assert params["org"] == "acme"


_ROWS = {
    # Loom's own metric point: `loom.namespace`, no `loom.org`, and Loom's
    # deployment constant in the resource.
    "loom_untagged": ({"service.namespace": "loom"}, {"loom.namespace": "default"}),
    "loom_other_org": (
        {"service.namespace": "loom"},
        {"loom.namespace": "default", "loom.org": "globex"},
    ),
    "loom_own_org": (
        {"service.namespace": "loom"},
        {"loom.namespace": "default", "loom.org": "acme"},
    ),
    # A tenant namespace named `loom`: untagged, in this org and in another.
    "ns_loom_untagged": ({"service.namespace": "loom"}, {"loom.namespace": "loom"}),
    "ns_loom_own_org": (
        {"service.namespace": "loom"},
        {"loom.namespace": "loom", "loom.org": "acme"},
    ),
    "ns_loom_other_org": (
        {"service.namespace": "loom"},
        {"loom.namespace": "loom", "loom.org": "globex"},
    ),
    # Loom rows of any org with no tenant attribute (a worker span, a log line).
    "loom_deployment": ({"service.namespace": "loom"}, {}),
    "foreign_emitter": ({"service.namespace": "default"}, {"service.namespace": "default"}),
    "foreign_own_org": (
        {"service.namespace": "default"},
        {"service.namespace": "default", "loom.org": "acme"},
    ),
    "foreign_other_org": (
        {"service.namespace": "default"},
        {"service.namespace": "default", "loom.org": "globex"},
    ),
}


@pytest.mark.skipif(CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.parametrize("many", [False, True])
@pytest.mark.parametrize(
    ("table", "attr_col"),
    [
        ("otel_metrics_sum", "Attributes"),
        ("otel_traces", "SpanAttributes"),
        ("hyperdx_sessions", "ResourceAttributes"),
    ],
)
@pytest.mark.parametrize("namespace", ["default", "loom"])
def test_namespace_erasure_matches_only_this_orgs_rows(table, attr_col, many, namespace):
    """A namespace name is unique per org only. A row keyed by Loom's
    `loom.namespace` is this org's only when it also carries this org's
    `loom.org`; Loom's op and domain metrics carry no org, so they are never matched
    by name alone. Evaluated on clickhouse-local over the predicate the
    backend builds."""
    backend = ClickStackBackend(database="default", timeout_s=5.0)
    where = backend._namespace_where(table, attr_col, many=many)
    if attr_col == "ResourceAttributes":  # one map holds both
        cols = "ResourceAttributes Map(String, String)"
        rows = ", ".join(f"('{t}', map({_map({**r, **a})}))" for t, (r, a) in _ROWS.items())
    else:
        cols = f"ResourceAttributes Map(String, String), {attr_col} Map(String, String)"
        rows = ", ".join(f"('{t}', map({_map(r)}), map({_map(a)}))" for t, (r, a) in _ROWS.items())
    setup = [
        f"CREATE TABLE loom.{table} (tag String, {cols}) ENGINE = Memory",
        f"INSERT INTO loom.{table} VALUES {rows}",
    ]
    got = run(
        setup,
        f"SELECT tag FROM loom.{table} WHERE {where} ORDER BY tag",
        {"u": [namespace] if many else namespace, "org": "acme"},
    )
    expected = (
        ["foreign_emitter", "foreign_own_org", "loom_own_org"]
        if namespace == "default"
        else ["ns_loom_own_org"]
    )
    assert [r["tag"] for r in got] == expected


def _map(d: dict[str, str]) -> str:
    return ", ".join(f"'{k}', '{v}'" for k, v in d.items())
