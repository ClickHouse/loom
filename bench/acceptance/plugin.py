"""Acceptance bench — coding-agent plugins: a hook shunt and
MCP tools inside someone else's session.

Precision first — the plugin is judged by the files the agent ends up
reading:

  hook fidelity       the shunt's decision on a deterministic fixture of
                      Read / Grep calls (`fixtures/shunt_decisions.json`),
                      run through the plugin's own `code-shunt.mjs` under
                      node; every decision must match. Skipped without node.
  wrong-file rate     of what the redirect serves: for each conceptual
                      question of the authored code-QA fixture the hook would
                      have denied as a tree-wide grep, `code.search` top-1 not
                      in the gold paths (lower is better); gold in top-5 as
                      the companion.
  replay (offline)    the agent-level harness on its fixture transcripts
                      (`bench/fixtures/code_plane_ablation`): file precision
                      of the reads the agent made with the plugin (arm C)
                      against without (arm A).

Economy second: tool-call reduction on the replayed transcripts (A → C),
denied and bypass calls, and — live — the outline the redirect names served
for each denied whole-file read.

Outcome feedback: the fixture's decisions are posted as `code.outcome
shunt_hit` / `shunt_pass` rows under the bench's consumer (the plugin's own
ledger path).

    python bench/acceptance/plugin.py --base-url http://127.0.0.1:7893 --token-file bench.token
    python bench/acceptance/plugin.py --dry     # hook fixture + replay only, no server
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
for _p in (str(_ROOT), str(_ROOT / "sdk" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from loom.reference_client import ReferenceClient  # noqa: E402

from bench import code_intel_qa as cq  # noqa: E402
from bench import code_plane_ablation as abl  # noqa: E402
from bench.acceptance._common import (  # noqa: E402
    ROOT,
    Metric,
    Report,
    add_common_args,
    finish,
    make_client,
    ratio,
)

HOOK_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "shunt_decisions.json"
ECONOMY_MIN_TASKS = 10
SHUNT_MJS = ROOT / "integrations" / "claude-code-plugin" / "lib" / "code-shunt.mjs"
ABLATION_FIXTURES = abl.FIXTURE_DIR

_NODE_RUNNER = """
import fs from 'node:fs';
const [mjs, fxPath] = process.argv.slice(-2);
const m = await import(mjs);
const fx = JSON.parse(fs.readFileSync(fxPath, 'utf8'));
const out = [];
for (const c of fx.cases) {
  const ctx = { ...c.ctx, prefixes: fx.prefixes };
  const r = c.tool === 'Grep' ? m.classifyGrep(ctx) : m.classifyRead(ctx, fx.max_lines);
  out.push({ id: c.id, reason: r.reason, deny: r.deny ? r.deny.slice(0, 80) : null });
}
process.stdout.write(JSON.stringify(out));
"""


# ---------------------------------------------------------------------------
# hook fixture (pure scoring; node runs the plugin's real decision code)
# ---------------------------------------------------------------------------


def load_hook_fixture(path: Path = HOOK_FIXTURE) -> dict[str, Any]:
    return dict(json.loads(path.read_text()))


def score_hook_decisions(
    fixture: dict[str, Any], decisions: list[dict[str, Any]]
) -> dict[str, Any]:
    """Fidelity = share of fixture cases whose decided reason equals the
    expected one; `mismatches` names the rest (pure)."""
    by_id = {str(d.get("id")): str(d.get("reason") or "") for d in decisions}
    cases = list(fixture.get("cases") or [])
    mism = [
        {"id": c["id"], "expected": c["expect"], "got": by_id.get(str(c["id"]), "<missing>")}
        for c in cases
        if by_id.get(str(c["id"])) != c["expect"]
    ]
    return {
        "n": len(cases),
        "fidelity": ratio(len(cases) - len(mism), len(cases)),
        "mismatches": mism,
    }


def run_hook_fixture(
    fixture_path: Path = HOOK_FIXTURE, mjs: Path = SHUNT_MJS
) -> list[dict[str, Any]] | None:
    """The plugin's classifyRead / classifyGrep on the fixture, under node.
    None when node is not installed or the module is missing; a module that
    is present but fails to run raises HookRunError (a broken hook is a
    failure, not a skip)."""
    node = shutil.which("node")
    if node is None or not mjs.exists():
        return None
    try:
        proc = subprocess.run(
            [node, "--input-type=module", "-e", _NODE_RUNNER, "--", str(mjs), str(fixture_path)],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as e:
        raise HookRunError(f"{mjs.name} did not answer within 60 s") from e
    if proc.returncode != 0:
        tail = " | ".join(proc.stderr.strip().splitlines()[-3:])
        raise HookRunError(f"{mjs.name} exited {proc.returncode}: {tail}")
    try:
        out = json.loads(proc.stdout or "[]")
    except ValueError as e:
        raise HookRunError(f"{mjs.name} printed no JSON list") from e
    if not isinstance(out, list):
        raise HookRunError(f"{mjs.name} printed {type(out).__name__}, not a list")
    return [dict(d) for d in out]


class HookRunError(RuntimeError):
    """The shunt module is installed but its classifier could not be run."""


# ---------------------------------------------------------------------------
# replay of the agent-level harness's fixture transcripts (offline)
# ---------------------------------------------------------------------------


def replay_fixture(fixture_dir: Path = ABLATION_FIXTURES) -> dict[str, Any]:
    tasks, _stats = abl.load_tasks(fixture_dir / "tasks.jsonl")
    by_id = {t.id: t for t in tasks}
    cells: dict[str, abl.CellScore] = {}
    denied_paths: list[str] = []
    for arm in ("A", "C"):
        tp = fixture_dir / f"transcript_arm_{arm}.jsonl"
        if not tp.exists():
            continue
        tr = abl.parse_transcript(tp)
        task = next(iter(by_id.values()))
        cells[arm] = abl.score_cell(task, tr, arm=arm, seed=1)
        if arm == "C":
            denied_paths = [e.path for e in tr.events if e.denied and e.path]
    return {"cells": cells, "denied_paths": denied_paths, "tasks": len(tasks)}


def replay_metrics(cells: dict[str, abl.CellScore]) -> dict[str, float | None]:
    a, c = cells.get("A"), cells.get("C")
    out: dict[str, float | None] = {
        "wrong_file_rate_replay": None,
        "tool_call_reduction": None,
        "denied_calls": None,
        "bypass_calls": None,
        "acc5_view_b_plugin": None,
    }
    if c is not None:
        if c.file_precision is not None:
            out["wrong_file_rate_replay"] = 1.0 - c.file_precision
        out["denied_calls"] = float(c.denied_calls)
        out["bypass_calls"] = float(c.bypass_calls)
        out["acc5_view_b_plugin"] = c.acc5_view_b
    if a is not None and c is not None and a.tool_calls:
        out["tool_call_reduction"] = (a.tool_calls - c.tool_calls) / a.tool_calls
    return out


# ---------------------------------------------------------------------------
# live: what the redirect serves
# ---------------------------------------------------------------------------


def redirect_precision(pairs: list[tuple[cq.Question, cq.Retrieval]]) -> dict[str, float | None]:
    """Wrong-file rate at rank 1 (served top path not in gold) and gold in
    the top 5, over conceptual questions that returned paths (pure)."""
    served = [(q, r) for q, r in pairs if not r.error and r.paths]
    wrong1 = sum(
        1 for q, r in served if cq.recall_at_k(list(q.gold.get("paths") or []), r.paths, 1) == 0
    )
    hit5 = sum(
        1 for q, r in served if cq.recall_at_k(list(q.gold.get("paths") or []), r.paths, 5) > 0
    )
    return {
        "wrong_file_rate_top1": ratio(wrong1, len(served)),
        "hit_at_5": ratio(hit5, len(served)),
        "n": float(len(served)),
        "unserved": float(len(pairs) - len(served)),
    }


async def live_redirects(
    client: ReferenceClient,
    questions: list[cq.Question],
) -> list[tuple[cq.Question, cq.Retrieval]]:
    pairs: list[tuple[cq.Question, cq.Retrieval]] = []
    for q in questions:
        r = await client.code_search(q.query, top_k=5, bodies="none")
        if not r.ok:
            pairs.append((q, cq.Retrieval(error=f"{r.code}: {r.error}")))
            continue
        pairs.append(
            (
                q,
                cq.Retrieval(
                    paths=[str(u.get("path") or "") for u in r.data.get("results") or []],
                    abstained=bool(r.data.get("abstain")),
                    tokens_returned=int(r.data.get("tokens_returned") or 0),
                ),
            )
        )
    return pairs


async def live_outlines(client: ReferenceClient, paths: list[str]) -> tuple[int, int]:
    served = 0
    for p in paths:
        r = await client.code_outline(p)
        served += int(
            r.ok and bool(r.data.get("entries") or r.data.get("rows") or r.data.get("declarations"))
        )
    return served, len(paths)


async def post_hook_outcomes(
    client: ReferenceClient,
    fixture: dict[str, Any],
    decisions: list[dict[str, Any]],
) -> int:
    """The fixture's decisions as the plugin's own ledger rows: one
    `shunt_hit` per deny (targets = the tool), one `shunt_pass` per pass
    with the reason as `source`."""
    posted = 0
    tools = {str(c["id"]): str(c["tool"]) for c in fixture.get("cases") or []}
    lines = {str(c["id"]): int(c["ctx"].get("lineCount") or 0) for c in fixture.get("cases") or []}
    for d in decisions:
        cid = str(d.get("id"))
        reason = str(d.get("reason") or "")
        if reason == "hit":
            r = await client.code_outcome(
                "shunt_hit", targets=[tools.get(cid, "Read")], tokens_saved=lines.get(cid, 0) * 12
            )
        else:
            r = await client.code_outcome(
                "shunt_pass", targets=[tools.get(cid, "Read")], source=reason
            )
        posted += int(r.ok)
    return posted


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def build_report(
    args: argparse.Namespace,
    *,
    hook: dict[str, Any] | None,
    replay: dict[str, float | None],
    redirect: dict[str, float | None],
    outlines: tuple[int, int] | None,
    outcomes_posted: int,
    probe_line: str,
) -> Report:
    rep = Report(
        bench="plugin",
        archetype="coding-agent plugin (hook shunt + MCP tools)",
        base_url=args.base_url,
        consumer=args.consumer,
        probe=probe_line,
    )
    if hook is None:
        rep.note("hook fidelity skipped: node or code-shunt.mjs not available")
    else:
        rep.add(
            Metric(
                "hook decision fidelity (fixture)",
                hook["fidelity"],
                args.min_hook_fidelity,
                unit="%",
                n=int(hook["n"]),
            )
        )
        if hook["mismatches"]:
            rep.note(f"hook mismatches: {hook['mismatches']}")
    if redirect.get("n"):
        rep.add(
            Metric(
                "wrong-file rate @1 (redirect → code.search)",
                redirect["wrong_file_rate_top1"],
                args.max_wrong_file_rate,
                higher_is_better=False,
                unit="%",
                n=int(redirect["n"] or 0),
            )
        )
        rep.add(
            Metric(
                "gold in top-5 (redirect → code.search)",
                redirect["hit_at_5"],
                args.min_hit5,
                unit="%",
                n=int(redirect["n"] or 0),
            )
        )
    rep.add(
        Metric(
            "wrong-file rate of agent reads (replay, arm C)",
            replay["wrong_file_rate_replay"],
            None,
            higher_is_better=False,
            unit="%",
            kind="info",
        )
    )
    rep.add(
        Metric(
            "acc@5 of agent reads (replay, arm C)",
            replay["acc5_view_b_plugin"],
            None,
            unit="%",
            kind="info",
        )
    )
    # the economy metric gates only over >= ECONOMY_MIN_TASKS replayed tasks;
    # below that it is information and the report is labelled smoke
    # (one synthetic task is not an economy)
    n_tasks = int(replay.get("tasks") or 0)
    rep.add(
        Metric(
            "tool-call reduction A→C (replay)",
            replay["tool_call_reduction"],
            args.min_tool_call_reduction if n_tasks >= ECONOMY_MIN_TASKS else None,
            unit="%",
            kind="economy" if n_tasks >= ECONOMY_MIN_TASKS else "info",
            n=n_tasks,
            note=""
            if n_tasks >= ECONOMY_MIN_TASKS
            else f"smoke: {n_tasks} task(s) < {ECONOMY_MIN_TASKS}",
        )
    )
    if n_tasks < ECONOMY_MIN_TASKS:
        rep.smoke = True
    rep.add(
        Metric(
            "bypass calls (replay, arm C)",
            replay["bypass_calls"],
            0.0,
            higher_is_better=False,
            kind="economy",
        )
    )
    rep.add(Metric("denied calls (replay, arm C)", replay["denied_calls"], None, kind="info"))
    if outlines is not None:
        rep.add(
            Metric(
                "outline served for denied whole-file reads",
                ratio(outlines[0], outlines[1]),
                None,
                unit="%",
                n=outlines[1],
                kind="economy",
            )
        )
    rep.add(Metric("outcome reports posted", float(outcomes_posted), None, kind="info"))
    rep.note(
        "replay = the agent-level harness's fixture transcripts (one synthetic task); the "
        "live run of that harness is `bench/code_plane_ablation.py run --driver claude`"
    )
    return rep


async def run(args: argparse.Namespace) -> int:
    fixture = load_hook_fixture(Path(args.hook_fixture))
    hook_error = ""
    try:
        decisions = run_hook_fixture(Path(args.hook_fixture))
    except HookRunError as e:
        decisions, hook_error = None, str(e)
    hook = score_hook_decisions(fixture, decisions) if decisions is not None else None
    if hook_error:
        # a gated metric with no value: the verdict fails it as not measured
        hook = {"n": len(fixture.get("cases") or []), "fidelity": None, "mismatches": []}
    rp = replay_fixture(Path(args.ablation_fixtures))
    replay = replay_metrics(rp["cells"])
    replay["tasks"] = float(rp["tasks"])
    if args.dry:
        rep = build_report(
            args,
            hook=hook,
            replay=replay,
            redirect={"n": 0.0},
            outlines=None,
            outcomes_posted=0,
            probe_line="probe skipped (--dry)",
        )
        if hook_error:
            rep.note(f"hook fixture failed: {hook_error}")
        return finish(rep, args)

    questions = [q for q in cq.load_questions(Path(args.dataset)) if q.qtype == "conceptual"]
    if args.limit:
        questions = questions[: args.limit]
    client = make_client(args, timeout_s=30.0)
    async with client:
        probe = await client.probe("plugin")
        pairs = await live_redirects(client, questions)
        redirect = redirect_precision(pairs)
        outlines = await live_outlines(client, rp["denied_paths"]) if rp["denied_paths"] else None
        posted = 0
        if not args.no_outcomes and decisions is not None:
            posted = await post_hook_outcomes(client, fixture, decisions)
        rep = build_report(
            args,
            hook=hook,
            replay=replay,
            redirect=redirect,
            outlines=outlines,
            outcomes_posted=posted,
            probe_line=probe.summary(),
        )
        if not probe.ok:
            rep.note(f"probe: missing ops {probe.missing}")
        if hook_error:
            rep.note(f"hook fixture failed: {hook_error}")
        if redirect.get("unserved"):
            rep.note(
                f"redirect questions unserved (error / empty): {int(redirect['unserved'] or 0)}"
            )
        return finish(rep, args, client)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    add_common_args(ap, namespace_default="code-clickhouse", consumer_default="acceptance-plugin")
    ap.add_argument("--dataset", default=str(cq._FIXTURE))
    ap.add_argument("--hook-fixture", default=str(HOOK_FIXTURE))
    ap.add_argument("--ablation-fixtures", default=str(ABLATION_FIXTURES))
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true", help="hook fixture + replay only, no server")
    ap.add_argument("--min-hook-fidelity", type=float, default=1.0)
    ap.add_argument("--max-wrong-file-rate", type=float, default=0.40)
    ap.add_argument("--min-hit5", type=float, default=0.75)
    ap.add_argument("--min-tool-call-reduction", type=float, default=0.0)
    args = ap.parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
