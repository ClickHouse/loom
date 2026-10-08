"""Operator command bodies for the `loom` CLI: recalibration, the chunk
backfill, knowledge rechunk and lexical stats, and the sensitivity and
integrity repairs. `loom --help` lists them."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import run_connected


def _cmd_backfill_chunks(args: argparse.Namespace) -> int:
    """Backfill loom.memory_chunks from existing rows in loom.memory.

    For namespaces that predate the chunks layer: each live row goes through
    memory.set's own chunk writer (`write_set_chunks`), so a backfilled chunk
    is the one memory.set would have written — the value's human text, under
    session `memory.set:<key>` — and a later re-set or delete of the key finds
    and tombstones it. Idempotent: a key whose live chunks already match is
    skipped without embedding.

    Scope:
      --org / --namespace: required, no global backfill (cost guard)
      --dry-run: report counts without writing
    """
    from types import SimpleNamespace

    from server.cli._ch import run_connected
    from server.embedding import get_embedder
    from server.memory.app.set_chunks import (
        plan_set_chunks,
        set_chunks_current,
        write_set_chunks,
    )
    from server.memory.store import parse_memory_value

    async def _run(ch: Any) -> int:
        embedder = get_embedder()
        rows = await ch.query_all(
            """
            SELECT memory_key, agent, user_id, run_id, memory_type,
                   if(length(toString(value_json)) > 2, toString(value_json), value) AS value
            FROM loom.memory FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            """,
            params={"org": args.org, "ns": args.namespace},
        )
        if not rows:
            print(f"no memories in {args.org}/{args.namespace}", file=sys.stderr)
            return 0
        chunks_written = keys_current = 0
        for row in rows:
            value = parse_memory_value(row.get("value"))
            memory_type = str(row.get("memory_type") or "")
            chunks, _ = plan_set_chunks(value, memory_type)
            if not chunks:
                continue
            if await set_chunks_current(
                ch,
                org=args.org,
                namespace=args.namespace,
                memory_key=row["memory_key"],
                chunks=chunks,
                user_id=str(row.get("user_id") or "-"),
            ):
                keys_current += 1
                continue
            if args.dry_run:
                chunks_written += len(chunks)
                continue
            chunks_written += await write_set_chunks(
                SimpleNamespace(
                    org=args.org,
                    namespace=args.namespace,
                    agent=str(row.get("agent") or ""),
                    user_id=str(row.get("user_id") or "-"),
                    run_id=str(row.get("run_id") or "-"),
                    memory_key=row["memory_key"],
                    value=value,
                ),
                memory_type=memory_type,
                ch=ch,
                embedder=embedder,
            )
        verb = "would write" if args.dry_run else "wrote"
        print(
            f"backfill-chunks: {len(rows)} memories, {keys_current} keys already chunked; "
            f"{verb} {chunks_written} chunks"
        )
        return 0

    return run_connected(_run)


def _cmd_recalibrate(args: argparse.Namespace) -> int:
    """Manually trigger the recalibration worker pass.

    Same logic as the weekly cron worker, but immediate. Use after shipping a
    new corpus so per-NS thresholds activate without waiting up to a week.
    Exit 0 on success, 1 on probe failure.
    """
    import sys

    from server.memory.workers.recalibration import run_pass

    async def _run(ch: Any) -> int:
        try:
            result = await run_pass()
        except Exception as e:  # noqa: BLE001 — a failed probe is exit 1, the calibration stays
            print(f"recalibration failed: probe error: {e}", file=sys.stderr)
            return 1
        if not result.ran:
            # Skipped on a fake embedder is OK, not a failure.
            print(f"recalibration skipped: {result.skipped_reason}", file=sys.stderr)
            return 0
        assert result.new_calibration_at is not None  # set whenever the pass ran
        print(f"✓ recalibration complete at {result.new_calibration_at.isoformat()}")
        if result.drift_warnings:
            print(f"  {len(result.drift_warnings)} drift warning(s):")
            for w in result.drift_warnings:
                print(f"    - {w}")
        return 0

    return run_connected(_run)


# ----------------------------------------------------------------------
# loom rechunk — re-ingest stored documents through the current chunker
# ----------------------------------------------------------------------


async def rechunk_namespace(
    org: str,
    namespace: str,
    *,
    source_uri: str = "",
    dry_run: bool = False,
    page_size: int = 100,
) -> dict[str, Any]:
    """Re-run every live document in a namespace through knowledge.upsert
    from its stored content: the current chunker
    (sections + breadcrumb) and embedder replace the chunks, old chunks are
    superseded, and the term-frequency view sees the new rows. Blobs are
    carried forward by upsert. Returns counts; `dry_run` only lists."""
    from server.knowledge.api.models_knowledge import KnowledgeUpsertInput
    from server.knowledge.app.writes import handle_knowledge_upsert
    from server.knowledge.store import documents as documents_repo

    docs: list[Mapping[str, Any]] = []
    cursor: tuple[Any, str] | None = None
    while True:
        page = await documents_repo.list_with_chunk_counts(
            org,
            namespace,
            include_superseded=False,
            cursor=cursor,
            limit=page_size,
        )
        if not page:
            break
        docs.extend(page)
        last = page[-1]
        cursor = (last["ingested_at"], last["source_uri"])
        if len(page) < page_size:
            break
    if source_uri:
        docs = [d for d in docs if d.get("source_uri") == source_uri]

    out: dict[str, Any] = {
        "namespace": namespace,
        "documents": len(docs),
        "rechunked": 0,
        "chunks_before": 0,
        "chunks_after": 0,
        "failed": [],
        "dry_run": dry_run,
    }
    for d in docs:
        uri = str(d["source_uri"])
        out["chunks_before"] += int(d.get("chunk_count") or d.get("chunks") or 0)
        if dry_run:
            continue
        row = await documents_repo.get_one(org, namespace, uri)
        if row is None:
            out["failed"].append(f"{uri}: no live document row")
            continue
        try:
            meta = json.loads(row.metadata) if row.metadata else {}
        except (TypeError, ValueError):
            meta = {}
        try:
            res = await handle_knowledge_upsert(
                KnowledgeUpsertInput(
                    org=org,
                    namespace=namespace,
                    source_uri=uri,
                    content=row.content,
                    title=row.title or "",
                    description=row.description or "",
                    tags=list(row.tags or []),
                    metadata=meta if isinstance(meta, dict) else {},
                )
            )
        except Exception as e:  # noqa: BLE001 — one bad document must not stop the pass
            out["failed"].append(f"{uri}: {type(e).__name__}: {e}")
            continue
        out["rechunked"] += 1
        out["chunks_after"] += int(res.chunks_created)
    return out


def _cmd_rechunk(args: argparse.Namespace) -> int:
    """Re-chunk and re-embed a namespace's documents from stored content so
    documents ingested before section chunking / the lexical leg get section
    metadata and document frequency. Idempotent; costs one embedding call per
    new chunk."""

    async def _run(ch: Any) -> int:
        out = await rechunk_namespace(
            args.org,
            args.namespace,
            source_uri=args.source_uri or "",
            dry_run=bool(args.dry_run),
        )
        verb = "would re-chunk" if out["dry_run"] else "re-chunked"
        print(
            f"{verb} {out['rechunked'] if not out['dry_run'] else out['documents']} of "
            f"{out['documents']} document(s) in {args.org}/{args.namespace}: "
            f"{out['chunks_before']} chunk(s) before"
            + ("" if out["dry_run"] else f", {out['chunks_after']} after")
        )
        for f in out["failed"]:
            print(f"  failed: {f}")
        return 1 if out["failed"] else 0

    return run_connected(_run)


# ----------------------------------------------------------------------
# loom lexical-stats-rebuild — recompute BM25 document frequency from live rows
# ----------------------------------------------------------------------


def _cmd_lexical_stats_rebuild(args: argparse.Namespace) -> int:
    """Recompute BM25 document frequency for a namespace from its live rows
    (knowledge chunks, memory excerpts). Run after bulk deletes / supersessions
    or on a schedule; the views only see inserts."""

    from server.memory.store import term_freq as term_freq_repo

    planes = tuple(p for p in ("knowledge", "memory") if getattr(args, p, True))

    async def _run(ch: Any) -> int:
        deleted = await term_freq_repo.rebuild(
            ch, org=args.org, namespace=args.namespace, planes=planes
        )
        for plane, n in deleted.items():
            print(f"{plane}: replaced {n} aggregate row(s) for {args.org}/{args.namespace}")
        return 0

    return run_connected(_run)


# ---------------------------------------------------------------------------
# loom sensitivity-sweep / loom integrity-repair
# ---------------------------------------------------------------------------


def _cmd_sensitivity_sweep(args: argparse.Namespace) -> int:
    """Label live rows the sensitivity detector would have labelled on write.

    `--dry-run` (default) prints the counts by level, by probe shape and by
    namespace and writes nothing. `--apply` writes one new memory version per
    labelled row, tombstones a `high` row's embedding rows and entity
    vectors, tags a `medium` row's embedding rows. The closing line is the
    high-sensitivity invariant: live `high` rows that still have a live embedding row.
    """
    return run_connected(lambda ch: _sensitivity_sweep_run(ch, args))


async def _sensitivity_sweep_run(ch: Any, args: argparse.Namespace) -> int:
    from server.memory.app import sensitivity_sweep as sweep

    org = args.org or None
    ns = args.namespace or None
    report = await sweep.scan(
        org=org,
        namespace=ns,
        since_days=int(args.since_days or 0) or None,
        limit=int(args.limit),
        only_shapes=getattr(args, "only_shape", None) or None,
        ch=ch,
    )
    levels, shapes = report.by_level, report.by_shape
    scope = " ".join(x for x in (f"org={org}" if org else "", f"namespace={ns}" if ns else "") if x)
    print(
        f"{report.scanned} unlabelled row(s) matched the coarse shape filter"
        + (f" ({scope})" if scope else "")
        + ("; LIMIT reached — re-run after applying" if report.truncated else "")
    )
    print(
        f"detector would label {len(report.candidates)}: high={levels.get('high', 0)} "
        f"medium={levels.get('medium', 0)} | shapes: credential={shapes.get('credential', 0)} "
        f"email={shapes.get('email', 0)} phone={shapes.get('phone', 0)} "
        f"other={shapes.get('other', 0)}"
    )
    per_ns = sorted(report.by_namespace().items(), key=lambda kv: -sum(kv[1].values()))
    for (o, n), c in per_ns[: int(args.show)]:
        print(f"  {o}/{n:<40} high={c.get('high', 0):>4} medium={c.get('medium', 0):>5}")
    if len(per_ns) > int(args.show):
        print(f"  … {len(per_ns) - int(args.show)} more namespace(s)")
    rc = 0
    if not args.dry_run and report.candidates:
        result = await sweep.apply(report.candidates, ch=ch)
        print(f"applied: {json.dumps(result.as_dict(), sort_keys=True)}")
        for err in result.errors[:10]:
            print(f"  error: {err}", file=sys.stderr)
        if result.errors:
            rc = 1
    elif report.candidates:
        print("dry run — pass --apply to write the labels")
        rc = 1
    high_live = await sweep.high_with_embedding_count(org=org, namespace=ns, ch=ch)
    print(f"invariant: high rows with a live embedding row = {high_live}")
    return rc if high_live == 0 else 1


def _cmd_integrity_repair(args: argparse.Namespace) -> int:
    """Read the three row-level invariants and, with
    `--apply`, re-emit the dim-0-with-vector rows with their true dimension.
    The drift and valid_to counts are listed, not repaired."""
    return run_connected(lambda ch: _integrity_repair_run(ch, args))


async def _integrity_repair_run(ch: Any, args: argparse.Namespace) -> int:
    from server.memory.store import integrity as repo

    org = args.org or None
    ns = args.namespace or None
    counts = await repo.integrity_counts(ch=ch)
    print(
        "gauges: " + " ".join(f"{k}={counts.get(k, 0)}" for k in repo.GAUGES) + " (deployment-wide)"
    )
    rows = await repo.dim0_with_vector_rows(limit=int(args.limit), org=org, namespace=ns, ch=ch)
    by_ns: dict[str, int] = {}
    for r in rows:
        key = f"{r.get('org')}/{r.get('namespace')}"
        by_ns[key] = by_ns.get(key, 0) + 1
    print(f"{len(rows)} dim-0 row(s) holding a vector in scope")
    for key, n in sorted(by_ns.items(), key=lambda kv: -kv[1])[: int(args.show)]:
        print(f"  {key:<50} rows={n:>5}")
    drift = await repo.drift_sample(limit=int(args.show), org=org, namespace=ns, ch=ch)
    if drift:
        print("tombstone/status drift, largest first (listed, not repaired):")
        for d in drift:
            print(
                f"  {d['org']}/{str(d['namespace']):<40} status={d['status']:<11} "
                f"rows={int(d['n']):>6}"
            )
    rc = 0
    if rows and not args.dry_run:
        n = await repo.repair_dim0_rows(rows, ch=ch)
        print(f"re-emitted {n} embedding row(s) with embedding_dim = length(embedding)")
        counts = await repo.integrity_counts(ch=ch)
        print("gauges after: " + " ".join(f"{k}={counts.get(k, 0)}" for k in repo.GAUGES))
    elif rows:
        print("dry run — pass --apply to re-emit them")
        rc = 1
    return rc if not any(counts.get(k, 0) for k in repo.GAUGES) else max(rc, 1)


def _add_sensitivity_sweep_parser(sub: Any) -> None:
    """`loom sensitivity-sweep`: the detector's late pass."""
    p_ssw = sub.add_parser(
        "sensitivity-sweep",
        help="Label live rows the sensitivity detector would have "
        "labelled on write but a path skipped. Dry run by default: "
        "counts by level, probe shape and namespace. --apply writes one new version "
        "per row, tombstones a high row's embedding rows and tags a medium row's. "
        "The closing line is the high-sensitivity invariant (high rows with a live "
        "embedding).",
    )
    p_ssw.add_argument("--org", default="", help="Limit to one org")
    p_ssw.add_argument("--namespace", default="", help="Limit to one namespace")
    p_ssw.add_argument(
        "--since-days", type=int, default=0, help="Only rows created in the last N days (0 = all)"
    )
    p_ssw.add_argument(
        "--limit", type=int, default=20000, help="Candidate rows read per run (default 20000)"
    )
    p_ssw.add_argument("--show", type=int, default=20, help="Namespaces to print")
    p_ssw.add_argument(
        "--only-shape",
        action="append",
        choices=["credential", "email", "phone", "other"],
        help="Keep candidates carrying this probe shape (repeatable); the phone shape is "
        "a ten-digit run and the noisiest, so apply credential + email first",
    )
    add_apply_flags(p_ssw, writes="write the labels")
    p_ssw.set_defaults(func=_cmd_sensitivity_sweep)


def _add_recalibrate_and_chunks_parsers(sub: Any) -> None:
    """The `recalibrate` and `backfill-chunks` subcommands."""
    p_recal = sub.add_parser(
        "recalibrate",
        help="Re-probe the embedder + run per-NS calibration immediately "
        "(otherwise this runs weekly via the recalibration worker).",
    )
    p_recal.set_defaults(func=_cmd_recalibrate)

    p_chunks = sub.add_parser(
        "backfill-chunks",
        help="Backfill loom.memory_chunks from existing memory rows in "
        "one namespace. Idempotent (skips chunks already present by "
        "content_sha256). Use after migrating from a pre-chunks deploy.",
    )
    p_chunks.add_argument("--org", required=True)
    p_chunks.add_argument("--namespace", required=True)
    p_chunks.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be written without inserting.",
    )
    p_chunks.set_defaults(func=_cmd_backfill_chunks)
