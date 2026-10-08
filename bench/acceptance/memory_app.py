"""Acceptance bench — the conversational memory application:
ask-heavy, one namespace per end user, cost-sensitive.

Precision first, two slices:

  stale retrieval   the proactive-interference paradigm of `bench/pi_llm.py`
                    (an attribute updated n times, then asked for its current
                    value): latest-value accuracy, the stale-retrieval rate
                    and `clean_correct` (latest present, no stale value) at
                    depth >= 10 — deterministic, no judge.
  knowledge update  `bench/longmemeval.py --question-type knowledge-update`
                    with the official judge and `--stale-check`, judged
                    accuracy on n items (default 30; `--lme-limit 0` skips —
                    the slice costs reader + judge spend and needs
                    OPENAI_API_KEY; the ledger below never sees that spend).

Economy second: `memory.ask` dollars per answer and p50 wall, read from the
deployment's cost ledger (`cost.summary`) for the bench's own namespaces.

Two optional modes:

  --follow-next     the revision-path arm: every PI
                    update goes through `memory.set` under a fresh key
                    (`--pi-ingest set`); the `follow_next` arm first asks with
                    `dry_run=true` and, when the verdict is `conflict` with a
                    `next` naming `memory.update(<peer key>)`, revises that row
                    instead of writing a copy; the `control` arm writes the
                    copy and ignores `next`. Both arms run interleaved on the
                    same server; the report carries each arm's rollup, the
                    verdict distribution and the paired delta of
                    `clean_correct` at depth >= 10 with a bootstrap CI.
  --replay NS       the tenant's shape: read `--replay-n` real `memory.ask`
                    queries of namespace NS from the ledger (`agent_events.args
                    ['query']`, read-only, `--env-file` for ClickHouse), ask
                    them again against the deployment under test with
                    `reinforce=false` (an evaluation read credits nothing),
                    and report the tenant's own ledger p50 and
                    $/ask beside the replay's client p50. Information, not a
                    gate: it is the number a memory application would see.

Every server call of the stale-retrieval slice goes through
`loom.reference_client.ReferenceClient` (consumer tag, pacing, fail-soft,
outcome feedback: `memory.outcome used` on the keys a correct answer cited).
The bench's namespaces are registered as bench and erased by explicit list when
the run ends, also when it fails (`--keep-namespaces` keeps them).

    python bench/acceptance/memory_app.py --base-url http://127.0.0.1:7893 \\
        --token-file bench.token --pi-episodes 8 --pi-depths 5,10,15 --lme-limit 10
    python bench/acceptance/memory_app.py ... --follow-next --lme-limit 0
    python bench/acceptance/memory_app.py ... --replay <namespace> --replay-n 30
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import datetime as dt
import os
import random
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "sdk" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from loom.reference_client import ReferenceClient  # noqa: E402

from bench import pi_llm  # noqa: E402
from bench.acceptance._common import (  # noqa: E402
    ROOT,
    Metric,
    Report,
    add_common_args,
    bench_post,
    erase_namespaces,
    erasure_note,
    finish,
    ledger_cost_by_op,
    make_client,
    nonce,
    p50,
    ratio,
)
from bench.bench_namespaces import register_bench_namespace  # noqa: E402

ARMS = ("control", "follow_next")
# Floors per PI slice: the level this build served on the slice, rounded down;
# `--min-*` / `--max-*` flags override. A floor is only valid for the depths it
# was measured on: floors from 5/10/15 do not hold at 10/15/20.
PI_FLOORS: dict[str, dict[str, float]] = {
    "10,15,20": {"min_latest_accuracy": 0.20, "max_stale_rate": 0.80, "min_clean_correct": 0.20},
    "5,10,15": {"min_latest_accuracy": 0.50, "max_stale_rate": 0.70, "min_clean_correct": 0.50},
}
CLEAN_CORRECT_FLOOR = PI_FLOORS["10,15,20"]["min_clean_correct"]


def resolve_pi_floors(args: argparse.Namespace) -> argparse.Namespace:
    """Fill the three PI floors from the preset for `--pi-depths` where the
    caller left them unset; an unknown slice takes the default slice's
    floors and the report says so (pure)."""
    key = ",".join(str(int(x)) for x in str(args.pi_depths).split(",") if x.strip())
    preset = PI_FLOORS.get(key) or PI_FLOORS["10,15,20"]
    args.pi_floors_slice = key if key in PI_FLOORS else f"{key} (no preset; 10,15,20 floors)"
    for k, v in preset.items():
        if getattr(args, k, None) is None:
            setattr(args, k, v)
    return args


# ---------------------------------------------------------------------------
# stale retrieval (pure scoring + one episode through the reference client)
# ---------------------------------------------------------------------------


def score_pi_answer(answer: str, ep: int, depth: int) -> dict[str, Any]:
    """pi_llm's verdict for one answer (pure): latest present, any stale
    present, clean, abstain."""
    a = (answer or "").lower()
    latest = pi_llm._val(ep, depth - 1).lower()
    stale_vals = [pi_llm._val(ep, k).lower() for k in range(depth - 1)]
    has_latest = latest in a
    has_stale = any(sv in a for sv in stale_vals)
    return {
        "has_latest": has_latest,
        "has_stale": has_stale,
        "clean_correct": has_latest and not has_stale,
        "abstain": not (has_latest or has_stale),
    }


def pi_rollup(rows: list[dict[str, Any]], *, min_depth: int = 10) -> dict[str, Any]:
    """Latest-value accuracy over every scored episode; stale rate and
    `clean_correct_deep` over the episodes at depth >= min_depth (the PI
    failure the bench exists for); `by_depth` carries accuracy / stale /
    clean / n per depth; `verdicts` the write verdicts the arm saw."""
    ok = [r for r in rows if not r.get("error")]
    deep = [r for r in ok if int(r["depth"]) >= min_depth]
    by_depth: dict[int, dict[str, float | None]] = {}
    for d in sorted({int(r["depth"]) for r in ok}):
        at = [r for r in ok if int(r["depth"]) == d]
        by_depth[d] = {
            "accuracy": ratio(sum(1 for r in at if r["has_latest"]), len(at)),
            "stale_rate": ratio(sum(1 for r in at if r["has_stale"]), len(at)),
            "clean_correct": ratio(sum(1 for r in at if r["clean_correct"]), len(at)),
            "n": float(len(at)),
        }
    verdicts: Counter[str] = Counter()
    followed = 0
    for r in ok:
        verdicts.update(r.get("verdicts") or {})
        followed += int(r.get("followed") or 0)
    return {
        "by_depth": by_depth,
        "accuracy": ratio(sum(1 for r in ok if r["has_latest"]), len(ok)),
        "stale_rate": ratio(sum(1 for r in deep if r["has_stale"]), len(deep)),
        "clean_correct": ratio(sum(1 for r in ok if r["clean_correct"]), len(ok)),
        "clean_correct_deep": ratio(sum(1 for r in deep if r["clean_correct"]), len(deep)),
        "abstain": ratio(sum(1 for r in ok if r["abstain"]), len(ok)),
        "n": float(len(ok)),
        "n_deep": float(len(deep)),
        "errors": float(len(rows) - len(ok)),
        "verdicts": dict(verdicts),
        "followed": float(followed),
    }


# `{key!r}`: single quotes, or double quotes when the key holds an apostrophe
_NEXT_UPDATE = re.compile(
    r"""memory\.update\(memory_key=(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")"""
)


def follow_next_target(resp: dict[str, Any]) -> str | None:
    """The peer key a `memory.set` response tells the caller to revise
    (its `next`), or None: only a `conflict` verdict whose `next`
    names `memory.update(memory_key='<key>', ...)` drives revision. A clear,
    duplicate, merge or refines verdict — or a hint naming another verb —
    leaves the caller on its own path (pure)."""
    if str(resp.get("conflict_verdict") or "") != "conflict":
        return None
    m = _NEXT_UPDATE.search(str(resp.get("next") or ""))
    if not m:
        return None
    key = m.group(1) if m.group(1) is not None else m.group(2)
    return key.replace("\\'", "'").replace('\\"', '"') or None


_PI_EPOCH = dt.date(2026, 1, 1)


def _pi_text(entity: str, k: int, v: str) -> tuple[str, str]:
    """(observation_date, sentence) of the k-th update. The date is 1 + k days
    after 1 January, so it strictly increases at any depth and valid_at orders
    the updates the way the bench scores them (the last one is current); the
    same rule as pi_llm.update_text."""
    date = (_PI_EPOCH + dt.timedelta(days=1 + k)).isoformat()
    text = (
        f"Project status update: as of {date}, {entity}'s assigned module "
        f"is now {v} (this supersedes any earlier assignment)."
    )
    return date, text


async def pi_write(
    client: ReferenceClient,
    ns: str,
    entity: str,
    ep: int,
    k: int,
    *,
    ingest: str,
    arm: str,
    verdicts: Counter[str],
) -> tuple[bool, str, bool]:
    """One PI update. `extract` goes through `memory.set_from_messages` (the
    bench's default); `set` writes the sentence under a fresh key. The
    `follow_next` arm dry-runs the set first and revises the peer the verdict
    names instead of writing a copy. Returns (ok, error, followed)."""
    v = pi_llm._val(ep, k)
    date, text = _pi_text(entity, k, v)
    if ingest == "extract":
        r = await client.call(
            "memory.set_from_messages",
            {
                "namespace": ns,
                "agent": "pi",
                "user_id": f"ep{ep}",
                "session_id": f"ep{ep}-s{k}",
                "messages": [{"role": "user", "content": text}],
                "max_tokens": 1024,
                "observation_date": date,
            },
            timeout_s=180.0,
        )
        return r.ok, f"{r.code}: {r.error}" if not r.ok else "", False
    body = {
        "namespace": ns,
        "memory_key": f"{entity.lower()}-status-{k}",
        "value": text,
        "memory_type": "semantic",
        "classify": False,
        "valid_at": f"{date}T12:00:00",
    }
    if arm == "follow_next":
        dry = await client.call("memory.set", {**body, "dry_run": True}, timeout_s=120.0)
        if not dry.ok:
            return False, f"dry_run {dry.code}: {dry.error}", False
        verdicts[f"dry:{dry.data.get('conflict_verdict') or 'none'}"] += 1
        target = follow_next_target(dry.data)
        if target is not None:
            up = await client.call(
                "memory.update",
                {
                    "namespace": ns,
                    "memory_key": target,
                    "value": text,
                    "reason": (
                        f"follow-next: set dry_run answered conflict with {target!r}; "
                        f"revised in place (bench pi ep{ep} k{k})"
                    ),
                },
                timeout_s=120.0,
            )
            if not up.ok:
                return False, f"update {up.code}: {up.error}", False
            verdicts[f"update:{up.data.get('conflict_verdict') or 'none'}"] += 1
            return True, "", True
    r = await client.call("memory.set", body, timeout_s=120.0)
    if not r.ok:
        return False, f"{r.code}: {r.error}", False
    verdicts[f"set:{r.data.get('conflict_verdict') or 'none'}"] += 1
    return True, "", False


async def pi_episode(
    client: ReferenceClient,
    ns: str,
    ep: int,
    depth: int,
    *,
    outcomes: bool,
    ingest: str = "extract",
    arm: str = "control",
) -> dict[str, Any]:
    entity = pi_llm._ENTITIES[ep % len(pi_llm._ENTITIES)] + f"{ep}"
    verdicts: Counter[str] = Counter()
    followed = 0
    for k in range(depth):
        ok, err, did_follow = await pi_write(
            client, ns, entity, ep, k, ingest=ingest, arm=arm, verdicts=verdicts
        )
        if not ok:
            return {"ep": ep, "depth": depth, "arm": arm, "error": err}
        followed += int(did_follow)
    q = f"What module is {entity} currently assigned to? Give the current module only."
    ans = await client.call(
        "memory.ask",
        {"namespace": ns, "query": q, "reinforce": False},
        required=("answer",),
        timeout_s=120.0,
    )
    if not ans.ok:
        return {"ep": ep, "depth": depth, "arm": arm, "error": f"{ans.code}: {ans.error}"}
    row: dict[str, Any] = {
        "ep": ep,
        "depth": depth,
        "arm": arm,
        "source": ans.data.get("source"),
        "elapsed_ms": ans.elapsed_ms,
        "verdicts": dict(verdicts),
        "followed": followed,
        **score_pi_answer(str(ans.data.get("answer") or ""), ep, depth),
    }
    if outcomes and row["clean_correct"]:
        for key in (ans.data.get("memory_keys") or [])[:5]:
            await client.call(
                "memory.outcome",
                {
                    "namespace": ns,
                    "agent": "*",
                    "memory_key": str(key),
                    "outcome": "used",
                    "source": client.consumer,
                },
            )
    return row


def paired_delta(
    rows: list[dict[str, Any]],
    *,
    metric: str = "clean_correct",
    arms: tuple[str, str] = ARMS,
    min_depth: int = 10,
    draws: int = 2000,
    seed: int = 4370,
) -> dict[str, float | None]:
    """Paired difference arms[1] − arms[0] of a 0/1 metric over the (ep,
    depth) pairs both arms scored at depth >= min_depth, with a percentile
    bootstrap 95% CI over the pairs (pure, seeded). `lb > 0` is the gate."""
    a, b = arms
    by: dict[tuple[int, int], dict[str, float]] = {}
    for r in rows:
        if r.get("error") or int(r["depth"]) < min_depth:
            continue
        by.setdefault((int(r["ep"]), int(r["depth"])), {})[str(r.get("arm"))] = float(
            bool(r[metric])
        )
    diffs = [v[b] - v[a] for v in by.values() if a in v and b in v]
    if not diffs:
        return {"delta": None, "lb": None, "ub": None, "n_pairs": 0.0}
    rng = random.Random(seed)
    n = len(diffs)
    means = sorted(sum(rng.choice(diffs) for _ in range(n)) / n for _ in range(draws))
    return {
        "delta": sum(diffs) / n,
        "lb": means[int(0.025 * draws)],
        "ub": means[min(draws - 1, int(0.975 * draws))],
        "n_pairs": float(n),
    }


# ---------------------------------------------------------------------------
# tenant replay: the queries a real memory app asks
# ---------------------------------------------------------------------------


def _ch_client(env_file: str) -> Any:
    from bench.code_intel_mine_prs import _client

    return _client(env_file)


def plain_queries(items: list[str]) -> tuple[list[str], int]:
    """Drop the queries the ledger holds only as a digest — `sha256:<hash>/<len>`
    is what a namespace without capture consent records — and count them
    (pure). A tenant whose queries are all digests has a ledger shape but no
    replayable text; the report says so instead of asking the hashes."""
    plain = [q for q in items if not q.startswith("sha256:")]
    return plain, len(items) - len(plain)


def pick_evenly(items: list[str], n: int) -> list[str]:
    """n items evenly spaced over the list (all of them when it is shorter);
    deterministic, order kept (pure)."""
    if n <= 0 or not items:
        return []
    if len(items) <= n:
        return list(items)
    step = len(items) / n
    return [items[int(i * step)] for i in range(n)]


def replay_queries(
    ch: Any, org: str, namespace: str, *, n: int, days: int
) -> tuple[list[str], dict[str, float | None]]:
    """Read-only: the tenant's distinct `memory.ask` queries over the window
    (`agent_events.args['query']`, newest first, evenly sampled to n) and the
    tenant's own ledger for the same window — ask p50 wall and dollars per ask
    as the deployment billed them."""
    sql_q = """
        SELECT q FROM (
            SELECT args['query'] AS q, max(ts) AS last_ts
            FROM loom.agent_events
            WHERE org = {org:String} AND namespace = {ns:String}
              AND operation = 'memory.ask' AND kind = 'request'
              AND ts > now() - INTERVAL {days:UInt32} DAY
              AND length(args['query']) BETWEEN 8 AND 2000
            GROUP BY q
        ) ORDER BY last_ts DESC LIMIT 5000
    """
    sql_l = """
        SELECT count() AS ops, quantile(0.5)(latency_ms) AS p50_ms,
               sum(upstream_cost_usd) AS cost_usd, countIf(success = false) AS errors
        FROM loom.agent_events
        WHERE org = {org:String} AND namespace = {ns:String}
          AND operation = 'memory.ask' AND kind = 'request'
          AND ts > now() - INTERVAL {days:UInt32} DAY
    """
    params = {"org": org, "ns": namespace, "days": days}
    qs, hashed = plain_queries([str(r[0]) for r in ch.query(sql_q, parameters=params).result_rows])
    led = ch.query(sql_l, parameters=params).result_rows
    ops = float(led[0][0]) if led else 0.0
    ledger = {
        "ops": ops,
        "p50_ms": float(led[0][1]) if led and ops else None,
        "usd_per_ask": ratio(float(led[0][2]), ops) if led else None,
        "errors": float(led[0][3]) if led else 0.0,
        "hashed_queries": float(hashed),
        "plain_queries": float(len(qs)),
    }
    return pick_evenly(qs, n), ledger


async def run_replay(
    client: ReferenceClient, namespace: str, queries: list[str], *, concurrency: int
) -> dict[str, Any]:
    """Ask the tenant's queries again against the deployment under test.
    `reinforce=false`: an evaluation read credits nothing; the
    tenant's rows are read, never written."""
    sem = asyncio.Semaphore(concurrency)

    async def one(q: str) -> dict[str, Any]:
        async with sem:
            r = await client.call(
                "memory.ask",
                {"namespace": namespace, "query": q, "reinforce": False},
                timeout_s=180.0,
            )
        return {
            "ok": r.ok,
            "elapsed_ms": r.elapsed_ms,
            "abstain": bool(r.data.get("abstain")) if r.ok else None,
            "code": r.code,
        }

    rows = await asyncio.gather(*[one(q) for q in queries])
    ok = [r for r in rows if r["ok"]]
    return {
        "n": float(len(rows)),
        "errors": float(len(rows) - len(ok)),
        "p50_ms": p50([float(r["elapsed_ms"]) for r in ok]),
        "abstain_rate": ratio(sum(1 for r in ok if r["abstain"]), len(ok)),
    }


# ---------------------------------------------------------------------------
# knowledge update (LongMemEval slice through the existing bench)
# ---------------------------------------------------------------------------


def lme_command(args: argparse.Namespace, out_csv: Path) -> list[str]:
    return [
        sys.executable,
        str(ROOT / "bench" / "longmemeval.py"),
        "--base-url",
        args.base_url,
        "--dataset",
        args.lme_dataset,
        "--question-type",
        "knowledge-update",
        "--shuffle",
        "--seed",
        str(args.lme_seed),
        "--limit",
        str(args.lme_limit),
        "--top-k",
        str(args.lme_top_k),
        "--answer-judge",
        "--stale-check",
        "--concurrency",
        str(args.lme_concurrency),
        "--out",
        str(out_csv),
    ]


def lme_rollup(rows: list[dict[str, str]]) -> dict[str, float | None]:
    """Judged knowledge-update accuracy and the stale-answer share of the
    wrong answers, from longmemeval's CSV (pure)."""
    ku = [r for r in rows if r.get("question_type") == "knowledge-update" and r.get("judge_label")]
    correct = sum(1 for r in ku if r["judge_label"] == "CORRECT")
    wrong = [r for r in ku if r["judge_label"] == "WRONG"]
    stale = sum(1 for r in wrong if str(r.get("stale_answer", "")).strip() == "1")
    recalled = sum(1 for r in ku if str(r.get("recalled", "")).strip().lower() in ("true", "1"))
    return {
        "accuracy": ratio(correct, len(ku)),
        "n": float(len(ku)),
        "stale_share_of_wrong": ratio(stale, len(wrong)),
        "wrong": float(len(wrong)),
        "recall": ratio(recalled, len(ku)),
    }


def run_lme(
    args: argparse.Namespace, token: str, out_csv: Path
) -> tuple[dict[str, float | None], str]:
    if not (os.environ.get("OPENAI_API_KEY") or os.environ.get("BENCH_OPENAI_API_KEY")):
        return {
            "accuracy": None,
            "n": 0.0,
        }, "knowledge-update slice skipped: OPENAI_API_KEY unset (judge)"
    cmd = lme_command(args, out_csv)
    env = {**os.environ, "LOOM_API_KEY": token}  # the bearer stays off the child's argv
    out_csv.unlink(missing_ok=True)  # a stale CSV must not be scored as this run's
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True, timeout=args.lme_timeout_s, env=env
        )
    except subprocess.TimeoutExpired:
        return {"accuracy": None, "n": 0.0}, (
            f"knowledge-update slice timed out after {args.lme_timeout_s} s"
        )
    if proc.returncode not in (0, 1) or not out_csv.exists():
        tail = (proc.stdout + proc.stderr).strip().splitlines()[-5:]
        return {"accuracy": None, "n": 0.0}, "knowledge-update slice failed: " + " | ".join(tail)
    with out_csv.open() as f:
        rows = list(csv.DictReader(f))
    return lme_rollup(rows), ""


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _verdict_line(v: dict[str, int]) -> str:
    return ", ".join(f"{k} {n}" for k, n in sorted(v.items())) if v else "none"


