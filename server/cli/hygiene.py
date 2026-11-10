"""Namespace hygiene commands for the `loom` CLI: memory drops, namespace
gc and adopt, and erasure re-verify."""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from server.cli._apply import add_apply_flags
from server.cli._ch import run_connected
from server.core.error_summary import error_summary
from server.erasure.domain.failure_reason import REPLAY_PENDING_REASON


def _bucket_client_installed() -> bool:
    """With a document bucket set, boto3 must be installed: an erasure or its
    census lists the bucket, and would otherwise stop partway on the import.
    Prints why and answers False when it is not."""
    from server.core import object_store

    try:
        object_store.require_client()
    except RuntimeError as e:
        print(f"loom: {e}", file=sys.stderr)
        return False
    return True


# ----------------------------------------------------------------------
# loom drops — memory ingest drop ledger (loom.memory_drops)
# ----------------------------------------------------------------------

_DROPS_SQL = """
SELECT stage, reason, sum(n) AS total, count() AS rows, max(ts) AS last_seen
FROM loom.memory_drops
WHERE namespace = {ns:String}
  AND ts > now() - INTERVAL {hours:UInt32} HOUR
  {org_clause}
GROUP BY stage, reason
ORDER BY total DESC, stage, reason
"""


async def _drops_summary(
    ch: Any, *, namespace: str, hours: int, org: str | None = None
) -> list[dict[str, Any]]:
    """Counts by (stage, reason) for one namespace over the last `hours`."""
    params: dict[str, Any] = {"ns": namespace, "hours": int(hours)}
    org_clause = ""
    if org:
        org_clause = "AND org = {org:String}"
        params["org"] = org
    sql = _DROPS_SQL.replace("{org_clause}", org_clause)
    rows = await ch.query_all(sql, params=params)
    return [dict(r) for r in rows]


def _format_drops(rows: list[dict[str, Any]], *, namespace: str, hours: int) -> str:
    if not rows:
        return f"no memory ingest drops recorded for namespace {namespace!r} in the last {hours}h"
    total = sum(int(r.get("total") or 0) for r in rows)
    lines = [
        f"memory ingest drops for namespace {namespace!r}, last {hours}h "
        f"({total} item(s) across {len(rows)} stage/reason pair(s)):",
        "",
        f"  {'stage':<20} {'reason':<18} {'items':>8}  last_seen",
    ]
    for r in rows:
        lines.append(
            f"  {str(r.get('stage', '')):<20} {str(r.get('reason', '')):<18} "
            f"{int(r.get('total') or 0):>8}  {r.get('last_seen', '')}"
        )
    lines.append("")
    lines.append(
        "details: SELECT * FROM loom.memory_drops WHERE namespace = '...' ORDER BY ts DESC"
    )
    return "\n".join(lines)


def _cmd_drops(args: argparse.Namespace) -> int:
    """Print the memory ingest drop ledger for a namespace, by stage/reason.

    Read-only. Every row is something the pipeline discarded, truncated or
    gave up on (see docs/operations/maintenance.md "Memory ingest drops").
    """

    async def _run(ch: Any) -> int:
        rows = await _drops_summary(
            ch, namespace=args.namespace, hours=int(args.hours), org=args.org or None
        )
        print(_format_drops(rows, namespace=args.namespace, hours=int(args.hours)))
        return 0

    return run_connected(_run)


# ----------------------------------------------------------------------
# loom namespaces-gc — erase throwaway namespaces in one list-scoped saga
# ----------------------------------------------------------------------


def _gc_default_prefixes() -> tuple[str, ...]:
    """The bench prefixes: one list for the auto-register
    hook's `kind = 'bench'` stamp and this command's default scan."""
    from server.identity.app.namespace_autoregister import bench_prefixes

    return bench_prefixes()


