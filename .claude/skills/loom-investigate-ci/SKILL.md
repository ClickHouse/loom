---
name: loom-investigate-ci
description: Takes a failing Loom CI check to its cause and fix — the failed log, whether main fails too, a local reproduction. Use when a PR check is red, a PR shows no checks, a job looks flaky, or when asked why CI fails. Not for bugs found outside CI (loom-debug).
argument-hint: "[pr-number | run-url]"
metadata:
  internal: true
---

# Investigating Loom CI

## 1. Which job, which line

```bash
gh pr view <n> --json statusCheckRollup --jq '.statusCheckRollup[] | select((.conclusion // .state) | IN("SUCCESS", "SKIPPED", "NEUTRAL") | not) | "\((.conclusion // "") as $c | if $c == "" then (.state // .status) else $c end)\t\(.name // .context)\t\(.detailsUrl // .targetUrl)"'
gh run view <run-id> --log-failed | tail -200
```

No checks at all: the PR is stacked on a branch other than `main` (retarget it), has a merge conflict, or the push was a no-op.

## 2. Is it yours?

```bash
gh run list --workflow ci.yml --branch main -L 5 --json conclusion,headSha,createdAt
```

- `main` red on the same job: the cause is on `main`. Run the failing test on a clean `origin/main` worktree before touching your branch. A test with a literal date in its fixture and a wall-clock staleness check fails the first day the date ages out; the fix is the clock seam, not a new date.
- `main` green, your branch red: your change, or your change combined with something that merged since your base — rebase on `origin/main` and re-run locally.

## 3. By job

| Job | Usual cause | Reproduce / fix |
| --- | --- | --- |
| `the ClickHouse release this run tests` (`ci.yml`, `docker-smoke.yml`) | the release lookup in `dev/clickhouse_release.sh` or the registry it reads | rerun once; if it fails again, run the script locally and read its error |
| `Quickstart (<arch>)` (`docker-smoke.yml`, on PRs touching the paths its `paths:` filter lists) | the image or its bundle, the quickstart stack | the dashboard test dependencies CI installs first (`npm --prefix frontend ci && npx --prefix frontend playwright install chromium`, after `loom-dev-setup` §6's link check), `docker build -t "loom:smoke-$(basename "$PWD")" .`, then `python3 scripts/gates/docker_quickstart_smoke.py --image "loom:smoke-$(basename "$PWD")" --clickhouse-image <image>` (a tag per worktree: two sessions share the image store; `docker rmi` it afterwards), with the image from `bash dev/clickhouse_release.sh` |
| `Repository-job sandbox under the shipped profiles` (`docker-smoke.yml`) | the AppArmor or seccomp profile, or a sandbox change the profile blocks | the profiles row of `loom-pre-push` §3 |
| `ruff + mypy-gate` (lint) | a generator `--check` is stale (the plugin sync also compares executable bits); ruff; function-length or mypy ratchet | `make lint`, `make typecheck`; run the named generator without `--check` and commit its output; split the long function |
| `pytest unit + coverage` | order dependence; a fake that drifted; a snapshot; a migration number `main` took since your base (renumber per the `migrations` rule); a per-module coverage floor; `integrations/generic/test.sh` | `pytest -p randomly --randomly-seed=<seed from the log> <test>`; update a snapshot only for an intended change; the coverage-floor command in `loom-pre-push` §3; `PYTHON=.venv/bin/python bash integrations/generic/test.sh` |
| `repository-job sandbox (real bubblewrap)` | a change to the repository-job sandbox or the files it runs | the sandbox-suite row of `loom-pre-push` §3 |
| `schema.sql in sync with migrations` | `schemas/schema.sql` dumped from another ClickHouse release, or not regenerated | start a private ClickHouse on the release the run names (job "the ClickHouse release this run tests"): `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:<ver>` plus the name and ports of `loom-dev-setup` §3, `bash dev/ci_start_clickhouse.sh`; migrate; `make schema-dump` |
| `builds N-1 and N on one ClickHouse` (mixed-version) | N writes something N-1 cannot read, or a lease or bundle rule broken | `make test-mixed-version REQUIRE=1` (docker); [rolling upgrades](../../../docs/architecture/rolling-upgrades.md) names the rule |
| `format` | an unformatted file; a dashboard accessibility rule (step "biome lint (dashboard accessibility)") | `make format` from the repository root (the pinned Biome reads `biome.json` there); `make lint` for the accessibility rules, fixed in the TSX ([dashboard design](../../../docs/development/dashboard-design.md#checks)) |
| `helm chart (lint, kubeconform)` | a template or value that does not render, or a manifest that fails the schema | the `deploy/helm/` row of `loom-pre-push` §3 |
| `coding-agent plugins` | a hook, grep-shim or Cursor test; a syntax error in a shipped `.mjs`; a non-executable `integrations/grep-shim/bin/` entry point | the commands of the `integrations/` row in `loom-pre-push` §3, which mirror the job's steps |
| `tsc (frontend)` | types, bundle budget | `npm --prefix frontend run build && npm --prefix frontend run budget` |
| `frontend vitest k/2`, `frontend playwright k/3` | a vitest or axe failure, or the Playwright Graph smoke or layout-shift assertion | the part alone (`cd frontend && npx vitest run --shard=k/2`, or `E2E_PORT=<free port> npx playwright test --shard=k/3` there), or `npm --prefix frontend run test`, then `npx --prefix frontend playwright install chromium` once (its system libraries, `playwright install-deps`, only on a machine you own), then `E2E_PORT=<free port> npm --prefix frontend run e2e` |
| `typescript sdk (...)` | generated types stale, lint, a vitest failure, the ESM/CJS dist build, publint or attw | `.venv/bin/python -m scripts.codegen.sdk_ts_gen`, then `npm --prefix sdk/typescript run check && npm --prefix sdk/typescript run check:dist` |
| `python sdk wheel (...)` | an undeclared dependency or a 3.12-only API (3.11 legs), a pin floor lower than the code needs (lowest-deps legs), a new release of a dependency (the other legs) | the `sdk/python/` row of `loom-pre-push` §3, on the leg's Python and with `--resolution highest` unless the job name says lowest deps |
| `pytest integration` | behaviour change; a test that needs an env CI lacks (self-signup) | the suite against a private server (`loom-dev-setup`); the failure report lists the last requests with `X-Request-Id` to find the server log line |

## 4. Flaky or real

A failure is flaky only when the same commit passes on re-run *and* you can name the race (timing, order, a shared resource). Re-run once with `gh run rerun <run-id> --failed`; a second failure is real. Name the cause in the PR even when it is flaky, and fix the test rather than adding retries.

## 5. Report

The failing job and line, the root cause, whether `main` is affected, the fix (or the commit that fixes it), and the local command that now passes.