def build_report(
    args: argparse.Namespace,
    pi: dict[str, Any],
    lme: dict[str, float | None],
    cost: dict[str, dict[str, float]],
    *,
    probe_line: str,
    lme_note: str,
    arm_pi: dict[str, dict[str, Any]] | None = None,
    paired: dict[str, float | None] | None = None,
    replay: dict[str, Any] | None = None,
) -> Report:
    rep = Report(
        bench="memory_app",
        archetype="conversational memory application",
        base_url=args.base_url,
        consumer=args.consumer,
        probe=probe_line,
    )
    min_depth = int(getattr(args, "pi_min_depth", 10))
    rep.add(
        Metric(
            "stale retrieval: latest-value accuracy",
            pi.get("accuracy"),
            args.min_latest_accuracy,
            unit="%",
            n=int(pi.get("n") or 0),
        )
    )
    rep.add(
        Metric(
            f"stale retrieval: stale rate (depth>={min_depth})",
            pi.get("stale_rate"),
            args.max_stale_rate,
            higher_is_better=False,
            unit="%",
            n=int(pi.get("n_deep") or 0),
        )
    )
    rep.add(
        Metric(
            f"stale retrieval: clean correct (depth>={min_depth})",
            pi.get("clean_correct_deep"),
            float(
                CLEAN_CORRECT_FLOOR
                if getattr(args, "min_clean_correct", None) is None
                else args.min_clean_correct
            ),
            unit="%",
            n=int(pi.get("n_deep") or 0),
        )
    )
    for d, row in sorted((pi.get("by_depth") or {}).items()):
        rep.add(
            Metric(
                f"  depth {d}: latest-value accuracy / stale rate",
                row["accuracy"],
                None,
                unit="%",
                n=int(row["n"] or 0),
                kind="info",
                note=f"stale {100.0 * (row['stale_rate'] or 0.0):.0f}%"
                + (
                    f", clean {100.0 * (row['clean_correct'] or 0.0):.0f}%"
                    if row.get("clean_correct") is not None
                    else ""
                ),
            )
        )
    rep.add(
        Metric(
            "stale retrieval: clean correct (all depths)",
            pi.get("clean_correct"),
            None,
            unit="%",
            n=int(pi.get("n") or 0),
            kind="info",
        )
    )
    if pi.get("verdicts"):
        rep.note(f"write verdicts ({pi.get('arm', 'control')}): {_verdict_line(pi['verdicts'])}")
    for arm, apu in sorted((arm_pi or {}).items()):
        if arm == pi.get("arm"):
            continue
        rep.add(
            Metric(
                f"arm {arm}: latest-value accuracy",
                apu.get("accuracy"),
                None,
                unit="%",
                n=int(apu.get("n") or 0),
                kind="info",
                note=f"stale(deep) {100.0 * (apu.get('stale_rate') or 0.0):.0f}%, "
                f"clean(deep) {100.0 * (apu.get('clean_correct_deep') or 0.0):.0f}%",
            )
        )
        for d, row in sorted((apu.get("by_depth") or {}).items()):
            rep.add(
                Metric(
                    f"  arm {arm} depth {d}: accuracy",
                    row["accuracy"],
                    None,
                    unit="%",
                    n=int(row["n"] or 0),
                    kind="info",
                    note=f"stale {100.0 * (row['stale_rate'] or 0.0):.0f}%, "
                    f"clean {100.0 * (row.get('clean_correct') or 0.0):.0f}%",
                )
            )
        rep.note(
            f"write verdicts ({arm}): {_verdict_line(apu.get('verdicts') or {})}; "
            f"followed next {int(apu.get('followed') or 0)}"
        )
    if paired is not None and paired.get("n_pairs"):
        lb, ub = paired.get("lb"), paired.get("ub")
        rep.add(
            Metric(
                f"paired Δ clean correct follow_next−control (depth>={min_depth})",
                paired.get("delta"),
                None,
                unit="%",
                n=int(paired["n_pairs"] or 0),
                kind="info",
                note=(
                    f"95% CI [{100.0 * lb:+.1f}, {100.0 * ub:+.1f}] pp; "
                    + ("excludes 0" if (lb or 0.0) > 0 or (ub or 0.0) < 0 else "includes 0")
                )
                if lb is not None and ub is not None
                else "",
            )
        )
    if lme.get("n"):
        rep.add(
            Metric(
                "knowledge-update judged accuracy",
                lme.get("accuracy"),
                args.min_knowledge_update,
                unit="%",
                n=int(lme.get("n") or 0),
            )
        )
        rep.add(
            Metric(
                "knowledge-update: stale share of wrong",
                lme.get("stale_share_of_wrong"),
                None,
                unit="%",
                n=int(lme.get("wrong") or 0),
                kind="info",
            )
        )
        rep.add(Metric("knowledge-update recall@k", lme.get("recall"), None, unit="%", kind="info"))
        if int(lme.get("n") or 0) < 30:
            rep.smoke = True
    elif lme_note:
        rep.note(lme_note)
    ask = cost.get("memory.ask", {})
    ingest = cost.get("memory.set_from_messages", {})
    rep.add(
        Metric(
            "memory.ask $ per answer (ledger)",
            ratio(ask.get("cost_usd", 0.0), ask.get("ops", 0.0)),
            args.max_usd_per_ask,
            higher_is_better=False,
            kind="economy",
            unit="$",
            n=int(ask.get("ops", 0)),
        )
    )
    rep.add(
        Metric(
            "memory.ask p50 wall (ledger, max over namespaces)",
            ask.get("latency_p50_ms") or None,
            None,
            higher_is_better=False,
            kind="economy",
            unit="ms",
        )
    )
    rep.add(
        Metric(
            "ingest $ per set_from_messages (ledger)",
            ratio(ingest.get("cost_usd", 0.0), ingest.get("ops", 0.0)),
            None,
            higher_is_better=False,
            kind="economy",
            unit="$",
            n=int(ingest.get("ops", 0)),
        )
    )
    if replay is not None:
        led = replay.get("ledger") or {}
        rp = replay.get("replay") or {}
        ns = replay.get("namespace", "")
        rep.add(
            Metric(
                f"tenant {ns}: ledger ask p50 ({replay.get('days', 7)} d)",
                led.get("p50_ms"),
                None,
                higher_is_better=False,
                kind="info",
                unit="ms",
                n=int(led.get("ops") or 0),
                note=f"errors {int(led.get('errors') or 0)}",
            )
        )
        rep.add(
            Metric(
                f"tenant {ns}: ledger $ per ask",
                led.get("usd_per_ask"),
                None,
                higher_is_better=False,
                kind="info",
                unit="$",
                n=int(led.get("ops") or 0),
            )
        )
        if not rp.get("n"):
            rep.note(
                f"tenant {ns}: no replayable query text — {int(led.get('hashed_queries') or 0)} "
                "distinct queries are digests (no capture consent); ledger shape only"
            )
        rep.add(
            Metric(
                f"tenant {ns}: replay ask p50 (client)",
                rp.get("p50_ms"),
                None,
                higher_is_better=False,
                kind="info",
                unit="ms",
                n=int(rp.get("n") or 0),
                note=f"abstain {100.0 * (rp.get('abstain_rate') or 0.0):.0f}%, "
                f"errors {int(rp.get('errors') or 0)}",
            )
        )
    if pi.get("errors"):
        rep.note(f"stale-retrieval episodes errored: {int(pi['errors'])}")
    return rep


