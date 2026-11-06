"""`loom knowledge sync`: one sync pass over a repository namespace's document
sources and the decisions mined from them, in-process with no token (operator only), the pass the
knowledge_source_sync worker runs (server/knowledge/app/source_sync.py). `loom knowledge
migrate-originals`: the originals held in ClickHouse moved to the document bucket
(server/knowledge/app/originals.py)."""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from server.cli._ch import run_connected


def register_knowledge_cli(sub: Any) -> None:
    p = sub.add_parser("knowledge", help="Knowledge documents and their sources.")
    ksub = p.add_subparsers(dest="subcommand", required=True)
    p_sync = ksub.add_parser(
        "sync",
        help="Run one sync pass over a repository namespace's document sources now: list "
        "each source's changed files (every file with --full), write the changed ones as "
        "documents and retire those a full listing no longer has, then mine each document "
        "written since it was last mined for decisions (model calls, within "
        "LOOM_KNOWLEDGE_SOURCE_SYNC_CAP_SHARE of the namespace's cost cap), bounded like a "
        "worker pass by LOOM_KNOWLEDGE_SOURCE_SYNC_DOCUMENTS_PER_PASS; run it again to go "
        "on. Leaves a manual knowledge_source_sync row in loom.worker_status. Exits 1 when a "
        "source's turn or a document's extraction failed.",
    )
    p_sync.add_argument("--org", required=True)
    p_sync.add_argument("--namespace", required=True, help="The repository's trunk code namespace")
    p_sync.add_argument("--source", default="", help="Only the source of this name")
    p_sync.add_argument(
        "--full",
        action="store_true",
        help="Record a full sync request for each source, as knowledge.source.sync_now "
        "full=true does, so every file is listed and waiting files are retried; a run "
        "while that request is unfinished goes on with it",
    )
    p_sync.set_defaults(func=_cmd_sync)
    p_move = ksub.add_parser(
        "migrate-originals",
        help="Move document originals held in ClickHouse to LOOM_DOCUMENT_S3_BUCKET: each "
        "one whose catalog row still holds its bytes is written to its own object with the "
        "store's SHA-256 check, read back and hash-verified, and its row then names the "
        "object, so every document with those bytes downloads from the bucket. The "
        "ClickHouse copy stays until loom.document_blobs merges its parts (OPTIMIZE TABLE "
        "loom.document_blobs FINAL drops it now). Needs LOOM_DOCUMENT_OBJECT_STORE=s3, set "
        "on every replica first so no new original lands in ClickHouse behind the walk. "
        "An original an unfinished erasure covers (its namespace or an uploader) is not "
        "moved; a move an erasure overtakes is taken back, or named to run that erasure "
        "again. Idempotent: run it "
        "again to go on after a stop. Exits 1 when an original was not moved (bytes that "
        "do not hash to their sha256 or fill their size, an erasure not yet finished, a "
        "bucket that refused); it stays where it was. Exits 1 too when an original was "
        "switched as an erasure of one of its uploaders started; that erasure is named to "
        "run again.",
    )
    p_move.add_argument("--org", default="", help="Only this org's originals")
    p_move.add_argument("--namespace", default="", help="Only this namespace of --org")
    p_move.add_argument(
        "--dry-run", action="store_true", help="Count the originals held in ClickHouse; move none"
    )
    p_move.set_defaults(func=_cmd_migrate_originals)


def _cmd_sync(args: argparse.Namespace) -> int:
    from server.knowledge.app.source_sync import run_sync
    from server.knowledge.domain.sources import normalize_name

    async def _run(_ch: Any) -> int:
        started = time.monotonic()
        result = await run_sync(
            org=args.org,
            namespace=args.namespace,
            only=normalize_name(args.source),
            full=bool(args.full),
        )
        print(
            f"{args.namespace}: {result.synced} of {result.sources} source(s) synced, "
            f"{result.changed} written, {result.removed + result.retired} retired, "
            f"{result.unconverted} unconverted, {result.skipped} skipped, "
            f"{result.extracted} mined for decisions"
            + ("; stopped at the pass bound, run again to go on" if result.cut else "")
        )
        for line in result.errors:
            print(f"error: {line}", file=sys.stderr)
        await _record(args.namespace, started, result)
        return 1 if result.failed else 0

    return run_connected(_run)


def _cmd_migrate_originals(args: argparse.Namespace) -> int:
    from server.settings import settings

    if args.namespace and not args.org:
        print("error: --namespace needs --org", file=sys.stderr)
        return 2
    if settings.document_object_store != "s3":
        print(
            "error: set LOOM_DOCUMENT_OBJECT_STORE=s3 (and LOOM_DOCUMENT_S3_BUCKET) first, "
            "so no new original is written to ClickHouse behind the move",
            file=sys.stderr,
        )
        return 2

    async def _run(_ch: Any) -> int:
        from server.core import object_store
        from server.knowledge.app.originals import migrate_originals

        object_store.require_client()
        result = await migrate_originals(
            org=args.org, namespace=args.namespace, dry_run=bool(args.dry_run)
        )
        if args.dry_run:
            print(f"{result.pending} original(s), {result.bytes} bytes, held in ClickHouse")
            return 0
        print(
            f"{result.moved} original(s) moved to the bucket ({result.bytes} bytes), "
            f"{result.skipped} gone, replaced or erased meanwhile, {len(result.errors)} not moved"
        )
        if result.moved:
            print(
                "the ClickHouse copies stay until loom.document_blobs merges their parts; "
                "OPTIMIZE TABLE loom.document_blobs FINAL drops them now"
            )
        for line in result.errors:
            print(f"error: {line}", file=sys.stderr)
        for line in result.rerun:
            print(f"erasure to run again: {line}", file=sys.stderr)
        return 1 if result.errors or result.rerun else 0

    return run_connected(_run)


async def _record(namespace: str, started: float, result: Any) -> None:
    """The manual pass's row in loom.worker_status, written now (the process exits)."""
    from server.knowledge.workers.source_sync import outcome
    from server.workers import status_store

    try:
        counts = outcome(result)
        pass_started, pass_finished = status_store.wall_from_monotonic(started)
        await status_store.record_manual_pass(
            "knowledge_source_sync",
            namespace=namespace,
            status="failed" if result.failed else "ok",
            started=pass_started,
            finished=pass_finished,
            items_planned=counts["items_planned"],
            items_done=counts["items_done"],
            rows_written=counts["rows_written"],
            partial_failures=result.failed,
            detail=status_store.detail_text(counts, {"command": "loom knowledge sync"}),
        )
    except Exception as e:  # noqa: BLE001 — health reporting never fails the command
        print(f"note: worker_status row not written: {e}", file=sys.stderr)
