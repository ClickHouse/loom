"""`loom reconcile`: after a restore of ClickHouse or of the Postgres authority, run again
in the restored store what the other records past the restore point, so nothing erased
or redacted after it comes back (docs/operations/backup-and-recovery.md).

Run it with every server and worker stopped: nothing it does waits for one, and a live
delivery worker would publish what it holds back before the erasures it predates. It
refuses while a worker reported a pass within two workers-lease terms
(`LOOM_SUPERVISOR_LEASE_TTL_S`, a minute by default), unless `--allow-live`."""

from __future__ import annotations

import argparse
import shlex
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import run_connected


def _add_reconcile_parser(sub: Any) -> None:
    p = sub.add_parser(
        "reconcile",
        help="After a restore of ClickHouse or Postgres, run again in the restored store "
        "the erasures and redactions the other records past the restore point, and (after "
        "a ClickHouse restore) publish again every revision committed after it. Stop every "
        "server and worker first. Dry run by default.",
    )
    p.add_argument("--restored", choices=["clickhouse", "postgres", "both"], required=True)
    p.add_argument(
        "--point",
        required=True,
        help="the restored backup's time (ISO 8601, UTC when no offset), from the backup's "
        "own metadata",
    )
    p.add_argument(
        "--allow-live",
        action="store_true",
        help="run although a worker reported a pass within two workers-lease terms "
        "(LOOM_SUPERVISOR_LEASE_TTL_S, a minute by default)",
    )
    add_apply_flags(p, writes="run the erasures and redactions again and publish")
    p.set_defaults(func=_cmd_reconcile)


def _parse_point(raw: str) -> datetime:
    point = datetime.fromisoformat(raw)
    return point if point.tzinfo else point.replace(tzinfo=UTC)


def _cmd_reconcile(args: argparse.Namespace) -> int:
    from server.core import pg

    if not pg.enabled():
        print("loom reconcile: needs LOOM_STORAGE_AUTHORITY=postgres", file=sys.stderr)
        return 2
    try:
        point = _parse_point(args.point)
    except ValueError:
        print(f"loom reconcile: --point {args.point!r} is not an ISO 8601 time", file=sys.stderr)
        return 2

    from server.cli.hygiene import _bucket_client_installed

    if not args.dry_run and not _bucket_client_installed():
        return 2  # an erasure run again lists the document bucket

    async def _go(ch: Any) -> int:
        return await reconcile(
            args.restored, point, apply=not args.dry_run, live=args.allow_live, ch=ch
        )

    return run_connected(_go)


def margin() -> timedelta:
    """How far before the point a write may have drawn its time and still committed or
    landed after it: a writer's lease plus the bound on an insert sent under it."""
    from server.core import pg
    from server.settings import settings

    return timedelta(seconds=settings.pg_request_lease_seconds + pg.insert_bound_s())


async def _refusal(restored: str, point: datetime, *, live: bool, ch: Any) -> str:
    """Why the reconcile may not run, or ''."""
    from server.settings import settings

    now = datetime.now(UTC)
    if point > now:
        return f"the restore point {point.isoformat()} is later than now ({now.isoformat()})"
    if restored == "both":
        return (
            f"both stores restored: erasures and redactions made after {point.isoformat()} "
            "are in neither; issue them again from the audit export, then reconcile each "
            "store from the older of its restore points"
        )
    oldest = now - timedelta(days=settings.erasure_ledger_days)
    if point < oldest:
        return (
            f"the restore point {point.isoformat()} is older than the erasure ledger keeps "
            f"(LOOM_ERASURE_LEDGER_DAYS, back to {oldest.isoformat()}): erasures before then "
            "cannot be run again"
        )
    if not live:
        # Two workers-lease terms (a minute by default).
        window_s = 2 * settings.supervisor_lease_ttl_s
        row = await ch.query_one(
            "SELECT count() AS n FROM loom.worker_status "
            "WHERE pass_finished > now64(3) - toIntervalSecond({s:UInt32})",
            params={"s": window_s},
        )
        if row and int(row["n"]):
            return (
                f"a worker reported a pass in the last {window_s} s: stop every server "
                "and worker first (or pass --allow-live)"
            )
    return ""


