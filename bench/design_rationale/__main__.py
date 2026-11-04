"""Design-rationale bench: does design knowledge served by Loom keep a coding agent from
re-proposing rejected options, and does it answer "why" questions correctly and cited?

    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> --out <dir> --dry-run
    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> --out <dir> \\
        --base-url http://127.0.0.1:<port> --org <org> --reader-model <m> --judge-model <m> \\
        --extract-model <server's LOOM_LLM_MODEL> --max-usd 5
    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale score <taskset> --out <dir>

The task set is a directory (bench/design_rationale/README.md has its schema;
`bench/design_rationale/example` is a synthetic one). Arms: 0 no design knowledge;
1 the whole document through knowledge.get; 2 the document as knowledge through
loom_search; 3 the gold rows through the pre-edit block (memory.for_file) and the
loom_search memory section; 4 the same surfaces over the rows `loom decisions extract`
mined from the documents (or, with --arm4-namespace, rows a document source's sync wrote).

`run --dry-run` reads nothing but the task set and prints the calls, tokens and spend a
run would take. A real run needs a Loom server and ClickHouse this run started, with the
server's environment in this shell (the extraction runs the CLI in a child process), and
--max-usd: every call is refused once its worst case would cross the cap. `--offline`
answers and judges with a stub that costs nothing and measures nothing: it checks the
plumbing against a server. `score` reads the run's files only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from bench.design_rationale import run as runner
from bench.design_rationale import score as scorer
from bench.design_rationale.llm import Ledger, Model, Reply, price, provider_complete
from bench.design_rationale.taskset import TaskSetError, load


def _arms(text: str) -> list[int]:
    arms = sorted({int(a) for a in text.split(",") if a.strip()})
    if not arms or any(a not in runner.ARMS for a in arms):
        raise argparse.ArgumentTypeError(f"arms are a comma list of {runner.ARMS}")
    return arms


def _offline(system: str, user: str, max_tokens: int) -> Reply:
    # A judge prompt asks for JSON; an answer prompt gets the abstention line.
    return Reply(
        "{}" if "Return JSON only" in system else "No recorded decision covers this.", 0, 0
    )


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bench.design_rationale",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="Set up the arms, read the surfaces, answer and judge.")
    r.add_argument("taskset", type=Path)
    r.add_argument("--out", type=Path, required=True, help="Run directory (resumable)")
    r.add_argument("--dry-run", action="store_true", help="Print the plan and its cost only")
    r.add_argument(
        "--offline",
        action="store_true",
        help="Answer and judge with a free stub: plumbing only, no measurement",
    )
    r.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", ""))
    r.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    r.add_argument(
        "--org",
        default="bench",
        help="Org prefix: each arm's namespace goes in its own org <org>-dr-<role>",
    )
    r.add_argument("--arms", type=_arms, default=[0, 1, 2, 3, 4])
    r.add_argument("--k", type=int, default=4, help="Samples per task and arm (default 4)")
    r.add_argument("--tasks", default="", help="Comma list of task ids (default all)")
    r.add_argument("--reader-model", default=os.environ.get("BENCH_READER_MODEL", ""))
    r.add_argument(
        "--judge-model",
        action="append",
        default=[],
        help="Judge model; repeat for a second judge (default BENCH_JUDGE_MODEL)",
    )
    r.add_argument(
        "--judge-orders",
        type=int,
        choices=[1, 2],
        default=2,
        help="2 also judges with the answer before the gold (position bias)",
    )
    r.add_argument(
        "--judge-repeats",
        type=int,
        default=2,
        help="Passes per order, for repetition stability (default 2)",
    )
    r.add_argument(
        "--extract-model",
        default=os.environ.get("LOOM_LLM_MODEL", ""),
        help="The server's extraction model, for its spend estimate",
    )
    r.add_argument(
        "--extract-repo",
        default="",
        help="Passed to `loom decisions extract --repo` to link rows to code",
    )
    r.add_argument("--extract-window-chars", type=int, default=6000)
    r.add_argument(
        "--arm4-namespace",
        default="",
        help="Serve arm 4 from rows a document source's sync already wrote in this "
        "namespace of --org, beside the documents they cite (a citation into another "
        "namespace counts as unresolved)",
    )
    r.add_argument("--max-usd", type=float, default=0.0, help="Spend cap for the run's ledger")
    r.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Keep the namespaces (a resumed run then reuses them)",
    )
    s = sub.add_parser("score", help="Metrics, paired comparisons and MDEs from a run.")
    s.add_argument("taskset", type=Path)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--human-labels", type=Path, help="Human-labelled answers (JSON list)")
    s.add_argument("--extraction-labels", type=Path, help="Human-judged extracted rows (JSON list)")
    return p


def _config(a: argparse.Namespace) -> runner.Config:
    judges = a.judge_model or (
        [os.environ["BENCH_JUDGE_MODEL"]] if os.environ.get("BENCH_JUDGE_MODEL") else []
    )
    return runner.Config(
        out=a.out,
        arms=a.arms,
        org=a.org,
        k=a.k,
        reader=a.reader_model,
        judges=judges,
        judge_orders=a.judge_orders,
        judge_repeats=a.judge_repeats,
        max_usd=a.max_usd,
        only={t for t in a.tasks.split(",") if t},
        keep_namespaces=a.keep_namespaces,
        arm4_namespace=a.arm4_namespace,
        extract_repo=a.extract_repo,
        extract_model=a.extract_model,
        extract_window_chars=a.extract_window_chars,
    )


def cmd_run(a: argparse.Namespace) -> int:
    ts = load(a.taskset)
    cfg = _config(a)
    if a.dry_run:
        runner.print_plan(runner.plan(ts, cfg))
        return 0
    if not a.base_url:
        print("--base-url (or LOOM_BASE_URL) names the run's own Loom server", file=sys.stderr)
        return 2
    if not a.offline:
        models = [cfg.reader, *cfg.judges] + (
            [cfg.extract_model] if 4 in cfg.arms and not cfg.arm4_namespace else []
        )
        if not cfg.reader or not cfg.judges:
            print("--reader-model and --judge-model are required", file=sys.stderr)
            return 2
        if missing := [m for m in models if not price(m)]:
            print(f"no price for {missing}: the spend cap cannot hold", file=sys.stderr)
            return 2
        if a.max_usd <= 0:
            print("--max-usd is required for a run that calls a provider", file=sys.stderr)
            return 2
    from bench.design_rationale.loom import Loom

    a.out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(a.out / "ledger.jsonl", a.max_usd)
    if a.offline:
        print("offline: answers and verdicts are a stub; nothing here is a measurement")
        reader = Model("offline", _offline, ledger, priced=False)
        judges = [Model("offline", _offline, ledger, priced=False)]
        cfg.judges = ["offline"]
        cfg.extract_model = cfg.extract_model or "fake-echo-1"
    else:
        reader = Model(cfg.reader, provider_complete(cfg.reader), ledger)
        judges = [Model(m, provider_complete(m), ledger) for m in cfg.judges]
    loom = Loom(a.base_url, a.token)
    try:
        runner.run(ts, cfg, loom, reader, judges, ledger)
    finally:
        loom.close()
    print(f"spent {ledger.spent():.4f} USD (ledger {ledger.path})")
    return 0


def cmd_score(a: argparse.Namespace) -> int:
    ts = load(a.taskset)
    human = json.loads(a.human_labels.read_text()) if a.human_labels else None
    labels = json.loads(a.extraction_labels.read_text()) if a.extraction_labels else None
    print(scorer.report(scorer.score(ts, a.out, human=human, extraction_labels=labels)))
    return 0


def main(argv: list[str] | None = None) -> int:
    a = parser().parse_args(argv)
    try:
        return cmd_run(a) if a.cmd == "run" else cmd_score(a)
    except TaskSetError as e:
        print(f"task set: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
