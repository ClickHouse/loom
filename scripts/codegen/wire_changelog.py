#!/usr/bin/env python3
"""What changed for clients between two revisions of loom.

The per-release "what changed for clients" section is generated from the
registry, not written by hand: this script builds the capability snapshot (the same shape
`ops.capabilities` serves — every operation, its request and response fields,
role, budget, MCP tier) at two git revisions and prints the diff.

    wire_changelog.py <rev_a> <rev_b>              # markdown
    wire_changelog.py --since-hours 24 [rev_b]     # rev_a = rev_b's ancestor N hours back
    wire_changelog.py <rev_a> <rev_b> --json
    wire_changelog.py --snapshot <rev> > snap.json # one revision's catalogue
    wire_changelog.py --diff a.json b.json         # two saved snapshots

(run as `python scripts/codegen/wire_changelog.py`).

A revision is materialised with `git archive` into a temporary directory and
its registry is imported by a child interpreter (`PYTHONPATH=<checkout>`); the
inline probe needs only `server.operations.registry.ALL_OPERATIONS` and
`server.operations.toolsets` (`server.transport.mcp_config` at an older revision).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]

# Runs inside the child interpreter against the archived checkout. Kept free
# of anything newer than the registry itself.
_PROBE = r"""
import json, sys
from server.operations.registry import ALL_OPERATIONS
try:
    from server.operations import toolsets as _mc
except ImportError:  # a revision from before the rosters left the transport
    from server.transport import mcp_config as _mc
hidden = set(_mc._MCP_HIDDEN_OPS)
core = set(_mc._MCP_CORE_OPS)
code = set(_mc._MCP_CODE_CORE_OPS)
def tier(n):
    if n in hidden: return "hidden"
    if n in core: return "core"
    if n in code: return "code"
    return "extended"
ops = {}
for op in ALL_OPERATIONS:
    ops[op.name] = {
        "path": "/v1/" + op.name,
        "mcp_tool": "loom_" + op.name.replace(".", "_"),
        "mcp_tier": tier(op.name),
        "required_role": op.required_role.name,
        "budget_p99_ms": int(op.budget_p99_ms),
        "idempotent": bool(op.idempotent),
        "audit": bool(getattr(op, "audit", False)),
        "request_fields": sorted(op.input_model.model_fields),
        "response_fields": sorted(op.output_model.model_fields),
    }
