"""Forget / switch / correction eval for set_from_messages.

Runs the REAL extraction step (the configured LLM) and the REAL write step of
the memory pipeline on each case, with storage faked: the existing memories
are the case's context rows, `_retire_forgotten` and `handle_memory_bulk_set`
record what they were asked to do, and nothing reaches ClickHouse. The end
state is scored against the extraction contract:

- a correction writes the new truth (`must_write`: every group's words appear
  in one written value);
- an explicit request to forget retires its target (`must_retire`);
- nothing outside `must_retire | may_retire` is retired (the ADD-ONLY rule:
  a new value for an attribute is its own dated fact, the prior one stays
  unless the user asked to forget it);
- a pure forget does not re-store what it forgot (`not_written`, whole words).

Usage (inside a server container, a copy of the tree under test first on
sys.path):  python bench/forget_switch_eval.py --tier compact --runs 2
"""

from __future__ import annotations

import argparse
import asyncio
import contextvars
import json
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Any

EXISTING: list[dict[str, str]] = [
    {"memory_key": "user_editor", "value": "User prefers Neovim."},
    {"memory_key": "user_deploy_day", "value": "User deploys on Tuesdays."},
    {
        "memory_key": "user_retries_backoff",
        "value": "User decided retries back off exponentially, capped at 30 seconds.",
    },
    {"memory_key": "user_coffee_preference", "value": "User prefers dark roast coffee."},
    {"memory_key": "user_city", "value": "User lives in Berlin."},
    {"memory_key": "user_manager", "value": "User's manager is Priya."},
    {"memory_key": "user_salary", "value": "User earns $120k a year."},
    {"memory_key": "user_diet", "value": "User is vegetarian."},
    {"memory_key": "user_ci_runner", "value": "CI runs on 8 vCPU runners."},
    {"memory_key": "user_standup_time", "value": "Team standup is at 9:30am."},
    {"memory_key": "user_language_learning", "value": "User is learning Rust."},
    {"memory_key": "user_gym_days", "value": "User goes to the gym on Mondays and Thursdays."},
]


@dataclass
class Case:
    name: str
    kind: str
    turns: list[tuple[str, str]]
    must_write: list[list[str]] = field(default_factory=list)
    must_retire: set[str] = field(default_factory=set)
    may_retire: set[str] = field(default_factory=set)
    not_written: list[str] = field(default_factory=list)


def _u(t: str) -> tuple[str, str]:
    return ("user", t)


def _a(t: str) -> tuple[str, str]:
    return ("assistant", t)


