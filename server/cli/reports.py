"""Read-only reports: `export-actions` (the action schema), `entity-export`,
`audit-namespace`, `deploy-delta` and `mentor`.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from typing import Any

from server.cli._ch import run_connected


def _cmd_export_actions(args: argparse.Namespace) -> int:
    """Emit the action schema derived from the operation registry: `--format json` is an MCP
    tools/list-shaped manifest with loom extras, the input a policy gateway reads;
    `--format cedar` is the equivalent Cedar `.cedarschema` (one action per operation
    in its effect group, context { input, output?, system })."""
    from server.transport import action_schema

    text = (
        action_schema.to_cedar_schema()
        if args.format == "cedar"
        else action_schema.to_manifest_json()
    )
    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.output} ({len(text)} bytes, format={args.format})")
    else:
        sys.stdout.write(text)
    return 0


def _cmd_entity_export(args: argparse.Namespace) -> int:
    """Export a namespace's entity graph to GraphML or JSON-LD."""
    from server.memory.app.entity_export import export_entity_graph

    text = run_connected(
        lambda ch: export_entity_graph(
            ch, org=args.org, namespace=args.namespace, fmt=args.format, live_only=not args.all
        ),
        org_free="reads",
    )
    if args.output and args.output != "-":
        with open(args.output, "w", encoding="utf-8") as fh:
            fh.write(text)
        print(f"wrote {args.output} ({len(text)} bytes, format={args.format})")
    else:
        sys.stdout.write(text)
    return 0


