"""The onboarding kit, end to end, as one runnable script (docs/getting-started/quickstart.md).

Probe the catalogue, register the namespace with an owner and a traffic class,
write two facts, ask, report the outcome, read the wire stats back. A fresh
shell following the kit against an isolated server should
reach the first correct answer with outcome feedback flowing in well under an
hour — this script is that walk, so the claim can be re-timed.

    LOOM_BASE_URL=http://127.0.0.1:7893 LOOM_API_KEY=... \\
        python examples/onboarding_walkthrough.py --org dev --namespace onboard-walkthrough \\
        --owner you@example.com --traffic-class bench

Exit code 0 when the answer contains the fact and the outcome was recorded.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))

from loom.reference_client import ReferenceClient


async def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--org", required=True)
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--owner", required=True, help="owner_email for the namespace row")
    ap.add_argument(
        "--traffic-class", default="production", choices=("production", "interactive", "bench")
    )
    ap.add_argument("--consumer", default="onboarding-walkthrough")
    ap.add_argument("--archetype", default="memory_app")
    args = ap.parse_args(argv)
    t0 = time.perf_counter()

    async with ReferenceClient(
        args.org, args.namespace, consumer=args.consumer, agent="walkthrough", timeout_s=30.0
    ) as c:
        # 1. the catalogue: are the ops this archetype needs served here?
        probe = await c.probe(args.archetype)
        print("1 probe     ", probe.summary())
        if not probe.ok:
            print("   missing ops:", probe.missing)
            return 2

        # 2. the first call: the namespace with an owner and a traffic class
        reg = await c.register_namespace(args.owner, traffic_class=args.traffic_class)
        print(
            "2 register  ",
            "ok" if reg.ok else f"failed [{reg.code}] {reg.error}",
            f"(registered={reg.get('registered')})",
        )

        # 3. two facts, one of which the question is about
        w1 = await c.set(
            "deploy_window",
            "Deploys go out Tuesday and Thursday only; never on a Friday.",
            tags=["ops"],
        )
        w2 = await c.set(
            "oncall_rota", "The on-call rota rotates every Monday at 09:00 UTC.", tags=["ops"]
        )
        print(
            "3 set       ",
            w1.get("conflict_verdict") or "ok",
            "/",
            w2.get("conflict_verdict") or "ok",
            f"({w1.attempts + w2.attempts} attempts)",
        )

        # 4. the question; the answer must come from the fact just written
        ans = await c.ask("Which days of the week can we deploy?", top_k=10)
        answer = str(ans.get("answer") or "")
        cited = list(ans.get("memory_keys") or [])
        correct = ans.ok and not ans.get("abstain") and "tuesday" in answer.lower()
        print(
            "4 ask       ",
            "correct" if correct else "NOT correct",
            "|",
            answer[:120].replace("\n", " "),
            "| cited",
            cited,
        )

        # 5. outcome feedback: the agent relied on what it cited, and the work went well
        recorded = 0
        for key in cited or ["deploy_window"]:
            r = await c.outcome(key, "used")
            recorded += int(r.ok and bool(r.get("recorded")))
            r2 = await c.outcome(key, "positive")
            recorded += int(r2.ok and bool(r2.get("recorded")))
        print("5 outcome   ", f"{recorded} rows recorded for {cited or ['deploy_window']}")

        # 6. what it cost on the wire
        print("6 wire      ", json.dumps(c.stats()))
        print(f"time to first correct answer with feedback: {time.perf_counter() - t0:.1f} s")
        return 0 if (correct and recorded) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
