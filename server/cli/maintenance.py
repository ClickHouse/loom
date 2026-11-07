"""Memory maintenance commands: activity-narration demotion, period digests,
reconcile status, and the reextract / reembed passes.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import run_connected
from server.cli._client import refused_for_agent, resolve_token


def _cmd_period_digest(args: argparse.Namespace) -> int:
    """Write period digests over K consecutive session digests of one namespace."""
    from server.memory.app.period_digest import PeriodDigestConfig, run_period_digest

    async def _go(ch: Any) -> int:
        cfg = PeriodDigestConfig(
            sessions_per_period=int(args.sessions),
            max_identities_per_pass=int(args.max_identities),
            max_periods_per_identity=int(args.max_periods),
            org_filter=args.org,
            namespace_filter=args.namespace,
            require_opt_in=False,
            rewrite_existing=bool(args.rewrite),
        )
        res = await run_period_digest(cfg, ch=ch, dry_run=bool(args.dry_run))
        verb = "would write" if args.dry_run else "wrote"
        print(
            f"  identities: {res.identities_scanned}; periods {verb}: "
            f"{res.periods_planned if args.dry_run else res.periods_written} "
            f"(skipped as written or taken out: {res.periods_skipped}; "
            f"digests covered: {res.digests_covered}; "
            f"tokens in/out {res.tokens_in}/{res.tokens_out})"
        )
        for f in res.failures[:10]:
            print(f"  failure: {f}")
        return 1 if res.failures and not res.periods_written else 0

    return run_connected(_go)


def _cmd_reconcile_status(args: argparse.Namespace) -> int:
    """Mirror loom.memory status onto memory_embeddings where they diverged."""

    from server.memory.store import divergent_namespaces, reconcile_embedding_status

    if not args.all_namespaces and not args.namespace:
        print("reconcile-status: --namespace NAME or --all-namespaces is required")
        return 2
    verb = "would re-emit" if args.dry_run else "re-emitted"

    def _report(ns: str, counts: dict[str, int]) -> None:
        print(f"  {ns}: divergent identity rows: {counts.get('divergent_rows', 0)}")
        for k, v in sorted(counts.items()):
            if k not in ("divergent_rows", "mirrored", "held_back"):
                print(f"    memory status {k}: {v}")
        if counts.get("held_back"):
            print(f"    held back (newer or erased in Postgres): {counts['held_back']}")
            if counts["held_back"] == int(args.limit):
                print(
                    f"    all {args.limit} rows read were held back: raise --limit, or rerun"
                    " once Postgres's changes reach ClickHouse (parked deliveries:"
                    " /v1/health?include=postgres)"
                )
        print(f"    {verb} {counts.get('mirrored', 0)} embeddings rows")

    async def _go(ch: Any) -> int:
        if args.all_namespaces:
            # One sweep over the org instead of one
            # invocation per namespace.
            targets = await divergent_namespaces(
                args.org, since_days=int(args.since_days), limit=int(args.limit), ch=ch
            )
            if args.exclude:
                import fnmatch

                before = len(targets)
                targets = [
                    t
                    for t in targets
                    if not any(fnmatch.fnmatch(t["namespace"], pat) for pat in args.exclude)
                ]
                print(f"  excluded {before - len(targets)} namespace(s) matching {args.exclude}")
            print(f"  {len(targets)} namespace(s) with divergent rows in org {args.org}")
            total = 0
            for tgt in targets:
                counts = await reconcile_embedding_status(
                    args.org,
                    tgt["namespace"],
                    limit=int(args.limit),
                    dry_run=bool(args.dry_run),
                    ch=ch,
                )
                total += int(counts.get("mirrored", 0))
                _report(tgt["namespace"], counts)
            print(f"  total: {verb} {total} embeddings rows across {len(targets)} namespace(s)")
            return 0
        counts = await reconcile_embedding_status(
            args.org, args.namespace, limit=int(args.limit), dry_run=bool(args.dry_run), ch=ch
        )
        _report(args.namespace, counts)
        return 0

    return run_connected(_go)


def _add_activity_trivia_parser(sub: Any) -> None:
    """`loom demote-activity-narration`."""
    p = sub.add_parser(
        "demote-activity-narration",
        help="Find a namespace's extracted facts that narrate tool activity ('User read "
        "lines 1-1000 of X', 'Claude ran a command on …') with no durable outcome and "
        "demote (confidence x0.25) or label them through memory.apply. Dry run by default; "
        "keys an agent read by name in the last --days are left alone. Never deletes.",
    )
    p.add_argument("--org", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--action", choices=["demote", "label"], default="demote")
    p.add_argument(
        "--classifier",
        choices=["narration", "fact_quality"],
        default="narration",
        help="fact_quality: every junk class plus word-for-word restatements",
    )
    p.add_argument("--days", type=int, default=30, help="read-by-name protection window")
    p.add_argument("--limit", type=int, default=20000, help="live rows scanned")
    add_apply_flags(p, writes="write the demotions or labels")
    p.set_defaults(func=_cmd_demote_activity_narration)


def _cmd_demote_activity_narration(args: argparse.Namespace) -> int:

    from server.memory.app import activity_trivia_cleanup as atc

    async def _go(ch: Any) -> int:
        fq = getattr(args, "classifier", "narration") == "fact_quality"
        finder = atc.find_junk if fq else atc.find
        found = await finder(
            ch, args.org, args.namespace, limit=int(args.limit), days=int(args.days)
        )
        totals = await atc.apply(
            args.org,
            args.namespace,
            found["candidates"],
            action=args.action,
            execute=not args.dry_run,
            **atc.junk_apply_kwargs(fq),
        )
        mode = "dry run (pass --apply to write)" if args.dry_run else "executed"
        print(f"demote-activity-narration {args.org}/{args.namespace}: {mode}")
        print(f"  action={args.action}")
        for line in atc.summary_lines(found, fq):
            print("  " + line)
        print("  " + ", ".join(f"{k}={v}" for k, v in totals.items()))
        for r in found["candidates"][: 10**6 if fq else 15]:
            print("    " + atc.candidate_line(r))
        return 0

    return run_connected(_go)


def _add_reextract_parser(sub: Any) -> None:
    """The `reextract` subcommand (kept out of `main` for the
    function-length gate)."""
    p_rx = sub.add_parser(
        "reextract",
        help="Replay a namespace's raw evidence (memory.set_from_messages turns) through a "
        "running server's write path into ANOTHER namespace — the current extractor over "
        "the stored transcripts; the source is never written.",
    )
    p_rx.add_argument("--org", required=True)
    p_rx.add_argument("--namespace", required=True, help="Source namespace (read only)")
    p_rx.add_argument("--into", required=True, help="Target namespace (must differ)")
    p_rx.add_argument(
        "--base-url", default=os.environ.get("LOOM_BASE_URL", "http://127.0.0.1:7777")
    )
    p_rx.add_argument(
        "--token",
        default=None,
        help="Writer token for --into (else $LOOM_API_KEY, else ~/.loom/token)",
    )
    p_rx.add_argument(
        "--dates",
        choices=("auto", "none"),
        default="auto",
        help="auto: send each session's day from its evidence (skipped for rows that "
        "carry the ingest minute); none: never send observation_date",
    )
    p_rx.add_argument("--concurrency", type=int, default=2)
    p_rx.add_argument(
        "--limit-sessions", type=int, default=0, help="Replay only the first N sessions"
    )
    p_rx.add_argument(
        "--dry-run", action="store_true", help="List the sessions and dates; post nothing"
    )
    p_rx.set_defaults(func=_cmd_reextract)


def _cmd_reextract(args: argparse.Namespace) -> int:
    """Replay a namespace's raw evidence through a running
    server's write path into another namespace — the current extractor over
    the stored transcripts, the source untouched."""
    import asyncio

    from server.core.ch import get_client
    from server.memory.app.reextract import load_sessions, render_report, replay

    if args.into == args.namespace:
        print(
            "  ✗ --into must differ from --namespace (the source is never written)",
            file=sys.stderr,
        )
        return 2
    token, source = resolve_token(args.token)
    # A dry run sends nothing, so it needs no token; anything else refuses an
    # agent before the evidence is read.
    if not args.dry_run and refused_for_agent(source):
        return 2

    async def _go() -> int:
        ch = get_client()
        await ch.connect()
        try:
            sessions = await load_sessions(ch, org=args.org, namespace=args.namespace)
        finally:
            await ch.disconnect()
        if args.limit_sessions:
            sessions = sessions[: args.limit_sessions]
        if not sessions:
            print(
                f"  no set_from_messages evidence in {args.org}/{args.namespace}",
                file=sys.stderr,
            )
            return 1
        rep = await replay(
            sessions,
            base_url=args.base_url,
            token=token,
            org=args.org,
            into=args.into,
            dates=args.dates,
            concurrency=args.concurrency,
            dry_run=args.dry_run,
        )
        print(render_report(rep, source=args.namespace, into=args.into, dry_run=args.dry_run))
        return 1 if rep.failed and rep.failed == rep.sessions else 0

    return asyncio.run(_go())


def _cmd_reembed(args: argparse.Namespace) -> int:
    """Re-embed off-model vectors to the configured embedder (run after a
    LOOM_EMBEDDING_MODEL change — Loom does not auto-re-embed)."""

    from server.core.errors import CodedWidthMismatchError
    from server.embedding import get_embedder
    from server.memory.app.reembed import reembed_all

    async def _go(ch: Any) -> int:
        embedder = get_embedder()
        print(f"target embedder: {embedder.model_name} ({embedder.dim}-dim)")
        try:
            written = await reembed_all(
                ch,
                embedder,
                org=args.org or None,
                namespace=args.namespace or None,
                batch=args.batch_size,
                dry_run=args.dry_run,
                delete_old=args.delete_old,
                in_database=(getattr(args, "in_database", "") or None),
                key_only=bool(getattr(args, "key_only", False)),
                resume=(args.resume or None),
            )
        except CodedWidthMismatchError as e:
            print(
                f"loom reembed: {e}. A coded table takes one width: change the model in "
                "Settings → Models & keys, which rebuilds the tables at the new width.",
                file=sys.stderr,
            )
            return 2
        except ValueError as e:  # --in-database or --resume refused before any row is read
            print(f"loom reembed: {e}", file=sys.stderr)
            return 2
        print(f"re-embedded: {written}")
        return 0

    return run_connected(_go)


def _add_reembed_parser(sub: Any) -> None:
    """The `reembed` subcommand."""
    p_reembed = sub.add_parser(
        "reembed",
        help="Re-embed vectors whose embedding_model != the configured "
        "embedder (run after changing LOOM_EMBEDDING_MODEL — Loom does not "
        "auto-re-embed). Covers memory, entity, and conversation vectors. Coded "
        "vector tables keep their width: change a model of another width in "
        "Settings → Models & keys, which rebuilds them.",
    )
    p_reembed.add_argument("--org", default="", help="Limit to one org")
    p_reembed.add_argument("--namespace", default="", help="Limit to one namespace")
    p_reembed.add_argument("--batch-size", type=int, default=200)
    p_reembed.add_argument(
        "--in-database",
        default="",
        metavar="NAMED_COLLECTION",
        help="Re-embed inside ClickHouse with aiEmbed (ClickHouse 26.7+): the "
        "named collection holding provider/endpoint/api_key. One INSERT … SELECT per "
        "table instead of a Python loop; refuses when an embed-text switch is on, and "
        "with a customer or identity header set or the inference gateway required "
        "(ClickHouse's calls name no one and skip the gateway check).",
    )
    p_reembed.add_argument(
        "--dry-run", action="store_true", help="Count off-model rows without writing"
    )
    p_reembed.add_argument(
        "--delete-old", action="store_true", help="Delete off-model rows after re-embedding"
    )
    p_reembed.add_argument(
        "--key-only",
        action="store_true",
        help="Rebuild only the key-axis vectors in scope, from the "
        "current key-axis text; value vectors are kept",
    )
    p_reembed.add_argument(
        "--resume",
        default="",
        help="Continue after the row a previous run's `resume:` line names "
        "(the tables before it are done)",
    )
    p_reembed.set_defaults(func=_cmd_reembed)


def _add_memory_repair_parsers(sub: Any) -> None:
    """The one-namespace memory passes: `reconcile-status` and `period-digest`."""
    p_rs = sub.add_parser(
        "reconcile-status",
        help="Re-emit memory_embeddings rows whose status diverged from loom.memory "
        "(superseded / conflicted / duplicate marks the pool never saw), compared and "
        "re-emitted per identity row (org, namespace, agent, user_id, session_id, key). "
        "Dry run by default (counts by status, the rows it would write); --apply writes.",
    )
    p_rs.add_argument("--org", required=True)
    p_rs.add_argument("--namespace", default="", help="one namespace (or --all-namespaces)")
    p_rs.add_argument(
        "--all-namespaces",
        action="store_true",
        help="every namespace of the org with divergent rows, largest first",
    )
    p_rs.add_argument(
        "--since-days",
        type=int,
        default=0,
        help="with --all-namespaces: only rows created in the last N days (0 = all)",
    )
    p_rs.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="GLOB",
        help="with --all-namespaces: skip namespaces matching this glob (repeatable), "
        "e.g. --exclude 'bench-*' for data another team owns",
    )
    p_rs.add_argument("--limit", type=int, default=5000)
    add_apply_flags(p_rs, writes="re-emit the diverged rows")
    p_rs.set_defaults(func=_cmd_reconcile_status)

    p_pd = sub.add_parser(
        "period-digest",
        help="Write period digests — one derived semantic memory over K consecutive "
        "session digests of each identity in one namespace. Additive and "
        "idempotent. Dry run by default (plans only); --apply writes.",
    )
    p_pd.add_argument("--org", required=True)
    p_pd.add_argument("--namespace", required=True)
    p_pd.add_argument("--sessions", type=int, default=5, help="digests per period (K)")
    p_pd.add_argument("--max-identities", type=int, default=50)
    p_pd.add_argument("--max-periods", type=int, default=50)
    add_apply_flags(p_pd, writes="write the period digests")
    p_pd.add_argument(
        "--rewrite",
        action="store_true",
        help="rewrite live windows already written; one deleted, redacted or no longer active "
        "stays out",
    )
    p_pd.set_defaults(func=_cmd_period_digest)
