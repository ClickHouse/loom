#!/usr/bin/env python3
"""Op reach census: one row per wire op — its reach set, its declared
maturity and, with ``--call-counts``, its call count from ``loom.agent_events``.

    python scripts/codegen/op_reach_census.py                      # table, calls "n/a"
    python scripts/codegen/op_reach_census.py --call-counts --days 30
    python scripts/codegen/op_reach_census.py --check               # CI gate, exit 1 on a violation
    python scripts/codegen/op_reach_census.py --write-manifest      # refresh reach_manifest.py
    python scripts/codegen/op_reach_census.py --owners --markdown   # + the introducing commit

``--call-counts`` is the only mode that opens a connection: the ClickHouse
the server CLI uses (``LOOM_ENV_FILE=/path/.env`` to name another dotenv).

The gate (also ``tests/unit/test_op_reach_census.py``): every op has a reach path other than a raw
HTTP POST or is declared experimental; every op is on the dashboard or named in
``server.operations.dashboard`` with the reason it is not; the committed
manifest is byte-identical to what ``--write-manifest`` writes. ``--owners``
names, for every experimental op, the commit that added its declaration: the
owner a deletion PR should cite.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:  # run as a file
    sys.path.insert(0, str(ROOT))

from scripts.codegen.common import GeneratedFile, ruff_format  # noqa: E402
from server.operations.base import MATURITIES, Operation  # noqa: E402
from server.operations.dashboard import DASHBOARD_PENDING, NOT_IN_DASHBOARD  # noqa: E402
from server.operations.reach import STATIC_KINDS, has_reach  # noqa: E402
from server.operations.registry import ALL_OPERATIONS  # noqa: E402

# Per-op call counts the census reads (agent_events is the one ledger that
# names the operation; audit_log carries event_type only). Plain WHERE, no
# PREWHERE.
CALLS_SQL = """
SELECT operation, count() AS calls, max(ts) AS last_ts, uniqExact(agent) AS agents
FROM loom.agent_events
WHERE ts >= now() - INTERVAL {days:UInt16} DAY
GROUP BY operation
"""


# ------------------------------------------------------------------ file scan

_SDK_DIR = "sdk/python/loom"
_EXAMPLE_DIRS = ("examples", "dev", "scripts")
_PLUGIN_DIRS = ("integrations",)
_PLUGIN_SUFFIXES = (".mjs", ".js", ".ts", ".tsx", ".sh", ".py", ".yaml", ".yml", ".json")
_DASHBOARD_DIR = "frontend/src"
_DASHBOARD_SUFFIXES = (".ts", ".tsx")

_V1 = re.compile(r"/v1/([a-z_]+(?:\.[a-z_]+){1,2})\b")
_QUOTED = re.compile(r"""["']([a-z_]+(?:\.[a-z_]+){1,2})["']""")
_SDK_CALL = re.compile(r"\.([a-z_]+)\.([a-z_]+)\(")
# A dashboard call site: the path as a string literal (`post("/v1/<op>", …)`), outside
# a comment. A bare op name there is an audit label, not a call.
_DASHBOARD_CALL = re.compile(r"""["'`]/v1/([a-z_]+(?:\.[a-z_]+){1,2})\b""")
# What a comment scan keeps (group 1: an escaped character, as in a regex literal's
# `\/`; a `'` or `"` string, ending at its line; a template literal, which may span
# lines) and what it drops (`//` and `/* */` comments). A lone `'` (JSX text's
# "can't") is code.
_TS_KEEP_OR_COMMENT = re.compile(
    r"""(\\.|"(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)|//[^\n]*|/\*.*?\*/""",
    re.DOTALL,
)


def _without_comments(code: str) -> str:
    """TypeScript `code` with its comments dropped and its strings kept."""
    return _TS_KEEP_OR_COMMENT.sub(lambda m: m.group(1) or "", code)


def _files(root: Path, rel: str, suffixes: tuple[str, ...]) -> list[Path]:
    base = root / rel
    if not base.exists():
        return []
    # Excluded directories are matched below `base` only: a checkout that
    # itself sits under a `tests` directory still scans every file. `test` is
    # the dashboard's helpers (`frontend/src/test`); a test kept beside its
    # source (the dashboard's `*.test.tsx`) is skipped by its name.
    return sorted(
        p
        for p in base.rglob("*")
        if p.is_file()
        and p.suffix in suffixes
        and ".test." not in p.name
        and not {"tests", "test", "__pycache__"} & set(p.relative_to(base).parts)
    )


def _sdk_index(root: Path) -> tuple[dict[str, dict[str, set[str]]], dict[str, str]]:
    """(class → method → ops posted) and (Agent attribute → class name).

    An attribute `Agent` assigns names its client over any other class's
    attribute of that name (`agent.context` is the ContextClient, not the
    generated `agent.client.context`)."""
    classes: dict[str, dict[str, set[str]]] = {}
    attrs: dict[str, str] = {}
    agent_attrs: dict[str, str] = {}
    for path in _files(root, _SDK_DIR, (".py",)):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for node in ast.walk(cls):
                if (
                    isinstance(node, ast.Assign)
                    and len(node.targets) == 1
                    and isinstance(node.targets[0], ast.Attribute)
                    and isinstance(node.targets[0].value, ast.Name)
                    and node.targets[0].value.id == "self"
                    and isinstance(node.value, ast.Call)
                    and isinstance(node.value.func, ast.Name)
                ):
                    target = agent_attrs if cls.name == "Agent" else attrs
                    target[node.targets[0].attr] = node.value.func.id
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                methods = classes.setdefault(node.name, {})
                for fn in node.body:
                    if isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
                        ops = {
                            m.group(1)
                            for c in ast.walk(fn)
                            if isinstance(c, ast.Constant) and isinstance(c.value, str)
                            for m in _V1.finditer(c.value)
                        }
                        if ops:
                            methods[fn.name] = ops
    return classes, {**attrs, **agent_attrs}


def derive_static_reach(root: Path | None = None) -> dict[str, tuple[str, ...]]:
    """Scan the tree for the file-derived reach kinds of every registered op.
    Returns ``{op: (kinds...)}`` for ops with at least one kind, kinds in
    ``STATIC_KINDS`` order — the shape ``reach_manifest.STATIC_REACH`` holds."""
    from server.operations.registry import ALL_OPERATIONS

    root = root or ROOT
    names = {op.name for op in ALL_OPERATIONS}
    found: dict[str, set[str]] = {}

    def hit(op: str, kind: str) -> None:
        if op in names:
            found.setdefault(op, set()).add(kind)

    # sdk_method: any /v1/<op> literal in the SDK package.
    for path in _files(root, _SDK_DIR, (".py",)):
        for m in _V1.finditer(path.read_text(encoding="utf-8")):
            hit(m.group(1), "sdk_method")

    # sdk_example: literals in examples/ and dev/, plus SDK calls resolved
    # through the Agent attribute → client class → method → op index.
    classes, attrs = _sdk_index(root)
    for rel in _EXAMPLE_DIRS:
        for path in _files(root, rel, (".py", ".sh")):
            text = path.read_text(encoding="utf-8")
            for m in _V1.finditer(text):
                hit(m.group(1), "sdk_example")
            for m in _QUOTED.finditer(text):
                hit(m.group(1), "sdk_example")
            for m in _SDK_CALL.finditer(text):
                attr, method = m.group(1), m.group(2)
                cls = attrs.get(attr)
                for op in classes.get(cls or "", {}).get(method, ()):
                    hit(op, "sdk_example")

    # plugin_hook: op names or /v1/<op> in the integrations (hooks, gateways,
    # shims).
    for rel in _PLUGIN_DIRS:
        for path in _files(root, rel, _PLUGIN_SUFFIXES):
            text = path.read_text(encoding="utf-8", errors="replace")
            for m in _V1.finditer(text):
                hit(m.group(1), "plugin_hook")
            for m in _QUOTED.finditer(text):
                hit(m.group(1), "plugin_hook")

    # dashboard: a quoted /v1/<op> call site in the dashboard source.
    for path in _files(root, _DASHBOARD_DIR, _DASHBOARD_SUFFIXES):
        code = _without_comments(path.read_text(encoding="utf-8", errors="replace"))
        for m in _DASHBOARD_CALL.finditer(code):
            hit(m.group(1), "dashboard")

    return {op: tuple(k for k in STATIC_KINDS if k in kinds) for op, kinds in sorted(found.items())}


def render_manifest(static: dict[str, tuple[str, ...]]) -> str:
    """The committed manifest module text for ``static``."""
    lines = [
        '"""GENERATED by scripts/codegen/op_reach_census.py --write-manifest — do not edit.',
        "",
        "File-derived reach kinds per op: which SDK methods, examples/dev",
        "scripts, integrations and dashboard call sites name it. The MCP kinds",
        "are read live from toolsets; tests/unit/test_op_reach_census.py regenerates",
        'this table from the tree and fails when it drifts."""',
        "",
        "from __future__ import annotations",
        "",
        "STATIC_REACH: dict[str, tuple[str, ...]] = {",
    ]
    for op, kinds in static.items():
        inner = ", ".join(f'"{k}"' for k in kinds)
        lines.append(f'    "{op}": ({inner},),')
    lines.append("}")
    return "\n".join(lines) + "\n"


_MANIFEST_PATH = "server/operations/reach_manifest.py"
MANIFEST = GeneratedFile(
    ROOT / _MANIFEST_PATH,
    "python scripts/codegen/op_reach_census.py --write-manifest",
)


def manifest_text() -> str:
    """The bytes `--write-manifest` writes: the tree scan, ruff-formatted as
    `make format` leaves the committed file."""
    return ruff_format(render_manifest(derive_static_reach(ROOT)), _MANIFEST_PATH)


# ------------------------------------------------------------------ gate


def violations(ops: list[Operation]) -> list[str]:
    """The census rules a PR must satisfy without a database."""
    out: list[str] = []
    for op in ops:
        if op.maturity not in MATURITIES:
            out.append(f"{op.name}: maturity {op.maturity!r} not in {MATURITIES}")
        if not has_reach(op.name) and op.maturity == "stable":
            out.append(f"{op.name}: no reach path (only a raw /v1 POST) and not experimental")
    out += dashboard_violations(ops)
    if not MANIFEST.is_current(manifest_text()):
        out.append(f"{MANIFEST.display} is stale — run {MANIFEST.command}")
    return out


def dashboard_violations(ops: list[Operation]) -> list[str]:
    """Every op is on the dashboard or says why not, and nothing says why not
    for an op the dashboard calls (`server.operations.dashboard`)."""
    out: list[str] = []
    names = {op.name for op in ops}
    for op in ops:
        on = "dashboard" in op.reach
        skipped = op.name in NOT_IN_DASHBOARD
        pending = op.name in DASHBOARD_PENDING
        if not on and not skipped and not pending:
            out.append(
                f"{op.name}: no dashboard call site; add its control, or name it in "
                "NOT_IN_DASHBOARD with the reason"
            )
        if on and (skipped or pending):
            out.append(
                f"{op.name}: the dashboard calls it, so drop it from server/operations/dashboard.py"
            )
        if skipped and pending:
            out.append(f"{op.name}: in both NOT_IN_DASHBOARD and DASHBOARD_PENDING")
    for name in sorted((set(NOT_IN_DASHBOARD) | DASHBOARD_PENDING) - names):
        out.append(f"{name}: in server/operations/dashboard.py but no such op")
    return out


# ------------------------------------------------------------------ calls


def call_counts(days: int) -> dict[str, dict[str, Any]]:
    """``{op: {calls, last_ts, agents}}`` over the last ``days``, read from the
    ClickHouse the server CLI would use (``server.settings``: LOOM_ENV_FILE,
    else ``.env``, and the LOOM_CH_* environment), as a background read."""
    from server.cli._ch import run_connected
    from server.core.ch import query_tag_var
    from server.observability.traffic_class import bind_background, reset_traffic_class
    from server.settings import settings

    print(f"# call counts from {settings.ch_host}/{settings.ch_database}", file=sys.stderr)
    token = bind_background("cli:op_reach_census")
    tag = query_tag_var.set("cli.op_reach_census")
    try:
        rows = run_connected(lambda ch: ch.query_all(CALLS_SQL, {"days": days}))
    finally:
        query_tag_var.reset(tag)
        reset_traffic_class(token)
    return {
        str(r["operation"]): {
            "calls": int(r["calls"]),
            "last_ts": str(r["last_ts"]),
            "agents": int(r["agents"]),
        }
        for r in rows
    }


# ------------------------------------------------------------------ owners


def owners(ops: list[Operation]) -> dict[str, str]:
    """The first commit that added ``name="<op>"`` to the registry, per op."""
    out: dict[str, str] = {}
    for op in ops:
        try:
            log = (
                subprocess.run(
                    [
                        "git",
                        "-C",
                        str(ROOT),
                        "log",
                        "--reverse",
                        "--format=%h %ad %s",
                        "--date=short",
                        "-S",
                        f'name="{op.name}"',
                        "--",
                        "server/operations",
                        # Domain packages keep their operations in api/.
                        ":(glob)server/*/api/**",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=60,
                )
                .stdout.strip()
                .splitlines()
            )
        except (OSError, subprocess.TimeoutExpired):
            log = []
        first = log[0] if log else ""
        m = re.search(r"\(#(\d+)\)\s*$", first)
        out[op.name] = (
            f"PR #{m.group(1)} ({first[:7]})" if m else (f"commit {first[:18]}" if first else "-")
        )
    return out


# ------------------------------------------------------------------ table


def rows(
    ops: list[Operation],
    calls: dict[str, dict[str, Any]] | None,
    owner: dict[str, str] | None,
) -> list[dict[str, Any]]:
    out = []
    for op in sorted(ops, key=lambda o: o.name):
        c = (calls or {}).get(op.name)
        row: dict[str, Any] = {
            "op": op.name,
            "reach": sorted(op.reach),
            "maturity": op.maturity,
            "calls_30d": c["calls"] if c else ("n/a" if calls is None else 0),
            "last_call": c["last_ts"][:10] if c else "",
        }
        if owner is not None:
            row["owner"] = owner.get(op.name, "")
        out.append(row)
    return out


def render_table(table: list[dict[str, Any]], markdown: bool) -> str:
    cols = list(table[0]) if table else ["op", "reach", "maturity", "calls_30d", "last_call"]
    cells = [
        [", ".join(r[c]) if isinstance(r[c], list) else str(r[c]) for c in cols] for r in table
    ]
    if markdown:
        lines = ["| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
        lines += ["| " + " | ".join(c) + " |" for c in cells]
        return "\n".join(lines)
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(cols)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    head = [fmt.format(*cols), fmt.format(*("-" * w for w in widths))]
    return "\n".join(head + [fmt.format(*c) for c in cells])


def summary(table: list[dict[str, Any]]) -> dict[str, Any]:
    from server.operations.toolsets import _MCP_HIDDEN_OPS

    have_calls = all(r["calls_30d"] != "n/a" for r in table)
    by_maturity: dict[str, int] = {}
    for r in table:
        by_maturity[r["maturity"]] = by_maturity.get(r["maturity"], 0) + 1
    return {
        "ops": len(table),
        "maturity": by_maturity,
        "http_only": sum(r["reach"] == ["http_only"] for r in table),
        "uncalled_30d": (sum(r["calls_30d"] == 0 for r in table) if have_calls else "n/a"),
        "experimental_called_30d": (
            sum(r["maturity"] == "experimental" and r["calls_30d"] not in (0, "n/a") for r in table)
            if have_calls
            else "n/a"
        ),
        "stable_uncalled_30d": (
            sum(r["maturity"] == "stable" and r["calls_30d"] == 0 for r in table)
            if have_calls
            else "n/a"
        ),
        "hidden": len(_MCP_HIDDEN_OPS),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--call-counts",
        action="store_true",
        help="read each op's calls from the server's ClickHouse (else calls are n/a)",
    )
    ap.add_argument("--days", type=int, default=30, help="the --call-counts window")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="exit 1 on a census violation")
    mode.add_argument("--write-manifest", action="store_true", help="regenerate reach_manifest.py")
    ap.add_argument("--owners", action="store_true", help="add the introducing commit")
    ap.add_argument("--markdown", action="store_true")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--only", choices=list(MATURITIES), default=None)
    args = ap.parse_args(argv)

    if args.write_manifest:
        return MANIFEST.sync(manifest_text(), check=False)

    bad = violations(ALL_OPERATIONS)
    if args.check:
        for b in bad:
            print(f"VIOLATION {b}")
        print("census: ok" if not bad else f"census: {len(bad)} violation(s)")
        return 1 if bad else 0

    calls = call_counts(args.days) if args.call_counts else None
    ops = [op for op in ALL_OPERATIONS if args.only is None or op.maturity == args.only]
    table = rows(ops, calls, owners(ops) if args.owners else None)
    if args.json:
        full = summary(rows(ALL_OPERATIONS, calls, None))
        print(json.dumps({"summary": full, "rows": table}, indent=1))
    else:
        print(render_table(table, args.markdown))
        print()
        print(json.dumps(summary(rows(ALL_OPERATIONS, calls, None))))
        for b in bad:
            print(f"VIOLATION {b}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
