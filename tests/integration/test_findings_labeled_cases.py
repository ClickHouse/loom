"""Labeled namespaces through namespace.audit and namespace.validate, scored for
precision and recall, against a live Loom + ClickHouse.

Each case is a namespace built through the API (and, for the two states an agent cannot
cause, a delete on the store the test owns) and labeled with the findings it must raise.
Every other rule must stay quiet on it. A finding is raised when its count is above zero
at warning severity or worse, or, for the system-state and intentional-read rules, when
its count is above zero. The run prints the per-rule table and asserts precision and
recall of 1.0 over every rule that ran.

Run: LOOM_BASE_URL=... LOOM_CH_HOST=... pytest -s tests/integration/test_findings_labeled_cases.py
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org

ORG = unique_org("labeled")

# Rules raised on count > 0 at any severity: their healthy reading is a count, not a grade.
_COUNT_RULES = {"telemetry_unavailable", "intentional_exact_key_reads", "index_backlog"}

Case = Callable[["_Ns"], Awaitable[None]]

# A procedure body memory.set keeps as procedural: a situation and at least two steps.
_PROCEDURE = {"situation": "a node is stuck draining", "steps": ["drain", "restart", "verify"]}


class _Ns:
    def __init__(self, c: httpx.AsyncClient, ch: Any, name: str) -> None:
        self.c, self.ch, self.name = c, ch, name

    async def op(self, op: str, body: dict[str, Any]) -> dict[str, Any]:
        r = await self.c.post(f"/v1/{op}", json={"org": ORG, "namespace": self.name, **body})
        assert r.status_code == 200, (op, r.text[:400])
        return r.json()

    async def set(self, key: str, value: Any, **kw: Any) -> dict[str, Any]:
        return await self.op(
            "memory.set", {"agent": "writer", "memory_key": key, "value": value, **kw}
        )

    async def all_embedded(self) -> bool:
        """Every searchable live row has a vector, as the worker leaves it."""
        return (
            self.count(
                "SELECT count() FROM (SELECT agent, user_id, session_id, memory_key "
                "FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String} "
                "AND tombstoned_at IS NULL AND memory_type NOT IN ('episodic', 'profile') "
                "AND sensitivity != 'high' AND length(value) > 0) AS a "
                "LEFT ANTI JOIN (SELECT agent, user_id, session_id, memory_key "
                "FROM loom.memory_embeddings FINAL WHERE org = {org:String} "
                "AND namespace = {ns:String} AND tombstoned_at IS NULL AND embedding_dim > 0) AS b "
                "USING (agent, user_id, session_id, memory_key)"
            )
            == 0
        )

    def count(self, sql: str) -> int:
        rows = self.ch.query(sql, parameters={"org": ORG, "ns": self.name}).result_rows
        return int(rows[0][0])


async def _claims(ns: _Ns, n: int = 4) -> None:
    """Plain, readable claims: the background every case writes."""
    for i in range(n):
        await ns.set(f"team/fact/{i}", f"The deploy window for service {i} is Tuesday.")


async def clean(ns: _Ns) -> None:
    await _claims(ns)
    for q in ("deploy window", "service owner"):
        await ns.op("memory.search", {"agent": "*", "query": q})


async def double_encoded(ns: _Ns) -> None:
    await _claims(ns)
    for i in range(6):
        await ns.set(f"cfg/svc/{i}", json.dumps({"service": f"s{i}", "owner": "payments"}))


async def oversized(ns: _Ns) -> None:
    await _claims(ns)
    await ns.set("docs/runbook", "The restart procedure is documented here. " * 900)


async def hash_keys(ns: _Ns) -> None:
    for i in range(4):
        await ns.set(f"cache/{uuid.uuid4().hex}{uuid.uuid4().hex[:8]}", f"Cached answer {i}.")


async def valid_typed_records(ns: _Ns) -> None:
    await _claims(ns)
    for i in range(4):
        await ns.set(f"svc/{i}/limits", {"cpu": i + 1, "memory_gb": 4 * (i + 1)})
    await ns.set("runbook/restart", _PROCEDURE, memory_type="procedural")


async def intentional_exact_key_reads(ns: _Ns) -> None:
    await _claims(ns)
    await ns.set("runbook/restart", _PROCEDURE, memory_type="procedural")
    for _ in range(220):
        await ns.op("memory.get", {"agent": "*", "memory_key": "runbook/restart"})


async def stale_records(ns: _Ns) -> None:
    await _claims(ns)
    w = await ns.set("deploy/window", "Tuesdays and Thursdays.")
    await ns.op("memory.verify", {"agent": "writer", "memory_key": "deploy/window"})
    await ns.op("memory.update_by_id", {"memory_id": w["memory_id"], "value": "Fridays now."})
    ended = (datetime.now(UTC) - timedelta(days=3)).replace(tzinfo=None).isoformat()
    await ns.set("oncall/rota", "Alex is on call.", valid_to=ended)


async def delayed_indexing(ns: _Ns) -> None:
    await _claims(ns)
    # A row the embedding worker has not reached yet: written, no embedding row. The
    # embedding row is inserted asynchronously; wait for it, then remove it.
    await ns.set("team/fresh", "The new deploy freeze starts Monday.")

    async def embedded() -> int:
        return ns.count(
            "SELECT count() FROM loom.memory_embeddings WHERE org = {org:String} "
            "AND namespace = {ns:String} AND memory_key = 'team/fresh'"
        )

    await eventually(embedded, description="the embedding row lands")
    ns.ch.command(
        "DELETE FROM loom.memory_embeddings WHERE org = {org:String} "
        "AND namespace = {ns:String} AND memory_key = 'team/fresh'",
        parameters={"org": ORG, "ns": ns.name},
    )


async def unavailable_telemetry(ns: _Ns) -> None:
    await _claims(ns)

    async def ledgered() -> int:
        return ns.count(
            "SELECT count() FROM loom.agent_events "
            "WHERE org = {org:String} AND namespace = {ns:String}"
        )

    async def every_write_ledgered() -> bool:
        return await ledgered() >= 4

    await eventually(every_write_ledgered, description="the writes reach the ledger")
    # The audit judges only writes older than its ledger lag (60 s): wait past it.
    await asyncio.sleep(61)
    # The ledger lost the window: the writes are there, their calls are not.
    ns.ch.command(
        "DELETE FROM loom.agent_events WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": ns.name},
    )
    assert await ledgered() == 0


# case -> the rules it must raise (audit codes and validate checks).
CASES: dict[str, tuple[Case, set[str]]] = {
    "clean": (clean, set()),
    "double_encoded": (double_encoded, {"double_encoded_values"}),
    "oversized_claim_blob": (oversized, {"oversized_values"}),
    "hash_keys": (hash_keys, {"hash_keys"}),
    "valid_typed_records": (valid_typed_records, set()),
    "intentional_exact_key_reads": (
        intentional_exact_key_reads,
        {"intentional_exact_key_reads"},
    ),
    "stale_records": (stale_records, {"stale_verification", "stale_valid_to"}),
    "delayed_indexing": (delayed_indexing, {"index_backlog"}),
    "unavailable_telemetry": (unavailable_telemetry, {"telemetry_unavailable"}),
}


def _raised(audit: dict[str, Any], validate: dict[str, Any]) -> tuple[set[str], set[str]]:
    """(rules raised, rules that ran) over both reports."""
    raised, ran = set(), set()
    for f in audit["findings"]:
        ran.add(f["code"])
        hit = f["count"] > 0 and (f["code"] in _COUNT_RULES or f["severity"] != "info")
        if hit:
            raised.add(f["code"])
    for f in validate["findings"]:
        ran.add(f["check"])
        if f["count"] > 0:
            raised.add(f["check"])
    return raised, ran


@pytest.mark.asyncio
async def test_labeled_cases_score_full_precision_and_recall(
    control_plane_url: str, mint_test_token: Any, ch_client: Any
) -> None:
    names = {case: f"lb-{case.replace('_', '-')[:20]}-{uuid.uuid4().hex[:4]}" for case in CASES}
    token = mint_test_token(org=ORG, namespaces=list(names.values()), roles=["writer"])
    scores: dict[str, list[int]] = {}  # rule -> [tp, fp, fn]
    errors: list[str] = []
    async with httpx.AsyncClient(
        base_url=control_plane_url, timeout=60, headers={"Authorization": f"Bearer {token}"}
    ) as c:
        for case, (build, labels) in CASES.items():
            ns = _Ns(c, ch_client, names[case])
            await build(ns)
            if case != "delayed_indexing":
                # Audit once every row is embedded: a row the worker has not reached yet is
                # an indexing backlog, which only the delayed case is labelled with.
                await eventually(ns.all_embedded, description=f"{case}: rows embedded")
            audit = await ns.op("namespace.audit", {"window_seconds": 3600})
            validate = await ns.op("namespace.validate", {})
            raised, ran = _raised(audit, validate)
            assert labels <= ran, (case, labels - ran)
            for rule in ran:
                tp, fp, fn = scores.setdefault(rule, [0, 0, 0])
                hit, want = rule in raised, rule in labels
                scores[rule] = [
                    tp + (hit and want),
                    fp + (hit and not want),
                    fn + (want and not hit),
                ]
                if hit != want:
                    errors.append(f"{case}: {rule} {'raised' if hit else 'missed'}")

    print("\nrule                           tp  fp  fn  precision  recall")
    for rule, (tp, fp, fn) in sorted(scores.items()):
        if tp or fp or fn:
            p = tp / (tp + fp) if tp + fp else 1.0
            r = tp / (tp + fn) if tp + fn else 1.0
            print(f"{rule:<30} {tp:>3} {fp:>3} {fn:>3}  {p:>9.2f}  {r:>6.2f}")
    tp, fp, fn = (sum(v[i] for v in scores.values()) for i in range(3))
    print(
        f"{'all rules':<30} {tp:>3} {fp:>3} {fn:>3}  {tp / (tp + fp):>9.2f}  {tp / (tp + fn):>6.2f}"
    )
    assert errors == []
