"""The tracker operator commands for the `loom` CLI: probe, contract check,
duplicate-check fit and embedding reset."""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import dry_run_orgs, run_connected

# ----------------------------------------------------------------------
# loom tracker-probe — first contact with the tracker source


def _cmd_tracker_probe(args: argparse.Namespace) -> int:
    """Counts and the newest timestamp per upstream table per configured
    credential, the breaker states and the configured namespaces — what the
    live source will serve before it serves it. Read-only."""
    import asyncio

    async def _run() -> int:
        from server.tracker.domain.namespaces import configured_namespaces, repo_namespaces
        from server.tracker.store.source import qa_intel

        try:
            out = await qa_intel.probe()
        finally:
            await qa_intel.close()
        out["routing"] = repo_namespaces()
        out["namespaces"] = {
            ns: {"repos": list(c.repos), "restricted": c.restricted, "credential": c.credential}
            for ns, c in configured_namespaces().items()
        }
        print(json.dumps(out, indent=2, default=str))
        return 0 if out.get("configured") else 1

    return asyncio.run(_run())


def register_tracker_probe(sub: Any) -> None:
    """`loom tracker-probe` on the operator CLI."""
    sub.add_parser(
        "tracker-probe",
        help="Read-only first contact with the tracker source: "
        "counts and newest timestamps per upstream table per credential, breaker states, "
        "the configured tracker namespaces.",
    ).set_defaults(func=_cmd_tracker_probe)


# ----------------------------------------------------------------------
# loom tracker-dupcheck-fit — refit tracker.duplicate_check's confidence on demand


def _cmd_tracker_dupcheck_fit(args: argparse.Namespace) -> int:
    """Replay the duplicate gold of one tracker namespace through the op's own
    path at each query's filing time, fit the logistic confidence, print the
    fit (coefficients, curve, holdout) and store it — the weekly leg of the
    tracker_links pass, run now. `--dry-run` fits and prints without writing."""

    if not args.org:
        raise SystemExit("no tracker org: pass --org or set LOOM_TRACKER_ORG")

    async def _run(ch: Any) -> tuple[int, dict[str, Any]]:
        from server.llm import customer
        from server.tracker.app import dupcheck_fit as df
        from server.tracker.app.source import tracker_source
        from server.tracker.store.source import qa_intel

        org = str(args.org or "")
        src = tracker_source(org, args.namespace)

        def progress(done: int, total: int) -> None:
            if done % 25 == 0 or done == total:
                print(f"  replayed {done}/{total}", file=sys.stderr)

        # The replay embeds the org's issue text: its model calls name the org, so the
        # switch holds them and the command fails rather than send them.
        with customer.for_org(org, namespace=str(args.namespace or "")):
            out = await df.refit_namespace(
                src,
                df.state_repo(),
                org,
                args.namespace,
                force=True,
                write=not args.dry_run,
                negatives=args.negatives,
                progress=progress,
            )
        fit = dict(out.fit or {})
        shown = {k: v for k, v in fit.items() if k != "curve"}
        shown["curve"] = [
            pt for pt in fit.get("curve") or [] if round(pt["threshold"] * 100) % 10 == 0
        ]
        print(
            json.dumps(
                {"status": out.status, "detail": out.detail, "fit": shown}, indent=2, default=str
            )
        )
        await qa_intel.close()
        return 0 if out.status in ("written", "dry_run") else 1, fit

    code, fit = run_connected(_run, orgs=dry_run_orgs(args))
    if args.out and fit:
        pathlib.Path(args.out).write_text(json.dumps(fit, indent=1, default=str))
    return code


def register_tracker_dupcheck_fit(sub: Any) -> None:
    """`loom tracker-dupcheck-fit` on the operator CLI."""
    from server.settings import settings

    p = sub.add_parser(
        "tracker-dupcheck-fit",
        help="Refit tracker.duplicate_check's calibrated confidence for one tracker namespace now: "
        "replays the duplicate gold at filing time, fits, stores the fit.",
    )
    p.add_argument("--namespace", required=True, help="the tracker namespace")
    p.add_argument("--org", default=settings.tracker_org)
    p.add_argument(
        "--negatives",
        type=int,
        default=None,
        help="non-duplicates to sample (default LOOM_TRACKER_DUPCHECK_FIT_NEGATIVES)",
    )
    p.add_argument("--dry-run", action="store_true", help="fit and print, write nothing")
    p.add_argument("--out", default="", help="write the whole fit JSON here")
    p.set_defaults(func=_cmd_tracker_dupcheck_fit)


# ----------------------------------------------------------------------
# loom tracker-embed-reset — re-walk a tracker namespace's embedding window


