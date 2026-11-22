# Daily Cell acceptance repair

The Cell acceptance workflow runs daily at 02:43 UTC. This optional user service
checks the due nightly result at 06:13 UTC, after the job's 150-minute timeout and
an hour of queue grace. It uses the existing Codex ChatGPT login and does not
configure provider keys or billing. Account access and quota remain required.

Copy this directory into `~/loom-cell-nightly-agent` on the agent host. Supply:

- `settings.json`, with `checkout` naming the local repository checkout and
  `first_expected_nightly` naming the first expected 02:43 UTC run after rollout.
- `TASK.md`, containing the owner's repair authorization, source restrictions,
  repository instructions, review and CI requirements, and resource policy.

The task must prohibit production replay, paid providers, gate weakening and blind
unchanged reruns. It can authorize one qualification of a changed, reviewed commit
through the manual workflow and its owned fixtures. An open repair PR uses
`fix/cell-nightly-repair`; each observation inspects that PR and its current checks.

Install the service and timer into `~/.config/systemd/user`, enable user lingering
when the schedule must survive logout, then enable `loom-cell-nightly-check.timer`.
The timer is persistent. A catch-up uses the most recent due 06:13 UTC window and
cannot consume an upcoming window. Missing, failed, skipped and overdue runs leave
visible state in `state.json`; preflight errors remain resumable on a later window.

The repair runs in a private tmux server within the service cgroup. The monitor
verifies that cgroup before starting the runner and kills only its own server.
The service bounds time, memory and processes and kills its control group on exit.
Logs retain at most one MiB of runner output. State retains at most 128 source heads.

Unfinished repairs resume with one-day then two-day backoff. Three attempts with
unchanged source, PR head and CI evidence require attention; subsequent daily checks
still inspect the PR and report that disposition. Changed evidence permits another
bounded attempt. Authentication failures do not consume a repair attempt.

Run `python3 monitor.py --dry-run` for deterministic synthetic checks without any
model or remote calls. `--dry-run --tmux-probe` also verifies an immediately exiting
inert runner. `--dry-run --timeout-probe` starts an inert waiting runner for a short,
separate systemd timeout test. Never use the timeout probe outside a bounded service.
The normal service writes its last check, next check, observation and repair outcome
into `state.json`; `last-tmux-ownership.json` records its exact socket and cgroup.
