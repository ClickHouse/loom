"""Drivers that produce one agent transcript per (task, arm, seed) cell.

Three drivers share one interface so the scorer never knows which ran:

  ClaudeCliDriver   `claude -p` in a detached worktree of the task's repository
                    at the PR's base commit, stream-JSON on stdout. Arms differ
                    only in the plugin directory (the read shunt) and the MCP
                    config (the code tools) — the same harness, toggled, so
                    an arm delta is the tools' and not the harness's.
  FixtureDriver     copies a recorded transcript into the cell — replays a
                    pilot, or proves the scorer on a synthetic transcript.
  DryRunDriver      writes the exact argv and env keys it would run, and no
                    transcript. Never spends.

Hooks fire under `claude -p` only WITHOUT `--bare` (`--bare` skips hooks and
plugins by definition; see the plugin README). The driver
therefore never passes `--bare`; the scorer proves the toggle from the
transcript's `system/init` row (`plugins`, `mcp_servers`) and the cell's own
shunt log, not from the flag set.

The shunt-only arm ships a plugin directory built from the repository's
plugin with exactly ONE hook wired (PreToolUse loom-code-shunt). The full
plugin's capture, run-ledger, usage and session-memory hooks write rows to
the server; a bench arm that measures the shunt must not also seed the
memory plane, and "shunt only" then means what it says.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

ARM_DESCRIPTIONS: dict[str, str] = {
    "A": "plugin off: no hooks, no loom MCP tools (the control)",
    "B": "shunt only: PreToolUse read shunt denies bulk reads, no loom tools to redirect to",
    "C": "shunt + MCP code tools: the shunt's redirect target is callable",
    "D": "shunt + MCP code tools + read-window cap (Read limit <= --read-cap lines)",
    "E": "shunt + MCP code tools against a server whose code.search default is bodies=none "
    "(the accuracy arm; --arm-url E=<url>)",
    "F": "shunt + MCP code tools against a server with code.grep neighbours on by default, "
    "plugin whose redirect names the one call; needs --arm-url F= and "
    "--arm-plugin-src F=",
}

# Arms that talk to a loom server over MCP; the others run with an empty,
# strict server list.
MCP_ARMS: frozenset[str] = frozenset({"C", "D", "E", "F"})

# Tools the localization task never needs. Disallowing them removes the file
# editors and the network and history tools, so the leak audit's rules are a
# backstop rather than the first line. Bash stays (the agent greps with it), so
# an arm is not strictly read-only: a task's cells share one worktree, so each
# cell's meta records whether the worktree was dirty after it ran.
DISALLOWED_TOOLS: tuple[str, ...] = (
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "WebFetch",
    "WebSearch",
    "Task",
    "Agent",
    "EnterWorktree",
    "ExitWorktree",
    "TaskCreate",
    "TaskUpdate",
    "TaskList",
    "TaskGet",
    "TaskOutput",
    "TaskStop",
    "SendMessage",
    "ScheduleWakeup",
    "PushNotification",
    "RemoteTrigger",
    "CronCreate",
    "CronDelete",
    "CronList",
)

# Every hook of the repository plugin except the shunt, switched off by the
# env the plugin documents; belt and braces beside the one-hook plugin dir.
_HOOKS_OFF_ENV: dict[str, str] = {
    "LOOM_INJECT_CONTEXT": "false",
    "LOOM_FILE_CONTEXT": "off",
    "LOOM_BLAST_RADIUS": "off",
    "LOOM_TEST_GATE": "off",
    "LOOM_USAGE": "off",
    "LOOM_RUN_LEDGER": "off",
    "LOOM_WORKING_MEMORY": "off",
    "LOOM_SESSION_MEMORY": "off",
    "LOOM_CAPTURE_CONVERSATIONS": "false",
    "LOOM_PROMPT_PACK": "off",
    "LOOM_PROMPT_RECALL": "off",
}


@dataclass
class Cell:
    task_id: str
    arm: str
    seed: int
    prompt: str
    system_prompt: str
    base_sha: str
    cell_dir: Path
    workdir: Path


@dataclass
class DriverConfig:
    model: str = "sonnet"
    max_turns: int = 30
    max_budget_usd: float = 1.5
    timeout_s: int = 900
    plugin_src: Path | None = None  # integrations/claude-code-plugin
    loom_url: str = ""
    loom_token: str = ""
    shunt_repo: str = ""  # owner/name the shunt resolves the checkout to
    mcp_server_name: str = "loom"
    read_cap: int = 0  # arm D: Read limit ceiling (lines); 0 = no cap
    shunt_report: bool = False  # ship shunt_hit/pass rows to the server
    # per-arm loom URL override (arm E: a second isolated server with a
    # different deployment default); arms not listed use `loom_url`
    arm_urls: dict[str, str] = field(default_factory=dict)
    # An arm built from ANOTHER plugin tree (arm F: the redirect that names
    # code.grep's one-call block); unset = plugin_src
    arm_plugin_src: dict[str, Path] = field(default_factory=dict)
    extra_env: dict[str, str] = field(default_factory=dict)
    # per-arm env on top of everything else (`--arm-env F=LOOM_SHUNT_READ_CAP=200`:
    # an arm that differs from another only in one plugin setting)
    arm_env: dict[str, dict[str, str]] = field(default_factory=dict)


class Driver(Protocol):
    def run(self, cell: Cell) -> dict[str, Any]: ...


def transcript_path(cell_dir: Path) -> Path:
    return cell_dir / "transcript.jsonl"


# ---------------------------------------------------------------------------
# plugin directory for the shunt-only arms
# ---------------------------------------------------------------------------


def build_shunt_only_plugin(src: Path, dest: Path, *, read_cap: int = 0) -> Path:
    """Copy the repository plugin's lib/ and the shunt hook into `dest` with a
    hooks.json that wires ONLY PreToolUse loom-code-shunt. With `read_cap`
    > 0 a second PreToolUse hook clamps Read.limit to the cap (arm D)."""
    dest.mkdir(parents=True, exist_ok=True)
    (dest / ".claude-plugin").mkdir(exist_ok=True)
    (dest / "hooks").mkdir(exist_ok=True)
    if (dest / "lib").exists():
        shutil.rmtree(dest / "lib")
    shutil.copytree(src / "lib", dest / "lib")
    shutil.copy2(src / "hooks" / "loom-code-shunt.mjs", dest / "hooks" / "loom-code-shunt.mjs")
    manifest = {
        "name": "loom-shunt-only",
        "version": "0.0.0-bench",
        "description": "bench arm: loom-code-shunt PreToolUse hook only",
    }
    (dest / ".claude-plugin" / "plugin.json").write_text(json.dumps(manifest, indent=2))
    hooks: list[dict[str, Any]] = [
        {
            "matcher": "Read|Grep|Bash",
            "hooks": [
                {
                    "type": "command",
                    "command": 'node "${CLAUDE_PLUGIN_ROOT}/hooks/loom-code-shunt.mjs" PreToolUse',
                }
            ],
        }
    ]
    if read_cap > 0:
        (dest / "hooks" / "read-cap.mjs").write_text(_READ_CAP_HOOK)
        hooks.append(
            {
                "matcher": "Read",
                "hooks": [
                    {
                        "type": "command",
                        "command": 'node "${CLAUDE_PLUGIN_ROOT}/hooks/read-cap.mjs"',
                    }
                ],
            }
        )
    (dest / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"PreToolUse": hooks}}, indent=2)
    )
    return dest


# Arm D's read-window cap: a Read without a limit, or with one above the cap,
# is rewritten to `limit = cap` (offset kept): the agent still reads, in
# windows.
_READ_CAP_HOOK = r"""#!/usr/bin/env node
import fs from "node:fs";
import path from "node:path";
let raw = "";
process.stdin.on("data", (d) => (raw += d));
process.stdin.on("end", () => {
  let j = {};
  try { j = JSON.parse(raw); } catch { process.exit(0); }
  const cap = Number(process.env.BENCH_READ_CAP || 0);
  const inp = j.tool_input || {};
  if (!cap || j.tool_name !== "Read") process.exit(0);
  const limit = inp.limit == null ? null : Number(inp.limit);
  if (limit != null && limit <= cap) process.exit(0);
  const updated = { ...inp, limit: cap };
  try {
    const dir = process.env.TMPDIR || "/tmp";
    const off = inp.offset == null ? 1 : inp.offset;
    const lim = limit == null ? "none" : limit;
    fs.appendFileSync(path.join(dir, "bench-read-cap.log"),
      `cap ${inp.file_path || "?"} ${off} ${lim} -> ${cap}\n`);
  } catch {}
  process.stdout.write(JSON.stringify({
    hookSpecificOutput: {
      hookEventName: "PreToolUse",
      permissionDecision: "allow",
      updatedInput: updated,
      permissionDecisionReason: `bench read-window cap: limit set to ${cap} lines`,
    },
  }) + "\n");
  process.exit(0);
});
"""


# ---------------------------------------------------------------------------
# claude -p
# ---------------------------------------------------------------------------


class ClaudeCliDriver:
    """`claude -p` per cell. The arm decides plugin dir + MCP config; the
    prompt, model, turn and dollar budgets are identical across arms."""

    def __init__(self, cfg: DriverConfig, run_dir: Path, claude_bin: str = "claude") -> None:
        self.cfg = cfg
        self.run_dir = run_dir
        self.claude_bin = claude_bin
        self._plugin_dirs: dict[str, Path] = {}
        # a task's cells run concurrently; the first two cells of an arm
        # must not build (rmtree + copytree) the same plugin directory at once
        self._plugin_lock = threading.Lock()

    # -- composition -------------------------------------------------------

    def plugin_dir_for(self, arm: str) -> Path | None:
        if arm == "A":
            return None
        with self._plugin_lock:
            if arm in self._plugin_dirs:
                return self._plugin_dirs[arm]
            if self.cfg.plugin_src is None:
                raise ValueError(
                    "every arm but A needs --plugin-src (integrations/claude-code-plugin)"
                )
            cap = self.cfg.read_cap if arm == "D" else 0
            src = self.cfg.arm_plugin_src.get(arm) or self.cfg.plugin_src
            dest = build_shunt_only_plugin(
                src, self.run_dir / "plugins" / f"arm_{arm}", read_cap=cap
            )
            self._plugin_dirs[arm] = dest
            return dest

    def loom_url_for(self, arm: str) -> str:
        return self.cfg.arm_urls.get(arm) or self.cfg.loom_url

    def mcp_config_for(self, arm: str) -> Path:
        """Arms A/B: an EMPTY server list under --strict-mcp-config, so a
        user-scope loom server in ~/.claude.json never leaks into the control.
        Arms C/D/E: an isolated server's /mcp with the bench bearer (E may
        point at a second server through `arm_urls`)."""
        p = self.run_dir / f"mcp_{arm}.json"
        url = self.loom_url_for(arm)
        if arm in MCP_ARMS and url:
            body: dict[str, Any] = {
                "mcpServers": {
                    self.cfg.mcp_server_name: {
                        "type": "http",
                        "url": url.rstrip("/") + "/mcp",
                        "headers": {"Authorization": f"Bearer {self.cfg.loom_token}"},
                    }
                }
            }
        else:
            body = {"mcpServers": {}}
        _write_private(p, json.dumps(body, indent=2))
        return p

    def env_for(self, arm: str, cell: Cell) -> dict[str, str]:
        env = dict(os.environ)
        env.update(self.cfg.extra_env)
        # per-cell tmp: the shunt hook keeps its repo cache and decision log
        # in $TMPDIR, so each cell owns its proof that the hook fired
        tmp = cell.cell_dir / "tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        env["TMPDIR"] = str(tmp)
        env["BENCH_CELL"] = f"{cell.task_id}:{arm}:{cell.seed}"
        if arm != "A":
            env.update(_HOOKS_OFF_ENV)
            env["LOOM_BASE_URL"] = self.loom_url_for(arm)
            env["LOOM_API_KEY"] = self.cfg.loom_token
            env["LOOM_CODE_SHUNT"] = "on"
            env["LOOM_SHUNT_LOG"] = "on"
            env["LOOM_SHUNT_REPORT"] = "on" if self.cfg.shunt_report else "off"
            if self.cfg.shunt_repo:
                env["LOOM_CODE_REPO"] = self.cfg.shunt_repo
            if arm == "D" and self.cfg.read_cap > 0:
                env["BENCH_READ_CAP"] = str(self.cfg.read_cap)
            env.update(self.cfg.arm_env.get(arm, {}))
        else:
            env["LOOM_CODE_SHUNT"] = "off"
            env.pop("LOOM_BASE_URL", None)
            env.pop("LOOM_API_KEY", None)
        return env

    def argv_for(self, arm: str, cell: Cell) -> list[str]:
        argv = [
            self.claude_bin,
            "-p",
            cell.prompt,
            "--model",
            self.cfg.model,
            "--max-turns",
            str(self.cfg.max_turns),
            "--max-budget-usd",
            f"{self.cfg.max_budget_usd:.2f}",
            "--output-format",
            "stream-json",
            "--verbose",
            "--no-session-persistence",
            "--permission-mode",
            "bypassPermissions",
            "--allow-dangerously-skip-permissions",
            "--strict-mcp-config",
            "--mcp-config",
            str(self.mcp_config_for(arm)),
            "--disallowedTools",
            *DISALLOWED_TOOLS,
        ]
        if cell.system_prompt:
            argv += ["--append-system-prompt", cell.system_prompt]
        pd = self.plugin_dir_for(arm)
        if pd is not None:
            argv += ["--plugin-dir", str(pd)]
        return argv

    # -- execution ---------------------------------------------------------

    def run(self, cell: Cell) -> dict[str, Any]:
        cell.cell_dir.mkdir(parents=True, exist_ok=True)
        argv = self.argv_for(cell.arm, cell)
        env = self.env_for(cell.arm, cell)
        out = transcript_path(cell.cell_dir)
        err = cell.cell_dir / "stderr.txt"
        t0 = time.monotonic()
        with out.open("wb") as fo, err.open("wb") as fe:
            rc, timed_out = run_process_group(
                argv, cwd=cell.workdir, env=env, stdout=fo, stderr=fe, timeout_s=self.cfg.timeout_s
            )
        dur = time.monotonic() - t0
        # The shunt's per-user decision log, loom-shunt-log-<hash>.log (lib/tmp-state.mjs).
        for shunt_log in sorted(Path(env["TMPDIR"]).glob("loom-shunt-log-*.log"))[:1]:
            shutil.copy2(shunt_log, cell.cell_dir / "shunt.log")
        cap_log = Path(env["TMPDIR"]) / "bench-read-cap.log"
        if cap_log.exists():
            shutil.copy2(cap_log, cell.cell_dir / "read_cap.log")
        meta = {
            "task_id": cell.task_id,
            "arm": cell.arm,
            "seed": cell.seed,
            "base_sha": cell.base_sha,
            "argv": redact_argv(argv),
            "env_keys": sorted(k for k in env if k.startswith(("LOOM_", "BENCH_", "TMPDIR"))),
            "returncode": rc,
            "timed_out": timed_out,
            "duration_s": round(dur, 2),
            "shunt_log": shunt_log.exists(),
            "read_cap_log": cap_log.exists(),
            # What the hook's repo resolve wrote for this
            # checkout — a plugin arm whose cache says `indexed: false` never
            # touched the index and is not the arm it claims to be
            "shunt_resolved": shunt_resolved(Path(env["TMPDIR"])) if cell.arm != "A" else None,
            # who paid: the init row's apiKeySource ("ANTHROPIC_API_KEY" when
            # the bench key in the environment was used, "none" = the login)
            "api_key_source": api_key_source(out),
            "workdir_dirty": workdir_dirty(cell.workdir),
        }
        (cell.cell_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta


def workdir_dirty(workdir: Path) -> bool | None:
    """True when the checkout has changes after the cell (a write through Bash
    that sibling cells of the same task would read); None when git cannot say."""
    try:
        out = subprocess.run(
            ["git", "-C", str(workdir), "status", "--porcelain", "--untracked-files=normal"],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return bool(out.strip())


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` mode 0600 from the first byte, atomically: the file
    carries a bearer, and concurrent cells of one arm rewrite it while another
    cell's `claude` may be reading it."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as fh:  # mkstemp creates the file 0600
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def run_process_group(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    stdout: Any,
    stderr: Any,
    timeout_s: float,
    grace_s: float = 10.0,
) -> tuple[int | None, bool]:
    """Run `argv` as the leader of its own process group and return
    (returncode, timed_out). The agent spawns hooks, MCP stdio servers and Bash
    tool children; on timeout the whole group gets SIGTERM, then SIGKILL after
    `grace_s`, and after a normal exit any member still alive is killed, so no
    cell leaves processes behind to run into the next one."""
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=stdout,
        stderr=stderr,
        start_new_session=True,
    )
    timed_out = False
    try:
        proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=grace_s)
        except subprocess.TimeoutExpired:
            _signal_group(proc.pid, signal.SIGKILL)
            proc.wait()
    finally:
        _signal_group(proc.pid, signal.SIGKILL)  # stragglers of a finished leader
    return (None if timed_out else proc.returncode), timed_out


def _signal_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass  # the group is already gone


def shunt_resolved(tmp: Path) -> bool | None:
    """The shunt's repo-resolve cache in the cell's $TMPDIR
    (`loom-shunt-<hash>.json`, lib/repo-scope.mjs): True when it says
    `indexed: true`, False when it says the checkout is not indexed or the
    lookup went unanswered, None when the hook never wrote one (it did not
    run: no Read/Grep/Bash call, or hooks off)."""
    try:
        files = sorted(tmp.glob("loom-shunt-*.json"))
    except OSError:
        return None
    for f in files:
        try:
            d = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(d, dict) and "indexed" in d:
            return bool(d.get("indexed"))
    return None


def repo_slug_of_mirror(repo: str | Path) -> str:
    """`owner/name` from the mirror's `origin` URL (the same
    slug the shunt derives inside a worktree), '' when there is none or the
    remote is a filesystem path (a local clone names no repository)."""
    try:
        url = subprocess.run(
            ["git", "-C", str(repo), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    return slug_of_remote(url)


_REMOTE_SLUG_RE = re.compile(r"[:/]([^/:]+)/([^/]+?)(?:\.git)?/?$")


def slug_of_remote(url: str) -> str:
    """`https://github.com/ClickHouse/loom.git`, `git@github.com:ClickHouse/loom.git`
    and `ssh://git@host/ClickHouse/loom` → `clickhouse/loom`; a path → ''."""
    if "://" not in url and "@" not in url:
        return ""
    m = _REMOTE_SLUG_RE.search(url)
    return f"{m.group(1)}/{m.group(2)}".lower() if m else ""


def api_key_source(transcript: Path) -> str:
    """`apiKeySource` from the stream-JSON init row, "" when absent."""
    try:
        with transcript.open() as fh:
            for ln in fh:
                if not ln.strip():
                    continue
                d = json.loads(ln)
                if d.get("type") == "system" and d.get("subtype") == "init":
                    return str(d.get("apiKeySource") or "")
                break
    except (OSError, json.JSONDecodeError):
        return ""
    return ""


def redact_argv(argv: list[str]) -> list[str]:
    """The prompt is long and the mcp config path is enough; never echo a
    bearer (it lives in the mcp json, mode 600, not in argv)."""
    out: list[str] = []
    skip = False
    for i, a in enumerate(argv):
        if skip:
            skip = False
            out.append("<prompt>" if argv[i - 1] == "-p" else "<system-prompt>")
            continue
        if a in ("-p", "--append-system-prompt"):
            skip = True
        out.append(a)
    return out


# ---------------------------------------------------------------------------
# fixture + dry run
# ---------------------------------------------------------------------------


class FixtureDriver:
    """Copies `<fixture_dir>/<task_id>/<arm>/s<seed>.jsonl` (or a single
    transcript for every cell) into the cell. Replays without spending."""

    def __init__(self, fixture: Path) -> None:
        self.fixture = fixture

    def run(self, cell: Cell) -> dict[str, Any]:
        cell.cell_dir.mkdir(parents=True, exist_ok=True)
        cand = self.fixture / cell.task_id / cell.arm / f"s{cell.seed}.jsonl"
        src = cand if cand.exists() else self.fixture
        if not src.is_file():
            raise FileNotFoundError(
                f"no fixture transcript for {cell.task_id}/{cell.arm}/s{cell.seed}"
            )
        shutil.copy2(src, transcript_path(cell.cell_dir))
        meta = {
            "task_id": cell.task_id,
            "arm": cell.arm,
            "seed": cell.seed,
            "driver": "fixture",
            "source": str(src),
            "returncode": 0,
            "timed_out": False,
            "duration_s": 0.0,
        }
        (cell.cell_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta


class DryRunDriver:
    """Writes the composed argv/env for the cell and stops. No transcript."""

    def __init__(self, inner: ClaudeCliDriver) -> None:
        self.inner = inner

    def run(self, cell: Cell) -> dict[str, Any]:
        cell.cell_dir.mkdir(parents=True, exist_ok=True)
        argv = self.inner.argv_for(cell.arm, cell)
        env = self.inner.env_for(cell.arm, cell)
        meta = {
            "task_id": cell.task_id,
            "arm": cell.arm,
            "seed": cell.seed,
            "driver": "dry-run",
            "argv": redact_argv(argv),
            "env": {
                k: ("<set>" if k == "LOOM_API_KEY" else v)
                for k, v in env.items()
                if k.startswith(("LOOM_", "BENCH_", "TMPDIR"))
            },
            "returncode": None,
            "timed_out": False,
            "duration_s": 0.0,
        }
        (cell.cell_dir / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta
