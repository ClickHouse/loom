"""Grade a coding agent's answer to a real work question against its reference.

`bench/fixtures/code_work_v2.jsonl` holds questions an engineer on the database
team asks while working (root-cause a report, plan a change, explain a mechanism,
size an impact, find why behaviour changed), each with a reference answer
verified in the code: the key points a correct answer must state, the functions
involved and, for an impact question, every caller.

A judge model marks each key point covered or not and says whether the answer
states something the reference contradicts. Two checks need no judge: the share
of the reference functions the answer names (by their last two `::` parts), and
for an impact question the callers named against the reference set.

    python -m bench.agent_work_judge --runs eval.jsonl --fixture bench/fixtures/code_work_v2.jsonl \
        --out judged.jsonl [--model claude-opus-5-5]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
from pathlib import Path
from typing import Any

import httpx

from bench.judges import achat

JUDGE_SYSTEM = (
    "You grade an engineer's answer about a C++ codebase against a reference that was "
    "verified in the code. Judge substance, not wording: a key point is covered when the "
    "answer states the same fact (same function or mechanism), even in other words; it is "
    "not covered when the answer omits it, hedges between alternatives, or names a "
    "different function. Reply with JSON only."
)


def judge_prompt(question: str, reference: dict[str, Any], answer: str) -> str:
    points = "\n".join(f"{i + 1}. {p}" for i, p in enumerate(reference.get("key_points") or []))
    return (
        f"QUESTION\n{question}\n\n"
        f"REFERENCE ANSWER\n{reference.get('answer', '')}\n\n"
        f"KEY POINTS\n{points}\n\n"
        f"ANSWER TO GRADE\n{answer or '(no answer)'}\n\n"
        'Reply with {"covered": [true|false per key point, in order], '
        '"contradicts": true|false, "note": "<one sentence>"}'
    )


def parse_verdict(text: str, n_points: int) -> dict[str, Any]:
    """The judge's JSON, fenced or not; a reply that does not parse covers nothing."""
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        data = json.loads(m.group(0)) if m else {}
    except ValueError:
        data = {}
    covered = [bool(x) for x in (data.get("covered") or [])][:n_points]
    covered += [False] * (n_points - len(covered))
    return {
        "covered": covered,
        "contradicts": bool(data.get("contradicts")),
        "note": str(data.get("note") or "")[:300],
    }


def answer_score(verdict: dict[str, Any]) -> float:
    """Share of key points covered, halved when the answer contradicts the reference
    (a confident wrong statement costs the engineer more than a gap)."""
    covered = verdict.get("covered") or []
    share = sum(covered) / len(covered) if covered else 0.0
    return round(share / 2 if verdict.get("contradicts") else share, 3)


def _tail(name: str) -> str:
    parts = [p for p in re.split(r"::|\.", name.strip()) if p]
    return "::".join(parts[-2:]) if len(parts) > 1 else (parts[0] if parts else "")


def function_recall(answer: str, functions: list[str]) -> float | None:
    """Share of the reference functions the answer names (`Class::method` or the bare
    name when the reference has no class); None when the reference names none."""
    if not functions:
        return None
    text = answer or ""
    hits = 0
    for f in functions:
        tail = _tail(f)
        if not tail:
            continue
        *owner, name = tail.split("::")
        named = re.search(rf"\b{re.escape(name)}\b", text) is not None
        # a method counts when the answer names it with its class, together or apart
        if tail in text or (named and (not owner or owner[0] in text)):
            hits += 1
    return round(hits / len(functions), 3)


async def judge_rows(
    rows: list[dict[str, Any]], fixture: dict[str, dict[str, Any]], *, model: str, lanes: int = 4
) -> list[dict[str, Any]]:
    gate = asyncio.Semaphore(lanes)
    async with httpx.AsyncClient() as client:

        async def one(row: dict[str, Any]) -> dict[str, Any]:
            q = fixture[row["instance_id"]]
            ref = q["reference"]
            answer = str(row.get("answer_text") or "")
            async with gate:
                text, _ = await achat(
                    client,
                    model,
                    judge_prompt(q["query"], ref, answer),
                    max_tokens=800,
                    system=JUDGE_SYSTEM,
                )
            verdict = parse_verdict(text, len(ref.get("key_points") or []))
            return {
                **row,
                "judge": verdict,
                "answer_score": answer_score(verdict),
                "function_recall": function_recall(answer, list(ref.get("functions") or [])),
            }

        return list(await asyncio.gather(*(one(r) for r in rows)))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", required=True, nargs="+")
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--model", default="claude-opus-5-5")
    args = ap.parse_args(argv)
    fixture = {
        q["id"]: q
        for q in (
            json.loads(line) for line in Path(args.fixture).read_text().splitlines() if line.strip()
        )
    }
    rows = [
        json.loads(line)
        for f in args.runs
        for line in Path(f).read_text().splitlines()
        if line.strip()
    ]
    rows = [r for r in rows if r.get("instance_id") in fixture]
    judged = asyncio.run(judge_rows(rows, fixture, model=args.model))
    Path(args.out).write_text("".join(json.dumps(r) + "\n" for r in judged))
    print(f"judged {len(judged)} answers -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