async def run(args: argparse.Namespace) -> int:
    resolve_pi_floors(args)
    depths = [int(x) for x in args.pi_depths.split(",") if x.strip()]
    arms = [a.strip() for a in args.pi_arms.split(",") if a.strip()]
    if args.follow_next:
        arms = list(ARMS)
        args.pi_ingest = "set"
    if "follow_next" in arms and args.pi_ingest != "set":
        print("the follow_next arm needs --pi-ingest set (a verdict rides memory.set)")
        return 2
    base = f"{args.namespace}-{nonce()}"
    client = make_client(args, namespace=base, agent="pi", timeout_s=60.0)
    token = client.transport._headers.get("Authorization", "").removeprefix("Bearer ")
    namespaces: list[str] = []
    async with client:
        probe = await client.probe("memory_app")
        post = bench_post(client)
        sem = asyncio.Semaphore(args.pi_concurrency)

        async def one(ep: int, depth: int) -> list[dict[str, Any]]:
            # both arms of one (episode, depth) run back to back under one
            # permit, so the server sees them interleaved, never batched by arm
            out: list[dict[str, Any]] = []
            async with sem:
                for arm in arms:
                    ns = f"{base}-d{depth}-e{ep}" + (f"-{arm}" if len(arms) > 1 else "")
                    namespaces.append(ns)
                    await register_bench_namespace(
                        post,
                        org=client.org,
                        namespace=ns,
                        description="acceptance memory_app PI episode",
                        keep=args.keep_namespaces,
                    )
                    out.append(
                        await pi_episode(
                            client,
                            ns,
                            ep,
                            depth,
                            outcomes=not args.no_outcomes,
                            ingest=args.pi_ingest,
                            arm=arm,
                        )
                    )
            return out

        # the PI namespaces are erased even when an episode or a later step raises
        erased: int | None = None
        try:
            nested = await asyncio.gather(
                *[one(ep, d) for d in depths for ep in range(args.pi_episodes)]
            )
            rows = [r for group in nested for r in group]
            arm_pi = {
                arm: {
                    **pi_rollup(
                        [r for r in rows if r.get("arm") == arm], min_depth=args.pi_min_depth
                    ),
                    "arm": arm,
                }
                for arm in arms
            }
            pi = arm_pi[arms[0]]
            paired = (
                paired_delta(rows, min_depth=args.pi_min_depth) if set(arms) >= set(ARMS) else None
            )
            lme: dict[str, float | None] = {"accuracy": None, "n": 0.0}
            lme_note = "knowledge-update slice skipped (--lme-limit 0)"
            if args.lme_limit > 0:
                lme_token = (
                    Path(args.lme_token_file).read_text().strip() if args.lme_token_file else token
                )
                out_csv = Path(args.lme_out or f"bench/acceptance_lme_{nonce()}.csv")
                lme, lme_note = await asyncio.to_thread(run_lme, args, lme_token, out_csv)
            replay: dict[str, Any] | None = None
            if args.replay:
                try:
                    ch = await asyncio.to_thread(_ch_client, args.env_file)
                    queries, ledger = await asyncio.to_thread(
                        replay_queries,
                        ch,
                        args.org,
                        args.replay,
                        n=args.replay_n,
                        days=args.replay_days,
                    )
                    rp = (
                        await run_replay(
                            client, args.replay, queries, concurrency=args.pi_concurrency
                        )
                        if queries
                        else {"n": 0.0, "errors": 0.0, "p50_ms": None, "abstain_rate": None}
                    )
                    replay = {
                        "namespace": args.replay,
                        "days": args.replay_days,
                        "ledger": ledger,
                        "replay": rp,
                    }
                except Exception as exc:  # noqa: BLE001 — the tenant readout is information; never fail the bench
                    replay = None
                    lme_note = (lme_note + " | " if lme_note else "") + f"replay skipped: {exc}"
            cost = await ledger_cost_by_op(client, namespaces)
            rep = build_report(
                args,
                pi,
                lme,
                cost,
                probe_line=probe.summary(),
                lme_note=lme_note,
                arm_pi=arm_pi if len(arms) > 1 else None,
                paired=paired,
                replay=replay,
            )
            rep.note(f"PI floors for slice {args.pi_floors_slice}")
            if len(arms) > 1:
                rep.note(
                    f"arms {arms} interleaved; gates read the {arms[0]} arm; "
                    f"ingest {args.pi_ingest}"
                )
            elif args.pi_ingest != "extract":
                rep.note(f"ingest {args.pi_ingest} (memory.set under fresh keys)")
            if not probe.ok:
                rep.note(f"probe: missing ops {probe.missing}")
        finally:
            if not args.keep_namespaces:
                erased = await erase_namespaces(client, namespaces)
        if erased is None:
            rep.note(f"{len(namespaces)} bench namespaces kept under {base}-*")
        else:
            rep.note(erasure_note(erased, len(set(namespaces))))
        rep.add(
            Metric(
                "ask p50 wall (client)",
                p50([float(r["elapsed_ms"]) for r in rows if "elapsed_ms" in r]),
                None,
                higher_is_better=False,
                kind="info",
                unit="ms",
            )
        )
        return finish(rep, args, client)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(
        ap, namespace_default="bench-acc-memapp", consumer_default="acceptance-memory-app"
    )
    ap.add_argument("--pi-episodes", type=int, default=16)
    ap.add_argument("--pi-depths", default="10,15,20")
    ap.add_argument("--pi-min-depth", type=int, default=10)
    ap.add_argument("--pi-concurrency", type=int, default=4)
    ap.add_argument(
        "--pi-ingest",
        choices=["extract", "set"],
        default="extract",
        help="how a PI update reaches the store: extract = memory.set_from_messages "
        "(default); set = memory.set under a fresh key per update",
    )
    ap.add_argument(
        "--pi-arms",
        default="control",
        help="comma list of control | follow_next; two arms run interleaved and the "
        "report adds the paired delta",
    )
    ap.add_argument(
        "--follow-next",
        action="store_true",
        help="shorthand for --pi-ingest set --pi-arms control,follow_next",
    )
    ap.add_argument(
        "--replay",
        default="",
        help="tenant namespace whose real memory.ask queries are read from the ledger "
        "(read-only) and asked again with reinforce=false",
    )
    ap.add_argument("--replay-n", type=int, default=30)
    ap.add_argument("--replay-days", type=int, default=7)
    ap.add_argument(
        "--env-file",
        default=str(ROOT / ".env"),
        help="ClickHouse credentials for --replay (LOOM_CH_*; read-only session)",
    )
    ap.add_argument("--lme-limit", type=int, default=30, help="knowledge-update items; 0 skips")
    ap.add_argument(
        "--lme-dataset", default=str(ROOT / "bench" / "datasets" / "longmemeval_s_cleaned.json")
    )
    ap.add_argument("--lme-top-k", type=int, default=3)
    ap.add_argument("--lme-seed", type=int, default=42)
    ap.add_argument("--lme-concurrency", type=int, default=2)
    ap.add_argument("--lme-timeout-s", type=int, default=5400)
    ap.add_argument(
        "--lme-token-file",
        default="",
        help="token for the LongMemEval slice when it needs another org (the bench "
        "writes under org `dev`)",
    )
    ap.add_argument("--lme-out", default="")
    ap.add_argument("--keep-namespaces", action="store_true")
    ap.add_argument(
        "--min-latest-accuracy",
        type=float,
        default=None,
        help="preset per --pi-depths slice (PI_FLOORS: the "
        "measured floor rounded down); ratchet up, never down",
    )
    ap.add_argument("--max-stale-rate", type=float, default=None, help="preset per slice")
    ap.add_argument(
        "--min-clean-correct",
        type=float,
        default=None,
        help="clean_correct at depth >= --pi-min-depth; preset per slice",
    )
    ap.add_argument("--min-knowledge-update", type=float, default=0.65)
    ap.add_argument("--max-usd-per-ask", type=float, default=0.05)
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
