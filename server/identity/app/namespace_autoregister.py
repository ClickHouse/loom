"""Every live namespace has an owner row.

Harnesses mint a namespace per task with a bare first write and walk away, so
without this most namespaces holding live memory would have no owner, no
retention policy and no kind.

The first write a principal makes into an unregistered namespace inserts the registry row
`namespace.register` would have written for it, with that op's defaults: owner = the token
subject, or none when the name is already in use (`IdentityDirectory.in_use`), `kind = 'bench'`
when the name is under one of `namespace_bench_prefixes` (else `memory`),
`retention_days = 0`. The row is the only write: no TTL clause is touched (a
per-namespace retention is a clause in eight tables' TTL expressions, and setting one
re-issues `MODIFY TTL` on all of them), no ALTER is issued, and no grant is written. An
owner row is not elevation: the register op's admin grant for its caller would have made a
writer an admin of every namespace it touched (`memory.unlock` open to writers;
`grants.revoke` refusing to remove "the only admin"), and `resolve_my_namespaces` unions a
wildcard token's grants, so a subject's searches fanned out across every namespace it had
ever written to. The owner named on an auto-registered row may complete its registration
with `namespace.register` while nobody else holds a live grant on it; nothing
else changes for the writer.

The row also opts into nothing an absent row did not: every column reader
treats "no row" and the register defaults alike.

Runs from both transports after authorization, before the handler.
Best-effort: a registry failure never fails the write, and the process
cache is filled only on success so the next write retries; a retry that finds
the earlier write's data records no owner. One point read per (org, namespace) per
process, plus the in-use check when no row exists (one ClickHouse statement, and a
Postgres read under the Postgres authority); the hot path is a set lookup.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, Final

from server.core.naming import reserved_for_sso
from server.identity.api.models import NamespaceRegisterInput
from server.identity.app.directory import IdentityDirectory
from server.operations.base import Operation, Role
from server.settings import settings

log = logging.getLogger("loom.namespace_autoregister")

_known: Final[set[tuple[str, str]]] = set()
_MAX_CACHE = 200_000

AUTO_REGISTER_TAG = "auto-registered"
# `updated_at`, the table's version column, of an auto-registered row. Aware: the
# driver encodes a naive datetime as host-local time.
AUTO_ROW_VERSION = datetime(1970, 1, 1, 0, 0, 1, tzinfo=UTC)
# `updated_at` of an ownerless auto row, below `AUTO_ROW_VERSION`: when two first writes
# race, the owned row of the one that found the name free wins.
OWNERLESS_ROW_VERSION = datetime(1970, 1, 1, tzinfo=UTC)
_MISSING = ("", "-", "*")
# The census tables a read or a run writes under a name (telemetry, a query, a run's
# metadata), so a writer who searched first still owns the name; a name holding only
# these rows goes to its first writer.
READ_TRACES: Final = frozenset(
    {
        "loom.agent_runs",
        "loom.events",
        "loom.memory_exposures",
        "loom.search_trace_log",
        "loom.code_search_trace",
        "loom.code_search_outcome",
    }
)


def _csv(value: str) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(value or "").split(",") if p.strip())


def bench_prefixes() -> tuple[str, ...]:
    return _csv(settings.namespace_bench_prefixes)


def classify_kind(namespace: str) -> str:
    """`personal` / `collection` by their reserved prefixes (server/auth/sharing.py),
    `bench` under a bench prefix, else `memory`."""
    from server.auth.sharing import is_collection, is_personal

    if is_personal(namespace):
        return "personal"
    if is_collection(namespace):
        return "collection"
    return "bench" if any(namespace.startswith(p) for p in bench_prefixes()) else "memory"


def should_autoregister(op: Operation, org: str | None, namespace: str | None) -> bool:
    """A write-shaped op (WRITER role or above) addressed IN a namespace:
    not an org admin's op (it sets an org-wide control such as an extraction
    policy, whatever namespace it names), not the self-service namespace ops,
    not the code plane, not the operator planes, and not a request whose
    identity is missing or the wildcard."""
    if not settings.namespace_autoregister_enabled:
        return False
    if not org or not namespace or org in _MISSING or namespace in _MISSING:
        return False
    if not Role.WRITER <= op.required_role < Role.ORG_ADMIN:
        return False
    skip = _csv(settings.namespace_autoregister_skip_prefixes)
    return not any(op.name.startswith(p) for p in skip)


def _remember(org: str, namespace: str) -> None:
    if len(_known) >= _MAX_CACHE:
        _known.clear()
    _known.add((org, namespace))


def forget_all() -> None:
    """Test seam: drop the process cache."""
    _known.clear()


async def ensure_registered(
    org: str,
    namespace: str,
    *,
    subject: str,
    op_name: str,
    ch: Any | None = None,
) -> bool:
    """Insert the registry row for (org, namespace) if none exists. Returns
    True when a row was inserted. Never raises."""
    if (org, namespace) in _known or reserved_for_sso(namespace):
        return False  # an SSO account's row is single sign-on's to create
    try:
        if ch is None:
            from server.core.ch import get_client

            ch = get_client()
        from server.core.ch import sequential_reads

        with sequential_reads():  # a row `namespace.register` just wrote on another replica
            existing = await ch.query_one(
                """
                SELECT 1 AS present
                FROM loom.namespaces
                WHERE org = {org:String} AND namespace = {ns:String}
                LIMIT 1
                """,
                params={"org": org, "ns": namespace},
            )
        if existing is not None:
            _remember(org, namespace)
            return False
        import server.operations  # noqa: F401 — wires handlers before importing one
        from server.identity.app.namespace import registry_row

        # A name that already holds state (data written with auth off, a grant) is not its
        # first writer's to own: the row names no owner, so only an admin completes it.
        in_use = await IdentityDirectory().in_use(org, namespace, ch=ch, skip=READ_TRACES)
        kind = classify_kind(namespace)
        now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
        row = registry_row(
            NamespaceRegisterInput(
                org=org,
                namespace=namespace,
                owner_email="" if in_use else subject,
                description=f"Auto-registered on first write ({op_name}) by {subject}.",
                kind=None,  # classified by name (`classify_kind`), as `kind` above
                tags=[AUTO_REGISTER_TAG] + (["bench"] if kind == "bench" else []),
                retention_days=0,
            ),
            None,
            now,
        )
        # Both versions sit below any explicit row's (a register, a restriction), so an
        # explicit row wins under FINAL, even one another replica wrote a moment ago.
        row["updated_at"] = OWNERLESS_ROW_VERSION if in_use else AUTO_ROW_VERSION
        await ch.insert("loom.namespaces", [row])
        _remember(org, namespace)
        from server.identity.app.traffic_class_registry import note_registered

        note_registered(org, namespace, row)
        log.info(
            "namespace auto-registered: %s/%s kind=%s owner=%s via %s",
            org,
            namespace,
            kind,
            row["owner_email"] or "-",
            op_name,
        )
        return True
    except Exception as e:  # noqa: BLE001 — never fail the write it rides on
        log.warning("namespace auto-register failed for %s/%s: %s", org, namespace, e)
        return False


async def maybe_autoregister(
    op: Operation,
    *,
    org: str | None,
    namespace: str | None,
    auth: Any,
) -> bool:
    """The transport hook: after authorization, before the handler."""
    if auth is None or not getattr(auth, "subject", ""):
        return False
    if not should_autoregister(op, org, namespace):
        return False
    assert org is not None and namespace is not None
    return await ensure_registered(org, namespace, subject=str(auth.subject), op_name=op.name)
