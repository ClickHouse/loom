"""External erasure backends — Langfuse and ClickStack (mock HTTP)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from server.erasure.app.backends import (  # noqa: E402
    ClickStackBackend,
    ErasureBackendError,
    LangfuseBackend,
    StubBackend,
    all_backends,
)


@pytest.fixture(autouse=True)
def _isolate_settings():
    # Reset shared settings singleton; re-bind eb.settings if test_auth reloaded it.
    import server.erasure.app.backends as eb
    from server.settings import settings

    if eb.settings is not settings:
        eb.settings = settings
    saved = {
        k: getattr(settings, k)
        for k in (
            "langfuse_url",
            "langfuse_admin_token",
            "clickstack_database",
            "clickstack_user_id_keys",
        )
    }
    settings.langfuse_url = ""
    settings.langfuse_admin_token = ""
    settings.clickstack_database = ""
    settings.clickstack_user_id_keys = "user_id,user.id,enduser.id"
    yield
    for k, v in saved.items():
        setattr(settings, k, v)


class _MockServer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.next_status = 200
        self.next_body: dict[str, Any] = {}
        # Langfuse trace-lookup responses.
        self.trace_ids: list[str] = []
        self.lookup_status = 200
        self.total_pages = 1
        self.omit_total_pages = False
        # The server version the health probe reports; it picks the lookup.
        self.langfuse_version = "3.227.0"

    async def handler(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                import json

                body = json.loads(request.content)
            except ValueError:
                body = request.content.decode("utf-8", errors="ignore")
        self.calls.append(
            {
                "method": request.method,
                "url": str(request.url),
                "auth": request.headers.get("Authorization", ""),
                "body": body,
            }
        )
        if request.method == "GET" and "/api/public/health" in str(request.url):
            return httpx.Response(200, json={"status": "OK", "version": self.langfuse_version})
        if request.method == "GET" and "/api/public/v2/observations" in str(request.url):
            if self.lookup_status != 200:
                return httpx.Response(self.lookup_status, json={})
            rows = [{"id": f"o-{t}", "traceId": t} for t in self.trace_ids]
            return httpx.Response(200, json={"data": rows, "meta": {}})
        # Langfuse erasure is query-then-delete, so a GET of /traces must return
        # a page shaped like the real API. `trace_ids` is what the lookup finds.
        if request.method == "GET" and "/api/public/traces" in str(request.url):
            if self.lookup_status != 200:
                return httpx.Response(self.lookup_status, json={})
            meta: dict[str, Any] = {
                "page": 1,
                "totalItems": len(self.trace_ids) * self.total_pages,
            }
            if not self.omit_total_pages:
                meta["totalPages"] = self.total_pages
            return httpx.Response(
                200,
                json={"data": [{"id": t} for t in self.trace_ids], "meta": meta},
            )
        return httpx.Response(self.next_status, json=self.next_body)


@pytest.fixture
def mock_server(monkeypatch):
    server = _MockServer()
    transport = httpx.MockTransport(server.handler)

    original_init = httpx.AsyncClient.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["transport"] = transport
        return original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", patched_init)
    yield server


# --- Langfuse ---


@pytest.mark.asyncio
async def test_langfuse_delete_user_basic_auth_with_pk_sk_pair(mock_server) -> None:
    import base64

    mock_server.next_status = 204
    mock_server.trace_ids = ["t1"]
    backend = LangfuseBackend(
        "http://lf.local",
        "pk-lf-abc:sk-lf-xyz",
        5.0,
    )
    n = await backend.delete_user_id("ch", "alice@x.com")
    assert n == 1
    expected_auth = base64.b64encode(b"pk-lf-abc:sk-lf-xyz").decode()
    # BOTH legs are authenticated: the lookup and the delete. An unauthenticated
    # lookup returns zero traces, which erasure would read as "nothing to erase".
    assert mock_server.calls, "no HTTP call was made"
    for call in mock_server.calls:
        assert call["auth"] == f"Basic {expected_auth}", call["url"]
    assert mock_server.calls[-1]["method"] == "DELETE"
    assert mock_server.calls[-1]["url"] == "http://lf.local/api/public/traces"


@pytest.mark.asyncio
async def test_langfuse_bearer_fallback_for_non_pk_sk(mock_server) -> None:
    # Token without `:` → Bearer.
    mock_server.next_status = 204
    backend = LangfuseBackend("http://lf.local", "tok-lf", 5.0)
    await backend.delete_user_id("ch", "alice@x.com")
    assert mock_server.calls[-1]["auth"] == "Bearer tok-lf"


@pytest.mark.asyncio
async def test_langfuse_no_matching_traces_returns_zero(mock_server) -> None:
    """Nothing to erase is a success, and must not issue a DELETE."""
    mock_server.trace_ids = []
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    n = await backend.delete_user_id("ch", "ghost@x.com")
    assert n == 0
    assert all(c["method"] != "DELETE" for c in mock_server.calls)


@pytest.mark.asyncio
async def test_langfuse_delete_user_finds_then_deletes_traces(mock_server) -> None:
    """User erasure is query-then-delete: filter traces by `userId`, then
    `DELETE /api/public/traces` with a `{"traceIds": [...]}` body. Deleting a
    trace takes its observations and scores with it, so the generations and the
    quality scores go too.
    """
    mock_server.trace_ids = ["t1", "t2", "t3"]
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    n = await backend.delete_user_id("ch", "alice@x.com")
    assert n == 3

    lookup = next(c for c in mock_server.calls if "/api/public/traces" in c["url"])
    assert "userId=alice%40x.com" in lookup["url"] or "userId=alice@x.com" in lookup["url"]
    deleted = [c for c in mock_server.calls if c["method"] == "DELETE"]
    assert len(deleted) == 1
    assert deleted[0]["url"] == "http://lf.local/api/public/traces"
    assert deleted[0]["body"] == {"traceIds": ["t1", "t2", "t3"]}
    # Langfuse has no per-user delete endpoint.
    assert all("/api/public/users/" not in c["url"] for c in mock_server.calls)


@pytest.mark.asyncio
async def test_langfuse_4_finds_traces_through_observations(mock_server) -> None:
    """Langfuse 4 answers 404 to the trace list; the lookup reads the
    observations list instead, filtered by the org tag and the user id."""
    mock_server.langfuse_version = "4.76.0"
    mock_server.trace_ids = ["t1", "t2"]
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    assert await backend.delete_user_id("ch", "alice@x.com") == 2
    assert all("/api/public/traces?" not in c["url"] for c in mock_server.calls)
    lookup = next(c for c in mock_server.calls if "/api/public/v2/observations" in c["url"])
    assert "alice%40x.com" in lookup["url"] and "org%3Ach" in lookup["url"]
    deleted = [c for c in mock_server.calls if c["method"] == "DELETE"]
    assert deleted[0]["body"] == {"traceIds": ["t1", "t2"]}


@pytest.mark.asyncio
async def test_langfuse_rerun_with_nothing_found_issues_no_delete(mock_server) -> None:
    """A re-run must not fail, and must not fake success either.

    `erasure_recovery` re-runs failed sagas, so a repeat pass has to be safe. A
    re-run whose lookup finds no matching traces returns 0 without issuing a
    delete at all; one that finds traces still being deleted deletes them again,
    which the delete endpoint answers with 200.

    It does NOT come from treating 404 on the delete as success, which would
    fail open. The bulk endpoint
    returns 200 even for ids that do not exist, so a 404 means the endpoint is
    missing and nothing was deleted.
    """
    mock_server.trace_ids = []
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    assert await backend.delete_user_id("ch", "alice") == 0
    assert all(c["method"] != "DELETE" for c in mock_server.calls)


@pytest.mark.asyncio
async def test_langfuse_404_on_delete_fails_closed(mock_server) -> None:
    """A missing delete endpoint must fail the saga, not report erasure.

    On a deployment where `DELETE /api/public/traces` is absent — an older
    self-hosted build, a proxy forwarding only GET/POST — the lookup still returns
    real ids and every delete 404s. Counting those as deleted would report a GDPR
    erasure as satisfied while every trace, its generations and its quality scores
    stayed in Langfuse.
    """
    mock_server.trace_ids = ["t1"]
    mock_server.next_status = 404
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    with pytest.raises(ErasureBackendError):
        await backend.delete_user_id("ch", "alice")


@pytest.mark.asyncio
async def test_langfuse_user_erasure_is_scoped_to_the_org(mock_server) -> None:
    """Unscoped user erasure can destroy another tenant's traces.

    `user_id` is a caller-supplied opaque string — an email, a JWT subject — and
    loom writes every org into ONE Langfuse project. Filtering on `userId` alone
    means a GDPR request from org A deletes org B's traces for anyone sharing an
    identifier: irrecoverable, with no error and nothing in the audit trail.
    `delete_namespace` ANDs the org tag for the same reason.
    """
    mock_server.trace_ids = ["t1"]
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    await backend.delete_user_id("clickhouse", "alice@x.com")
    lookup = next(c for c in mock_server.calls if "/api/public/traces" in c["url"])
    assert "org%3Aclickhouse" in lookup["url"], (
        f"user erasure must be org-scoped; got {lookup['url']}"
    )


@pytest.mark.asyncio
async def test_langfuse_refuses_to_erase_a_truncated_set(mock_server) -> None:
    """Deleting the first N of a larger set and reporting success is worse than failing.

    The callers turn a returned count into a `completed` saga step the recovery
    worker never retries, so a truncated delete is silent, permanent
    non-erasure.
    """
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    # More pages than the cap allows, all non-empty.
    mock_server.trace_ids = [f"t{i}" for i in range(100)]
    mock_server.total_pages = 10_000
    with pytest.raises(ErasureBackendError) as ei:
        await backend.delete_namespace("clickhouse", "big-namespace")
    assert "truncated" in str(ei.value)


@pytest.mark.asyncio
async def test_langfuse_missing_total_pages_fails_closed(mock_server) -> None:
    """Without totalPages we cannot know whether more traces exist.

    Defaulting the field to the current page would end pagination after page 1
    and report 100 deleted traces out of any number as complete.
    """
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    mock_server.trace_ids = ["t1", "t2"]
    mock_server.omit_total_pages = True
    with pytest.raises(ErasureBackendError) as ei:
        await backend.delete_user_id("ch", "alice")
    assert "totalPages" in str(ei.value)


@pytest.mark.asyncio
async def test_langfuse_5xx_raises_fail_closed(mock_server) -> None:
    """A 5xx on the DELETE means the data may still be there → fail closed."""
    mock_server.trace_ids = ["t1"]
    mock_server.next_status = 500
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    with pytest.raises(ErasureBackendError):
        await backend.delete_user_id("ch", "alice")


@pytest.mark.slow
@pytest.mark.recovery
@pytest.mark.asyncio
async def test_langfuse_failed_lookup_raises_rather_than_reporting_zero(
    mock_server,
) -> None:
    """A broken lookup must not masquerade as "nothing to erase".

    If the trace query fails, whether
    the user's data still exists is unknown, and the saga must fail so the
    recovery worker retries instead of recording a false `completed`.
    """
    mock_server.lookup_status = 503
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    with pytest.raises(ErasureBackendError):
        await backend.delete_user_id("ch", "alice")


@pytest.mark.asyncio
async def test_langfuse_transport_error_raises_fail_closed(monkeypatch) -> None:
    def boom_init(self, *args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", boom_init)
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    with pytest.raises(ErasureBackendError):
        await backend.delete_user_id("ch", "alice")


@pytest.mark.asyncio
async def test_langfuse_namespace_5xx_raises(mock_server) -> None:
    mock_server.trace_ids = ["t1"]
    mock_server.next_status = 503
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    with pytest.raises(ErasureBackendError):
        await backend.delete_namespace("ch", "eng-tooling")


@pytest.mark.asyncio
async def test_langfuse_namespace_deletes_by_tag_scoped_to_the_org(
    mock_server,
) -> None:
    """Namespace erasure deletes the traces tagged with the namespace.

    Loom writes into the single project implied by one `pk:sk`, so erasure
    never deletes a project; spans carry namespace tags. The org tag is required as well as the namespace: namespaces are not globally
    unique, and erasing another org's traces would be far worse than erasing none.
    """
    mock_server.trace_ids = ["a", "b"]
    mock_server.next_status = 200
    backend = LangfuseBackend("http://lf.local", "tok", 5.0)
    n = await backend.delete_namespace("clickhouse", "eng-tooling")
    assert n == 2

    lookup = next(c for c in mock_server.calls if "/api/public/traces" in c["url"])
    assert "namespace%3Aeng-tooling" in lookup["url"]
    assert "org%3Aclickhouse" in lookup["url"], (
        "namespace erasure must be scoped to the org, or it can delete another "
        "tenant's traces that happen to share a namespace name"
    )
    assert all("/api/public/projects/" not in c["url"] for c in mock_server.calls)


# --- ClickStack ---


@pytest.mark.asyncio
async def test_clickstack_no_tables_returns_zero() -> None:
    backend = ClickStackBackend(database="default", timeout_s=5.0, tables=[])
    assert await backend.delete_user_id("ch", "carol@x.com") == 0
    assert await backend.delete_namespace("ch", "ml-team") == 0


@pytest.mark.asyncio
async def test_clickstack_issues_a_lightweight_delete(monkeypatch) -> None:
    calls: list[tuple[str, str, dict]] = []

    class FakeCH:
        async def query_one(self, sql: str, params=None):
            calls.append(("query", sql, params or {}))
            return {"n": 7}

        async def execute(self, sql: str, params=None):
            calls.append(("execute", sql, params or {}))

        async def query_all(self, sql, params=None):  # the saga's system.mutations poll
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: FakeCH())

    backend = ClickStackBackend(
        database="default",
        timeout_s=5.0,
        tables=[
            ("otel_logs", "LogAttributes"),
            ("otel_traces", "SpanAttributes"),
        ],
        user_id_keys=["user_id", "user.id"],
    )
    n = await backend.delete_user_id("ch", "carol@x.com")
    assert n == 14  # 7 per table × 2 tables

    # the DELETE is multi-line (awaited by tag) and lightweight (the _row_exists
    # mask, not a part rewrite); compare on one line
    queries = [" ".join(c[1].split()) for c in calls]
    assert any("SELECT count() AS n FROM default.otel_logs" in q for q in queries)
    assert any(q.startswith("DELETE FROM default.otel_logs WHERE") for q in queries)
    assert not any("ALTER TABLE" in q for q in queries)
    assert any("LogAttributes['user_id'] = {u:String}" in q for q in queries)
    assert any("LogAttributes['user.id'] = {u:String}" in q for q in queries)
    assert any("SpanAttributes['user_id'] = {u:String}" in q for q in queries)


@pytest.mark.asyncio
async def test_clickstack_skips_tables_with_zero_matches(monkeypatch) -> None:
    # Pre-count 0 → no ALTER DELETE.
    calls: list[tuple[str, str]] = []

    class FakeCH:
        async def query_one(self, sql, params=None):
            calls.append(("query", sql))
            return {"n": 0}

        async def execute(self, sql, params=None):
            calls.append(("execute", sql))

        async def query_all(self, sql, params=None):  # the saga's system.mutations poll
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: FakeCH())
    backend = ClickStackBackend(
        database="default",
        timeout_s=5.0,
        tables=[("otel_logs", "LogAttributes")],
    )
    n = await backend.delete_user_id("ch", "no-such-user")
    assert n == 0
    assert not any(c[0] == "execute" for c in calls), (
        "ALTER DELETE shouldn't fire when pre-count is 0"
    )


@pytest.mark.asyncio
async def test_clickstack_precount_failure_returns_zero(monkeypatch) -> None:
    # A pre-count on a table the service does not have stays a 0 no-op.
    class FakeCH:
        async def query_one(self, sql, params=None):
            raise RuntimeError("Code: 60. DB::Exception: Unknown table default.otel_logs")

        async def execute(self, sql, params=None):  # pragma: no cover
            raise AssertionError("execute must not run when precount failed")

        async def query_all(self, sql, params=None):  # the saga's system.mutations poll
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: FakeCH())
    backend = ClickStackBackend(
        database="default",
        timeout_s=5.0,
        tables=[("otel_logs", "LogAttributes")],
    )
    assert await backend.delete_user_id("ch", "carol@x.com") == 0


@pytest.mark.asyncio
async def test_clickstack_delete_failure_raises_fail_closed(monkeypatch) -> None:
    # Pre-count confirms rows, then ALTER DELETE fails → fail closed.
    class FakeCH:
        async def query_one(self, sql, params=None):
            return {"n": 5}

        async def execute(self, sql, params=None):
            raise RuntimeError("TIMEOUT_EXCEEDED during mutation")

        async def query_all(self, sql, params=None):  # the saga's system.mutations poll
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: FakeCH())
    backend = ClickStackBackend(
        database="default",
        timeout_s=5.0,
        tables=[("otel_logs", "LogAttributes")],
    )
    with pytest.raises(ErasureBackendError):
        await backend.delete_user_id("ch", "carol@x.com")


@pytest.mark.asyncio
async def test_clickstack_namespace_uses_resource_attributes(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class FakeCH:
        async def query_one(self, sql, params=None):
            calls.append(("query", sql))
            return {"n": 3}

        async def execute(self, sql, params=None):
            calls.append(("execute", sql))

        async def query_all(self, sql, params=None):  # the saga's system.mutations poll
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]

    monkeypatch.setattr("server.core.ch.get_client", lambda: FakeCH())
    backend = ClickStackBackend(
        database="default",
        timeout_s=5.0,
        tables=[
            ("otel_logs", "LogAttributes"),
            ("otel_metrics_gauge", "Attributes"),
        ],
    )
    n = await backend.delete_namespace("ch", "ml-team")
    assert n == 6  # 3 per table × 2

    queries = [c[1] for c in calls]
    assert any(
        "ResourceAttributes['service.namespace']" in q and "default.otel_logs" in q for q in queries
    )
    assert any(
        "Attributes['service.namespace']" in q and "default.otel_metrics_gauge" in q
        for q in queries
    )


# --- Registry + stub fallback ---


def test_all_backends_returns_two_with_default_settings() -> None:
    backends = all_backends()
    assert len(backends) == 2
    assert {b.name for b in backends} == {"langfuse", "clickstack"}
    assert all(isinstance(b, StubBackend) for b in backends)


@pytest.mark.asyncio
async def test_stub_backend_returns_zero_logs_intent() -> None:
    stub = StubBackend("test-system")
    assert await stub.delete_user_id("ch", "alice") == 0
    assert await stub.delete_namespace("ch", "ns") == 0
