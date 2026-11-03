"""External erasure backends — HTTP DELETE against the federated stack.

Erasure must cascade across every external system that holds personal
data. Each backend implements the `ExternalEraseBackend` protocol; the
saga calls `delete_user_id()` (per-user) or `delete_namespace()`
(whole-namespace teardown). Backends return the count of rows removed.
When a backend isn't configured, `_backend_for` returns a `StubBackend`
that logs intent and returns 0.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from typing import Any, ClassVar, Protocol

import httpx

from server.core.ch_errors import error_code
from server.observability.otel import SERVICE_NAMESPACE
from server.settings import settings

log = logging.getLogger(__name__)

# Truncate upstream error bodies before logging.
LOG_ERROR_TEXT_LIMIT = 200


def _describe(e: BaseException) -> str:
    """`str(httpx.ReadTimeout())` is empty, which would leave the saga's
    failure message as "transport error: "; name the type as well."""
    text = str(e).strip()
    return f"{type(e).__name__}: {text}" if text else type(e).__name__


# UNKNOWN_TABLE, UNKNOWN_DATABASE: the only precount failures that prove
# there is nothing to erase.
_MISSING_TABLE_CODES = frozenset({60, 81})


def _is_missing_table(e: BaseException) -> bool:
    code = error_code(e)
    if code is not None:
        return code in _MISSING_TABLE_CODES
    text = str(e)
    return "UNKNOWN_TABLE" in text or "UNKNOWN_DATABASE" in text


class ErasureBackendError(Exception):
    """A backend failed in a way that means data may NOT have been deleted
    (5xx, timeout, network drop, or a confirmed-rows-but-failed delete).

    MUST propagate so the saga records the request `failed` instead of falsely
    `completed`; the recovery worker re-runs failed sagas idempotently (backends
    404→0 on already-deleted data). NOT raised for "nothing to erase" (404, zero
    matching rows, unconfigured backend) — those return 0.
    """


class ExternalEraseBackend(Protocol):
    """Per-system erasure callable.

    Returns the count of rows removed (0 is a valid success). Raises
    `ErasureBackendError` when a delete may NOT have happened, so the saga
    fails-closed instead of falsely completing."""

    name: str

    async def delete_user_id(self, org: str, user_id: str) -> int: ...
    async def delete_namespace(self, org: str, namespace: str) -> int: ...


async def delete_namespaces(
    backend: ExternalEraseBackend, org: str, namespaces: Sequence[str]
) -> int:
    """Erase a list of namespaces on one backend.

    A backend that can erase a list in one pass exposes `delete_namespaces`;
    the others are called once per namespace. The saga calls this
    for every scope so a `namespace_list` erasure over 200 throwaway
    namespaces is one count and one mutation per OTel table, not 800 of each.
    """
    names = [str(n) for n in namespaces]
    bulk = getattr(backend, "delete_namespaces", None)
    if bulk is not None and len(names) > 1:
        return int(await bulk(org, names) or 0)
    total = 0
    for ns in names:
        total += int(await backend.delete_namespace(org, ns) or 0)
    return total


class StubBackend:
    """No-op backend that logs intent and returns 0 (used when unconfigured)."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def delete_user_id(self, org: str, user_id: str) -> int:
        log.info(
            "erasure %s.delete_user_id(org=%s, user_id=%s) — stub",
            self.name,
            org,
            user_id,
        )
        return 0

    async def delete_namespace(self, org: str, namespace: str) -> int:
        log.info(
            "erasure %s.delete_namespace(org=%s, ns=%s) — stub",
            self.name,
            org,
            namespace,
        )
        return 0