CASES: list[Case] = [
    # forget + replacement, explicit forget verb
    Case(
        "fr_editor",
        "forget_replace",
        [_u("forget that I prefer Neovim, I switched to VS Code")],
        [["vs", "code"]],
        {"user_editor"},
    ),
    Case(
        "fr_coffee",
        "forget_replace",
        [_u("Forget the dark roast thing — I drink light roast now.")],
        [["light", "roast"]],
        {"user_coffee_preference"},
    ),
    Case(
        "fr_city",
        "forget_replace",
        [_u("please forget I live in Berlin, I moved to Lisbon")],
        [["lisbon"]],
        {"user_city"},
    ),
    Case(
        "fr_deploy",
        "forget_replace",
        [_u("forget what I said about Tuesdays, we deploy on Thursdays now")],
        [["thursday"]],
        {"user_deploy_day"},
    ),
    Case(
        "fr_retries",
        "forget_replace",
        [_u("forget the 30s cap, retries now cap at 60s")],
        [["60"]],
        {"user_retries_backoff"},
    ),
    Case(
        "fr_rust",
        "forget_replace",
        [_u("forget Rust — I'm learning Haskell instead of Rust now")],
        [["haskell"]],
        {"user_language_learning"},
    ),
    Case(
        "fr_emacs",
        "forget_replace",
        [_u("don't keep the Neovim preference, I'm on Emacs")],
        [["emacs"]],
        {"user_editor"},
    ),
    Case(
        "fr_vegan",
        "forget_replace",
        [_u("please stop remembering that I'm vegetarian — I'm vegan now")],
        [["vegan"]],
        {"user_diet"},
    ),
    Case(
        "fr_manager",
        "forget_replace",
        [_u("Forget Priya as my manager; my new manager is Dana")],
        [["dana"]],
        {"user_manager"},
    ),
    Case(
        "fr_standup_inline",
        "forget_replace",
        [_u("my standup is at 9:30. no wait, forget that — it's 9:45")],
        [["9:45"]],
        set(),
        {"user_standup_time"},
    ),
    # "no longer" / "anymore" — a switch, no forget verb (ADD-ONLY: old may stay)
    Case(
        "nl_editor",
        "switch",
        [_u("I no longer use Neovim, I use Zed now")],
        [["zed"]],
        set(),
        {"user_editor"},
    ),
    Case(
        "nl_diet",
        "switch",
        [_u("I'm no longer vegetarian, I eat fish now")],
        [["fish"]],
        set(),
        {"user_diet"},
    ),
    Case(
        "nl_manager",
        "switch",
        [_u("Priya is no longer my manager — it's Omar now")],
        [["omar"]],
        set(),
        {"user_manager"},
    ),
    Case(
        "nl_gym",
        "partial",
        [_u("I no longer go to the gym on Mondays, only Thursdays")],
        [["thursday"]],
        set(),
        {"user_gym_days"},
    ),
    Case(
        "nl_city",
        "switch",
        [_u("I no longer live in Berlin; Lisbon is home now")],
        [["lisbon"]],
        set(),
        {"user_city"},
    ),
    Case(
        "nl_retries",
        "switch",
        [_u("we don't cap retries at 30s anymore; the cap is 2 minutes")],
        [["2", "minute"]],
        set(),
        {"user_retries_backoff"},
    ),
    # "actually" corrections
    Case(
        "ac_standup",
        "correction",
        [_u("actually, standup moved to 10am")],
        [["10"]],
        set(),
        {"user_standup_time"},
    ),
    Case(
        "ac_salary",
        "correction",
        [_u("Actually I earn $135k now after the raise")],
        [["135"]],
        set(),
        {"user_salary"},
    ),
    Case(
        "ac_ci",
        "correction",
        [_u("actually we run CI on 16 vCPU runners since last week")],
        [["16"]],
        set(),
        {"user_ci_runner"},
    ),
    # "scratch that"
    Case(
        "st_deploy",
        "scratch",
        [_u("we deploy on Fridays"), _a("Noted."), _u("scratch that, Thursdays")],
        [["thursday"]],
        set(),
        {"user_deploy_day"},
    ),
    Case(
        "st_zig",
        "scratch",
        [_u("I'm learning Go. Scratch that — I switched to learning Zig.")],
        [["zig"]],
        set(),
        {"user_language_learning"},
    ),
    # switch stated without any correction marker
    Case(
        "sw_city",
        "switch",
        [_u("I moved to Amsterdam last month")],
        [["amsterdam"]],
        set(),
        {"user_city"},
    ),
    Case(
        "sw_coffee",
        "switch",
        [_u("I switched from dark roast to oat flat whites")],
        [["flat", "white"]],
        set(),
        {"user_coffee_preference"},
    ),
    Case(
        "sw_retries",
        "switch",
        [_u("we changed the retry cap to 45 seconds")],
        [["45"]],
        set(),
        {"user_retries_backoff"},
    ),
    Case(
        "pc_gym",
        "partial",
        [_u("gym days are Mondays and Saturdays now, not Thursdays")],
        [["monday", "saturday"]],
        set(),
        {"user_gym_days"},
    ),
    Case(
        "pc_manager_same",
        "not_correction",
        [_u("my manager is still Priya but she's moving to the Paris office")],
        [["paris"]],
        set(),
        set(),
    ),
    # forget with no replacement
    Case(
        "fo_salary",
        "forget",
        [_u("please forget my salary")],
        [],
        {"user_salary"},
        set(),
        ["120k", "\\$120k", "120"],
    ),
    Case(
        "fo_diet",
        "forget",
        [_u("don't remember that I'm vegetarian")],
        [],
        {"user_diet"},
        set(),
        ["vegetarian"],
    ),
    Case(
        "fo_coffee",
        "forget",
        [_u("forget about my coffee preference")],
        [],
        {"user_coffee_preference"},
        set(),
        ["dark roast"],
    ),
    Case(
        "fo_city",
        "forget",
        [_u("Can you forget where I live?")],
        [],
        {"user_city"},
        set(),
        ["berlin"],
    ),
    Case(
        "fo_standup",
        "forget",
        [_u("delete the note about my standup time")],
        [],
        {"user_standup_time"},
        set(),
        ["9:30"],
    ),
    # forget of something never stored
    Case("fn_shoe", "forget_absent", [_u("forget my shoe size")], [], set(), set(), ["shoe"]),
    Case(
        "fn_passport",
        "forget_absent",
        [_u("please forget my passport number")],
        [],
        set(),
        set(),
        ["passport"],
    ),
    # restatements that are not corrections
    Case("rs_editor", "restatement", [_u("I still use Neovim every day")]),
    Case("rs_deploy", "restatement", [_u("as I said, we deploy on Tuesdays")]),
    Case("rs_coffee", "restatement", [_u("I love my dark roast in the mornings")]),
    # hypotheticals / reminders / behaviour — not a forget
    Case(
        "hy_charger",
        "not_forget",
        [_u("if I forget my laptop charger again, remind me to buy a spare")],
    ),
    Case("hy_remind", "not_forget", [_u("don't forget to remind me about standup tomorrow")]),
    Case("hy_always", "not_forget", [_u("I always forget that the deploy is on Tuesdays")]),
    Case("hy_whatif", "not_forget", [_u("what would happen if I forgot to set the retry cap?")]),
    Case("hy_emoji", "not_forget", [_u("stop using emojis in your replies")]),
    Case("hy_forget_it", "not_forget", [_u("forget it, let's move on")]),
    # forget + an unrelated new fact in the same turn
    Case(
        "fu_salary_cat",
        "forget_plus_new",
        [_u("forget my salary. Also I got a cat named Miso")],
        [["miso"]],
        {"user_salary"},
        set(),
        ["120k", "120"],
    ),
    Case(
        "fu_city_dog",
        "forget_plus_new",
        [_u("I adopted a dog named Rex, and please forget that I live in Berlin")],
        [["rex"]],
        {"user_city"},
        set(),
        ["berlin"],
    ),
    # the premise and its correction in one batch
    Case(
        "bp_tea",
        "batch_premise",
        [
            _u("I prefer tea in the morning"),
            _a("Noted — tea in the morning."),
            _u("forget that, I prefer coffee in the morning"),
        ],
        [["coffee", "morning"]],
        set(),
        set(),
        ["tea"],
    ),
]


