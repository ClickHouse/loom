#!/usr/bin/env python3
"""Two axes vs one on real embeddings.

Seeds pairs of procedures that share a precondition and differ in goal — the shape a
one-string (goal-only or situation-only) key gets wrong — into a scratch namespace, then
asks `procedure.match` with a paraphrased situation, once without and once with the
goal, and reports rank@1 of the intended procedure under each. Runs the handlers
in-process against the configured ClickHouse and embedder (no server, no app lifespan,
so no migrations and no workers); tombstones the scratch rows afterwards.

    set -a; . ./.env; set +a; LOOM_BACKGROUND_WORKERS=off \\
      .venv/bin/python bench/procedure_match_axes.py [--org O] [--keep]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# (shared precondition, [(goal name, steps)], paraphrased situation for the query)
PAIRS: list[tuple[str, list[tuple[str, list[str]]], str]] = [
    (
        "tests are green on main and the release branch is cut",
        [
            (
                "ship the release",
                [
                    "tag the release commit",
                    "push the tag",
                    "publish the changelog",
                ],
            ),
            (
                "roll back the release",
                [
                    "revert the release commit",
                    "delete the tag",
                    "announce the rollback",
                ],
            ),
        ],
        "CI is green on main and the release branch exists",
    ),
    (
        "a ClickHouse table has parts piling up and merges lag",
        [
            (
                "compact the table",
                [
                    "check system.merges",
                    "run OPTIMIZE TABLE FINAL off-peak",
                    "confirm part count fell",
                ],
            ),
            (
                "widen the merge pool",
                [
                    "raise background_pool_size",
                    "restart the merge scheduler",
                    "watch merge throughput",
                ],
            ),
        ],
        "parts count is climbing on a ClickHouse table and merges are behind",
    ),
    (
        "a PR has a failing unit test in CI",
        [
            (
                "fix the test",
                [
                    "reproduce locally",
                    "read the assertion",
                    "patch the code or the fixture",
                ],
            ),
            (
                "quarantine the test",
                [
                    "mark it xfail with the issue",
                    "open the tracking issue",
                    "notify the owner",
                ],
            ),
        ],
        "the unit job on a PR is red",
    ),
    (
        "a customer reports slow memory.search on a large namespace",
        [
            (
                "tune the search",
                [
                    "read search_latency.py",
                    "lower ef_search or top_k",
                    "re-measure p50",
                ],
            ),
            (
                "split the namespace",
                [
                    "profile rows per agent",
                    "create the shard namespaces",
                    "migrate with namespace.snapshot",
                ],
            ),
        ],
        "search is slow for one big namespace",
    ),
    (
        "the embedding provider is returning 429s",
        [
            (
                "back off the embedder",
                [
                    "raise the retry backoff",
                    "cap concurrent embeds",
                    "resume the queue",
                ],
            ),
            (
                "switch the embedder",
                [
                    "set LOOM_EMBEDDING_MODEL to the fallback",
                    "run loom reembed",
                    "verify dims match",
                ],
            ),
        ],
        "embeddings are rate-limited by the provider",
    ),
    (
        "a migration file is added to schemas/migrations",
        [
            (
                "apply the migration",
                [
                    "run loom migrate --dry-run",
                    "run loom migrate",
                    "check the ledger row",
                ],
            ),
            (
                "dump the schema",
                [
                    "start the pinned CH image",
                    "run scripts/ops/dump_schema.py",
                    "diff schemas/schema.sql",
                ],
            ),
        ],
        "there is a new SQL migration in the repo",
    ),
    (
        "the agent plugin sends outcome none at session end",
        [
            (
                "derive the outcome server-side",
                [
                    "read session_outcomes",
                    "map judged label to outcome",
                    "record the procedure runs",
                ],
            ),
            (
                "make the plugin report",
                [
                    "add the outcome prompt to the hook",
                    "send memories_used",
                    "test the hook",
                ],
            ),
        ],
        "the Claude Code hook closes runs with no outcome",
    ),
    (
        "two open PRs claim the same decision number",
        [
            (
                "renumber the later one",
                [
                    "find which merged first",
                    "rename the file",
                    "update the references",
                ],
            ),
            (
                "block the merge",
                [
                    "request changes on the later PR",
                    "point at the first",
                    "wait for the rename",
                ],
            ),
        ],
        "decision records collide on a number across PRs",
    ),
    (
        "staging ClickHouse reports too many open files",
        [
            (
                "restart the server",
                [
                    "announce on the socket",
                    "run redeploy.sh",
                    "verify health",
                ],
            ),
            (
                "find the fd leak",
                [
                    "count fds per worker",
                    "group by socket state",
                    "bisect the connector",
                ],
            ),
        ],
        "the loom server hits EMFILE against ClickHouse Cloud",
    ),
    (
        "a bench namespace is left behind after a run",
        [
            (
                "erase the namespace",
                [
                    "list bench namespaces",
                    "call erasure.request",
                    "verify the census",
                ],
            ),
            (
                "set a retention window",
                [
                    "set retention_days on the namespace",
                    "run the retention worker",
                    "check the TTL clause",
                ],
            ),
        ],
        "old bench namespaces are still active on staging",
    ),
    (
        "a procedure's last runs all failed",
        [
            (
                "quarantine the procedure",
                [
                    "run procedure.evolve",
                    "confirm strikes",
                    "drop it from the pack",
                ],
            ),
            (
                "revise the procedure",
                [
                    "read the failure notes",
                    "propose a new graph",
                    "record runs against it",
                ],
            ),
        ],
        "a stored procedure keeps failing when agents follow it",
    ),
    (
        "the mypy gate is over its baseline",
        [
            (
                "fix the new errors",
                [
                    "diff mypy output against main",
                    "annotate the new code",
                    "rerun the gate",
                ],
            ),
            (
                "lower the baseline",
                [
                    "confirm errors dropped",
                    "edit scripts/gates/mypy_gate.py",
                    "commit the lower number",
                ],
            ),
        ],
        "mypy-gate fails on the branch",
    ),
]


async def run(org: str, keep: bool) -> dict[str, Any]:
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.api.models_procedure import ProcedureAddInput, ProcedureMatchInput
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.app.procedure import handle_procedure_add, handle_procedure_match

    ch = get_client()
    await ch.connect()
    ns = f"bench-match-axes-{int(time.time())}"
    keys: list[str] = []
    for situation, goals, _ in PAIRS:
        for name, steps in goals:
            out = await handle_procedure_add(
                ProcedureAddInput(
                    org=org, namespace=ns, name=name, situation=situation, steps=steps
                )
            )
            keys.append(out.memory_key)
    await asyncio.sleep(2.5)  # read-your-writes lag on the RMT

    rows: list[dict[str, Any]] = []
    for situation, goals, query in PAIRS:
        for name, _ in goals:
            one = await handle_procedure_match(
                ProcedureMatchInput(
                    org=org, namespace=ns, situation=query, namespace_scope="this", top_k=5
                )
            )
            two = await handle_procedure_match(
                ProcedureMatchInput(
                    org=org,
                    namespace=ns,
                    situation=query,
                    goal=name,
                    namespace_scope="this",
                    top_k=5,
                )
            )
            rows.append(
                {
                    "precondition": situation,
                    "goal": name,
                    "one_axis_top": one.matches[0].name if one.matches else None,
                    "two_axis_top": two.matches[0].name if two.matches else None,
                    "one_axis_hit": bool(one.matches) and one.matches[0].name == name,
                    "two_axis_hit": bool(two.matches) and two.matches[0].name == name,
                    "two_axis_goal_sims": [(m.name, m.goal_similarity) for m in two.matches[:2]],
                }
            )
    if not keep:
        for k in keys:
            await handle_memory_delete(
                MemoryDeleteInput(org=org, namespace=ns, agent="-", memory_key=k)
            )
    await ch.disconnect()
    n = len(rows)
    return {
        "namespace": ns,
        "queries": n,
        "one_axis_rank1": sum(r["one_axis_hit"] for r in rows),
        "two_axis_rank1": sum(r["two_axis_hit"] for r in rows),
        "rows": rows,
    }


def main() -> int:
    from server.wiring import wire

    wire()  # the insert observers (write marks, recent writes) this run's handlers feed
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--org", default="dev")
    ap.add_argument("--keep", action="store_true", help="leave the scratch rows in place")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = asyncio.run(run(a.org, a.keep))
    if a.json:
        print(json.dumps(res, indent=1, default=str))
        return 0
    n = res["queries"]
    print(f"namespace {res['namespace']}: {n} queries (same precondition, two goals each)")
    print(
        f"rank@1 of the intended procedure — situation only: {res['one_axis_rank1']}/{n}"
        f"   situation × goal: {res['two_axis_rank1']}/{n}"
    )
    for r in res["rows"]:
        flag = "" if r["two_axis_hit"] else "  <-- two-axis miss"
        print(
            f"  {r['goal'][:32]:32s} one-axis→{str(r['one_axis_top'])[:28]:28s} "
            f"two-axis→{str(r['two_axis_top'])[:28]:28s}{flag}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