async def _gc_candidates(
    ch: Any,
    *,
    org: str,
    prefixes: tuple[str, ...],
    idle_hours: int,
    include_registered: bool,
    explicit: tuple[str, ...] = (),
    exclude_prefixes: tuple[str, ...] = (),
) -> list[dict[str, Any]]:
    """Namespaces that look like finished bench runs: rows in loom.memory,
    a name under one of the prefixes (or named explicitly), no write for
    `idle_hours`, and — unless asked — no row in loom.namespaces (a
    registered namespace is somebody's, even when its name matches)."""
    if not prefixes and not explicit:
        return []
    params: dict[str, Any] = {"org": org, "idle": int(idle_hours)}
    params.update({f"p{i}": p for i, p in enumerate(prefixes)})
    # startsWith: a prefix is matched as literal text, `_` and `%` included.
    name_pred = " OR ".join(
        [f"startsWith(namespace, {{p{i}:String}})" for i in range(len(prefixes))]
        + (["namespace IN {explicit:Array(String)}"] if explicit else [])
    )
    if explicit:
        params["explicit"] = list(explicit)
    # A reuse set another run still reads (a paired A/B, a --namespaces-json
    # map) is fenced off by prefix, whatever its last write says.
    exclude_pred = ""
    for i, p in enumerate(exclude_prefixes):
        params[f"x{i}"] = p
        exclude_pred += f" AND NOT startsWith(namespace, {{x{i}:String}})"
    registered_pred = (
        ""
        if include_registered
        else (
            "AND namespace NOT IN "
            "(SELECT namespace FROM loom.namespaces FINAL WHERE org = {org:String})"
        )
    )
    # Explicit names are the operator's list: every row counts, live or
    # tombstoned, so a namespace whose first erasure tombstoned where it meant
    # to delete can be finished. Prefix scans stay
    # live-only — a tombstoned namespace is not a candidate, it is done.
    live_pred = "" if explicit else "AND tombstoned_at IS NULL"
    rows: list[dict[str, Any]] = await ch.query_all(
        f"""
        SELECT namespace, count() AS rows, max(created_at) AS last_write,
               countIf(recall_count > 0) AS recalled_rows,
               countIf(tombstoned_at IS NULL) AS live_rows
        FROM loom.memory FINAL
        WHERE org = {{org:String}} {live_pred} AND ({name_pred})
          {registered_pred} {exclude_pred}
        GROUP BY namespace
        HAVING max(created_at) < now() - INTERVAL {{idle:UInt32}} HOUR
        ORDER BY last_write ASC
        """,
        params,
    )
    return rows


async def _gc_memoryless_explicit(
    ch: Any,
    *,
    org: str,
    names: tuple[str, ...],
    include_registered: bool,
    exclude_prefixes: tuple[str, ...],
) -> list[str]:
    """The explicitly named namespaces with no row at all in loom.memory,
    minus the registered ones (unless asked) and the excluded prefixes —
    the names `_gc_candidates` cannot see but may still hold side-table rows."""
    names = tuple(n for n in names if not any(n.startswith(p) for p in exclude_prefixes))
    if not names:
        return []
    params = {"org": org, "names": list(names)}
    present = {
        str(r["namespace"])
        for r in await ch.query_all(
            "SELECT DISTINCT namespace FROM loom.memory "
            "WHERE org = {org:String} AND namespace IN {names:Array(String)}",
            params,
        )
    }
    if not include_registered:
        present |= {
            str(r["namespace"])
            for r in await ch.query_all(
                "SELECT namespace FROM loom.namespaces FINAL "
                "WHERE org = {org:String} AND namespace IN {names:Array(String)}",
                params,
            )
        }
    return [n for n in names if n not in present]


async def _parts_footprint(ch: Any) -> dict[str, tuple[int, int]]:
    rows = await ch.query_all(
        """
        SELECT table, count() AS parts, sum(rows) AS rows
        FROM system.parts
        WHERE database = currentDatabase()
          AND table IN ('memory', 'memory_embeddings', 'memory_versions')
          AND active
        GROUP BY table
        """
    )
    return {str(r["table"]): (int(r["parts"]), int(r["rows"])) for r in rows}


def _cmd_namespaces_gc(args: argparse.Namespace) -> int:
    """List — and with --erase, erase — throwaway namespaces.

    Dry-run by default. --erase submits ONE list-scoped erasure saga per
    batch (`scope_type=namespace_list`, purge on unless --no-purge): one
    mutation per table for the whole batch, the residue census as the last
    step. Bench harnesses do this themselves at the end of a run; this
    command is for the backlog and for runs that died before cleanup.
    """
    if args.erase and not _bucket_client_installed():
        return 2
    prefixes = (
        tuple(args.prefix) if args.prefix else (() if args.namespace else _gc_default_prefixes())
    )
    explicit = tuple(args.namespace or ())

    async def _run(ch: Any) -> int:
        # One connection for the whole command: the erasure handler below reads
        # the same module-level client, so it stays connected until the sagas
        # and the after-footprint are done (a saga on a disconnected client
        # fails its first checkpoint insert).
        if getattr(args, "worker_pass", False):
            return await _gc_worker_pass(ch, args, explicit=explicit)
        return await _gc_run(ch, args, prefixes=prefixes, explicit=explicit)

    return run_connected(_run)


