"""Design-rationale bench: does design knowledge served by Loom keep a coding agent from
re-proposing rejected options, and does it answer "why" questions correctly and cited?

    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> \\
        --out <dir> --dry-run
    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> \\
        --out <dir> --base-url http://127.0.0.1:<port> --org <org> \\
        --reader-model <m> --judge-model <m> --code-index <url> \\
        --code-index-org <org> --code-index-namespace <ns> --max-usd 5
    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale score <taskset> \\
        --out <dir>

The task set is a directory (bench/design_rationale/README.md has its schema;
`bench/design_rationale/example` is a synthetic one). Arms: 0 no design knowledge; 1 the
whole document through knowledge.get; 2 the document as knowledge through loom_search; 3
the gold rows through the pre-edit block (memory.for_file) and the loom_search memory
section; 4 the same surfaces over the rows `loom decisions extract` mined from the
documents (or, with --arm4-namespace, rows a document source's sync wrote); 5 the gold
rows through the loom_search memory section only, added whenever arm 4 has no files; 6
arm 4's surfaces with the knowledge hits of the same loom_search beside them.

Arm 4's rows name code by its names; the files that give them a pre-edit block come from
a code index of the repository (--code-index: read-only code.symbol calls on the Loom
that indexes it, with the extractor's own rule: one defining file that is not a test or
a page). Without one (and without --arm4-namespace), arm 4 is served through loom_search
only, and arm 5 is added so that arm 4 is compared with gold rows served the same way.

`run --dry-run` needs no server: it reads the task set and, with arm 4, this
environment's server settings (the extraction's model), and prints the calls, tokens and
spend a run would take. A real run needs a Loom server (and its ClickHouse) you started
for this run, with the server's environment in this shell (the extraction runs the CLI
in a child process, whose model is read from that environment and must match
--extract-model when given), and --max-usd: every provider request the harness sends,
and each extraction's bound, is refused once it would cross the cap; the server's own
spend is recorded after the fact. `--offline` answers and judges with a stub that costs
nothing and measures nothing: it checks the plumbing against a server. `score` reads the
run's files only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from bench.design_rationale import run as runner
from bench.design_rationale import score as scorer
from bench.design_rationale.llm import Ledger, Model, ProviderComplete, Reply, price
from bench.design_rationale.taskset import EXTRACTED_ARMS, TaskSetError, load


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
    r = sub.add_parser(
        "run",
        help="Set up the arms, read the surfaces, answer and judge.",
    )
    r.add_argument("taskset", type=Path)
    r.add_argument("--out", type=Path, required=True, help="Run directory (resumable)")
    r.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and its cost only",
    )
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
        help="Org prefix: each role's namespace goes in its own org "
        "<org>-<prefix>-<role>, the prefix dr-<hex> fixed per run; --arm4-namespace is "
        "read in --org itself",
    )
    r.add_argument(
        "--arms",
        type=_arms,
        default=[0, 1, 2, 3, 4],
        help="Comma list of arms (default 0,1,2,3,4; 5 is added when arm 4 has no "
        "files: no --code-index or --arm4-namespace; 6 serves arm 4's rows with the "
        "documents' knowledge hits)",
    )
    r.add_argument(
        "--k",
        type=int,
        default=4,
        help="Samples per task and arm (default 4)",
    )
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
        default="",
        help="The model the extraction must run on; refused when this environment's "
        "server settings name another (default: the one they name)",
    )
    r.add_argument(
        "--extract-window-chars",
        type=int,
        default=runner.EXTRACT_WINDOW_CHARS,
        help="Passed to `loom decisions extract --window-chars` "
        f"(default {runner.EXTRACT_WINDOW_CHARS})",
    )
    r.add_argument(
        "--code-index",
        default="",
        help="Base URL of the Loom whose code index resolves arm 4's code names",
    )
    r.add_argument("--code-index-org", default="")
    r.add_argument("--code-index-namespace", default="")
    r.add_argument("--code-index-token", default=os.environ.get("LOOM_CODE_INDEX_TOKEN", ""))
    r.add_argument(
        "--arm4-namespace",
        default="",
        help="Serve arm 4 from rows a document source's sync already wrote in this "
        "namespace of --org, beside the documents they cite (a citation into another "
        "namespace counts as unresolved)",
    )
    r.add_argument(
        "--max-usd",
        type=float,
        default=0.0,
        help="Spend cap for the run's ledger",
    )
    r.add_argument(
        "--keep-namespaces",
        action="store_true",
        help="Do not erase the namespaces when the run finishes or its setup fails",
    )
    s = sub.add_parser("score", help="Metrics, paired comparisons and MDEs from a run.")
    s.add_argument("taskset", type=Path)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument(
        "--human-labels",
        type=Path,
        help="Human-labelled answers (JSON list)",
    )
    s.add_argument(
        "--extraction-labels",
        type=Path,
        help="Human-judged extracted rows (JSON list)",
    )
    s.add_argument(
        "--summary",
        type=Path,
        help="Also write the aggregates without the per-task rows or document ids, the "
        "task set's counts, the run's flags with K and the judge orders and repeats, and "
        "its spend to this file (the committed results artifact)",
    )
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
        extract_model=a.extract_model,
        extract_window_chars=a.extract_window_chars,
    )


def server_extract_model() -> tuple[str, str, str]:
    """(provider, model, fallback model) the extraction child will run on: the server
    settings this environment resolves, as the CLI resolves them."""
    from server.llm.client import get_llm_client
    from server.settings import settings

    client = get_llm_client()
    provider, model = client.provider, client.model
    return provider, model, settings.llm_fallback_model.strip()


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def cmd_run(a: argparse.Namespace) -> int:
    ts = load(a.taskset)
    cfg = _config(a)
    unknown = sorted(cfg.only - {t["id"] for _, t in ts.all_tasks()})
    if unknown:
        return _fail(f"--tasks names no such task: {', '.join(unknown)}")
    # Rows a sync wrote carry the files it linked; rows this run extracts get theirs
    # from the code index, or have none.
    cfg.arm4_files = bool(a.code_index or a.arm4_namespace)
    extracted = sorted(set(EXTRACTED_ARMS) & set(cfg.arms))
    if extracted and not cfg.arm4_files:
        names = " and ".join(f"arm {n}" for n in extracted)
        print(f"{names}: no code index, the rows reach loom_search only, no pre-edit block")
        if 5 not in cfg.arms:
            cfg.arms = [*cfg.arms, 5]
            print("arm 5 (gold rows through loom_search only) is added as its reference")
    provider = ""
    if runner.own_extraction(cfg):
        provider, model, fallback = server_extract_model()
        if fallback and fallback != model:
            return _fail(
                f"LOOM_LLM_FALLBACK_MODEL={fallback} would carry some extraction calls on "
                "a model the estimate does not price; unset it for the run"
            )
        if cfg.extract_model and cfg.extract_model != model:
            return _fail(
                f"the extraction would run on {model!r} (this environment's server "
                f"settings), not --extract-model {cfg.extract_model!r}"
            )
        if a.offline and provider != "fake":
            return _fail(
                "--offline with arm 4 or 6 needs LOOM_LLM_PROVIDER=fake: the extraction "
                f"would call {provider}"
            )
        cfg.extract_model = model
    if a.dry_run:
        runner.print_plan(runner.plan(ts, cfg))
        return 0
    if runner.own_extraction(cfg) and not a.offline and provider == "fake":
        return _fail(
            "the server settings resolve to the fake model, whose decisions pass the "
            "extractor's checks by construction: set LOOM_LLM_PROVIDER and its key, or "
            "use --offline"
        )
    if not a.base_url:
        return _fail("--base-url (or LOOM_BASE_URL) names the run's own Loom server")
    if a.code_index and not (a.code_index_org and a.code_index_namespace):
        return _fail("--code-index needs --code-index-org and --code-index-namespace")
    if not a.offline:
        models = [cfg.reader, *cfg.judges] + (
            [cfg.extract_model] if runner.own_extraction(cfg) else []
        )
        if not cfg.reader or not cfg.judges:
            return _fail("--reader-model and --judge-model are required")
        if missing := [m for m in models if not price(m)]:
            return _fail(f"no price for {missing}: the spend cap cannot hold")
        if a.max_usd <= 0:
            return _fail("--max-usd is required for a run that calls a provider")
    from bench.design_rationale.loom import CodeIndex, Loom

    a.out.mkdir(parents=True, exist_ok=True)
    ledger = Ledger(a.out / "ledger.jsonl", a.max_usd)
    if a.offline:
        print("offline: answers and verdicts are a stub; nothing here is a measurement")
        reader = Model("offline", _offline, ledger, priced=False)
        judges = [Model("offline", _offline, ledger, priced=False)]
        cfg.judges = ["offline"]
    else:
        reader = Model(cfg.reader, ProviderComplete(cfg.reader, ledger), ledger)
        judges = [Model(m, ProviderComplete(m, ledger), ledger) for m in cfg.judges]
    loom = Loom(a.base_url, a.token)
    code = (
        CodeIndex(a.code_index, a.code_index_org, a.code_index_namespace, a.code_index_token)
        if a.code_index
        else None
    )
    try:
        finished = runner.run(ts, cfg, loom, reader, judges, ledger, code)
    except runner.RunError as e:
        return _fail(str(e))
    finally:
        loom.close()
        if code is not None:
            code.close()
    print(f"spent {ledger.spent():.4f} USD (ledger {ledger.path})")
    # 1: the cap stopped the run; the same command with a higher cap resumes it.
    return 0 if finished else 1


def cmd_score(a: argparse.Namespace) -> int:
    ts = load(a.taskset)
    human = json.loads(a.human_labels.read_text()) if a.human_labels else None
    labels = json.loads(a.extraction_labels.read_text()) if a.extraction_labels else None
    try:
        metrics = scorer.score(ts, a.out, human=human, extraction_labels=labels)
    except scorer.LabelError as e:
        return _fail(f"{a.extraction_labels}: {e}; fill each row's verdict first")
    if a.summary:
        text = json.dumps(scorer.summary(ts, metrics, a.out), indent=2) + "\n"
        a.summary.write_text(text)
    print(scorer.report(metrics))
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
