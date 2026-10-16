---
name: loom-bench
description: Run, read and report a Loom benchmark — when a run is allowed, which harness answers which question, how to run it against the right code, how to judge a delta against noise, and how results are written up. Use when asked to benchmark, measure recall or accuracy, compare two configurations, A/B a ranking change, check a model or embedder change, or update docs/benchmarks/results.md. NOT for unit-test speed or CI timing.
argument-hint: "<question to answer>"
---

# Loom benchmarks

## 0. Ask first

A benchmark run spends provider money and hours of wall clock. Start one only when the requester asked for that run. "Keep the bench numbers" means the existing numbers. Offline harnesses and `--help` are free; anything that ingests or judges is not.

## 1. Pick the harness

Name the question first ("does rerank X raise recall@10 on multi-hop without losing single-hop?"), then the harness and the arms that answer it: [running](../../../docs/benchmarks/running.md) lists them, [methodology](../../../docs/benchmarks/methodology.md) says what each measures. Prefer one paired run (`--ask-arms`, `--repeats 3 --gate`) over two separate runs: one ingest, per-item deltas.

## 2. Run against the code you mean

```bash
PYTHONPATH=$PWD .venv/bin/python bench/<harness>.py --help
curl -s "$LOOM_BASE_URL/v1/health" | jq '{build_commit, status}'   # the server runs your build
```

- The server is a private one started from this worktree (`loom-dev-setup`) with real providers; fake providers make every score meaningless.
- A cached answer path can replay an older build's answers: confirm the cache key covers the setting under test, or turn the cache off for the run.
- Bench namespaces are erased at the end of the run; pass `--keep-namespaces` only when a re-score is planned.

## 3. Prove the treatment ran

Before reading a single score, check from the run's own output or the server's ledger that arm B did what B means: the setting was applied, the tool was called, the leg returned rows. A "no difference" or a big win from an arm that never ran is the most common false result.

## 4. Judge the delta

- A change ships on the **lower bound** of the paired, clustered bootstrap, not the mean ([the verdict](../../../docs/benchmarks/methodology.md#the-verdict)).
- `unresolved` inside the null band is noise; outside it the run is under-powered — add items, not repeats.
- An A/A run of the same arm twice is the noise reference for a new harness or judge.
- Report every category, including the one that went down.

## 5. Write it up

- The PR body carries the numbers, the command that reproduces them, the model and judge, and the item count.
- `docs/benchmarks/results.md` names files by bench and sections by model, never by commit or date (history is rewritten; commit ids rot).
- A measurement never goes in a code comment.
- Keep run outputs (`bench/matrix_runs/`, `MATRIX_RESULTS.md`, `/tmp/*.csv`) out of commits.