async def _gc_worker_pass(ch: Any, args: argparse.Namespace, *, explicit: tuple[str, ...]) -> int:
    """One pass of the namespace_gc worker, from the CLI:
    registered `kind = 'bench'` namespaces with live memory and no write for
    --idle-hours, narrowed to --org and any --namespace. Dry run unless
    --erase; the worker's own settings decide the batch unless overridden
    here, and --erase reaches the external backends unless --skip-external."""
    from server.identity.workers.namespace_gc import GcConfig, run_pass

    base = GcConfig.from_settings()
    cfg = GcConfig(
        idle_hours=int(args.idle_hours),
        batch=max(1, int(args.batch)),
        sagas_per_pass=base.sagas_per_pass if not explicit else 1_000,
        external=not bool(getattr(args, "skip_external", False)) if args.erase else base.external,
        purge=not args.no_purge,
        org=args.org,
        namespaces=explicit,
        dry_run=not args.erase,
        requester=args.requester,
    )
    res = await run_pass(cfg, ch=ch)
    print(
        f"{res.candidates} registered bench namespace(s) in org {args.org!r} "
        f"idle>{args.idle_hours}h holding {res.candidate_rows} live rows"
    )
    for name in res.names[: int(args.show)]:
        print(f"  {name}")
    if len(res.names) > int(args.show):
        print(f"  … {len(res.names) - int(args.show)} more")
    if cfg.dry_run:
        if res.candidates:
            print("dry run — pass --erase to submit the erasure")
        return 0
    tail = f" stopped_on={res.stopped_on}" if res.stopped_on else ""
    print(f"sagas={res.sagas} erased={res.erased}{tail}")
    return 1 if res.stopped_on else 0


# loom namespaces-adopt — give the unregistered backlog an owner row
# ----------------------------------------------------------------------


def _cmd_namespaces_adopt(args: argparse.Namespace) -> int:
    """Insert registry rows for namespaces that hold live memory and have
    none. Dry run by default: lists every unregistered live
    namespace of --org with the kind it would get — `bench` under the bench
    prefixes (or under --prefix, or everything with --kind bench), else
    `memory` — and the owner (--owner). --apply writes the rows through the
    same builder `namespace.register` uses, retention_days 0, no ALTER.
    The namespace_gc worker then retires the bench ones as they are idle;
    the memory ones stay, owned."""

    async def _run(ch: Any) -> int:
        return await _adopt_run(ch, args)

    return run_connected(_run)