def _words(text: str) -> str:
    return " " + re.sub(r"[^a-z0-9:$]+", " ", text.lower()) + " "


def _has(text: str, word: str) -> bool:
    w = word.lower().replace("\\", "")
    t = _words(text)
    return f" {w}" in t if w[-1:].isalpha() else w in t


def score(case: Case, retired: set[str], written: list[dict[str, str]]) -> tuple[bool, list[str]]:
    why: list[str] = []
    values = [w["value"] for w in written]
    for group in case.must_write:
        if not any(all(_has(v, g) for g in group) for v in values):
            why.append(f"missing write {group}")
    for k in case.must_retire - retired:
        why.append(f"not retired {k}")
    extra = retired - case.must_retire - case.may_retire
    extra = {k for k in extra if k in {e["memory_key"] for e in EXISTING}}
    if extra:
        why.append(f"over-retired {sorted(extra)}")
    for word in case.not_written:
        hits = [v for v in values if _has(v, word)]
        if hits:
            why.append(f"re-stored {word!r}: {hits[0][:60]}")
    return (not why), why


_SINK: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("sink")
_PATCHED = False


def _install_fakes() -> None:
    """Patch storage once; each case's asyncio task carries its own sink in
    a ContextVar, so concurrent cases never write into each other's."""
    global _PATCHED
    if _PATCHED:
        return
    from server.memory.app import bulk_set as bulk_set_mod
    from server.memory.app import drops as _drops
    from server.memory.app import pipeline as mp
    from server.memory.app import pipeline_state as pipeline_state

    async def _retire(state: Any, keys: set[str]) -> int:
        _SINK.get()["retired"].update(keys)
        return len(keys)

    async def _fetch(state: Any, requests: list[dict[str, Any]], *, limit: int = 10) -> list:
        return []  # the case's existing rows are already the candidates

    async def _bulk(inp: Any) -> Any:
        for e in inp.entries:
            _SINK.get()["written"].append({"memory_key": e.memory_key, "value": e.value})
        return type("O", (), {"written": [], "stage_ms": {}})()

    async def _nw(state: Any) -> bool:
        return True

    mp._retire_forgotten = _retire
    mp._fetch_for_forget_requests = _fetch
    mp._newest_wins = _nw
    bulk_set_mod.handle_memory_bulk_set = _bulk
    _drops.record_drop = lambda *a, **k: None
    if hasattr(mp, "record_drop"):
        mp.record_drop = lambda *a, **k: None
    _PATCHED = True


