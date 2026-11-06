#!/usr/bin/env python3
"""Tenant onboarding — one command, zero to operational.

Composes:
  1. dev/loom_init.py        — service account JWT + .env + starter script
  2. /v1/admin/ratelimit.set — raise burst caps for the new namespace to
                              --rate-limit-multiplier (default 3) × the
                              server's per-class defaults
  3. (optional) /v1/admin/budget.set — install relaxed budgets on listed ops
  4. Smoke test               — exec the generated loom_starter.py

Arguments are validated before step 1, so a malformed --budget-override
changes nothing. Steps 2 and 3 need a platform_admin bearer (`--token` or
`$LOOM_ADMIN_TOKEN`) when auth is on; without one they are skipped with a
message and the tenant keeps the server's defaults. The token is never
printed: the next steps read it from the .env loom_init wrote.

Exit codes: 0 onboarded · 1 a step failed (the message names it) · 2 bad arguments.

Subprocesses `loom_init.py` so the two stay decoupled — loom_init is
still independently runnable and tested.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from textwrap import dedent
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOOM_INIT = ROOT / "dev" / "loom_init.py"

from server.core.ratelimit import DEFAULT_LIMITS  # noqa: E402

# The op classes an anchor tenant gets raised caps on; the base values are
# the server's own defaults, so the multiplier means what it says.
ANCHOR_CLASSES = ("memory", "knowledge.search", "knowledge.read", "knowledge.add")


def anchor_overrides(multiplier: float) -> dict[str, tuple[float, float]]:
    """(capacity, rate) per anchor class: `multiplier` × DEFAULT_LIMITS."""
    return {
        c: (DEFAULT_LIMITS[c].capacity * multiplier, DEFAULT_LIMITS[c].rate * multiplier)
        for c in ANCHOR_CLASSES
    }


def parse_budget_overrides(entries: list[str]) -> dict[str, int]:
    """`op=ms` entries → {op: ms}; raises ValueError on a malformed one."""
    parsed: dict[str, int] = {}
    for entry in entries:
        op_name, sep, ms = entry.partition("=")
        if not sep or not op_name.strip() or not ms.strip().isdigit():
            raise ValueError(f"--budget-override must be 'op=ms'; got {entry!r}")
        parsed[op_name.strip()] = int(ms.strip())
    return parsed


class AdminRefused(RuntimeError):
    """The server refused the admin bearer (401 / 403)."""


def _post_json(url: str, body: dict, timeout_s: float = 5.0, *, token: str = "") -> dict:
    """POST one admin call. `/v1/admin/*` sits behind a bearer + PLATFORM_ADMIN
    gate whenever auth is on (server/insights/app/admin_routes.py::require_admin),
    so the call carries `token` as a bearer; a refused one raises AdminRefused."""
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error = AdminRefused if e.code in (401, 403) else RuntimeError
        raise error(f"{url} returned HTTP {e.code}: {e.read()!r}") from e


def _apply_rate_limits(
    base_url: str, namespace: str, overrides: dict[str, tuple[float, float]], *, token: str = ""
) -> dict[str, dict]:
    result = {}
    for op_class, (capacity, rate) in overrides.items():
        result[op_class] = _post_json(
            f"{base_url.rstrip('/')}/v1/admin/ratelimit.set",
            {"namespace": namespace, "op_class": op_class, "capacity": capacity, "rate": rate},
            token=token,
        )
    return result


def _apply_budget_overrides(
    base_url: str, ops_and_budgets: dict[str, int], *, token: str = ""
) -> dict[str, dict]:
    """Optional per-op latency-budget overrides during a new handler's
    bake-in window. Empty by default."""
    result = {}
    for op_name, budget_ms in ops_and_budgets.items():
        result[op_name] = _post_json(
            f"{base_url.rstrip('/')}/v1/admin/budget.set",
            {"op": op_name, "budget_ms": budget_ms},
            token=token,
        )
    return result


def _read_env_file(env_path: Path) -> dict[str, str]:
    """KEY=VALUE lines of the .env loom_init wrote (no quoting, no exports)."""
    out: dict[str, str] = {}
    for line in env_path.read_text().strip().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def _resolve_admin_token(explicit: str | None, env_path: Path) -> str:
    """The bearer for the `/v1/admin/*` calls, in order: `--token`,
    `$LOOM_ADMIN_TOKEN`, then the service-account token loom_init just
    minted. That last one is a namespace admin, enough only with auth off."""
    if explicit:
        return explicit
    from_env = os.environ.get("LOOM_ADMIN_TOKEN", "")
    if from_env:
        return from_env
    return _read_env_file(env_path).get("LOOM_API_KEY", "")


def _next_steps(*, base_url: str, org: str, namespace: str, agent: str, env_path: Path) -> str:
    """The block printed after a successful onboard: where the token lives
    and three ready-to-paste ways to use it (MCP, SDK, the dashboard). The
    token itself stays in the file; the commands read it from there."""
    base = base_url.rstrip("/")
    return dedent(f"""
        Next steps (the token is LOOM_API_KEY in {env_path}):

          set -a && . {env_path} && set +a

          # Claude Code / any MCP client — coding toolset
          claude mcp add --transport http loom {base}/mcp?toolset=coding \\
            -H "Authorization: Bearer $LOOM_API_KEY" -s user

          # Python SDK (reads LOOM_API_KEY from the environment)
          from loom import Agent
          async with Agent(org={org!r}, namespace={namespace!r}, agent={agent!r},
                           base_url={base!r}) as a:
              await a.memory.set("hello", {{"from": {agent!r}}})

          # Browse the namespace (paste the token when asked)
          {base}/
        """)


def _run_loom_init(
    *, org: str, namespace: str, agent: str, base_url: str, output_dir: Path, force: bool
) -> dict:
    cmd = [
        sys.executable,
        str(LOOM_INIT),
        "--org",
        org,
        "--namespace",
        namespace,
        "--agent",
        agent,
        "--base-url",
        base_url,
        "--output-dir",
        str(output_dir),
        "--json",
    ]
    if force:
        cmd.append("--force")
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if proc.returncode != 0:
        raise RuntimeError(
            f"loom_init failed (exit {proc.returncode}): "
            f"stdout={proc.stdout!r} stderr={proc.stderr!r}"
        )
    return json.loads(proc.stdout)


def _run_smoke_test(starter_path: Path, env_path: Path, timeout_s: int = 30) -> dict:
    """Exec the generated loom_starter.py and capture result."""
    env = os.environ.copy()
    env.update(_read_env_file(env_path))
    env["PYTHONPATH"] = f"{ROOT / 'sdk' / 'python'}{os.pathsep}{env.get('PYTHONPATH', '')}"
    proc = subprocess.run(
        [sys.executable, str(starter_path)],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Onboard a tenant: service account + raised limits + smoke test."
    )
    parser.add_argument(
        "--org",
        default="dev",
        help="Org the tenant belongs to (default: %(default)s, as in the quickstart).",
    )
    parser.add_argument("--namespace", required=True)
    parser.add_argument(
        "--token",
        default=None,
        help="platform_admin bearer for the /v1/admin/* calls (default: "
        "$LOOM_ADMIN_TOKEN). Without one, auth-on servers skip those steps.",
    )
    parser.add_argument("--agent", required=True)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"),
    )
    parser.add_argument(
        "--output-dir",
        default=os.environ.get("LOOM_OUTPUT_DIR", "."),
    )
    parser.add_argument("--force", action="store_true", help="Overwrite existing .env")
    parser.add_argument(
        "--skip-smoke", action="store_true", help="Don't run loom_starter.py as a smoke test."
    )
    parser.add_argument(
        "--rate-limit-multiplier",
        type=float,
        default=3.0,
        help="Multiply the global default caps by this for the new namespace.",
    )
    parser.add_argument(
        "--budget-override",
        action="append",
        default=[],
        help='Format "op_name=ms"; repeatable. Installs a latency budget override.',
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit one-line JSON summary on stdout.",
    )
    args = parser.parse_args(argv)

    try:
        budgets = parse_budget_overrides(args.budget_override)
    except ValueError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 2
    if args.rate_limit_multiplier <= 0:
        print("✗ --rate-limit-multiplier must be positive", file=sys.stderr)
        return 2
    base = args.base_url.rstrip("/")
    out_dir = Path(args.output_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        summary = _onboard(args, base, out_dir, budgets)
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        if (out_dir / ".env").exists():
            print(f"  {out_dir / '.env'} was written; re-run with --force", file=sys.stderr)
        return 1
    _report(args, base, summary)
    return 0


def _onboard(
    args: argparse.Namespace, base: str, out_dir: Path, budgets: dict[str, int]
) -> dict[str, Any]:
    init_summary = _run_loom_init(
        org=args.org,
        namespace=args.namespace,
        agent=args.agent,
        base_url=base,
        output_dir=out_dir,
        force=args.force,
    )
    token = _resolve_admin_token(args.token, Path(init_summary["env_path"]))
    try:
        ratelimits = _apply_rate_limits(
            base, args.namespace, anchor_overrides(args.rate_limit_multiplier), token=token
        )
        budgets_applied = _apply_budget_overrides(base, budgets, token=token) if budgets else {}
    except AdminRefused:
        if args.token or os.environ.get("LOOM_ADMIN_TOKEN"):
            raise  # the operator named this token; its refusal is the answer
        print(
            "! raised rate limits and budget overrides skipped: they need a "
            "platform_admin token (--token or $LOOM_ADMIN_TOKEN)",
            file=sys.stderr,
        )
        ratelimits, budgets_applied = {}, {}
    if not args.skip_smoke:
        smoke = _run_smoke_test(starter_path=out_dir / "loom_starter.py", env_path=out_dir / ".env")
        if smoke["returncode"] != 0:
            raise RuntimeError(
                f"smoke test failed (exit {smoke['returncode']}): "
                f"stdout={smoke['stdout']!r} stderr={smoke['stderr']!r}"
            )
    return {
        "ok": True,
        "org": args.org,
        "namespace": args.namespace,
        "agent": args.agent,
        "init": init_summary,
        "ratelimits": ratelimits,
        "budget_overrides": budgets_applied,
        "smoke_test": "skipped" if args.skip_smoke else "passed",
    }


def _report(args: argparse.Namespace, base: str, summary: dict[str, Any]) -> None:
    if args.json:
        print(json.dumps(summary))
        return
    init_summary = summary["init"]
    print(f"✓ Onboarded {args.namespace}/{args.agent}")
    print(f"  - service account:    {init_summary['subject']}")
    print(f"  - config:             {init_summary['env_path']}")
    print(f"  - rate-limit classes: {list(summary['ratelimits'])}")
    if summary["budget_overrides"]:
        print(f"  - budget overrides:   {list(summary['budget_overrides'])}")
    smoke = "passed" if summary["smoke_test"] == "passed" else "skipped (--skip-smoke)"
    print(f"  - smoke test:         {smoke}")
    print(
        _next_steps(
            base_url=base,
            org=args.org,
            namespace=args.namespace,
            agent=args.agent,
            env_path=Path(init_summary["env_path"]),
        )
    )


if __name__ == "__main__":
    raise SystemExit(main())
