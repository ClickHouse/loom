"""The lifecycle of a bench namespace: registered as bench, erased when the run ends.

Every harness here creates throwaway namespaces (`lme-…`, `locomo-…`, `mab-…`)
that hold public dataset text and are asked their questions once.

Registration. A harness registers each namespace before its first write with
`traffic_class = "bench"`, `kind = "bench"` and `capture_generation_io = True`:

* `traffic_class = "bench"` makes the namespace's requests resolve to the bench
  traffic class (bench rate buckets, bench ClickHouse priority, not the
  production cost ledger). A row registered without it or a bench kind is
  production traffic.
* `kind = "bench"` makes the namespace a candidate for the idle-namespace gc,
  which purges it after `namespace_gc_idle_hours` without a write. A run that
  keeps its namespaces for a later run (`keep=True`) registers `kind = "memory"`
  instead, so an ask-only reuse days later still finds them.
* `capture_generation_io` records query text on the trace rows, so the
  relied-on flywheel, which joins usage events to trace rows by query, can
  learn from bench traffic. Consent stays a per-namespace choice for real
  consumers; `--no-capture-consent` leaves the server default.

`namespace.register` needs only the viewer role, so any bench token can call it.

Erasure. Harnesses record the namespaces they mint (`remember_bench_namespace`)
and erase them with one list-scoped, purging erasure per batch. Run the work
inside `erase_on_exit` (async) or `erase_on_exit_sync` so the erasure also runs
when the run fails or is interrupted; `keep=True` (`--keep-namespaces`) skips it
for namespaces a later run reuses. Erasure needs an org-admin token; a refusal
is logged with the `loom namespaces-gc` command that finishes the job.
"""

from __future__ import annotations

import contextlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from typing import Any

log = logging.getLogger(__name__)

BENCH_OWNER_EMAIL = "bench@loom.local"
BENCH_KIND = "bench"

AsyncPost = Callable[[str, dict[str, Any]], Awaitable[Any]]
SyncPost = Callable[[str, dict[str, Any]], Any]


