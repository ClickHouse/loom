"""What an agent reads to answer a definition or caller question with grep, beside Loom's answer.

For each `symbol_def` and `callers` question of `bench/fixtures/code_intel_qa.jsonl`, runs
`git grep -n -w` for the symbol's last name component over a checkout of the indexed
repository, the way an agent without the code index would look, and records the lines,
files and tokens (characters / 4) that come back and whether the gold file is among them.
Paired with a `bench/code_intel_qa.py --dump` file, it puts Loom's answer size and
correctness beside each grep. No LLM, no network.

    python -m bench.code_grep_baseline --checkout <ClickHouse checkout> \\
        --loom bench/results/code_intel_qa.jsonl --out out/code_grep_baseline.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from statistics import median
from typing import Any

FIXTURE = Path(__file__).parent / "fixtures" / "code_intel_qa.jsonl"


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("//"):
            rows.append(json.loads(line))
    return rows


def symbol_of(q: dict[str, Any]) -> str:
    gold = q["gold"]
    name = gold.get("symbol") or gold.get("qualified_name") or q["query"]
    return str(name).split("::")[-1]


def qualified_of(q: dict[str, Any]) -> str:
    """`Class::name` without the namespace: the narrowest string a careful agent greps."""
    gold = q["gold"]
    parts = str(gold.get("symbol") or gold.get("qualified_name") or q["query"]).split("::")
    parts = [p for p in parts if p != "DB"]
    return "::".join(parts[-2:])


def grep(checkout: str, name: str, word: bool = True) -> list[str]:
    flags = ["-w"] if word else ["-F"]
    out = subprocess.run(
        ["git", "-C", checkout, "grep", "-n", *flags, "-I", "--no-color", name],
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return [line for line in out.splitlines() if line]


def caller_names_found(lines: list[str], expected: list[str]) -> int:
    """Expected callers whose own name appears in a grep line: an upper bound on what
    the agent could attribute without opening the files."""
    text = "\n".join(lines)
    return sum(e.split("::")[-1] in text for e in expected)


def row(q: dict[str, Any], checkout: str, loom: dict[str, Any] | None) -> dict[str, Any]:
    lines = grep(checkout, symbol_of(q))
    files = sorted({ln.split(":", 1)[0] for ln in lines})
    qlines = grep(checkout, qualified_of(q), word=False)
    out: dict[str, Any] = {
        "qid": q["id"],
        "type": q["type"],
        "symbol": symbol_of(q),
        "grep_lines": len(lines),
        "grep_files": len(files),
        "grep_tokens": sum(len(ln) + 1 for ln in lines) // 4,
        "qualified": qualified_of(q),
        "qualified_grep_lines": len(qlines),
        "qualified_grep_files": len({ln.split(":", 1)[0] for ln in qlines}),
        "qualified_grep_tokens": sum(len(ln) + 1 for ln in qlines) // 4,
    }
    gold = q["gold"]
    if q["type"] == "symbol_def":
        prefix = gold.get("path_prefix", "")
        out["grep_files_under_gold_prefix"] = sum(f.startswith(prefix) for f in files)
    else:
        expected = list(gold.get("expected_callers") or [])
        out["expected_callers"] = len(expected)
        out["expected_callers_named_in_grep"] = caller_names_found(lines, expected)
    if loom:
        out["loom_tokens"] = int(loom.get("tokens_returned") or 0)
        if q["type"] == "symbol_def":
            prefix = gold.get("path_prefix", "")
            paths = loom.get("paths") or []
            out["loom_correct_first"] = bool(paths) and str(paths[0]).startswith(prefix)
        else:
            got = {str(c).split("(")[0] for c in loom.get("callers") or []}
            out["loom_callers_returned"] = len(got)
            out["loom_expected_callers_found"] = sum(
                e in got for e in gold.get("expected_callers") or []
            )
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--checkout", required=True, help="a git checkout of the indexed repository")
    ap.add_argument("--fixture", default=str(FIXTURE))
    ap.add_argument("--loom", default="", help="a bench/code_intel_qa.py --dump file")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    questions = [
        q for q in load_jsonl(Path(args.fixture)) if q["type"] in ("symbol_def", "callers")
    ]
    loom = {r["qid"]: r for r in load_jsonl(Path(args.loom))} if args.loom else {}
    rows = [row(q, args.checkout, loom.get(q["id"])) for q in questions]
    summary: dict[str, Any] = {"n": len(rows)}
    for kind in ("symbol_def", "callers"):
        rs = [r for r in rows if r["type"] == kind]
        if not rs:
            continue
        summary[kind] = {
            "n": len(rs),
            "grep_tokens_median": median(r["grep_tokens"] for r in rs),
            "grep_files_median": median(r["grep_files"] for r in rs),
            "qualified_grep_tokens_median": median(r["qualified_grep_tokens"] for r in rs),
            "qualified_grep_files_median": median(r["qualified_grep_files"] for r in rs),
        }
        loom_tokens = [r["loom_tokens"] for r in rs if r.get("loom_tokens")]
        if loom_tokens:
            summary[kind]["loom_tokens_median"] = median(loom_tokens)
    print(json.dumps(summary, indent=1))
    if args.out:
        Path(args.out).write_text(json.dumps({"summary": summary, "rows": rows}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