async def reconcile(
    restored: str, point: datetime, *, apply: bool, live: bool = False, ch: Any
) -> int:
    """The command's body; returns its exit code."""
    from server.erasure.app import replay as erasure_replay
    from server.knowledge.app import restore_reconcile as knowledge_reconcile
    from server.memory.app import restore_reconcile

    why = await _refusal(restored, point, live=live, ch=ch)
    if why:
        print(f"loom reconcile: refused: {why}", file=sys.stderr)
        return 2
    store: erasure_replay.Restored = "clickhouse" if restored == "clickhouse" else "postgres"
    # Revisions and redactions from `margin()` before the point: one could draw its time
    # before the point and commit after it, and running either again changes nothing
    # else. Erasures from the point alone: every step of one ran before its completion,
    # so one completed before the point is in the restored store, and running it again
    # would delete what the user wrote after it.
    after = point - margin()
    erasures = await erasure_replay.completed_since(store, point)
    still_open = await erasure_replay.still_open(store)
    redactions = await restore_reconcile.redactions_lost_by(store, after)
    print(f"restored: {store}, point {point.isoformat()}, counting from {after.isoformat()}")
    print(f"  erasures completed since, to run again: {len(erasures)}")
    print(
        f"  erasures still open: {len(still_open)}, left to the recovery worker unless "
        "the restored ledger shows them ended"
    )
    print(f"  redactions to run again: {len(redactions)}")
    if store == "clickhouse":
        # A dataset's rows live in ClickHouse alone too, and only an ingest builds them.
        for s in await knowledge_reconcile.dataset_sources_lost(after):
            print(
                f"  to ingest again (its datasets may miss rows): org {s['org']}, "
                f"namespace {s['namespace']}, source_uri {s['source_uri']}"
            )
    if not apply:
        return 0
    from server.code.app.index import repo_registry

    # A redaction checks the code registry (`require_registry_read`), which only a
    # server's boot reads otherwise.
    if not await repo_registry.read_registry(ch):
        print(f"loom reconcile: refused: {repo_registry.REGISTRY_UNREAD_MESSAGE}", file=sys.stderr)
        return 2
    failed = 0
    if store == "clickhouse":
        # Held first: nothing republished lands before the erasures it predates run.
        memory = await restore_reconcile.hold(after)
        knowledge = await knowledge_reconcile.hold(after)
        behind = await restore_reconcile.hold_heads_behind(ch)
        behind += await knowledge_reconcile.hold_documents_behind(ch)
        print(
            f"  held for publication: {memory} memory revisions, {knowledge} knowledge heads, "
            f"{behind} heads ClickHouse holds at an older version"
        )
        # A document's chunks live in ClickHouse alone: the restore lost those of every
        # live head held here, which `loom rechunk` builds again from the stored content.
        for line in _rechunk_lines(await knowledge_reconcile.held_sources()):
            print(line)
    # A step that raises is counted and the rest run: what `hold` held is published
    # whatever an erasure did.
    for entry in erasures:
        status = await _step(erasure_replay.replay(entry, store, point))
        print(f"  erasure {entry['id']} ({entry['scope_type']}): {status}")
        failed += status != "completed"
    for entry in still_open:
        failed += await _step(erasure_replay.requeue(entry, store, point)) == "failed"
    if store == "clickhouse":
        memory = await restore_reconcile.publish_held()
        knowledge = await knowledge_reconcile.publish_held()
        print(f"  published: {memory} memory revisions, {knowledge} knowledge documents")
    for entry in redactions:
        state = await _step(restore_reconcile.replay_redaction(entry, store))
        print(f"  redaction {entry['memory_id']} v{entry['version']}: {state}")
        failed += state == "failed"
    if failed:
        print(f"loom reconcile: {failed} step(s) did not complete; run again", file=sys.stderr)
        return 1
    return 0


# Up to this many sources of one namespace are named one by one; past it, the namespace.
_RECHUNK_SOURCES = 20


def _rechunk_lines(held: list[dict[str, Any]]) -> list[str]:
    """The `loom rechunk` to run, once the deployment is up, for each held source: by
    `--source-uri`, or for a namespace with many, the namespace (which re-embeds every
    live document in it)."""
    by_ns: dict[tuple[str, str], list[str]] = {}
    for h in held:
        by_ns.setdefault((h["org"], h["namespace"]), []).append(h["source_uri"])
    lines = []
    for (org, ns), uris in by_ns.items():
        base = f"loom rechunk --org {shlex.quote(org)} --namespace {shlex.quote(ns)}"
        if len(uris) <= _RECHUNK_SOURCES:
            lines += [f"  to re-chunk: {base} --source-uri {shlex.quote(u)} --apply" for u in uris]
        else:
            lines.append(
                f"  to re-chunk: {base} --apply  ({len(uris)} sources; re-embeds every live "
                "document of the namespace)"
            )
    return lines


async def _step(run: Any) -> Any:
    """`run`'s result, or "failed" with the error printed."""
    try:
        out = await run
    except Exception as e:  # noqa: BLE001 — one step's failure must not strand the held rest
        from server.core.error_summary import error_summary

        print(f"  failed: {error_summary(e)}", file=sys.stderr)
        return "failed"
    return out