def register_body(
    org: str,
    namespace: str,
    *,
    description: str,
    capture: bool = True,
    retention_days: int = 0,
    keep: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The `namespace.register` request for a bench namespace.

    `keep` is the harness's `--keep-namespaces`: the row is `kind = "memory"`
    so the idle gc leaves it for the later run; the traffic class stays bench.

    `retention_days` is 0 (unset) by default. A per-namespace retention is a
    clause in every managed table's TTL expression, and registering one
    re-issues `MODIFY TTL` on every managed table; dozens of bench namespaces
    registered with a retention in one hour can back up the mutation queue far
    enough to make a server read-only. Bench namespaces are erased at the end of
    the run, or retired by the idle gc. Pass a value only for a namespace that
    must expire on its own.

    `capture` sets `capture_generation_io` (query text on the trace row and
    prompt/completion text on the generation spans, both consent-gated);
    False leaves the key out so the server default applies unchanged.
    `extra` are caller fields (e.g. a conflict policy) and win on collision."""
    body: dict[str, Any] = {
        "org": org,
        "namespace": namespace,
        "owner_email": BENCH_OWNER_EMAIL,
        "description": description,
        "kind": "memory" if keep else BENCH_KIND,
        "traffic_class": BENCH_KIND,
        "tags": ["bench"],
    }
    if capture:
        body["capture_generation_io"] = True
    if retention_days and retention_days > 0:
        body["retention_days"] = int(retention_days)
    if extra:
        body.update(extra)
    return body


async def register_bench_namespace(
    post: AsyncPost,
    *,
    org: str,
    namespace: str,
    description: str,
    capture: bool = True,
    retention_days: int = 0,
    keep: bool = False,
    extra: dict[str, Any] | None = None,
) -> bool:
    """Register the namespace through the harness's own POST helper
    (`post(path, body)`); True on success. A failure is logged and the
    harness continues with a bare first write: a bench must never fail on
    its own bookkeeping."""
    body = register_body(
        org,
        namespace,
        description=description,
        capture=capture,
        retention_days=retention_days,
        keep=keep,
        extra=extra,
    )
    try:
        await post("/v1/namespace.register", body)
        return True
    except Exception as e:  # noqa: BLE001 — bookkeeping, never the measurement
        log.warning("namespace.register for %s/%s failed (continuing): %s", org, namespace, e)
        return False


def register_bench_namespace_sync(
    post: SyncPost,
    *,
    org: str,
    namespace: str,
    description: str,
    capture: bool = True,
    keep: bool = False,
) -> bool:
    """`register_bench_namespace` for a harness with a synchronous POST helper."""
    body = register_body(org, namespace, description=description, capture=capture, keep=keep)
    try:
        post("/v1/namespace.register", body)
        return True
    except Exception as e:  # noqa: BLE001 — bookkeeping, never the measurement
        log.warning("namespace.register for %s/%s failed (continuing): %s", org, namespace, e)
        return False


# ---------------------------------------------------------------------------
# Erasure

_MINTED: list[tuple[str, str]] = []


def remember_bench_namespace(org: str, namespace: str) -> None:
    """Record a namespace this run created, for `erase_bench_namespaces`."""
    if (org, namespace) not in _MINTED:
        _MINTED.append((org, namespace))


def minted_bench_namespaces(org: str | None = None) -> list[str]:
    return [ns for o, ns in _MINTED if org is None or o == org]


def erasure_bodies(
    org: str,
    namespaces: list[str],
    *,
    purge: bool = True,
    batch: int = 200,
    requester: str = BENCH_OWNER_EMAIL,
) -> list[dict[str, Any]]:
    """The `erasure.request` bodies that erase `namespaces`, `batch` per request.
    `purge=True` removes the rows from the parts, not only from the readable set.
    `external` is False: bench rows have no traces in external backends, and
    with the server default an unreachable backend fails the whole erasure."""
    step = max(1, int(batch))
    return [
        {
            "org": org,
            "scope_type": "namespace_list",
            "scope_value": json.dumps(
                {"namespaces": namespaces[i : i + step], "purge": bool(purge)}
            ),
            "requester": requester,
            "external": False,
        }
        for i in range(0, len(namespaces), step)
    ]


def gc_command(org: str, namespaces: list[str]) -> str:
    """The operator command that erases namespaces a refused erasure left behind.
    `--include-registered` because every bench namespace is registered."""
    shown = " ".join(f"--namespace {ns}" for ns in namespaces[:5])
    more = " …" if len(namespaces) > 5 else ""
    return f"loom namespaces-gc --org {org} --include-registered --erase {shown}{more}"


def _names(org: str, namespaces: list[str] | None) -> list[str]:
    return list(namespaces if namespaces is not None else minted_bench_namespaces(org))


def _log_refusal(org: str, chunk: list[str], e: Exception) -> None:
    log.warning(
        "erasure.request for %d bench namespace(s) failed (%s); clean up with: %s",
        len(chunk),
        e,
        gc_command(org, chunk),
    )


def _chunk_of(body: dict[str, Any]) -> list[str]:
    return list(json.loads(body["scope_value"])["namespaces"])


async def erase_bench_namespaces(
    post: AsyncPost,
    *,
    org: str,
    namespaces: list[str] | None = None,
    purge: bool = True,
    batch: int = 200,
    requester: str = BENCH_OWNER_EMAIL,
) -> int:
    """Erase this run's namespaces (or `namespaces`) through the harness's own
    async POST helper. Returns the number of namespaces submitted. A refusal is
    logged with the gc command, never raised: a bench never fails on its own
    cleanup."""
    submitted = 0
    for body in erasure_bodies(
        org, _names(org, namespaces), purge=purge, batch=batch, requester=requester
    ):
        chunk = _chunk_of(body)
        try:
            await post("/v1/erasure.request", body)
            submitted += len(chunk)
        except Exception as e:  # noqa: BLE001 — cleanup, never the measurement
            _log_refusal(org, chunk, e)
    if submitted:
        log.info("erased %d bench namespace(s) in %s (purge=%s)", submitted, org, purge)
    return submitted


def erase_bench_namespaces_sync(
    post: SyncPost,
    *,
    org: str,
    namespaces: list[str] | None = None,
    purge: bool = True,
    batch: int = 200,
    requester: str = BENCH_OWNER_EMAIL,
) -> int:
    """`erase_bench_namespaces` for a harness with a synchronous POST helper."""
    submitted = 0
    for body in erasure_bodies(
        org, _names(org, namespaces), purge=purge, batch=batch, requester=requester
    ):
        chunk = _chunk_of(body)
        try:
            post("/v1/erasure.request", body)
            submitted += len(chunk)
        except Exception as e:  # noqa: BLE001 — cleanup, never the measurement
            _log_refusal(org, chunk, e)
    if submitted:
        log.info("erased %d bench namespace(s) in %s (purge=%s)", submitted, org, purge)
    return submitted


def _minted_since(start: int, org: str) -> list[str]:
    return [ns for o, ns in _MINTED[start:] if o == org]


@contextlib.asynccontextmanager
async def erase_on_exit(post: AsyncPost, *, org: str, keep: bool = False) -> AsyncIterator[None]:
    """Erase the namespaces remembered inside the block when it exits: after
    success, after an error, and after Ctrl-C (`asyncio.run` cancels the main
    task, and the cleanup still runs before the interrupt propagates). `post`
    must stay usable until the block exits, so open the client outside it."""
    start = len(_MINTED)
    try:
        yield
    finally:
        if not keep:
            await erase_bench_namespaces(post, org=org, namespaces=_minted_since(start, org))


@contextlib.contextmanager
def erase_on_exit_sync(post: SyncPost, *, org: str, keep: bool = False) -> Iterator[None]:
    """`erase_on_exit` for a harness with a synchronous POST helper."""
    start = len(_MINTED)
    try:
        yield
    finally:
        if not keep:
            erase_bench_namespaces_sync(post, org=org, namespaces=_minted_since(start, org))