json.dump({"ops": dict(sorted(ops.items()))}, sys.stdout)
"""


def _git(*args: str, repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def resolve_since(hours: float, rev: str, *, repo: Path) -> str:
    """The last commit on `rev`'s first-parent line committed at least
    `hours` ago — the client-facing state a deployment from then served."""
    ts = int(_git("show", "-s", "--format=%ct", rev, repo=repo)) - int(hours * 3600)
    out = _git("rev-list", "-1", "--first-parent", f"--before={ts}", rev, repo=repo)
    if not out:
        raise SystemExit(f"no commit on {rev} older than {hours} h")
    return out


def snapshot_at(rev: str, *, repo: Path, python: str | None = None) -> dict[str, Any]:
    """The capability snapshot the registry at `rev` would serve."""
    sha = _git("rev-parse", "--short=8", rev, repo=repo)
    archive = subprocess.run(
        ["git", "-C", str(repo), "archive", "--format=tar", rev, "server", "sdk"],
        capture_output=True,
        check=True,
    ).stdout
    with tempfile.TemporaryDirectory(prefix=f"wire-{sha}-") as tmp:
        with tarfile.open(fileobj=BytesIO(archive)) as tar:
            tar.extractall(tmp, filter="data")
        env = {k: v for k, v in os.environ.items() if not k.startswith("LOOM_")}
        env["PYTHONPATH"] = tmp
        env["LOOM_AUTH_REQUIRED"] = "false"
        proc = subprocess.run(
            [python or sys.executable, "-c", _PROBE],
            cwd=tmp,
            env=env,
            capture_output=True,
            text=True,
        )
    if proc.returncode != 0:
        raise SystemExit(f"registry probe failed at {rev} ({sha}):\n{proc.stderr[-4000:]}")
    snap = json.loads(proc.stdout)
    snap["build_commit"] = sha
    snap["revision"] = rev
    return snap


def diff_snapshots(a: dict[str, Any], b: dict[str, Any]) -> dict[str, Any]:
    """Per-op changes a client can see: ops added/removed; request and
    response fields added/removed; role, budget, tier, idempotency changes."""
    ops_a, ops_b = a.get("ops", {}), b.get("ops", {})
    out: dict[str, Any] = {
        "from": a.get("build_commit", ""),
        "to": b.get("build_commit", ""),
        "ops_added": sorted(set(ops_b) - set(ops_a)),
        "ops_removed": sorted(set(ops_a) - set(ops_b)),
        "changed": {},
    }
    for name in sorted(set(ops_a) & set(ops_b)):
        oa, ob = ops_a[name], ops_b[name]
        ch: dict[str, Any] = {}
        for side in ("request_fields", "response_fields"):
            fa, fb = set(oa.get(side, [])), set(ob.get(side, []))
            if fb - fa:
                ch[f"{side}_added"] = sorted(fb - fa)
            if fa - fb:
                ch[f"{side}_removed"] = sorted(fa - fb)
        for attr in ("required_role", "budget_p99_ms", "mcp_tier", "idempotent", "audit"):
            va, vb = oa.get(attr), ob.get(attr)
            if va != vb and va is not None and vb is not None:
                ch[attr] = {"from": va, "to": vb}
        if ch:
            out["changed"][name] = ch

    def _total(key: str) -> int:
        return sum(len(c.get(key, [])) for c in out["changed"].values())

    out["counts"] = {
        "ops_added": len(out["ops_added"]),
        "ops_removed": len(out["ops_removed"]),
        "request_fields_added": _total("request_fields_added"),
        "request_fields_removed": _total("request_fields_removed"),
        "response_fields_added": _total("response_fields_added"),
        "response_fields_removed": _total("response_fields_removed"),
    }
    return out


def _names(fields: list[str]) -> str:
    return ", ".join(f"`{f}`" for f in fields)


def render_markdown(d: dict[str, Any], a: dict[str, Any], b: dict[str, Any]) -> str:
    lines = [f"## What changed for clients: {d['from']} → {d['to']}", ""]
    c = d["counts"]
    lines.append(
        f"{len(b.get('ops', {}))} operations served (was {len(a.get('ops', {}))}); "
        f"ops +{c['ops_added']} −{c['ops_removed']}; "
        f"request fields +{c['request_fields_added']} −{c['request_fields_removed']}; "
        f"response fields +{c['response_fields_added']} −{c['response_fields_removed']}."
    )
    lines.append("")
    if d["ops_added"]:
        lines.append("### New operations")
        for name in d["ops_added"]:
            spec = b["ops"][name]
            fields = ", ".join(spec.get("request_fields", [])) or "—"
            lines.append(
                f"- `{name}` — role {spec.get('required_role')}, "
                f"budget {spec.get('budget_p99_ms')} ms, MCP {spec.get('mcp_tier')}; "
                f"request fields: {fields}"
            )
        lines.append("")
    if d["ops_removed"]:
        lines.append("### Removed operations")
        lines.extend(f"- `{name}`" for name in d["ops_removed"])
        lines.append("")
    if d["changed"]:
        lines.append("### Changed operations")
        for name, ch in d["changed"].items():
            parts = []
            if ch.get("request_fields_added"):
                parts.append("request +" + _names(ch["request_fields_added"]))
            if ch.get("request_fields_removed"):
                parts.append("request −" + _names(ch["request_fields_removed"]))
            if ch.get("response_fields_added"):
                parts.append("response +" + _names(ch["response_fields_added"]))
            if ch.get("response_fields_removed"):
                parts.append("response −" + _names(ch["response_fields_removed"]))
            for attr in ("required_role", "budget_p99_ms", "mcp_tier", "idempotent", "audit"):
                if attr in ch:
                    parts.append(f"{attr} {ch[attr]['from']} → {ch[attr]['to']}")
            lines.append(f"- `{name}`: " + "; ".join(parts))
        lines.append("")
    if not (d["ops_added"] or d["ops_removed"] or d["changed"]):
        lines.append("No client-visible change.")
        lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("rev_a", nargs="?")
    ap.add_argument("rev_b", nargs="?")
    ap.add_argument("--repo", default=str(ROOT))
    ap.add_argument("--python", default=None, help="interpreter for the registry probe")
    ap.add_argument("--since-hours", type=float, default=None)
    ap.add_argument("--snapshot", metavar="REV", help="print one revision's snapshot as JSON")
    ap.add_argument(
        "--diff", nargs=2, metavar=("A_JSON", "B_JSON"), help="diff two saved snapshots"
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    repo = Path(args.repo)

    if args.snapshot:
        snap = snapshot_at(args.snapshot, repo=repo, python=args.python)
        json.dump(snap, sys.stdout, indent=1, sort_keys=True)
        print()
        return 0
    if args.diff:
        a = json.loads(Path(args.diff[0]).read_text(encoding="utf-8"))
        b = json.loads(Path(args.diff[1]).read_text(encoding="utf-8"))
    else:
        if args.since_hours is not None:
            rev_b = args.rev_b or args.rev_a or "HEAD"
            rev_a = resolve_since(args.since_hours, rev_b, repo=repo)
        else:
            rev_a, rev_b = args.rev_a, args.rev_b
            if not rev_a or not rev_b:
                ap.error("two revisions, --since-hours, --snapshot or --diff required")
        a = snapshot_at(rev_a, repo=repo, python=args.python)
        b = snapshot_at(rev_b, repo=repo, python=args.python)
    d = diff_snapshots(a, b)
    if args.json:
        json.dump(d, sys.stdout, indent=1, sort_keys=True)
        print()
    else:
        print(render_markdown(d, a, b))
    return 0


if __name__ == "__main__":
    sys.exit(main())