async def _adopt_run(ch: Any, args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    import server.operations  # noqa: F401 — wires handlers before importing one
    from server.identity.api.models import NamespaceRegisterInput
    from server.identity.app.namespace import registry_row
    from server.identity.app.namespace_autoregister import (
        AUTO_REGISTER_TAG,
        AUTO_ROW_VERSION,
        classify_kind,
    )
    from server.identity.store import namespace_census

    rows = await namespace_census.unregistered_live(org=args.org, ch=ch)
    forced = tuple(args.prefix or ())
    if forced:
        rows = [r for r in rows if any(r.namespace.startswith(p) for p in forced)]
    if args.namespace:
        wanted = set(args.namespace)
        rows = [r for r in rows if r.namespace in wanted]

    def _kind(name: str) -> str:
        if args.kind:
            return str(args.kind)
        if forced:
            return "bench"
        return classify_kind(name)

    plan = [(r.namespace, _kind(r.namespace), r.live_rows) for r in rows]
    n_bench = sum(1 for _, k, _ in plan if k == "bench")
    print(
        f"{len(plan)} unregistered live namespace(s) in org {args.org!r}: "
        f"{n_bench} bench, {len(plan) - n_bench} memory; owner={args.owner!r}"
    )
    for name, kind, live in plan[: int(args.show)]:
        print(f"  {name:<48} kind={kind:<6} live_rows={live}")
    if len(plan) > int(args.show):
        print(f"  … {len(plan) - int(args.show)} more")
    if not plan or args.dry_run:
        if plan and args.dry_run:
            print("dry run — pass --apply to write the registry rows")
        return 0
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    batch = [
        registry_row(
            NamespaceRegisterInput(
                org=args.org,
                namespace=name,
                owner_email=args.owner,
                description=f"Adopted by namespaces-adopt ({args.owner}).",
                kind=kind,  # type: ignore[arg-type]
                tags=[AUTO_REGISTER_TAG, "adopted"] + (["bench"] if kind == "bench" else []),
                retention_days=0,
            ),
            None,
            now,
        )
        for name, kind, _ in plan
    ]
    for row in batch:
        row["updated_at"] = AUTO_ROW_VERSION  # an explicit row always wins
    for i in range(0, len(batch), 500):
        await ch.insert("loom.namespaces", batch[i : i + 500])
    print(f"adopted {len(batch)} namespace(s); retention_days=0 on every row, no TTL altered")
    return 0


async def _gc_run(
    ch: Any, args: argparse.Namespace, *, prefixes: tuple[str, ...], explicit: tuple[str, ...]
) -> int:
    import json

    cands = await _gc_candidates(
        ch,
        org=args.org,
        prefixes=prefixes,
        idle_hours=int(args.idle_hours),
        include_registered=bool(args.include_registered),
        explicit=explicit,
        exclude_prefixes=tuple(args.exclude_prefix or ()),
    )
    # An explicit name is erased even when loom.memory has no rows for it: a
    # namespace whose memory rows are gone can still hold rows in the side
    # tables (embedding rows a worker re-emitted after the purge, say). Only
    # that case is re-added;
    # a name the scan dropped for being registered, excluded or not idle stays
    # dropped.
    known = {str(c["namespace"]) for c in cands}
    for name in await _gc_memoryless_explicit(
        ch,
        org=args.org,
        names=tuple(n for n in explicit if n not in known),
        include_registered=bool(args.include_registered),
        exclude_prefixes=tuple(args.exclude_prefix or ()),
    ):
        cands.append({"namespace": name, "rows": 0, "recalled_rows": 0, "last_write": None})
        known.add(name)
    total_rows = sum(int(c["rows"]) for c in cands)
    print(
        f"{len(cands)} candidate namespace(s) in org {args.org!r}, {total_rows} live rows "
        f"(prefixes={list(prefixes)}, idle>{args.idle_hours}h, "
        f"registered={'included' if args.include_registered else 'excluded'})"
    )
    for c in cands[: int(args.show)]:
        print(
            f"  {str(c['namespace']):<40} rows={int(c['rows']):>7} "
            f"recalled_rows={int(c['recalled_rows']):>5} last_write={c['last_write']}"
        )
    if len(cands) > int(args.show):
        print(f"  … {len(cands) - int(args.show)} more")
    if not cands or not args.erase:
        if cands and not args.erase:
            print("dry run — pass --erase to submit the erasure")
        return 0
    before = await _parts_footprint(ch)

    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.app.handlers import handle_erasure_request

    names = [str(c["namespace"]) for c in cands]
    batch = max(1, int(args.batch))
    erased = 0
    for i in range(0, len(names), batch):
        chunk = names[i : i + batch]
        out = await handle_erasure_request(
            ErasureRequestInput(
                org=args.org,
                scope_type="namespace_list",
                scope_value=json.dumps(
                    {
                        "namespaces": chunk,
                        "purge": not args.no_purge,
                        "external": not bool(getattr(args, "skip_external", False)),
                    }
                ),
                purge=not args.no_purge,
                external=not bool(getattr(args, "skip_external", False)),
                requester=args.requester,
            )
        )
        touched = {s.step: s.rows_affected for s in out.steps if s.rows_affected}
        print(
            f"erasure {out.erasure_id} {out.status}: {len(chunk)} namespace(s), "
            f"residue={out.residue_count}, rows per step={json.dumps(touched)}"
        )
        if out.status != "completed":
            print("stopping: the saga did not complete; see erasure.status", file=sys.stderr)
            return 1
        erased += len(chunk)

    after = await _parts_footprint(ch)
    for t in sorted(set(before) | set(after)):
        bp, br = before.get(t, (0, 0))
        ap, ar = after.get(t, (0, 0))
        print(f"  {t:<18} parts {bp:>5} → {ap:<5} rows {br:>9} → {ar}")
    print(f"erased {erased} namespace(s); the erasure ledger holds each saga")
    return 0


# ---------------------------------------------------------------------------
# loom erasure-re-verify
# ---------------------------------------------------------------------------


def _cmd_erasure_re_verify(args: argparse.Namespace) -> int:
    """Re-verify the sagas the ledger holds as `failed`, or with `--completed-since`
    instead the `completed` ones verified since then (after a rollout, rule 14 of
    docs/architecture/rolling-upgrades.md).

    Read-only by default: for every such saga, print the last step-named
    reason and a fresh residue census over its scope. `--resume` re-runs each
    from its checkpoint (the recovery worker stops at `max_attempts`; an
    operator does not) — `--skip-external` for a `namespace_list` saga whose
    external step is what keeps failing. `--orphan-graph` lists namespaces
    holding entity edges with no live memory and no live knowledge chunk;
    with `--erase` each batch leaves in one loom-only purge saga. The
    closing line reads the two integrity gauges the fix must bring to 0.
    """
    # Only a run that submits a saga needs the bucket client: a read-only one still
    # prints the table counts when a store cannot be read (`evidence_census`).
    if (args.resume or args.erase) and not _bucket_client_installed():
        return 2

    async def _run(ch: Any) -> int:
        # One connection for the whole command (see `_cmd_namespaces_gc`).
        return await _re_verify_run(ch, args)

    return run_connected(_run)


def _utc_instant(text: str) -> datetime:
    """An ISO timestamp as naive UTC, the ledger's own form; one without an offset is UTC."""
    try:
        at = datetime.fromisoformat(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"not an ISO timestamp: {text!r}") from e
    if at.tzinfo is not None:
        at = at.astimezone(UTC).replace(tzinfo=None)
    return at


def _re_verify_scope(row: Mapping[str, Any]) -> tuple[str, str | list[str], bool, bool]:
    """(census scope_type, census scope_value, purge, external) of a ledger row."""
    from server.erasure.domain.scopes import (
        applied_purge,
        parse_external_flag,
        parse_namespace_list,
    )

    scope_type = str(row["scope_type"])
    scope_value = str(row["scope_value"])
    purge = applied_purge(scope_type, scope_value, bool(row.get("purge")))
    if scope_type == "namespace_list":
        return (
            "namespace",
            parse_namespace_list(scope_value),
            purge,
            parse_external_flag(scope_value),
        )
    return ("user_id" if scope_type == "user_id" else "namespace", scope_value, purge, True)


def _re_verify_input(row: Mapping[str, Any], *, skip_external: bool) -> Any:
    """The request the saga resumes with. `--skip-external` rewrites the
    `namespace_list` scope object (the ledger's copy wins over the field, so
    the flag must change there) and is refused for any other scope."""
    from server.erasure.api.erasure import ErasureRequestInput
    from server.erasure.domain.scopes import parse_namespace_list

    scope_type = str(row["scope_type"])
    scope_value = str(row["scope_value"])
    _st, _sv, purge, external = _re_verify_scope(row)
    if skip_external:
        if scope_type != "namespace_list":
            raise ValueError("--skip-external applies to namespace_list sagas only")
        external = False
        scope_value = json.dumps(
            {"namespaces": parse_namespace_list(scope_value), "purge": purge, "external": False}
        )
    # Ledger strings, validated against the model's literals at runtime.
    return ErasureRequestInput.model_validate(
        {
            "org": str(row["org"]),
            "scope_type": scope_type,
            "scope_value": scope_value,
            "requester": str(row["requester"]),
            "urgency": str(row.get("urgency") or "normal"),
            "purge": purge,
            "external": external,
        }
    )


async def _re_verify_completed(ch: Any, row: Mapping[str, Any], *, resume: bool) -> int:
    """A completed saga gets no second run: everything in its scope now may have
    been written after it finished. Only the row links an older build's erasure
    did not know, and whose memory has no live row and no row written since the
    census, are counted (and with `resume`, deleted). 1 while any are left, else 0."""
    from server.erasure.store import steps_graph

    org = str(row["org"])
    scope_type, scope_value, _purge, _external = _re_verify_scope(row)
    # Memories written since the census are not the saga's, even soft-deleted.
    cut = row["verified_at"]
    print(f"  {row['id']} {org}/{row['scope_type']} completed")
    orphans = await steps_graph.count_orphan_row_links(
        org, scope_type, scope_value, verified_at=cut, ch=ch
    )
    found = {t.split(".", 1)[-1]: n for t, n in orphans.items() if n}
    print(f"    orphan row links: {sum(found.values())} {json.dumps(found, sort_keys=True)}")
    if not found:
        return 0
    if not resume:
        return 1
    deleted = await steps_graph.delete_orphan_row_links(
        org, scope_type, scope_value, verified_at=cut, ch=ch
    )
    left = await steps_graph.count_orphan_row_links(
        org, scope_type, scope_value, verified_at=cut, ch=ch
    )
    print(f"    deleted {deleted}; left {sum(left.values())}")
    return 1 if any(left.values()) else 0


async def _re_verify_run(ch: Any, args: argparse.Namespace) -> int:
    from server.erasure.store import census as erasure_census
    from server.erasure.store import health as erasure_health
    from server.erasure.store import ledger as erasure_ledger
    from server.erasure.store import mutations as erasure_mutations

    rc = 0
    # --completed-since takes the completed sagas instead of the failed ones.
    completed = bool(args.completed_since)
    if completed and args.skip_external:
        # A completed saga is not resumed, so the flag would be ignored.
        print(
            "--skip-external applies to failed sagas only, not --completed-since", file=sys.stderr
        )
        return 2
    if completed:
        rows = await erasure_ledger.list_completed_requests(
            since=args.completed_since, org=args.org or None, ch=ch
        )
        label = f"saga(s) completed since {args.completed_since}"
    else:
        rows = await erasure_ledger.list_failed_requests(org=args.org or None, ch=ch)
        label = "failed saga(s)"
    wanted = set(args.id or ())
    if wanted:
        rows = [r for r in rows if str(r["id"]) in wanted]
    if not args.orphan_graph or rows or wanted:
        print(f"{len(rows)} {label}" + (f" in org {args.org!r}" if args.org else ""))
    for row in rows:
        if completed:
            rc = max(rc, await _re_verify_completed(ch, row, resume=bool(args.resume)))
            continue
        if row.get("failure_reason") == REPLAY_PENDING_REASON:
            # A `loom reconcile` replay that did not finish: a census of its scope counts
            # the user's writes made after the erasure, and a resume would delete them.
            print(f"  {row['id']} {row['org']}/{row['scope_type']}: {REPLAY_PENDING_REASON}")
            rc = 1
            continue
        scope_type, scope_value, purge, _external = _re_verify_scope(row)
        names = scope_value if isinstance(scope_value, list) else [scope_value]
        with erasure_mutations.purge_scope(purge):
            census, unread = await erasure_census.evidence_census(
                str(row["org"]), scope_type, scope_value, ch=ch
            )
        residue = {t.removeprefix("loom."): n for t, n in census.items() if n}
        done = list(row.get("steps_completed") or [])
        print(
            f"  {row['id']} {row['org']}/{row['scope_type']} attempts={row['attempts']} "
            f"steps_done={len(done)} scope={len(names)} name(s) purge={purge}"
        )
        print(f"    last: {str(row.get('failure_reason') or '')[:160]}")
        print(f"    residue now: {sum(residue.values())} {json.dumps(residue, sort_keys=True)}")
        for store, error in unread.items():
            print(f"    {store}: could not be read ({error_summary(error)})")
        if args.resume:
            from server.erasure.app.saga import request_row_template, run_saga

            try:
                input_obj = _re_verify_input(row, skip_external=bool(args.skip_external))
            except ValueError as e:
                print(f"    not resumed: {e}", file=sys.stderr)
                rc = 1
                continue
            # The fresh census above (and the ledger's) decide
            # which checkpointed steps run again — the owners of every table
            # with rows. Without this a purge saga whose tombstone steps ran
            # before the purge flag reached them would skip them on every resume.
            reopen = erasure_census.steps_to_reopen(census, scope_type)
            reopen |= erasure_census.steps_to_reopen(row.get("residue"), scope_type)
            reopen &= set(done)
            if reopen:
                print(f"    re-opening {sorted(reopen)} (checkpointed, but their tables hold rows)")
            out = await run_saga(
                input=input_obj,
                row=request_row_template(
                    erasure_id=str(row["id"]),
                    input=input_obj,
                    actor=row["actor"],
                    requested_at=row["requested_at"],
                    attempts=int(row["attempts"]) + 1,
                ),
                erasure_id=str(row["id"]),
                requested_at=row["requested_at"],
                previously_completed=set(done) - reopen,
            )
            touched = {s.step: s.rows_affected for s in out.steps if s.rows_affected}
            skipped = sum(1 for s in out.steps if "skipped" in (s.notes or ""))
            print(
                f"    resumed → {out.status}: residue={out.residue_count} "
                f"{json.dumps(out.residue, sort_keys=True)} rows per step={json.dumps(touched)} "
                f"skipped={skipped} ran={len(out.steps) - skipped}"
                + (f" reason={out.failure_reason[:160]}" if out.failure_reason else "")
            )
            if out.status != "completed":
                rc = 1
        elif residue or unread:
            rc = 1

    if args.orphan_graph:
        if not args.org:
            print("--orphan-graph needs --org", file=sys.stderr)
            return 2
        orphans = await erasure_health.orphan_graph_namespaces(org=args.org, ch=ch)
        edges = sum(int(o["edges"]) for o in orphans)
        print(
            f"{len(orphans)} namespace(s) in org {args.org!r} hold {edges} entity edges "
            "with no live memory and no live knowledge chunk"
        )
        for o in orphans[: int(args.show)]:
            print(f"  {str(o['namespace']):<40} edges={int(o['edges']):>7}")
        if len(orphans) > int(args.show):
            print(f"  … {len(orphans) - int(args.show)} more")
        if orphans and args.erase:
            from server.erasure.api.erasure import ErasureRequestInput
            from server.erasure.app.handlers import handle_erasure_request

            names = [str(o["namespace"]) for o in orphans]
            batch = max(1, int(args.batch))
            for i in range(0, len(names), batch):
                chunk = names[i : i + batch]
                out = await handle_erasure_request(
                    ErasureRequestInput(
                        org=args.org,
                        scope_type="namespace_list",
                        scope_value=json.dumps(
                            {"namespaces": chunk, "purge": True, "external": False}
                        ),
                        purge=True,
                        external=False,
                        requester=args.requester,
                    )
                )
                touched = {s.step: s.rows_affected for s in out.steps if s.rows_affected}
                print(
                    f"erasure {out.erasure_id} {out.status}: {len(chunk)} namespace(s), "
                    f"residue={out.residue_count}, rows per step={json.dumps(touched)}"
                )
                if out.status != "completed":
                    print(
                        "stopping: the saga did not complete; see erasure.status",
                        file=sys.stderr,
                    )
                    return 1
        elif orphans and not args.erase:
            print("dry run — pass --erase to purge them in loom-only namespace_list sagas")
            rc = 1

    gauges = await erasure_health.integrity_counts(ch=ch)
    print(
        f"gauges: orphan_entity_vectors={gauges['orphan_entity_vectors']} "
        f"orphan_graph_namespaces={gauges['orphan_graph_namespaces']}"
    )
    return rc


def _add_namespace_hygiene_parsers(sub: Any) -> None:
    """The ledger and cleanup subcommands: `drops`, `namespaces-gc` and
    `erasure-re-verify`."""
    p_drops = sub.add_parser(
        "drops",
        help="Print the memory ingest drop ledger (loom.memory_drops) for a "
        "namespace, by stage/reason — what the pipeline discarded, truncated "
        "or gave up on.",
    )
    p_drops.add_argument("--namespace", required=True, help="Namespace to summarise")
    p_drops.add_argument("--org", default="", help="Limit to one org")
    p_drops.add_argument("--hours", type=int, default=24, help="Look-back window (default 24)")
    p_drops.set_defaults(func=_cmd_drops)

    p_nsgc = sub.add_parser(
        "namespaces-gc",
        help="List (default) or erase throwaway namespaces — finished bench runs "
        "under the bench prefixes that nobody registered and nobody "
        "has written to for --idle-hours — in one list-scoped erasure saga per "
        "batch.",
    )
    p_nsgc.add_argument("--org", required=True, help="Org whose namespaces to scan")
    p_nsgc.add_argument(
        "--prefix",
        action="append",
        help="Namespace prefix (repeatable); default: LOOM_NAMESPACE_BENCH_PREFIXES",
    )
    p_nsgc.add_argument(
        "--namespace",
        action="append",
        help="An explicit namespace (repeatable); counted whether its rows are live or "
        "tombstoned, so a half-erased namespace can be finished",
    )
    p_nsgc.add_argument(
        "--exclude-prefix",
        action="append",
        help="Never touch namespaces under this prefix (repeatable): a reuse set another run reads",
    )
    p_nsgc.add_argument(
        "--idle-hours", type=int, default=24, help="No write for this long (default 24)"
    )
    p_nsgc.add_argument(
        "--include-registered",
        action="store_true",
        help="Also erase namespaces that have a loom.namespaces row",
    )
    p_nsgc.add_argument(
        "--erase", action="store_true", help="Submit the erasure (default: dry run)"
    )
    p_nsgc.add_argument(
        "--no-purge",
        action="store_true",
        help="Tombstone instead of deleting the rows (keeps the bytes)",
    )
    p_nsgc.add_argument(
        "--skip-external",
        action="store_true",
        help="Loom tables only: skip the external backends (Langfuse traces, "
        "ClickStack OTel rows), which erase per namespace and fail closed",
    )
    p_nsgc.add_argument("--batch", type=int, default=200, help="Namespaces per saga (default 200)")
    p_nsgc.add_argument("--show", type=int, default=20, help="Candidates to print (default 20)")
    p_nsgc.add_argument("--requester", default="cli:namespaces-gc")
    p_nsgc.add_argument(
        "--worker-pass",
        action="store_true",
        help="Run one pass of the namespace_gc worker instead of the prefix scan: "
        "registered kind=bench namespaces with live memory and no "
        "write for --idle-hours; --namespace narrows it; dry run unless --erase",
    )
    p_nsgc.set_defaults(func=_cmd_namespaces_gc)

    p_erv = sub.add_parser(
        "erasure-re-verify",
        help="Re-verify failed erasure sagas: print each one's last "
        "step-named reason and a fresh residue census (default, read-only); "
        "--resume re-runs them from their checkpoint past the recovery worker's "
        "attempts budget; --orphan-graph lists namespaces whose entity graph "
        "outlived their memories and, with --erase, purges them.",
    )
    p_erv.add_argument("--org", default="", help="Limit to one org (required for --orphan-graph)")
    p_erv.add_argument("--id", action="append", help="Only this erasure id (repeatable)")
    p_erv.add_argument(
        "--resume",
        action="store_true",
        help="Re-run each failed saga from its steps_completed checkpoint",
    )
    p_erv.add_argument(
        "--skip-external",
        action="store_true",
        help="With --resume, on namespace_list sagas: loom tables only, skip the external "
        "backends whose step kept failing (the ledger's scope object is rewritten)",
    )
    p_erv.add_argument(
        "--completed-since",
        type=_utc_instant,
        metavar="ISO_TIMESTAMP",
        help="Re-verify the completed sagas whose census ran at or after this time, not "
        "the failed ones (UTC unless it names an offset): after a rollout, the erasures "
        "the previous build ran. "
        "Only their orphan row links are counted (entity_triple_rows and entity_edge_rows "
        "rows whose memory has no live row and no row written since the census); --resume "
        "deletes them",
    )
    p_erv.add_argument(
        "--orphan-graph",
        action="store_true",
        help="List namespaces holding entity edges with no live memory and no live knowledge chunk",
    )
    p_erv.add_argument(
        "--erase",
        action="store_true",
        help="With --orphan-graph: purge them in loom-only namespace_list sagas",
    )
    p_erv.add_argument("--batch", type=int, default=200, help="Namespaces per saga (default 200)")
    p_erv.add_argument("--show", type=int, default=20, help="Orphan namespaces to print")
    p_erv.add_argument("--requester", default="cli:erasure-re-verify")
    p_erv.set_defaults(func=_cmd_erasure_re_verify)


def _add_namespaces_adopt_parser(sub: Any) -> None:
    """The `namespaces-adopt` subcommand."""
    p_adopt = sub.add_parser(
        "namespaces-adopt",
        help="Give namespaces that hold live memory and no registry row an owner row: "
        "kind=bench under the bench prefixes, else memory; "
        "retention_days 0, no TTL. Dry run unless --apply.",
    )
    p_adopt.add_argument("--org", required=True, help="Org whose namespaces to adopt")
    p_adopt.add_argument("--owner", required=True, help="owner_email to write on every adopted row")
    p_adopt.add_argument(
        "--prefix",
        action="append",
        help="Only namespaces under this prefix (repeatable); they are adopted as bench",
    )
    p_adopt.add_argument("--namespace", action="append", help="An explicit namespace (repeatable)")
    p_adopt.add_argument(
        "--kind", choices=("bench", "memory"), help="Force the kind on every adopted row"
    )
    add_apply_flags(p_adopt, writes="write the rows")
    p_adopt.add_argument("--show", type=int, default=30, help="Namespaces to print (default 30)")
    p_adopt.set_defaults(func=_cmd_namespaces_adopt)
