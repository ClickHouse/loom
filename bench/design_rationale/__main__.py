"""Design-rationale bench: does design knowledge served by Loom keep a coding agent from
re-proposing rejected options, and does it answer "why" questions correctly and cited?

    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> --out <dir> --dry-run
    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale run <taskset> --out <dir> \\
        --base-url http://127.0.0.1:<port> --org <org> --reader-model <m> --judge-model <m> \\
        --code-index <url> --code-index-org <org> --code-index-namespace <ns> --max-usd 5
    PYTHONPATH=$PWD .venv/bin/python -m bench.design_rationale score <taskset> --out <dir>

The task set is a directory (bench/design_rationale/README.md has its schema;
`bench/design_rationale/example` is a synthetic one). Arms: 0 no design knowledge;
1 the whole document through knowledge.get; 2 the document as knowledge through
loom_search; 3 the gold rows through the pre-edit block (memory.for_file) and the
loom_search memory section; 4 the same surfaces over the rows `loom decisions extract`
mined from the documents (or, with --arm4-namespace, rows a document source's sync wrote);
5 the gold rows through the loom_search memory section only.

Arm 4's rows name code by its names; the files that give them a pre-edit block come from
a code index of the repository (--code-index: read-only code.symbol calls on the Loom that
indexes it, with the extractor's own rule: one defining file that is not a test or a page).
Without one (and without --arm4-namespace), arm 4 is served through loom_search only,
and arm 5 is added so that arm 4 is compared with gold rows served the same way.

`run --dry-run` reads nothing but the task set and prints the calls, tokens and spend a
run would take. A real run needs a Loom server and ClickHouse this run started, with the
server's environment in this shell (the extraction runs the CLI in a child process, whose
model is read from that environment and must match --extract-model when given), and
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
from bench.design_rationale.llm import Ledger, Model, ProviderComplete, Reply, price
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
    r.add_argument(
        "--arms",
        type=_arms,
        default=[0, 1, 2, 3, 4],
        help="Comma list of arms (default 0,1,2,3,4; 5 is added when arm 4 has no code index)",
    )
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
        default="",
        help="The model the extraction must run on; refused when this environment's "
        "server settings name another (default: the one they name)",
    )
    r.add_argument(
        "--extract-window-chars",
        type=int,
        default=6000,
        help="Passed to `loom decisions extract --window-chars` (default 6000)",
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
        extract_model=a.extract_model,
        extract_window_chars=a.extract_window_chars,
    )


def server_extract_model() -> tuple[str, str, str]:
    """(provider, model, fallback model) the extraction child will run on: the server
    settings this environment resolves, as the CLI resolves them."""
    from server.llm.client import get_llm_client
    from server.settings import settings

    client = get_llm_client()
    provider, model = str(getattr(client, "provider", "")), str(getattr(client, "model", ""))
    return provider, model, settings.llm_fallback_model.strip()


def _fail(message: str) -> int:
    print(message, file=sys.stderr)
    return 2


def cmd_run(a: argparse.Namespace) -> int:
    ts = load(a.taskset)
    cfg = _config(a)
    # Rows a sync wrote carry the files it linked; rows this run extracts get theirs from
    # the code index, or have none.
    cfg.arm4_files = bool(a.code_index or a.arm4_namespace)
    if 4 in cfg.arms and not cfg.arm4_files:
        print("arm 4 has no code index: its rows reach loom_search only, no pre-edit block")
        if 3 in cfg.arms and 5 not in cfg.arms:
            cfg.arms = [*cfg.arms, 5]
            print("arm 5 (gold rows through loom_search only) is added as its reference")
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
                "--offline with arm 4 needs LOOM_LLM_PROVIDER=fake: the extraction "
                f"would call {provider}"
            )
        cfg.extract_model = model
    if a.dry_run:
        runner.print_plan(runner.plan(ts, cfg))
        return 0
    if not a.base_url:
        return _fail("--base-url (or LOOM_BASE_URL) names the run's own Loom server")
    if a.code_index and not (a.code_index_org and a.code_index_namespace):
        return _fail("--code-index needs --code-index-org and --code-index-namespace")
    if not a.offline:
        models = [cfg.reader, *cfg.judges] + (
            [cfg.extract_model] if runner.own_extraction(cfg) else []
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