def _add_entity_export_parser(sub: Any) -> None:
    """The `entity-export` subcommand (kept out of `main` for
    the function-length gate)."""
    p = sub.add_parser(
        "entity-export",
        help="Export a namespace's entity graph (co-occurrence edges + triples) to GraphML "
        "or JSON-LD for an external graph or RDF tool. Read-only; every edge "
        "carries its provenance and a note that a path is declared, not proven.",
    )
    p.add_argument("--org", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--format", choices=("graphml", "jsonld"), default="graphml")
    p.add_argument("--output", default="-", help="Output file, or '-' for stdout (default)")
    p.add_argument(
        "--all", action="store_true", help="Include invalidated/expired edges (default: live only)"
    )
    p.set_defaults(func=_cmd_entity_export)


def _cmd_audit_namespace(args: argparse.Namespace) -> int:
    """How a namespace is being used, with the anti-patterns named."""
    import json

    from server.identity.api.models_namespace_catalog import NamespaceAuditInput
    from server.identity.app.namespace_audit import handle_namespace_audit, render_table

    inp = NamespaceAuditInput(
        org=args.org,
        namespace=args.namespace,
        window_seconds=int(args.window_days) * 86400,
        sample_limit=int(args.samples),
    )

    async def _go(ch: Any) -> int:
        out = await handle_namespace_audit(inp)
        if args.json:
            print(json.dumps(out.model_dump(mode="json"), indent=2, sort_keys=True))
        else:
            print(render_table(out))
        return 0 if out.healthy else 1

    return run_connected(_go, org_free="reads")


def _add_audit_namespace_parser(sub: Any) -> None:
    """The `audit-namespace` subcommand."""
    p_an = sub.add_parser(
        "audit-namespace",
        help="How a namespace is being used (namespace.audit): exact-key "
        "reads vs searches, hash keys, double-encoded and oversized "
        "values, hand-rolled index rows, leases stored as memories, version churn. "
        "Read-only; prints a table (code, severity, kind, count, share, first sample) or "
        "--json. Exit 1 on a critical finding.",
    )
    p_an.add_argument("--org", required=True)
    p_an.add_argument("--namespace", required=True)
    p_an.add_argument("--window-days", type=int, default=7, help="Ledger window (default 7)")
    p_an.add_argument("--samples", type=int, default=5, help="Sample keys per finding (default 5)")
    p_an.add_argument("--json", action="store_true", help="Dump the output model as JSON")
    p_an.set_defaults(func=_cmd_audit_namespace)


def _add_deploy_delta_parser(sub: Any) -> None:
    """The `deploy-delta` subcommand (kept out of `main` for
    the function-length gate)."""
    p_dd = sub.add_parser(
        "deploy-delta",
        help="What a colour swap changed: the deployment's ledger rows (volume, errors, "
        "p50/p95, upstream spend per operation) and its ClickHouse statement shapes "
        "(runs, bytes, seconds) for a window on each side of the swap, with flags for "
        "what moved past a coarse threshold.",
    )
    p_dd.add_argument(
        "--swap-at",
        default="var/deployed.json",
        help="ISO-8601 instant of the swap, `now`, or a deployed.json path whose "
        "`deployed_at` names it (default: var/deployed.json)",
    )
    p_dd.add_argument("--window-min", type=int, default=60, help="Minutes on each side")
    p_dd.add_argument(
        "--ch-os-user",
        default="loom",
        help="os_user of the deployment's ClickHouse sessions in system.query_log "
        "(the colour containers run as `loom`; isolated servers as the operator)",
    )
    p_dd.add_argument(
        "--cluster",
        default="default",
        help="Cluster for clusterAllReplicas over system.query_log; '' = this replica only",
    )
    p_dd.add_argument("--top", type=int, default=25, help="Statement shapes to keep")
    p_dd.add_argument("--json", default="", help="Also write the full reading as JSON here")
    p_dd.set_defaults(func=_cmd_deploy_delta)


def parse_swap_at(text: str) -> datetime:
    """`now`, an ISO-8601 instant (a trailing `Z` accepted), or the path of a
    `deployed.json` whose `deployed_at` names the swap."""
    if text == "now":
        return datetime.now(UTC)
    if text.endswith(".json"):
        with open(text, encoding="utf-8") as fh:
            text = str(json.load(fh)["deployed_at"])
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _cmd_deploy_delta(args: argparse.Namespace) -> int:
    """Read both records around the swap and print the delta;
    exit 1 when a flag fired so a post-swap script can notice."""

    from server.insights.domain.deploy_delta import flags, render
    from server.insights.store.deploy_delta import collect

    try:
        swap_at = parse_swap_at(args.swap_at)
    except (OSError, KeyError, ValueError) as e:
        print(f"  ✗ --swap-at {args.swap_at!r}: {e}", file=sys.stderr)
        return 2

    async def _go(ch: Any) -> Any:
        return await collect(
            ch,
            swap_at=swap_at,
            window_min=args.window_min,
            ch_os_user=args.ch_os_user,
            cluster=args.cluster,
            top=args.top,
        )

    delta = run_connected(_go, org_free="reads")
    print(render(delta), end="")
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            fh.write(delta.to_json())
    if delta.empty:
        return 2
    return 1 if flags(delta) else 0


def _cmd_mentor(args: argparse.Namespace) -> int:
    """The store's advice to one agent, with the evidence."""
    import json

    from server.insights.api.models_mentor import MentorBriefInput
    from server.insights.app.mentor_brief import handle_mentor_brief, render_table

    inp = MentorBriefInput(
        org=args.org,
        namespace=args.namespace,
        agent=args.agent or "",
        window_seconds=int(args.window_days) * 86400,
    )

    async def _go(ch: Any) -> int:
        out = await handle_mentor_brief(inp)
        if args.json:
            print(json.dumps(out.model_dump(mode="json"), indent=2, sort_keys=True, default=str))
        else:
            print(render_table(out))
        return 0 if out.healthy else 1

    return run_connected(_go, org_free="reads")


def _add_mentor_parser(sub: Any) -> None:
    """The `mentor` subcommand."""
    p_m = sub.add_parser(
        "mentor",
        help="The store's advice to one agent (mentor.brief): what it "
        "called, the planes its namespace offers, the hints with their evidence, and "
        "how often past hints were followed. Read-only; prints a table or --json. "
        "Exit 1 when a warning-level hint fires.",
    )
    p_m.add_argument("--org", required=True)
    p_m.add_argument("--namespace", required=True)
    p_m.add_argument("--agent", default="", help="Agent name (default: every hint for '-')")
    p_m.add_argument("--window-days", type=int, default=7, help="Ledger window (default 7)")
    p_m.add_argument("--json", action="store_true")
    p_m.set_defaults(func=_cmd_mentor)
