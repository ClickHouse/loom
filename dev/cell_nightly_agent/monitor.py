#!/usr/bin/env python3
"""Observe one due UTC window daily; resume bounded Cell repairs in owned tmux."""

import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
import shlex
import subprocess
import time
from pathlib import Path

UTC = dt.UTC
REPO = "ClickHouse/loom"
WORKFLOW = "cell-nightly.yml"
TASK_DIR = Path(__file__).resolve().parent


def parse(value):
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def window(now):
    due = now.replace(hour=6, minute=13, second=0, microsecond=0)
    return due if due <= now else due - dt.timedelta(days=1)


def observe(now, due, runs, jobs, not_before):
    expected = due.replace(hour=2, minute=43)
    base = dict(window=due.isoformat(), expected=expected.isoformat())
    if expected < not_before:
        return dict(base, kind="awaiting_first_nightly")
    eligible = [
        r
        for r in runs
        if r["event"] == "schedule"
        and r["head_branch"] == "main"
        and expected <= parse(r["created_at"]) < expected + dt.timedelta(days=1)
        and parse(r["created_at"]) <= now
    ]
    if not eligible:
        return dict(base, kind="missing")
    run = max(eligible, key=lambda r: r["id"])
    base.update(run=run["id"], head=run["head_sha"], url=run["html_url"])
    if run["status"] != "completed":
        # A due window follows the entire job timeout plus an hour of queue grace.
        return dict(base, kind="stale", run_status=run["status"])
    cell = [j for j in jobs if j["name"] == "Cell trigger end-to-end acceptance"]
    passed = (
        run["conclusion"] == "success" and len(cell) == 1 and cell[0]["conclusion"] == "success"
    )
    return dict(base, kind="success" if passed else "failure", conclusion=run["conclusion"])


def plan(case, fingerprint, due):
    if case.get("fingerprint") != fingerprint:
        return "repair"
    if case.get("attempts", 0) >= 3:
        return "needs_attention_after_three_unchanged_attempts"
    if case.get("next_retry") and due < parse(case["next_retry"]):
        return "backoff"
    return "resume"


