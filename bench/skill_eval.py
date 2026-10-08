"""skill_eval — the memory guidance is graded by the namespace it produces.

Skill guidance written on judgement can leave a namespace full of
double-encoded values, hash keys, a lease read a hundred times and gets that
outnumber searches a hundred to one. The store has an objective grader for
such a namespace — `namespace.audit` — so the guidance can be measured
instead of trusted: run an agent through a scripted investigation with the
skill loaded, audit the namespace it leaves behind, and treat criticals as
failures. Without this the next skill edit is again unmeasured.

Three task fixtures (`bench/skill_eval/tasks/*.md`), each a realistic brief
that tempts the anti-patterns the audit names; each states the namespace it
writes to and ends by saying the namespace will be audited.

Runners (`--runner`):

  claude-code  shells out to `claude -p` with the plugin's skills loaded
               (`--plugin-dir integrations/claude-code-plugin`) and loom
               mounted from a generated MCP config (`toolset=all`, bearer from
               `LOOM_API_KEY`); the prompt is the task body. Costs model tokens;
               never run from tests.
  codex        `codex exec` with the same MCP wiring (the Codex plugin, when
               installed, supplies the skills).
  manual       prints the task; you run any agent you like against the
               namespace, then re-run with `--audit-only`.
  replay       applies a recorded op stream (`{op, body}` JSONL) to the
               server — the fixtures under `bench/skill_eval/replays/` are a
               bad agent (the anti-patterns the audit names) and a good one (the recipe).

After the run: `namespace.audit` (window 1 h, `sample_limit` 5) and the
score — `pass` = 0 criticals and ≤ 1 warning. The report lists every finding
with count, share and the writer-side remedy, plus "shape" counters read
directly from `memory.list` / `memory.get` (and `knowledge.list`,
`memory.record.query`, `working_memory.render`): rows, share of readable
keys, share of plain-text values, documents, records, working-memory blocks
— so the grader is not only the audit. Shape reads run AFTER the audit so
they do not count as the agent's gets. Output:
`bench/out/skill_eval_<task>_<runner>_<ts>.json`; `--baseline` also stores
the run as the task's reference; `--compare <json>` prints the delta table.

Run:
    LOOM_BASE_URL=http://localhost:7777 LOOM_API_KEY=... LOOM_ORG=<org> \\
      python bench/skill_eval.py --task sre_alert_investigation --runner replay \\
      --replay bench/skill_eval/replays/good_agent.jsonl
    python bench/skill_eval.py --task pr_review_batch --runner claude-code --baseline
    python bench/skill_eval.py --task pr_review_batch --runner claude-code \\
      --compare bench/out/skill_eval_pr_review_batch_baseline.json

`--runner claude-code` runs, from the repo root:
    claude -p --plugin-dir integrations/claude-code-plugin \\
      --mcp-config <generated: {loom: {type: http, url: $LOOM_BASE_URL/mcp?toolset=all,
                                       headers: {Authorization: Bearer $LOOM_API_KEY}}}> \\
      --strict-mcp-config --allowedTools "mcp__loom__*" "Skill" "Read" \\
      --permission-mode bypassPermissions --output-format json --max-turns 60 \\
      "<task body, with the namespace line>"

The pure pieces (task parsing, replay substitution, scoring, shape counters,
rendering, compare) are importable; `tests/unit/test_skill_eval.py`
runs both replay fixtures through this module against a fake loom whose
`namespace.audit` uses the real classifier helpers.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import server.operations  # noqa: E402,F401 — wires handlers first (avoids a circular import)
from server.identity.app.namespace_audit import _HASH_KEY_SEGMENT_RE  # noqa: E402

TASKS_DIR = ROOT / "bench" / "skill_eval" / "tasks"
REPLAYS_DIR = ROOT / "bench" / "skill_eval" / "replays"
OUT_DIR = ROOT / "bench" / "out"
PLUGIN_DIR = ROOT / "integrations" / "claude-code-plugin"

RUNNERS = ("claude-code", "codex", "manual", "replay")
AUDIT_WINDOW_SECONDS = 3600
AUDIT_SAMPLE_LIMIT = 5
# pass = no critical finding and at most this many warnings.
MAX_WARNINGS_TO_PASS = 1
LIST_PAGE = 1000
SHAPE_GET_CAP = 500  # memory.get fallbacks when memory.list carries no value

# The audit's own key classifier: a key is readable when its
# last segment is not a ≥ 24-hex run and no segment is a ≥ 40-hex sha.
_HASH_KEY_RE = re.compile(_HASH_KEY_SEGMENT_RE)
_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_PLACEHOLDER_RE = re.compile(r"\$(ORG|NAMESPACE|AGENT)\b")
# Ops whose input carries no `agent` field.
_NO_AGENT_PREFIXES = ("knowledge.", "memory_schema.", "namespace.")


# ---------------------------------------------------------------- tasks


@dataclass
class Task:
    name: str
    namespace: str
    agent: str
    body: str
    path: Path

    @property
    def prompt(self) -> str:
        return self.body.strip() + "\n"


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """(`key: value` pairs between the leading `---` fences, the rest)."""
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip()
    return meta, text[m.end() :]


def list_tasks() -> list[str]:
    return sorted(p.stem for p in TASKS_DIR.glob("*.md"))


def load_task(name: str) -> Task:
    path = TASKS_DIR / f"{name}.md"
    if not path.is_file():
        raise SystemExit(f"unknown task {name!r}; have {', '.join(list_tasks())}")
    meta, body = parse_frontmatter(path.read_text(encoding="utf-8"))
    ns = meta.get("namespace")
    if not ns:
        raise SystemExit(f"{path}: frontmatter lacks `namespace:`")
    return Task(
        name=meta.get("task", name),
        namespace=ns,
        agent=meta.get("agent", "skill-eval"),
        body=body,
        path=path,
    )


# ---------------------------------------------------------------- client


class LoomError(RuntimeError):
    def __init__(self, op: str, status: int, text: str) -> None:
        super().__init__(f"{op}: HTTP {status}: {text[:300]}")
        self.op, self.status = op, status


class LoomClient:
    """One `/v1/<op>` POST per call. `transport` is for tests (httpx.MockTransport)."""

    def __init__(
        self,
        base_url: str,
        token: str = "",
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 30.0,
    ) -> None:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"), headers=headers, timeout=timeout, transport=transport
        )
        self.calls = 0

    def call(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        self.calls += 1
        r = self._http.post(f"/v1/{op}", json=body)
        if r.status_code >= 400:
            raise LoomError(op, r.status_code, r.text)
        out = r.json()
        return out if isinstance(out, dict) else {"result": out}

    def close(self) -> None:
        self._http.close()


def client_from_env(transport: httpx.BaseTransport | None = None) -> LoomClient:
    base = os.environ.get("LOOM_BASE_URL") or "http://localhost:7777"
    token = os.environ.get("LOOM_API_KEY", "")
    return LoomClient(base, token, transport=transport)


# ---------------------------------------------------------------- replay


def substitute(obj: Any, *, org: str, namespace: str, agent: str) -> Any:
    """`$ORG` / `$NAMESPACE` / `$AGENT` inside any string of a JSON value."""
    values = {"ORG": org, "NAMESPACE": namespace, "AGENT": agent}
    if isinstance(obj, str):
        return _PLACEHOLDER_RE.sub(lambda m: values[m.group(1)], obj)
    if isinstance(obj, list):
        return [substitute(x, org=org, namespace=namespace, agent=agent) for x in obj]
    if isinstance(obj, dict):
        return {k: substitute(v, org=org, namespace=namespace, agent=agent) for k, v in obj.items()}
    return obj


def load_replay(path: Path) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        rec = json.loads(line)
        if not isinstance(rec, dict) or "op" not in rec or "body" not in rec:
            raise SystemExit(f"{path}:{n}: expected {{op, body}}")
        ops.append(rec)
    return ops


def bind_identity(op: str, body: dict[str, Any], *, org: str, namespace: str, agent: str) -> dict:
    out = dict(body)
    out.setdefault("org", org)
    out.setdefault("namespace", namespace)
    if not op.startswith(_NO_AGENT_PREFIXES):
        out.setdefault("agent", agent)
    return out


@dataclass
class ReplayResult:
    applied: int = 0
    by_op: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


def run_replay(
    client: LoomClient, ops: list[dict[str, Any]], *, org: str, namespace: str, agent: str
) -> ReplayResult:
    """Apply the stream in order; an op the server refuses is recorded and
    the replay continues — a writer whose calls fail is part of what the
    namespace shows."""
    res = ReplayResult()
    for rec in ops:
        op = str(rec["op"])
        body = bind_identity(
            op,
            substitute(rec["body"], org=org, namespace=namespace, agent=agent),
            org=org,
            namespace=namespace,
            agent=agent,
        )
        try:
            client.call(op, body)
        except (LoomError, httpx.HTTPError) as exc:
            res.errors.append(f"{op}: {exc}")
            continue
        res.applied += 1
        res.by_op[op] = res.by_op.get(op, 0) + 1
    return res


# ---------------------------------------------------------------- agents


def mcp_config(base_url: str, token: str, *, toolset: str = "all") -> dict[str, Any]:
    return {
        "mcpServers": {
            "loom": {
                "type": "http",
                "url": f"{base_url.rstrip('/')}/mcp?toolset={toolset}",
                "headers": {"Authorization": f"Bearer {token}"},
            }
        }
    }


def claude_code_command(
    task: Task, *, mcp_config_path: Path, plugin_dir: Path = PLUGIN_DIR, max_turns: int = 60
) -> list[str]:
    """The exact `claude -p` invocation (documented in the module docstring)."""
    return [
        "claude",
        "-p",
        "--plugin-dir",
        str(plugin_dir),
        "--mcp-config",
        str(mcp_config_path),
        "--strict-mcp-config",
        "--allowedTools",
        "mcp__loom__*",
        "Skill",
        "Read",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--max-turns",
        str(max_turns),
        task.prompt,
    ]


def codex_command(task: Task, *, base_url: str, toolset: str = "all") -> list[str]:
    """`codex exec`; the bearer rides `$LOOM_API_KEY` in the environment, never argv."""
    return [
        "codex",
        "exec",
        "--json",
        "-c",
        f'mcp_servers.loom.url="{base_url.rstrip("/")}/mcp?toolset={toolset}"',
        "-c",
        'mcp_servers.loom.bearer_token_env_var="LOOM_API_KEY"',
        task.prompt,
    ]


def run_agent(task: Task, runner: str, *, base_url: str, token: str, max_turns: int) -> dict:
    """Shell out to the agent. Never called from tests."""
    env = dict(os.environ, LOOM_BASE_URL=base_url, LOOM_API_KEY=token)
    if runner == "claude-code":
        fd, tmp = tempfile.mkstemp(prefix="skill_eval_mcp_", suffix=".json")
        with os.fdopen(fd, "w") as fh:
            json.dump(mcp_config(base_url, token), fh)
        os.chmod(tmp, 0o600)
        cmd = claude_code_command(task, mcp_config_path=Path(tmp), max_turns=max_turns)
    elif runner == "codex":
        cmd = codex_command(task, base_url=base_url)
        tmp = ""
    else:
        raise ValueError(runner)
    print(f"$ {' '.join(shlex.quote(c) for c in cmd[:-1])} '<task prompt>'", flush=True)
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True, check=False)
    finally:
        if tmp:
            Path(tmp).unlink(missing_ok=True)
    out = {
        "command": cmd[:-1] + ["<task prompt>"],
        "returncode": proc.returncode,
        "seconds": round(time.time() - t0, 1),
        "stdout_tail": proc.stdout[-4000:],
        "stderr_tail": proc.stderr[-2000:],
    }
    if proc.returncode != 0:
        print(f"{runner} exited {proc.returncode}: {proc.stderr[-800:]}", file=sys.stderr)
    return out


# ---------------------------------------------------------------- audit + score


def audit(client: LoomClient, *, org: str, namespace: str) -> dict[str, Any]:
    return client.call(
        "namespace.audit",
        {
            "org": org,
            "namespace": namespace,
            "window_seconds": AUDIT_WINDOW_SECONDS,
            "sample_limit": AUDIT_SAMPLE_LIMIT,
        },
    )


def score_audit(report: dict[str, Any]) -> dict[str, Any]:
    """`pass` = 0 criticals and ≤ MAX_WARNINGS_TO_PASS warnings. Every finding
    rides along with count, share and the writer-side remedy."""
    findings = [
        {
            "code": f["code"],
            "severity": f["severity"],
            "count": int(f.get("count", 0)),
            "share": float(f.get("share", 0.0)),
            "bytes": int(f.get("bytes", 0)),
            "remedy": f.get("remedy", ""),
            "samples": f.get("samples", []),
        }
        for f in report.get("findings", [])
    ]
    criticals = sum(1 for f in findings if f["severity"] == "critical")
    warnings = sum(1 for f in findings if f["severity"] == "warning")
    return {
        "passed": criticals == 0 and warnings <= MAX_WARNINGS_TO_PASS,
        "criticals": criticals,
        "warnings": warnings,
        "findings": findings,
        "skipped": list(report.get("skipped", [])),
        "summary": dict(report.get("summary", {})),
    }


# ---------------------------------------------------------------- shape


def is_readable_key(key: str) -> bool:
    return not _HASH_KEY_RE.search(key)


def is_plain_text(value: Any) -> bool:
    """A string that is not itself serialized JSON of an object or array —
    the value shape search can embed as a claim."""
    if not isinstance(value, str):
        return False
    s = value.strip()
    if not s or s[0] not in "{[":
        return True
    try:
        parsed = json.loads(s)
    except ValueError:
        return True
    return not isinstance(parsed, (dict, list))


def _paged_list(client: LoomClient, body: dict[str, Any]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    offset = 0
    while True:
        out = client.call("memory.list", {**body, "limit": LIST_PAGE, "offset": offset})
        page = list(out.get("entries", []))
        entries.extend(page)
        if not out.get("truncated") or not page:
            return entries
        offset += len(page)


def shape(client: LoomClient, *, org: str, namespace: str, agent: str) -> dict[str, Any]:
    """Counters read straight from the rows: what the agent left, by kind."""
    ident = {"org": org, "namespace": namespace, "agent": agent}
    entries = _paged_list(client, {**ident, "fields": ["memory_key", "memory_type", "value"]})
    gets = 0
    readable = plain = 0
    for e in entries:
        key = str(e.get("memory_key", ""))
        if "value" not in e and gets < SHAPE_GET_CAP:
            gets += 1
            e["value"] = client.call("memory.get", {**ident, "memory_key": key}).get("value")
        readable += is_readable_key(key)
        plain += is_plain_text(e.get("value"))
    rows = len(entries)
    out: dict[str, Any] = {
        "rows": rows,
        "readable_key_share": round(readable / rows, 3) if rows else 1.0,
        "plain_text_value_share": round(plain / rows, 3) if rows else 1.0,
        "documents": None,
        "records": None,
        "working_memory_blocks": None,
        "skipped": [],
    }
    try:
        docs = 0
        cursor: str | None = None
        while True:
            body: dict[str, Any] = {"org": org, "namespace": namespace}
            if cursor:
                body["cursor"] = cursor
            page = client.call("knowledge.list", body)
            docs += sum(1 for r in page.get("rows", []) if not r.get("superseded"))
            cursor = page.get("next_cursor")
            if not cursor:
                break
        out["documents"] = docs
    except (LoomError, httpx.HTTPError) as exc:
        out["skipped"].append(f"documents: {exc}")
    try:
        schemas = client.call("memory_schema.list", {"org": org, "namespace": namespace})
        records = 0
        for s in schemas.get("schemas", []):
            q = client.call(
                "memory.record.query",
                {"org": org, "namespace": namespace, "schema_name": s["name"], "limit": 1000},
            )
            records += len(q.get("rows", []))
        out["records"] = records
    except (LoomError, httpx.HTTPError) as exc:
        out["skipped"].append(f"records: {exc}")
    try:
        wm = client.call("working_memory.render", {**ident, "block_keys": [], "limit": 100})
        out["working_memory_blocks"] = int(wm.get("block_count", len(wm.get("blocks", []))))
    except (LoomError, httpx.HTTPError) as exc:
        out["skipped"].append(f"working_memory: {exc}")
    return out


# ---------------------------------------------------------------- report


def _pct(x: float | None) -> str:
    return "-" if x is None else f"{x:.0%}"


def render_report(result: dict[str, Any]) -> str:
    sc = result["score"]
    sm = sc.get("summary", {})
    verdict = "PASS" if sc["passed"] else "FAIL"
    lines = [
        f"skill_eval {result['task']} runner={result['runner']} "
        f"{result['org']}/{result['namespace']} — {verdict} "
        f"({sc['criticals']} critical, {sc['warnings']} warning; "
        f"pass = 0 critical and ≤ {MAX_WARNINGS_TO_PASS} warning)",
        f"  audit window {AUDIT_WINDOW_SECONDS // 60} min: live {sm.get('live_rows', 0)} rows / "
        f"{sm.get('live_bytes', 0):,} B; gets {sm.get('gets', 0)} / searches "
        f"{sm.get('searches', 0)} / sets {sm.get('sets', 0)} "
        f"(ratio {sm.get('get_search_ratio', 0)})",
        "",
        f"  {'code':<24} {'severity':<9} {'count':>6} {'share':>6}  remedy",
    ]
    for f in sc["findings"]:
        lines.append(
            f"  {f['code']:<24} {f['severity']:<9} {f['count']:>6} {_pct(f['share']):>6}  "
            f"{f['remedy']}"
        )
    for sk in sc.get("skipped", []):
        lines.append(f"  skipped: {sk}")
    sh = result.get("shape") or {}
    if sh:
        lines += [
            "",
            f"  shape: rows {sh.get('rows')}; readable keys {_pct(sh.get('readable_key_share'))}; "
            f"plain-text values {_pct(sh.get('plain_text_value_share'))}; documents "
            f"{sh.get('documents')}; records {sh.get('records')}; working-memory blocks "
            f"{sh.get('working_memory_blocks')}",
        ]
        for sk in sh.get("skipped", []):
            lines.append(f"  shape skipped: {sk}")
    rp = result.get("replay")
    if rp:
        lines.append(
            f"  replay: {rp['applied']} ops applied, {len(rp['errors'])} refused; "
            + ", ".join(f"{k} ×{v}" for k, v in sorted(rp["by_op"].items()))
        )
        for err in rp["errors"][:5]:
            lines.append(f"    refused: {err}")
    return "\n".join(lines)


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> str:
    """The delta table: every finding code (count and severity, before →
    after) and every shape counter."""
    b_f = {f["code"]: f for f in baseline["score"]["findings"]}
    c_f = {f["code"]: f for f in current["score"]["findings"]}
    lines = [
        f"compare {baseline['task']}: baseline {baseline.get('runner')} @ {baseline.get('ts')} "
        f"({'PASS' if baseline['score']['passed'] else 'FAIL'}) → current "
        f"{current.get('runner')} @ {current.get('ts')} "
        f"({'PASS' if current['score']['passed'] else 'FAIL'})",
        f"  {'code':<30} {'before':>16} {'after':>16} {'Δcount':>7}",
    ]
    for code in sorted(set(b_f) | set(c_f)):
        b, c = b_f.get(code), c_f.get(code)
        bs = f"{b['severity']} {b['count']}" if b else "-"
        cs = f"{c['severity']} {c['count']}" if c else "-"
        delta = (c["count"] if c else 0) - (b["count"] if b else 0)
        lines.append(f"  {code:<30} {bs:>16} {cs:>16} {delta:>+7}")
    bsh, csh = baseline.get("shape") or {}, current.get("shape") or {}
    for k in (
        "rows",
        "readable_key_share",
        "plain_text_value_share",
        "documents",
        "records",
        "working_memory_blocks",
    ):
        bv, cv = bsh.get(k), csh.get(k)
        if isinstance(bv, float) or isinstance(cv, float):
            lines.append(f"  shape.{k:<24} {_pct(bv):>16} {_pct(cv):>16}")
        else:
            lines.append(f"  shape.{k:<24} {str(bv):>16} {str(cv):>16}")
    return "\n".join(lines)


# ---------------------------------------------------------------- one run


def evaluate(
    client: LoomClient,
    task: Task,
    *,
    runner: str,
    org: str,
    namespace: str | None = None,
    replay_path: Path | None = None,
    audit_only: bool = False,
    agent_run: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply the run (replay here; agents are run by `main`), then audit,
    score and read the shape. Pure with respect to the process: no files."""
    ns = namespace or task.namespace
    result: dict[str, Any] = {
        "task": task.name,
        "runner": runner,
        "org": org,
        "namespace": ns,
        "agent": task.agent,
        "ts": time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
        "audit_only": audit_only,
    }
    if runner == "replay" and not audit_only:
        if replay_path is None:
            raise SystemExit("--runner replay needs --replay <ops.jsonl>")
        rp = run_replay(client, load_replay(replay_path), org=org, namespace=ns, agent=task.agent)
        result["replay"] = {**asdict(rp), "path": str(replay_path)}
    if agent_run:
        result["agent_run"] = agent_run
    result["score"] = score_audit(audit(client, org=org, namespace=ns))
    result["shape"] = shape(client, org=org, namespace=ns, agent=task.agent)
    return result


def write_result(
    result: dict[str, Any], *, out_dir: Path = OUT_DIR, baseline: bool = False
) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"skill_eval_{result['task']}_{result['runner']}_{result['ts']}.json"
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if baseline:
        ref = out_dir / f"skill_eval_{result['task']}_baseline.json"
        ref.write_text(json.dumps({**result, "baseline": True}, indent=2, sort_keys=True) + "\n")
    return path


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--task", required=True, help=f"one of: {', '.join(list_tasks())}")
    ap.add_argument("--runner", choices=RUNNERS, default="manual")
    ap.add_argument("--replay", type=Path, help="ops JSONL for --runner replay")
    ap.add_argument("--org", default=os.environ.get("LOOM_ORG", ""))
    ap.add_argument("--namespace", help="override the task's namespace")
    ap.add_argument("--max-turns", type=int, default=60)
    ap.add_argument(
        "--audit-only",
        action="store_true",
        help="skip the agent/replay step; grade the namespace as it stands "
        "(after a `manual` run or a `claude plugin eval` run with real servers)",
    )
    ap.add_argument(
        "--baseline", action="store_true", help="store this run as the task's reference"
    )
    ap.add_argument(
        "--compare", type=Path, help="a previous result JSON to print the delta against"
    )
    ap.add_argument("--out-dir", type=Path, default=OUT_DIR)
    args = ap.parse_args(argv)

    task = load_task(args.task)
    if args.runner == "manual" and not args.audit_only:
        ns = args.namespace or task.namespace
        print(f"# task {task.name} → namespace {ns} (agent {task.agent})")
        print(task.prompt)
        print("# run your agent against that namespace, then:")
        print(f"#   python bench/skill_eval.py --task {task.name} --runner manual --audit-only")
        return 0
    if not args.org:
        raise SystemExit("--org or LOOM_ORG is required")
    client = client_from_env()
    agent_run = None
    if args.runner in ("claude-code", "codex") and not args.audit_only:
        base = os.environ.get("LOOM_BASE_URL") or "http://localhost:7777"
        token = os.environ.get("LOOM_API_KEY", "")
        if not token:
            raise SystemExit("LOOM_API_KEY is required for an agent run")
        agent_run = run_agent(
            task, args.runner, base_url=base, token=token, max_turns=args.max_turns
        )
    try:
        result = evaluate(
            client,
            task,
            runner=args.runner,
            org=args.org,
            namespace=args.namespace,
            replay_path=args.replay,
            audit_only=args.audit_only,
            agent_run=agent_run,
        )
    finally:
        client.close()
    print(render_report(result))
    path = write_result(result, out_dir=args.out_dir, baseline=args.baseline)
    print(f"\nwrote {path}")
    if args.compare:
        print()
        print(compare(json.loads(args.compare.read_text()), result))
    return 0 if result["score"]["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
