"""Offline replay CLI. Live adapters require a separately approved controller."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import ReplayContext, ReplayProvider
from .engine import Factory
from .model import MODES, Task, digest
from .report import compare
from .workspace import ObjectiveGrader

ROOT = Path(__file__).resolve().parent


def run_task(task_id: str, output: Path, mode: str) -> dict:
    suite = json.loads((ROOT / "suite.json").read_text())
    frozen = suite["tasks"][task_id]
    inputs = {}
    for kind in ("task", "grader", "replay"):
        path = ROOT / frozen[kind]["path"]
        data = json.loads(path.read_text())
        if digest(data) != frozen[kind]["sha256"]:
            raise ValueError(f"Frozen {kind} hash mismatch")
        inputs[kind] = path
    task = Task.load(inputs["task"])
    provider = ReplayProvider(inputs["replay"])
    grader = ObjectiveGrader(inputs["grader"], frozen["grader"]["sha256"])
    backend = None if mode == "baseline" else ReplayContext(task)
    return Factory(task, provider, grader, output, mode=mode, backend=backend).run()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one frozen authored task with scripted actions")
    run.add_argument("--task", required=True)
    run.add_argument("--mode", choices=MODES, required=True)
    run.add_argument("--output", type=Path, required=True)
    smoke = commands.add_parser("smoke", help="Replay all three authored tasks in all modes")
    smoke.add_argument("--output", type=Path, required=True)
    report = commands.add_parser(
        "compare", help="Compare explicit result files, retaining failures"
    )
    report.add_argument("results", nargs="+", type=Path)
    report.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.command == "run":
        result = run_task(args.task, args.output, args.mode)
        print(json.dumps(result, indent=2))
        return 0 if result["resolved"] else 1
    if args.command == "compare":
        result = compare([json.loads(p.read_text()) for p in args.results])
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        return 0
    args.output.mkdir(parents=True, exist_ok=False)
    results = [
        run_task(task, args.output / f"{task}-{mode}", mode)
        for task in json.loads((ROOT / "suite.json").read_text())["tasks"]
        for mode in MODES
    ]
    report = compare(results)
    report["runtime"] = {"python": sys.version, "executable": sys.executable}
    (args.output / "comparison.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if all(r["resolved"] for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