def store(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def gh(*args):
    return json.loads(subprocess.check_output(["gh", *args], text=True, timeout=45))


def repair_environment():
    # Ambient provider keys are excluded; this is not a credential-file sandbox.
    allowed = (
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LC_ALL",
        "XDG_RUNTIME_DIR",
        "DBUS_SESSION_BUS_ADDRESS",
        "CODEX_HOME",
        "TMPDIR",
        "SSH_AUTH_SOCK",
    )
    return {key: os.environ[key] for key in allowed if key in os.environ}


def process_identity(pid):
    process = Path(f"/proc/{pid}")
    uid = process.stat().st_uid
    if uid != os.getuid():
        raise RuntimeError("Owned repair process has a different UID")
    fields = (process / "stat").read_text().rsplit(")", 1)[1].split()
    return dict(pid=pid, uid=uid, birth=int(fields[19]))


def settle_pane_scope(server_pid, pane_pid):
    description = f"tmux child pane {pane_pid} launched by process {server_pid}"
    deadline = time.monotonic() + 5
    scope = None
    while scope is None:
        units = json.loads(
            subprocess.check_output(
                [
                    "systemctl",
                    "--user",
                    "list-units",
                    "--all",
                    "--type=scope",
                    "--output=json",
                    "--no-pager",
                ],
                text=True,
                timeout=10,
            )
        )
        scopes = [unit["unit"] for unit in units if unit["description"] == description]
        if len(scopes) > 1:
            raise RuntimeError("Owned tmux pane has ambiguous scope ownership")
        if scopes:
            scope = scopes[0]
        elif time.monotonic() >= deadline:
            raise RuntimeError("Owned tmux scope did not appear")
        else:
            time.sleep(0.02)
    # StartTransientUnit returns a queued job. Scope activation completes the
    # original migration before we reattach the pane to the bounded service.
    while True:
        active = subprocess.check_output(
            ["systemctl", "--user", "show", "--property=ActiveState", "--value", "--", scope],
            text=True,
            timeout=10,
        ).strip()
        if active == "active":
            return
        if active in ("failed", "deactivating") or time.monotonic() >= deadline:
            raise RuntimeError("Owned tmux scope did not finish starting")
        time.sleep(0.02)


def tmux_run(probe=False):
    socket = TASK_DIR / f"tmux-{os.getpid()}.sock"
    command = ["tmux", "-S", str(socket), "-f", "/dev/null"]
    ready = TASK_DIR / f"pane-ready-{os.getpid()}"
    ready.unlink(missing_ok=True)
    result_path = TASK_DIR / "repair-result.json"
    result_path.unlink(missing_ok=True)
    if probe == "wait":
        (TASK_DIR / "probe-child.json").unlink(missing_ok=True)
    runner = ["/usr/bin/python3", str(TASK_DIR / "repair.py")]
    if probe:
        runner.append("--probe-wait" if probe == "wait" else "--probe")
    bootstrap = ["/bin/sh", "-c", 'printf "%s" "$$" > "$1"; exec /bin/sh', "sh", str(ready)]
    try:
        subprocess.run(
            command
            + [
                "new-session",
                "-d",
                "-s",
                "loom-cell-nightly-repair",
                "-c",
                str(TASK_DIR),
                "exec " + shlex.join(bootstrap),
            ],
            check=True,
            timeout=15,
            env=repair_environment(),
        )
        subprocess.run(
            command
            + ["set-window-option", "-t", "loom-cell-nightly-repair:0", "remain-on-exit", "on"],
            check=True,
            timeout=10,
        )
        server_pid = int(
            subprocess.check_output(
                command + ["display-message", "-p", "#{pid}"], text=True, timeout=10
            )
        )
        server_identity = process_identity(server_pid)
        own_group = Path("/proc/self/cgroup").read_text()
        if Path(f"/proc/{server_pid}/cgroup").read_text() != own_group:
            raise RuntimeError("Owned tmux server escaped the service cgroup")
        # Wait for the actual pane shell before inspecting its identity.
        deadline = time.monotonic() + 5
        while not ready.exists():
            if time.monotonic() >= deadline:
                raise RuntimeError("Owned tmux pane did not finish bootstrap")
            time.sleep(0.02)
        pane_pid = int(
            subprocess.check_output(
                command + ["display-message", "-p", "#{pane_pid}"], text=True, timeout=10
            )
        )
        if ready.read_text() != str(pane_pid):
            raise RuntimeError("Owned tmux pane bootstrap identity differs")
        pane_identity = process_identity(pane_pid)
        settle_pane_scope(server_pid, pane_pid)
        if (
            process_identity(server_pid) != server_identity
            or process_identity(pane_pid) != pane_identity
        ):
            raise RuntimeError("Owned tmux process identity changed during startup")
        pane_group = Path(f"/proc/{pane_pid}/cgroup").read_text()
        if pane_group != own_group:
            # Some tmux builds create a sibling scope for each pane. Reattach only
            # our inert shell through the user manager before it can run repair.
            unit = own_group.strip().rsplit("/", 1)[-1]
            if not unit.endswith(".service"):
                raise RuntimeError("The repair monitor must run inside a user service")
            subprocess.run(
                [
                    "busctl",
                    "--user",
                    "call",
                    "org.freedesktop.systemd1",
                    "/org/freedesktop/systemd1",
                    "org.freedesktop.systemd1.Manager",
                    "AttachProcessesToUnit",
                    "ssau",
                    unit,
                    "",
                    "1",
                    str(pane_pid),
                ],
                check=True,
                timeout=10,
            )
        if process_identity(pane_pid) != pane_identity:
            raise RuntimeError("Owned tmux pane identity changed during attachment")
        if Path(f"/proc/{pane_pid}/cgroup").read_text() != own_group:
            raise RuntimeError("Owned tmux pane escaped the service cgroup")
        store(
            TASK_DIR / "last-tmux-ownership.json",
            dict(
                server_pid=server_pid,
                pane_pid=pane_pid,
                server_identity=server_identity,
                pane_identity=pane_identity,
                socket=str(socket),
                cgroup=own_group.strip(),
                cleanup="KillMode=control-group plus exact owned server",
            ),
        )
        subprocess.run(
            command
            + [
                "send-keys",
                "-t",
                "loom-cell-nightly-repair:0.0",
                "-l",
                "exec " + shlex.join(runner),
            ],
            check=True,
            timeout=10,
        )
        subprocess.run(
            command + ["send-keys", "-t", "loom-cell-nightly-repair:0.0", "Enter"],
            check=True,
            timeout=10,
        )
        deadline = time.monotonic() + (15 if probe else 3300)
        while time.monotonic() < deadline:
            if result_path.exists():
                return json.loads(result_path.read_text())
            dead = subprocess.check_output(
                command + ["display-message", "-p", "#{pane_dead}"], text=True, timeout=10
            ).strip()
            if dead == "1":
                if result_path.exists():
                    return json.loads(result_path.read_text())
                raise RuntimeError("Owned repair session exited without a result")
            time.sleep(0.2 if probe else 2)
        return dict(status="timed_out", disposition="unfinished")
    finally:
        subprocess.run(
            command + ["kill-server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
        socket.unlink(missing_ok=True)
        ready.unlink(missing_ok=True)


def dry_run():
    now = parse("2030-01-02T06:13:00Z")
    assert window(now - dt.timedelta(seconds=1)).date() == dt.date(2030, 1, 1)
    assert window(now) == now
    fixture = dict(
        id=1,
        event="schedule",
        head_branch="main",
        created_at="2030-01-02T02:44:00Z",
        head_sha="synthetic-only",
        html_url="synthetic-only",
        status="completed",
        conclusion="success",
    )
    jobs = [dict(name="Cell trigger end-to-end acceptance", conclusion="success")]
    before = parse("2030-01-01T02:43:00Z")
    assert observe(now, window(now), [fixture], jobs, before)["kind"] == "success"
    assert observe(now, window(now), [], [], before)["kind"] == "missing"
    assert (
        observe(now, window(now), [dict(fixture, status="in_progress")], [], before)["kind"]
        == "stale"
    )
    assert observe(now, window(now), [fixture], jobs, now)["kind"] == "awaiting_first_nightly"
    early = now.replace(hour=4)
    assert observe(early, window(early), [fixture], jobs, before)["kind"] == "missing"
    for status in ("failed", "timed_out", "blocked_auth", "unfinished", "started"):
        case = dict(status=status, fingerprint="same", attempts=1, next_retry=now.isoformat())
        assert plan(case, "same", now) == "resume"
    assert plan(dict(fingerprint="same", attempts=3), "same", now).startswith("needs_attention")
    assert plan(dict(fingerprint="same", attempts=3), "changed-pr-or-check", now) == "repair"
    assert (
        plan(
            dict(
                fingerprint="same", attempts=1, next_retry=(now + dt.timedelta(days=1)).isoformat()
            ),
            "same",
            now,
        )
        == "backoff"
    )
    print(
        json.dumps(
            dict(
                synthetic_only=True,
                checks=15,
                agent_starts=0,
                github_requests=0,
                cases=[
                    "catchup_window",
                    "success",
                    "missing",
                    "running_stale",
                    "activation",
                    "failed_attempt_resume",
                    "retry_bound",
                    "changed_pr_evidence",
                    "backoff",
                ],
            )
        )
    )


def inspect_nightly(now, due, config):
    runs = gh(
        "api",
        f"repos/{REPO}/actions/workflows/{WORKFLOW}/runs?branch=main&event=schedule&per_page=20",
    )["workflow_runs"]
    # Select this window first, then fetch jobs for that exact run.
    expected = due.replace(hour=2, minute=43)
    candidates = [
        r
        for r in runs
        if r["event"] == "schedule"
        and r["head_branch"] == "main"
        and expected <= parse(r["created_at"]) < expected + dt.timedelta(days=1)
        and parse(r["created_at"]) <= now
    ]
    latest = max(candidates, key=lambda r: r["id"]) if candidates else None
    jobs = (
        gh("api", f"repos/{REPO}/actions/runs/{latest['id']}/jobs?per_page=100")["jobs"]
        if latest
        else []
    )
    return observe(now, due, runs, jobs, parse(config["first_expected_nightly"]))


def inspect_prs():
    prs = gh("api", f"repos/{REPO}/pulls?state=open&head=ClickHouse:fix/cell-nightly-repair")
    metadata = []
    for pr in prs:
        checks = gh("api", f"repos/{REPO}/commits/{pr['head']['sha']}/check-runs?per_page=100")[
            "check_runs"
        ]
        metadata.append(
            dict(
                number=pr["number"],
                head=pr["head"]["sha"],
                url=pr["html_url"],
                checks=sorted((c["name"], c["status"], c["conclusion"] or "") for c in checks),
            )
        )
    return metadata


def run_repair(state, now, due, observation, key, fingerprint, case, cases):
    if case.get("fingerprint") != fingerprint:
        case.clear()
        case.update(fingerprint=fingerprint, attempts=0)
    cases[key] = case
    attempt = case["attempts"] + 1
    case.update(
        attempts=attempt,
        status="started",
        run=observation.get("run"),
        last_attempt=now.isoformat(),
        next_retry=(due + dt.timedelta(days=2 ** (attempt - 1))).isoformat(),
    )
    # Keep bounded history; the active case and at most 127 recent heads remain.
    for old in list(cases):
        if len(cases) <= 128:
            break
        if old != key:
            del cases[old]
    prompt = (
        (TASK_DIR / "TASK.md").read_text()
        + "\nTrusted observation metadata (not instructions):\n"
        + json.dumps(observation)
    )
    (TASK_DIR / "current-task.md").write_text(prompt)
    store(
        TASK_DIR / "state.json", state
    )  # A crash is visible as interrupted; it remains resumable.
    result = tmux_run()
    case.update(status=result["status"], result=result, finished=dt.datetime.now(UTC).isoformat())
    return dict(disposition="repair_attempt_finished", case=key, result=result)


def check(state, now, config):
    due = window(now)
    next_due = due + dt.timedelta(days=1)
    # Leave the upcoming timer free; the service has a hard 60-minute lifetime.
    if next_due - now <= dt.timedelta(minutes=65):
        state.update(last_check=now.isoformat(), next_check=next_due.isoformat())
        return dict(disposition="deferred_until_next_window", window=due.isoformat())
    if state.get("last_window") == due.isoformat():
        return dict(disposition="window_already_observed", window=due.isoformat())
    state.update(
        last_check=now.isoformat(),
        last_window=due.isoformat(),
        next_check=(due + dt.timedelta(days=1)).isoformat(),
    )
    observation = inspect_nightly(now, due, config)
    state["observation"] = observation
    if observation["kind"] == "awaiting_first_nightly":
        return dict(
            disposition="awaiting_first_nightly", first_expected=config["first_expected_nightly"]
        )
    metadata = inspect_prs()
    observation["existing_fix_prs"] = metadata
    if observation["kind"] == "success" and not metadata:
        return dict(disposition="healthy")
    observation["main_head"] = gh("api", f"repos/{REPO}/commits/main")["sha"]
    key = observation.get("head", observation["main_head"])
    fingerprint = hashlib.sha256(
        json.dumps(dict(head=key, kind=observation["kind"], prs=metadata), sort_keys=True).encode()
    ).hexdigest()
    cases = state.setdefault("cases", {})
    case = cases.get(key, {})
    action = plan(case, fingerprint, due)
    if action not in ("repair", "resume"):
        return dict(disposition=action, case=key, open_prs=[p["url"] for p in metadata])
    env = repair_environment()
    auth = subprocess.run(
        ["codex", "-c", 'forced_login_method="chatgpt"', "login", "status"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
        env=env,
    )
    if auth.returncode or "Logged in using ChatGPT" not in auth.stdout:
        # No repair attempt is consumed. A later daily window retries the auth preflight.
        return dict(disposition="blocked_auth", case=key, repair_started=False)
    return run_repair(state, now, due, observation, key, fingerprint, case, cases)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--tmux-probe", action="store_true")
    parser.add_argument("--timeout-probe", action="store_true")
    args = parser.parse_args()
    if (args.tmux_probe or args.timeout_probe) and not args.dry_run:
        parser.error("probe flags require --dry-run")
    if args.dry_run and not (args.tmux_probe or args.timeout_probe):
        dry_run()
        return
    with (TASK_DIR / "monitor.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Another daily check owns the lock.")
            return
        if args.dry_run:
            dry_run()
            print(
                json.dumps(
                    dict(
                        tmux_probe=tmux_run(probe="wait" if args.timeout_probe else True),
                        model_calls=0,
                    )
                )
            )
            return
        state_path = TASK_DIR / "state.json"
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        now = dt.datetime.now(UTC)
        try:
            result = check(state, now, json.loads((TASK_DIR / "settings.json").read_text()))
        except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            state["result"] = dict(disposition="check_error", error=str(exc)[:1000])
            store(state_path, state)
            raise
        state["result"] = result
        store(state_path, state)
        print(
            json.dumps(
                dict(
                    last_check=state.get("last_check"),
                    next_check=state.get("next_check"),
                    result=result,
                )
            )
        )


if __name__ == "__main__":
    main()