class LangfuseBackend:
    """Langfuse erasure.

    `LOOM_LANGFUSE_ADMIN_TOKEN` holds a colon-separated `public_key:secret_key`
    pair used as HTTP Basic auth; a value with no colon is treated as a Bearer
    token (single-token deployments).
    """

    name = "langfuse"

    def __init__(self, base_url: str, admin_token: str, timeout_s: float) -> None:
        import base64

        self._base_url = base_url.rstrip("/")
        if ":" in admin_token:
            auth = base64.b64encode(admin_token.encode()).decode()
            self._headers = {"Authorization": f"Basic {auth}"}
        else:
            self._headers = {"Authorization": f"Bearer {admin_token}"}
        self._timeout = timeout_s

    # Langfuse has no bulk-by-filter delete, so erasure is query-then-delete:
    # find the matching trace ids, then delete them in batches. Bounded so a
    # runaway filter cannot loop forever on a GDPR path that must terminate.
    _PAGE_SIZE: ClassVar[int] = 100
    _DELETE_BATCH: ClassVar[int] = 100
    _MAX_TRACES: ClassVar[int] = 100_000
    # The trace list is asked for its core fields only: the default payload
    # carries every trace's input, output and metadata, and on a project with
    # millions of traces a one-row default list can take tens of seconds or
    # time out, where `fields=core` answers in seconds. Erasure needs ids and
    # tags, nothing else.
    _LIST_FIELDS: ClassVar[str] = "core"
    # A page that times out or returns 5xx/429 is retried this many times with
    # exponential backoff before the step fails closed (the recovery worker
    # then retries the whole step). Bounded: a GDPR path must terminate.
    _PAGE_ATTEMPTS: ClassVar[int] = 3
    _PAGE_BACKOFF_S: ClassVar[float] = 2.0

    async def _find_trace_ids(
        self,
        c: httpx.AsyncClient,
        # httpx's own param type: a list value is encoded as repeated params,
        # which is how the org+namespace tag filter is ANDed server-side.
        params: dict[str, str | list[str]],
    ) -> list[str]:
        """Collect every trace id matching a filter, following pagination.

        Fails closed on both partial outcomes. This is a GDPR path, and the
        callers turn a returned count into a `completed` saga step that the
        recovery worker will never retry — so under-collecting here is silent,
        permanent non-erasure:

        * **Truncation.** Stopping at `_MAX_TRACES` and returning what we have
          would delete the first 100k traces of a larger namespace and report
          success; a busy namespace reaches the cap.
        * **A missing `totalPages`.** `int(meta.get("totalPages", page))` defaults
          to the current page, so a response that omits the field ends pagination
          after page 1 — 100 traces deleted out of any number, reported as done.
        """
        ids: list[str] = []
        page = 1
        while True:
            r = await self._get_page(c, {"page": page, "limit": self._PAGE_SIZE, **params})
            if r.status_code != 200:
                raise ErasureBackendError(
                    f"langfuse trace lookup {params} → HTTP {r.status_code}: "
                    f"{r.text[:LOG_ERROR_TEXT_LIMIT]}"
                )
            body = r.json()
            rows = body.get("data") or []
            ids.extend(str(t["id"]) for t in rows if t.get("id"))
            if not rows:
                break
            meta = body.get("meta") or {}
            total_pages = meta.get("totalPages")
            if total_pages is None:
                raise ErasureBackendError(
                    f"langfuse trace lookup {params} returned {len(rows)} rows "
                    f"with no meta.totalPages — cannot tell whether more exist, "
                    f"so erasure must not report success"
                )
            if len(ids) > self._MAX_TRACES:
                raise ErasureBackendError(
                    f"langfuse trace lookup {params} matched more than "
                    f"{self._MAX_TRACES} traces; refusing to erase a truncated "
                    f"set, because a partial delete would be recorded as a "
                    f"completed erasure and never retried"
                )
            if page >= int(total_pages):
                break
            page += 1
        return ids

    async def _get_page(self, c: httpx.AsyncClient, params: dict[str, Any]) -> httpx.Response:
        """One page of the trace list, core fields only, retried on a timeout,
        a 429 or a 5xx. Anything else is returned to the
        caller's status check unchanged."""
        query: dict[str, Any] = {"fields": self._LIST_FIELDS, **params}
        last: Exception | None = None
        for attempt in range(self._PAGE_ATTEMPTS):
            try:
                r = await c.get(
                    f"{self._base_url}/api/public/traces", headers=self._headers, params=query
                )
            except httpx.TimeoutException as e:
                last = e
            else:
                if r.status_code < 500 and r.status_code != 429:
                    return r
                last = None
                if attempt == self._PAGE_ATTEMPTS - 1:
                    return r
            if attempt < self._PAGE_ATTEMPTS - 1:
                await asyncio.sleep(self._PAGE_BACKOFF_S * (2**attempt))
        assert last is not None
        raise last

    async def _delete_traces(self, c: httpx.AsyncClient, ids: list[str]) -> int:
        """Delete traces in batches. Returns the number requested for deletion."""
        deleted = 0
        for i in range(0, len(ids), self._DELETE_BATCH):
            batch = ids[i : i + self._DELETE_BATCH]
            r = await c.request(
                "DELETE",
                f"{self._base_url}/api/public/traces",
                headers=self._headers,
                json={"traceIds": batch},
            )
            # 404 is NOT success: the bulk endpoint returns 200 even for ids
            # that do not exist, so a 404 means the ENDPOINT is missing (an
            # older self-hosted build, or a proxy forwarding only GET/POST)
            # and nothing was deleted.
            #
            # Idempotency for the recovery worker comes from the LOOKUP instead:
            # a re-run finds no matching traces and returns 0 without ever
            # issuing a delete.
            if r.status_code in (200, 202, 204):
                deleted += len(batch)
                continue
            raise ErasureBackendError(
                f"langfuse.delete_traces ({len(batch)} ids) → HTTP "
                f"{r.status_code}: {r.text[:LOG_ERROR_TEXT_LIMIT]}"
            )
        return deleted

    async def delete_user_id(self, org: str, user_id: str) -> int:
        """Erase every trace attributed to this user in this org.

        Langfuse has no user-deletion endpoint (`DELETE /api/public/users/{id}`
        answers 404 with an HTML page, which would read as nothing to erase):
        the traces are found with `GET /api/public/traces?userId=` (filtered
        server-side) and deleted with `DELETE /api/public/traces`. Deleting a
        trace removes its observations and scores with it.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                # Scoped to the org, exactly as `delete_namespace` is: `user_id`
                # is a caller-supplied opaque string and every org writes into
                # one Langfuse project, so `userId` alone would erase another
                # tenant's traces for anyone sharing an identifier.
                ids = await self._find_trace_ids(c, {"userId": user_id, "tags": f"org:{org}"})
                if not ids:
                    return 0
                return await self._delete_traces(c, ids)
        except httpx.HTTPError as e:
            log.warning("langfuse.delete_user_id %s failed: %s", user_id, _describe(e))
            raise ErasureBackendError(
                f"langfuse.delete_user_id {user_id} transport error: {_describe(e)}"
            ) from e

    async def delete_namespace(self, org: str, namespace: str) -> int:
        """Erase every trace tagged with this namespace in this org.

        Every span carries `langfuse.trace.tags = ["org:…", "namespace:…"]`;
        the traces holding both tags are found and deleted. Loom writes into
        the one project its key pair implies, so there is no per-namespace
        project to delete. The org is part of the filter because namespace
        names are unique per org, not globally.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as c:
                # Repeated `tags` params are ANDed server-side (a nonexistent
                # org tag matches nothing); httpx encodes a list value as
                # repeated params.
                ids = await self._find_trace_ids(
                    c, {"tags": [f"namespace:{namespace}", f"org:{org}"]}
                )
                if not ids:
                    return 0
                return await self._delete_traces(c, ids)
        except httpx.HTTPError as e:
            log.warning("langfuse.delete_namespace %s/%s failed: %s", org, namespace, _describe(e))
            raise ErasureBackendError(
                f"langfuse.delete_namespace {org}/{namespace} transport error: {_describe(e)}"
            ) from e

    # Langfuse filters traces by tag one namespace at a time (repeated `tags`
    # are ANDed, never ORed), so a list is still one lookup per namespace —
    # but the lookups need not wait on one another.
    _LIST_CONCURRENCY = 4

    async def delete_namespaces(self, org: str, namespaces: Sequence[str]) -> int:
        sem = asyncio.Semaphore(self._LIST_CONCURRENCY)

        async def _one(ns: str) -> int:
            async with sem:
                return int(await self.delete_namespace(org, ns) or 0)

        return sum(await asyncio.gather(*(_one(ns) for ns in namespaces)))


class ClickStackBackend:
    """ClickStack erasure via a lightweight `DELETE FROM` against the OTel
    tables (no admin REST API for user-data deletion). An `ALTER TABLE …
    DELETE` rewrites every part the predicate touches (GiBs to remove a
    handful of rows); a lightweight delete writes only the `_row_exists` mask, the rows leave
    every read at once and the bytes go at the next merge. The predicate
    still scans a metrics table that lacks a `mapValues(Attributes)` bloom
    filter (the collector's default logs/traces tables carry one, the
    metrics tables do not); `loom doctor` names the tables missing it.

    `user_id` is not a top-level column; it lives in a Map column whose name
    varies per table (see DEFAULT_TABLES), under one of several attribute keys
    by SDK convention — we OR across all configured keys. Per-namespace deletion
    matches `loom.namespace` or `service.namespace` (see `_namespace_where`).
    """

    name = "clickstack"

    # (table, Map column carrying user attributes)
    DEFAULT_TABLES: ClassVar[list[tuple[str, str]]] = [
        ("otel_logs", "LogAttributes"),
        ("otel_traces", "SpanAttributes"),
        ("otel_metrics_gauge", "Attributes"),
        ("otel_metrics_sum", "Attributes"),
        ("otel_metrics_histogram", "Attributes"),
        ("otel_metrics_exponential_histogram", "Attributes"),
        ("otel_metrics_summary", "Attributes"),
        ("hyperdx_sessions", "ResourceAttributes"),
    ]

    DEFAULT_USER_ID_KEYS: ClassVar[list[str]] = ["user_id", "user.id", "enduser.id"]
    NAMESPACE_KEY = "service.namespace"
    # Loom's exporter sets service.namespace to the constant "loom" (the
    # deployment, per OTel semconv) and carries the tenant namespace as the
    # `loom.namespace` attribute, so matching service.namespace alone matches
    # no Loom row (and 0 rows reads as a successful delete). Both keys are
    # ORed: service.namespace covers non-Loom emitters an operator points at
    # the same ClickStack.
    TENANT_NAMESPACE_KEY = "loom.namespace"
    # The org Loom stamps beside `loom.namespace`. A subject or namespace name
    # is unique per org only, so a row tagged with ANOTHER org is never erased.
    # A row with no org tag (a non-Loom emitter) is still erased by a user key
    # or by `service.namespace`, since it may hold the subject.
    TENANT_ORG_KEY = "loom.org"

    def __init__(
        self,
        database: str,
        timeout_s: float,
        tables: list[tuple[str, str]] | None = None,
        user_id_keys: list[str] | None = None,
    ) -> None:
        self._database = database
        self._tables = tables if tables is not None else self.DEFAULT_TABLES
        self._user_id_keys = user_id_keys or self.DEFAULT_USER_ID_KEYS
        self._timeout = timeout_s

    def _attribute_or_clause(self, attr_column: str, keys: list[str]) -> str:
        """`attr_col['key1'] = {u:String} OR attr_col['key2'] = {u:String}` …"""
        parts = [f"{attr_column}[{k!r}] = {{u:String}}" for k in keys]
        return "(" + " OR ".join(parts) + ")"

    async def _delete_where(
        self,
        fq_table: str,
        where: str,
        params: dict[str, object],
    ) -> int:
        from server.core.ch import get_client

        ch = get_client()
        # Pre-count for an honest row-count report.
        try:
            row = await ch.query_one(
                f"SELECT count() AS n FROM {fq_table} WHERE {where}",
                params=params,
            )
            n = int(row["n"]) if row else 0
        except Exception as e:
            # A table or database the service does not have holds no rows:
            # the operator does not run ClickStack, or not that signal. Any
            # other failure (timeout, memory limit, grant, dropped socket)
            # leaves the rows unconfirmed, so the step fails closed and the
            # recovery worker retries it.
            if _is_missing_table(e):
                log.warning("clickstack precount: %s is not on this service: %s", fq_table, e)
                return 0
            raise ErasureBackendError(
                f"clickstack precount on {fq_table} failed: {_describe(e)}"
            ) from e
        if n == 0:
            return 0
        try:
            # Awaited by state, not by socket: the OTel
            # tables are the largest on the service and a DELETE over them
            # outlives an HTTP read timeout. Lightweight: the
            # mask, not a part rewrite.
            from server.erasure.store.mutations import run_lightweight_delete

            await run_lightweight_delete(fq_table, where=where, params=params, ch=ch)
            return n
        except Exception as e:
            # Confirmed n>0 rows then the delete failed — rows survive. Fail
            # closed so the saga records `failed` and the recovery worker retries.
            log.warning("clickstack DELETE on %s failed: %s", fq_table, e)
            raise ErasureBackendError(
                f"clickstack DELETE on {fq_table} failed after confirming {n} matching rows: {e}"
            ) from e

    def _org_clause(self, attr_col: str) -> str:
        """The row carries this org's tag, or none (Map lookup of an absent key is '')."""
        return f"{attr_col}[{self.TENANT_ORG_KEY!r}] IN ({{org:String}}, '')"

    async def delete_user_id(self, org: str, user_id: str) -> int:
        total = 0
        for table_name, attr_col in self._tables:
            fq = f"{self._database}.{table_name}"
            where = (
                f"{self._attribute_or_clause(attr_col, self._user_id_keys)}"
                f" AND {self._org_clause(attr_col)}"
            )
            total += await self._delete_where(fq, where, {"u": user_id, "org": org})
        return total

    def _namespace_where(self, table_name: str, attr_col: str, *, many: bool) -> str:
        # service.namespace is in ResourceAttributes for logs/traces/sessions,
        # else the table's own attribute column.
        ns_col = (
            "ResourceAttributes"
            if table_name in {"otel_logs", "otel_traces", "hyperdx_sessions"}
            else attr_col
        )
        op = "IN {u:Array(String)}" if many else "= {u:String}"
        # A row keyed by Loom's own `loom.namespace` is this org's only when it
        # also carries this org's `loom.org`: Loom's op and domain metrics carry
        # the namespace and no org, so by name alone they may be another org's.
        # Another emitter's `service.namespace` keeps the untagged allowance,
        # but never Loom's deployment constant, which every Loom row carries.
        return (
            f"(({ns_col}[{self.NAMESPACE_KEY!r}] {op}"
            f" AND {ns_col}[{self.NAMESPACE_KEY!r}] != {SERVICE_NAMESPACE!r}"
            f" AND {self._org_clause(attr_col)})"
            f" OR ({attr_col}[{self.TENANT_NAMESPACE_KEY!r}] {op}"
            f" AND {attr_col}[{self.TENANT_ORG_KEY!r}] = {{org:String}}))"
        )

    async def delete_namespace(self, org: str, namespace: str) -> int:
        total = 0
        for table_name, attr_col in self._tables:
            fq = f"{self._database}.{table_name}"
            where = self._namespace_where(table_name, attr_col, many=False)
            total += await self._delete_where(fq, where, {"u": namespace, "org": org})
        return total

    async def delete_namespaces(self, org: str, namespaces: Sequence[str]) -> int:
        """One count and one DELETE per OTel table for the whole list: the Map
        lookups scan the table once per predicate, so 200 namespaces cost the same read as one."""
        total = 0
        names = [str(n) for n in namespaces]
        for table_name, attr_col in self._tables:
            fq = f"{self._database}.{table_name}"
            where = self._namespace_where(table_name, attr_col, many=True)
            total += await self._delete_where(fq, where, {"u": names, "org": org})
        return total


def _backend_for(system: str) -> ExternalEraseBackend:
    """Resolve the active backend for a system. Stub when not configured."""
    if system == "langfuse":
        if settings.langfuse_url and settings.langfuse_admin_token:
            return LangfuseBackend(
                settings.langfuse_url,
                settings.langfuse_admin_token,
                settings.external_erasure_timeout_s,
            )
    elif system == "clickstack":
        # Same CH cluster — no API token; point at the OTel collector's database.
        if settings.clickstack_database:
            keys = [k.strip() for k in settings.clickstack_user_id_keys.split(",") if k.strip()]
            return ClickStackBackend(
                database=settings.clickstack_database,
                timeout_s=settings.external_erasure_timeout_s,
                user_id_keys=keys or None,
            )
    return StubBackend(system)


def all_backends() -> list[ExternalEraseBackend]:
    """The full set of external backends iterated by the erasure saga."""
    return [_backend_for(s) for s in ("langfuse", "clickstack")]


__all__ = [
    "ClickStackBackend",
    "ExternalEraseBackend",
    "LangfuseBackend",
    "StubBackend",
    "all_backends",
]