async def run_case(case: Case, tier: str) -> dict[str, Any]:
    from server.memory.app import pipeline as mp
    from server.memory.app import pipeline_state as pipeline_state

    _install_fakes()
    sink: dict[str, Any] = {"retired": set(), "written": []}
    _SINK.set(sink)
    retired: set[str] = sink["retired"]
    written: list[dict[str, str]] = sink["written"]
    state = pipeline_state.WorkflowState(
        org="clickhouse",
        namespace="bench-forget-switch",
        agent="forget-switch-eval",
        messages=[{"role": r, "content": t} for r, t in case.turns],
        observation_date="2026-09-24",
        existing_memories=[dict(e) for e in EXISTING],
        llm_cache=False,
    )
    t0 = time.monotonic()
    state = await mp._step_llm_extract(state, {})
    extracted = [
        {
            "key": f["key"],
            "value": f["value"],
            "forget": f.get("forget"),
            "linked": f.get("linked_keys"),
            "tags": f.get("tags"),
        }
        for f in state.extracted_facts
    ]
    state = await mp._step_bulk_write(state, {})
    ok, why = score(case, retired, written)
    return {
        "case": case.name,
        "kind": case.kind,
        "tier": tier,
        "ok": ok,
        "why": why,
        "retired": sorted(retired),
        "written": written,
        "extracted": extracted,
        "tokens_in": state.tokens_in,
        "ms": int((time.monotonic() - t0) * 1000),
    }


async def main() -> int:
    from server.wiring import wire

    wire()  # the insert observers (write marks, recent writes) this run's handlers feed
    ap = argparse.ArgumentParser()
    ap.add_argument("--tier", choices=["compact", "full"], default="compact")
    ap.add_argument("--runs", type=int, default=2)
    ap.add_argument("--out", default="")
    ap.add_argument("--only", default="")
    ap.add_argument("--concurrency", type=int, default=6)
    args = ap.parse_args()
    from server.core.ch import get_client
    from server.settings import settings

    await get_client().connect()
    if args.tier == "full":
        settings.memory_extract_compact_prompt_max_chars = 0
    cases = [c for c in CASES if not args.only or c.name in args.only.split(",")]
    sem = asyncio.Semaphore(args.concurrency)

    async def one(c: Case, run: int) -> dict[str, Any]:
        async with sem:
            r = await run_case(c, args.tier)
            r["run"] = run
            return r

    results = await asyncio.gather(*(one(c, r) for r in range(args.runs) for c in cases))
    by_run: dict[int, int] = {}
    for r in results:
        by_run[r["run"]] = by_run.get(r["run"], 0) + int(r["ok"])
    fails = [r for r in results if not r["ok"]]
    for r in sorted(fails, key=lambda x: (x["case"], x["run"])):
        shown = [w["value"][:50] for w in r["written"]]
        print(f"FAIL {r['case']:<20} run{r['run']} {r['why']}  written={shown}")
    tin = [r["tokens_in"] for r in results]
    print(
        json.dumps(
            {
                "tier": args.tier,
                "cases": len(cases),
                "runs": args.runs,
                "pass_by_run": by_run,
                "tokens_in_mean": round(sum(tin) / len(tin)),
                "by_kind": _by_kind(results),
            }
        )
    )
    if args.out:
        with open(args.out, "w") as f:
            json.dump(results, f, indent=1, default=str)
    return 1 if fails else 0


def _by_kind(results: list[dict[str, Any]]) -> dict[str, str]:
    agg: dict[str, list[int]] = {}
    for r in results:
        a = agg.setdefault(r["kind"], [0, 0])
        a[0] += int(r["ok"])
        a[1] += 1
    return {k: f"{v[0]}/{v[1]}" for k, v in sorted(agg.items())}


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