def _cmd_tracker_embed_reset(args: argparse.Namespace) -> int:
    """Clear the tracker_embed pass's per-kind cursors for one tracker namespace, so
    the next pass reads the whole window again and re-embeds every item
    whose embedded text differs from the stored digest — the way a recipe
    change (`tracker_embed_body_chars`) reaches rows the incremental walk would
    never revisit. Unchanged digests are skipped, so this costs one full
    read and only the embeddings that actually changed."""

    if not args.org:
        raise SystemExit("no tracker org: pass --org or set LOOM_TRACKER_ORG")

    async def _run(ch: Any) -> int:
        from server.tracker.app.embed_index import KINDS, cursor_state

        kinds = [k for k in KINDS if not args.kind or k == args.kind]
        state = cursor_state()
        for kind in kinds:
            before = await state.get(args.org, args.namespace, kind)
            if args.dry_run:
                print(f"{args.namespace}/{kind}: cursor {before} (dry run, kept)")
                continue
            await state.clear(args.org, args.namespace, kind)
            print(f"{args.namespace}/{kind}: cursor {before} → cleared")
        return 0

    return run_connected(_run, orgs=dry_run_orgs(args))


def register_tracker_embed_reset(sub: Any) -> None:
    """`loom tracker-embed-reset` on the operator CLI."""
    from server.settings import settings

    p = sub.add_parser(
        "tracker-embed-reset",
        help="Clear the tracker_embed pass cursors of one tracker namespace so the next "
        "pass re-walks the window and re-embeds every item whose text recipe changed. "
        "Dry run by default (prints the cursors); --apply clears them.",
    )
    p.add_argument("--namespace", required=True, help="the tracker namespace")
    p.add_argument("--org", default=settings.tracker_org)
    p.add_argument("--kind", default="", help="issue or pr (default: both)")
    add_apply_flags(p, writes="clear the cursors")
    p.set_defaults(func=_cmd_tracker_embed_reset)


# ----------------------------------------------------------------------
# loom tracker-contract-check — does a tracker source meet the record contract?


def _render_contract_report(rep: Any) -> list[str]:
    d = rep.to_dict()
    lines = [f"{d['source']} ({d['database']}): {'OK' if d['ok'] else 'FAIL'} — {d['summary']}"]
    lines.append("  optional present: " + (", ".join(d["optional_present"]) or "-"))
    if d["optional_incomplete"]:
        lines.append(
            "  optional incomplete: "
            + ", ".join(f"{t} ({'; '.join(c)})" for t, c in d["optional_incomplete"].items())
        )
    lines.append(
        "  features: " + ", ".join(f"{k}={'yes' if v else 'no'}" for k, v in d["features"].items())
    )
    return lines


def _cmd_tracker_contract_check(args: argparse.Namespace) -> int:
    """Read each configured tracker source's schema through its own credential
    and compare it with the record contract: required tables and columns,
    optional tables, feature columns. Exit 0 when every checked source meets
    it. Read-only."""
    import asyncio

    async def _run() -> int:
        from server.tracker.app import contract
        from server.tracker.domain import sources
        from server.tracker.store.source import qa_intel

        eps = sources.endpoints()
        names = [args.source] if getattr(args, "source", "") else sorted(eps)
        if getattr(args, "namespace", ""):
            entry = sources.namespace_entries().get(args.namespace)
            if entry is None:
                print(
                    f"namespace {args.namespace!r} is not a configured tracker namespace",
                    file=sys.stderr,
                )
                return 1
            names = [entry.source]
        reports: dict[str, Any] = {}
        rc = 0
        try:
            for name in names:
                ep = eps.get(name)
                if ep is None or not sources.credential_configured(name):
                    print(
                        f"{name}: not configured (endpoint or credential missing)",
                        file=sys.stderr,
                    )
                    rc = 1
                    continue
                try:
                    rep = await contract.check(name, ep.database)
                except Exception as e:  # noqa: BLE001 — the source did not answer
                    print(
                        f"{name} ({ep.database}): UNAVAILABLE — {type(e).__name__}: {e}",
                        file=sys.stderr,
                    )
                    rc = 1
                    continue
                reports[name] = rep.to_dict()
                if not getattr(args, "json", False):
                    print("\n".join(_render_contract_report(rep)))
                rc = rc if rep.ok else 1
        finally:
            await qa_intel.close()
        if getattr(args, "json", False):
            print(json.dumps(reports, indent=2, default=str))
        return rc

    return asyncio.run(_run())


def register_tracker_contract_check(sub: Any) -> None:
    """`loom tracker-contract-check` on the operator CLI."""
    p = sub.add_parser(
        "tracker-contract-check",
        help="Read-only: does each tracker source's schema meet the record contract "
        "(docs/features/tracker/record-contract.md)? Names missing tables / columns, "
        "optional tables present, feature columns. Exit 0 when every checked source meets it.",
    )
    p.add_argument("--source", default="", help="check one source by name")
    p.add_argument("--namespace", default="", help="check the source of one tracker namespace")
    p.add_argument("--json", action="store_true", help="machine-readable report")
    p.set_defaults(func=_cmd_tracker_contract_check)
