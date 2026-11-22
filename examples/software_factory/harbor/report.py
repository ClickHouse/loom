"""Descriptive comparisons retain every supplied attempt, including failures."""

from __future__ import annotations

from collections import defaultdict


def compare(results: list[dict]) -> dict:
    grouped = defaultdict(list)
    for result in results:
        grouped[(result["provider"], result["kind"], result["mode"])].append(result)
    arms = []
    for (provider, kind, mode), attempts in sorted(grouped.items()):
        successes = sum(r["resolved"] for r in attempts)
        costs_known = all(r["total_cost_usd"] is not None for r in attempts)
        total = sum(r["total_cost_usd"] for r in attempts) if costs_known else None
        arms.append(
            {
                "provider": provider,
                "kind": kind,
                "mode": mode,
                "attempts": len(attempts),
                "resolved": successes,
                "quality": successes / len(attempts),
                "total_cost_usd": total,
                "cost_per_success_usd": total / successes
                if total is not None and successes
                else None,
                "latency_s": [r["duration_s"] for r in attempts],
                "selected_calls": sum(r["model_selected_loom_requests"] for r in attempts),
                "memory_rows_written": sum(r["memory_rows_written"] for r in attempts),
                "statuses": [r["status"] for r in attempts],
            }
        )
    return {
        "schema_version": 1,
        "arms": arms,
        "benchmark_claims": False,
        "interpretation": "Replay measures no model quality, savings or adoption.",
        "inference": "No significance estimate. A live study needs paired clusters and A/A noise.",
    }
