---
name: loom-investigate-ci
description: Take a failing Loom CI check from the red job to its root cause and a fix — fetch the failed log, decide whether main fails too, reproduce locally, and fix at the source. Use when a PR check is red, CI shows "no checks", a job is flaky, or asked "why is CI failing", "fix CI", "is this flaky".
argument-hint: "[pr-number | run-url]"
---

# Investigating Loom CI

## 1. Which job, which line

```bash
gh pr view <n> --json statusCheckRollup --jq '.statusCheckRollup[] | select(.conclusion | IN("SUCCESS", "SKIPPED", "NEUTRAL") | not) | "\((if (.conclusion // "") == "" then .status else .conclusion end))\t\(.name)\t\(.detailsUrl)"'
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
| `ruff + mypy-gate` (lint) | a generator `--check` is stale; ruff; function-length or mypy ratchet | `make lint`, `make typecheck`; run the named generator without `--check` and commit its output; split the long function |
| `pytest unit + coverage` | order dependence; a fake that drifted; a snapshot | `pytest -p randomly --randomly-seed=<seed from the log> <test>`; update a snapshot only for an intended change |
| `schema.sql in sync with migrations` | `schemas/schema.sql` dumped from another ClickHouse release, or not regenerated | dump against the release the run names (job "the ClickHouse release this run tests"): `LOOM_CLICKHOUSE_IMAGE=clickhouse/clickhouse-server:<ver> bash dev/ci_start_clickhouse.sh`, migrate, `make schema-dump` |
| `format` | an unformatted file | `make format` from the repository root (the pinned Biome reads `biome.json` there) |
| `coding-agent plugins` | a hook test, an unsynced Codex copy, a non-executable `bin/` helper | `node --test` in the plugin directory; `python scripts/codegen/agent_plugin_sync.py` |
| `tsc (frontend)`, `frontend tests` | types, bundle budget, an axe rule | `npm --prefix frontend run build && npm --prefix frontend run budget && npm --prefix frontend run test` |
| `pytest integration` | behaviour change; a test that needs an env CI lacks (self-signup) | the suite against a private server (`loom-dev-setup`); the failure report lists the last requests with `X-Request-Id` to find the server log line |

## 4. Flaky or real

A failure is flaky only when the same commit passes on re-run *and* you can name the race (timing, order, a shared resource). Re-run once with `gh run rerun <run-id> --failed`; a second failure is real. Name the cause in the PR even when it is flaky, and fix the test rather than adding retries.

## 5. Report

The failing job and line, the root cause, whether `main` is affected, the fix (or the commit that fixes it), and the local command that now passes.
