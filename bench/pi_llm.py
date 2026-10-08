"""Proactive-interference (PI) stale-retrieval benchmark for loom.

Adapted from the proactive-interference paradigm: an entity's attribute is UPDATED n
times, then queried for its CURRENT value. Every prior value is stale and constitutes
interference; a read that systematically returns an OVERWRITTEN value is the failure
being measured. This bench turns that into a number loom does not otherwise measure: the
**stale-retrieval rate**, not just accuracy.

Unlike LoCoMo (mixed reasoning), this isolates supersession/staleness on loom's
real ingest+ask path: each update is a separate `set_from_messages` call with an
increasing observation_date (so valid_at orders them), and the query goes
through `memory.ask` synthesis. Scoring is deterministic (distinct coined value
tokens, substring match) — no judge, so it is cheap and repeatable.

Metrics per PI depth n:
  accuracy       answer contains the LATEST value
  stale_rate     answer contains ANY superseded value (the PI failure)
  clean_correct  latest present AND no superseded present (strictest)
  abstain_rate   answer contains no value token at all

Each (depth, episode) gets its own namespace under `--namespace` plus a per-run id,
registered as bench and erased when the run ends (`--keep-namespaces` keeps them).
Exit 1 when every episode of the run errored.

Usage:
  LOOM_API_KEY=... python bench/pi_llm.py --base-url http://localhost:7777 \\
      --namespace pi-bench --episodes 20 --depths 1,2,5,10,15,20,30
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from datetime import date, timedelta

import httpx

try:
    from bench._gate import measurement_fields
    from bench.bench_namespaces import (
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )
except ModuleNotFoundError:  # `python bench/pi_llm.py` puts bench/ itself on sys.path
    from _gate import measurement_fields  # type: ignore[no-redef]
    from bench_namespaces import (  # type: ignore[no-redef]
        erase_on_exit,
        register_bench_namespace,
        remember_bench_namespace,
    )

_ENTITIES = [
    "Quorvex",
    "Vantrelle",
    "Blenthar",
    "Crumwald",
    "Drovanni",
    "Eskalith",
    "Fenwarden",
    "Glimmerbeck",
    "Hollowmere",
    "Ivensport",
    "Jarravel",
    "Kessleroth",
    "Lundquaith",
    "Marrowfen",
    "Nesterling",
    "Ombervale",
    "Pryndle",
    "Quillhaven",
    "Rendashire",
    "Stroventy",
    "Thornquist",
    "Ulmarrow",
    "Vexbourne",
    "Wrenfallow",
    "Xanthemoor",
    "Ysolde",
    "Zircumbe",
    "Almswick",
    "Brindlecap",
    "Cindervale",
]
# Distinct coined value tokens; a run uses globally-unique values so a substring
# match is unambiguous (value = f"{_VALUES[k]}-e{ep}").
_VALUES = [
    "azimuthal",
    "borealis",
    "cindral",
    "duskwane",
    "emberlyn",
    "fjordic",
    "gravamen",
    "halcyon",
    "isolde",
    "jubilant",
    "kestrel",
    "luminaire",
    "marrow",
    "nocturne",
    "oberon",
    "pellucid",
    "quixotic",
    "riparian",
    "sable",
    "tempest",
    "umbral",
    "verdant",
    "wisteria",
    "xenolith",
    "yarrow",
    "zenithal",
    "argent",
    "bryony",
    "cobalt",
    "dahlia",
]


def _val(ep: int, k: int) -> str:
    return f"{_VALUES[k % len(_VALUES)]}-e{ep}"


_EPOCH = date(2026, 1, 1)


def update_text(entity: str, k: int, value: str, base_day: int = 1) -> tuple[str, str]:
    """(observation_date, sentence) of the k-th update. The dates strictly
    increase with k at any depth, so valid_at orders the updates the way the
    bench scores them: the last one is the current value."""
    day = (_EPOCH + timedelta(days=base_day + k)).isoformat()
    text = (
        f"Project status update: as of {day}, {entity}'s assigned module "
        f"is now {value} (this supersedes any earlier assignment)."
    )
    return day, text


async def _post(cl: httpx.AsyncClient, base: str, path: str, body: dict, tok: str) -> dict:
    for _ in range(5):
        try:
            r = await cl.post(
                base + path, json=body, headers={"Authorization": f"Bearer {tok}"}, timeout=180
            )
            if r.status_code == 200:
                return r.json()
            if r.status_code in (429, 500, 502, 503):
                await asyncio.sleep(3)
                continue
            return {"_err": r.status_code, "_body": r.text[:160]}
        except (httpx.HTTPError, ValueError):
            await asyncio.sleep(3)
    return {"_err": "retries"}


async def run_episode(cl, base, tok, ns, org, ep, depth, base_day, credit=False) -> dict:
    entity = _ENTITIES[ep % len(_ENTITIES)] + f"{ep}"
    # n updates to the same entity attribute, oldest -> newest.
    for k in range(depth):
        obs_date, text = update_text(entity, k, _val(ep, k), base_day)
        body = {
            "org": org,
            "namespace": ns,
            "agent": "pi",
            "user_id": f"ep{ep}",
            "session_id": f"ep{ep}-s{k}",
            "messages": [{"role": "user", "content": text}],
            "max_tokens": 1024,
            "observation_date": obs_date,
        }
        res = await _post(cl, base, "/v1/memory.set_from_messages", body, tok)
        if res.get("_err"):
            return {"ep": ep, "depth": depth, "error": res}
    # query the CURRENT value
    q = f"What module is {entity} currently assigned to? Give the current module only."
    ask = {**measurement_fields("ask", credit=credit), "org": org, "namespace": ns, "query": q}
    ans = await _post(cl, base, "/v1/memory.ask", ask, tok)
    if ans.get("_err"):
        return {"ep": ep, "depth": depth, "error": ans}
    answer = (ans.get("answer") or "").lower()
    latest = _val(ep, depth - 1).lower()
    stale_vals = [_val(ep, k).lower() for k in range(depth - 1)]
    has_latest = latest in answer
    has_stale = any(sv in answer for sv in stale_vals)
    has_any = has_latest or has_stale
    return {
        "ep": ep,
        "depth": depth,
        "source": ans.get("source"),
        "has_latest": has_latest,
        "has_stale": has_stale,
        "clean_correct": has_latest and not has_stale,
        "abstain": not has_any,
        "answer": answer[:120],
    }


def _parse(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    p.add_argument("--org", default="dev")
    p.add_argument("--namespace", default="pi-bench", help="namespace prefix")
    p.add_argument("--episodes", type=int, default=20)
    p.add_argument("--depths", default="1,2,5,10,15,20,30")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument(
        "--keep-namespaces", action="store_true", help="do not erase this run's namespaces"
    )
    p.add_argument(
        "--credit",
        action="store_true",
        help="send the server's crediting default (ask reinforce) instead of the "
        "evaluation-read field",
    )
    return p.parse_args(argv)


def _row(depth: int, res: list[dict]) -> dict:
    ok = [r for r in res if not r.get("error")]
    n = max(1, len(ok))
    return {
        "depth": depth,
        "n": len(ok),
        "errors": len(res) - len(ok),
        "accuracy": 100 * sum(r["has_latest"] for r in ok) / n,
        "stale_rate": 100 * sum(r["has_stale"] for r in ok) / n,
        "clean_correct": 100 * sum(r["clean_correct"] for r in ok) / n,
        "abstain": 100 * sum(r["abstain"] for r in ok) / n,
    }


async def main(argv: list[str] | None = None) -> int:
    args = _parse(argv)
    tok = args.token
    if not tok:
        print("need --token or LOOM_API_KEY", file=sys.stderr)
        return 2
    depths = [int(x) for x in args.depths.split(",") if x.strip()]
    sem = asyncio.Semaphore(args.concurrency)
    run_id = uuid.uuid4().hex[:6]

    print("depth  n  accuracy  stale_rate  clean_correct  abstain")
    rows = []
    async with httpx.AsyncClient() as cl:

        async def post(path: str, body: dict) -> dict:
            out = await _post(cl, args.base_url, path, body, tok)
            if out.get("_err"):
                raise RuntimeError(f"{path}: {out}")
            return out

        async with erase_on_exit(post, org=args.org, keep=args.keep_namespaces):
            for depth in depths:
                # ONE entity per namespace: the PI paradigm isolates a single
                # entity per episode. Near-identical template sentences of
                # different entities in one namespace sit above the conflict
                # threshold (only the subject differs) and flag each other,
                # which is a bench artifact, not same-entity interference.
                async def _one(ep, depth=depth):
                    ns = f"{args.namespace}-{run_id}-d{depth}-e{ep}"
                    remember_bench_namespace(args.org, ns)
                    await register_bench_namespace(
                        post,
                        org=args.org,
                        namespace=ns,
                        description=f"PI depth {depth} episode {ep}",
                        keep=args.keep_namespaces,
                    )
                    async with sem:
                        return await run_episode(
                            cl, args.base_url, tok, ns, args.org, ep, depth, 1, args.credit
                        )

                row = _row(depth, await asyncio.gather(*[_one(ep) for ep in range(args.episodes)]))
                print(
                    f"{depth:5d}  {row['n']:2d}  {row['accuracy']:7.1f}%  "
                    f"{row['stale_rate']:8.1f}%  {row['clean_correct']:11.1f}%  "
                    f"{row['abstain']:6.1f}%   errs={row['errors']}"
                )
                rows.append(row)
    # headline
    hi = [r for r in rows if r["depth"] >= 10]
    if hi:
        avg_stale = sum(r["stale_rate"] for r in hi) / len(hi)
        print(f"\nPI stale-retrieval rate at depth>=10 (mean): {avg_stale:.1f}%")
    if rows and not any(r["n"] for r in rows):
        print("every episode errored: no measurement was taken", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
