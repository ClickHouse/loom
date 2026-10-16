"""ClickGap → Loom migration validation harness.

Proves a `loom import-clickgap` load preserved retrieval quality. It does
NOT seed: it evaluates an ALREADY-LOADED namespace, which is what migration
validation needs.

The defining difference from the agent-memory-export validator: the ClickGap
export carries NO embeddings, so there is no offline cosine baseline /
neighbour-Jaccard to compute. ClickGap's native ranking is instead `recall_count DESC` — its
dominant signal (the top ~8% of lessons hold ~half of all recalls). So the
baseline this harness checks Loom against is the recall_count ordering, and
the headline metric is whether Loom's retrieval order AGREES with it.

Metrics per probe (the probe query is a lesson's own assertion text):

  • known-item recall@k — is the probe lesson itself in Loom's top-k?
    Catches the "rows are invisible to search" class (wrong embedding_model
    tag, identity-tuple drift, unembedded stubs) directly. Broken down by
    kind AND by entity-type so a single collapsing slice is visible.

  • recall-order agreement (Kendall-τ) — for the returned keys that exist in
    the corpus, τ between Loom's rank order and the source recall_count
    order. ≈0 means Loom is recall-blind (the default before the
    reinforcement weight is enabled); rising toward 1 means recall is
    driving the ranking. This is the direct measure of the #1 migration gap.

  • recall-weighted recall@k (recallw) — known-item recall weighted by
    log1p(recall_count), so missing a hot lesson costs far more than missing
    a cold one. Quantifies "are the lessons that actually matter findable".

Redaction: only synthetic memory_keys ('lesson_<id>'), kind labels, entity
KINDS (not values), and aggregate metrics are printed. Assertion text is
sensitive and never written to the report (pass --show-queries to opt in
for local debugging). Querying Loom sends probe text to the server's
embedder; point LOOM_BASE_URL at a trusted endpoint.

Run (after `loom import-clickgap`, and after enabling the reinforcement
weight per docs/guides/importing-clickgap-memory.md to test recall ranking):

    LOOM_BASE_URL=http://localhost:7777 LOOM_JWT_SECRET=... \\
    python3 bench/clickgap_migration_validate.py \\
      --sqlite ~/Downloads/clickgap-memory-.../memory.db \\
      --org <org> --namespace clickgap --agent clickgap \\
      --probes 200 --top-k 10 --min-recall 0.9

Read-only: every memory.search passes `auto_touch: false`, so the probes do
not feed the recall/reinforcement signal τ measures. LOOM_BASE_URL has no
default; the run refuses to start without it.

Exits non-zero if --min-recall / --min-recallw / --min-tau gates fail, or if
any probe's search failed (a failed search is excluded from the ratios and
refuses the gates rather than scoring as a miss).
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import random
import sqlite3
import sys
from dataclasses import dataclass, field

import httpx

# Add server to path for direct token minting (matches the other benches).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bench._gate import measurement_fields
from server.auth import keyring
from server.auth.jwt import mint_token
from server.cli.import_clickgap import _clickgap_memory_type
from server.memory.domain.memory_types import is_searchable_type

LOOM_BASE_URL = os.environ.get("LOOM_BASE_URL", "")


@dataclass
class Corpus:
    """Parsed ClickGap lessons + the source signals we validate against."""

    keys: list[str]  # loom memory_key per lesson ("lesson_<id>")
    kinds: list[str]  # source kind per lesson
    texts: list[str]  # assertion text (probe query source)
    recall: list[int]  # source recall_count (the baseline signal)
    has_ttl: list[bool]  # expires_at populated? (TTL crosstab)
    entity_kinds: list[set[str]]  # entity_kinds attached to each lesson
    searchable: list[bool]  # kind maps to a searchable memory_type?
    recall_by_key: dict[str, int] = field(default_factory=dict)


def load_corpus(sqlite_path: str) -> Corpus:
    """Load every lesson + its recall_count, TTL flag, and entity kinds."""
    db = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    ek_by_lesson: dict[int, set[str]] = {}
    for r in db.execute("SELECT lesson_id, entity_kind FROM lesson_entities"):
        ek_by_lesson.setdefault(int(r["lesson_id"]), set()).add(str(r["entity_kind"] or ""))
    rows = db.execute(
        "SELECT id, text, kind, recall_count, expires_at FROM lessons "
        "WHERE text IS NOT NULL AND text != ''"
    ).fetchall()
    db.close()

    keys: list[str] = []
    kinds: list[str] = []
    texts: list[str] = []
    recall: list[int] = []
    has_ttl: list[bool] = []
    entity_kinds: list[set[str]] = []
    searchable: list[bool] = []
    for r in rows:
        lid = int(r["id"])
        kind = str(r["kind"] or "")
        keys.append(f"lesson_{lid}")
        kinds.append(kind)
        texts.append(str(r["text"]))
        recall.append(int(r["recall_count"] or 0))
        has_ttl.append(r["expires_at"] not in (None, ""))
        entity_kinds.append(ek_by_lesson.get(lid, set()))
        # Mirror the importer's kind→memory_type remap: a kind routed to a
        # non-searchable type (summary→episodic) is deliberately absent from
        # memory.search, so probing for it would unfairly score as a miss.
        searchable.append(is_searchable_type(_clickgap_memory_type(kind)))
    if not keys:
        raise SystemExit("no lessons found in the export")
    return Corpus(
        keys=keys,
        kinds=kinds,
        texts=texts,
        recall=recall,
        has_ttl=has_ttl,
        entity_kinds=entity_kinds,
        searchable=searchable,
        recall_by_key={k: c for k, c in zip(keys, recall, strict=False)},
    )


def _kendall_tau(order: list[int]) -> float | None:
    """Kendall-τ between the given sequence's position order and its values.

    `order` is the source recall_count of Loom's returned keys, in Loom's
    rank order. If Loom ranks by recall, this list is descending → τ≈1; if
    recall-blind, τ≈0. Returns None for <2 comparable points. O(k²) — k is
    tiny (≤ top_k)."""
    n = len(order)
    if n < 2:
        return None
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            # Loom places i before j. Concordant if recall[i] >= recall[j]
            # (higher-recall first), discordant otherwise. Ties ignored.
            if order[i] == order[j]:
                continue
            if order[i] > order[j]:
                concordant += 1
            else:
                discordant += 1
    total = concordant + discordant
    if total == 0:
        return None
    return (concordant - discordant) / total


def _token(org: str, namespace: str) -> str:
    keyring.load_for_offline_mint()
    return mint_token(
        subject="clickgap-migration-validate",
        org=org,
        namespaces=[namespace],
        roles=["admin"],
        ttl_seconds=3600,
        is_service_account=False,
    )


async def loom_search(
    client: httpx.AsyncClient,
    token: str,
    org: str,
    namespace: str,
    agent: str,
    query: str,
    top_k: int,
) -> list[str]:
    """Top-k memory_keys (in rank order) from the real memory.search wire op."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {
        **measurement_fields("memory.search"),
        "org": org,
        "namespace": namespace,
        "agent": agent,
        "user_id": "-",
        "session_id": "-",
        "query": query,
        "top_k": top_k,
    }
    r = await client.post(f"{LOOM_BASE_URL}/v1/memory.search", headers=headers, json=body)
    r.raise_for_status()
    results = r.json().get("results") or []
    return [hit.get("memory_key", "") for hit in results]


@dataclass
class ProbeResult:
    kind: str
    entity_kinds: set[str]
    recall_count: int
    recall_hit: bool
    loom_empty: bool
    tau: float | None
    error: str = ""


async def run(args: argparse.Namespace) -> int:
    print(f"loading corpus from {args.sqlite} …")
    corpus = load_corpus(args.sqlite)
    n_nonsearch = len(corpus.keys) - sum(corpus.searchable)
    print(
        f"  {len(corpus.keys)} lessons "
        f"(ttl'd={sum(corpus.has_ttl)} permanent={len(corpus.keys) - sum(corpus.has_ttl)}; "
        f"{n_nonsearch} non-searchable [summary→episodic] excluded from probes)"
    )

    rng = random.Random(args.seed)
    # Probe only searchable lessons — a non-searchable kind is intentionally
    # absent from memory.search and would score as a known-item miss.
    searchable_idxs = [i for i in range(len(corpus.keys)) if corpus.searchable[i]]
    probe_idxs = rng.sample(searchable_idxs, min(args.probes, len(searchable_idxs)))
    print(f"  evaluating {len(probe_idxs)} probes, top_k={args.top_k} vs {LOOM_BASE_URL}\n")

    token = _token(args.org, args.namespace)
    sem = asyncio.Semaphore(args.concurrency)
    results: list[ProbeResult] = []

    async def one(client: httpx.AsyncClient, idx: int) -> None:
        async with sem:
            key = corpus.keys[idx]
            query = corpus.texts[idx]
            try:
                loom_keys = await loom_search(
                    client,
                    token,
                    args.org,
                    args.namespace,
                    args.agent,
                    query,
                    args.top_k,
                )
            except Exception as e:  # noqa: BLE001 — a failed probe is reported, never scored
                print(f"  ! search failed for {key}: {e}", file=sys.stderr)
                results.append(
                    ProbeResult(
                        kind=corpus.kinds[idx],
                        entity_kinds=corpus.entity_kinds[idx],
                        recall_count=corpus.recall[idx],
                        recall_hit=False,
                        loom_empty=True,
                        tau=None,
                        error=str(e)[:200],
                    )
                )
                return
            # Recall-order agreement: source recall_count of the returned keys
            # that we recognise, in Loom's rank order.
            ranked_recall = [
                corpus.recall_by_key[k] for k in loom_keys if k in corpus.recall_by_key
            ]
            results.append(
                ProbeResult(
                    kind=corpus.kinds[idx],
                    entity_kinds=corpus.entity_kinds[idx],
                    recall_count=corpus.recall[idx],
                    recall_hit=key in loom_keys,
                    loom_empty=not loom_keys,
                    tau=_kendall_tau(ranked_recall),
                )
            )
            if args.show_queries:
                hit = "✓" if key in loom_keys else "✗"
                print(
                    f"  {hit} {key} [{corpus.kinds[idx]}] rc={corpus.recall[idx]} q={query[:60]!r}"
                )

    async with httpx.AsyncClient(timeout=60.0) as client:
        await asyncio.gather(*(one(client, i) for i in probe_idxs))

    return report(results, args)


def _weighted_recall(results: list[ProbeResult]) -> float:
    """Known-item recall weighted by log1p(recall_count) — missing a hot
    lesson costs far more than missing a cold one."""
    num = sum(math.log1p(r.recall_count) for r in results if r.recall_hit)
    den = sum(math.log1p(r.recall_count) for r in results)
    return num / den if den else 0.0


def report(all_results: list[ProbeResult], args: argparse.Namespace) -> int:
    results = [r for r in all_results if not r.error]
    errored = len(all_results) - len(results)
    n = len(results)
    if not n:
        print(f"no probe results ({errored} searches failed)", file=sys.stderr)
        return 1
    recall = sum(r.recall_hit for r in results) / n
    recallw = _weighted_recall(results)
    empties = sum(r.loom_empty for r in results)
    taus = [r.tau for r in results if r.tau is not None]
    mean_tau = sum(taus) / len(taus) if taus else None

    print("\n=== ClickGap migration validation ===")
    print(f"  probes:                       {n}  (searches failed, excluded: {errored})")
    print(f"  recall@{args.top_k} (known-item):        {recall:.3f}")
    print(f"  recall@{args.top_k} (recall-weighted):   {recallw:.3f}")
    tau_text = "n/a (no probe returned two known keys)" if mean_tau is None else f"{mean_tau:+.3f}"
    print(f"  recall-order agreement (τ):   {tau_text}  (τ≈0 ⇒ recall-blind; →1 ⇒ ranks by recall)")
    print(f"  empty Loom results:           {empties} ({empties / n:.1%})")

    # Per-kind breakdown — surfaces a single taxonomy whose recall collapses.
    by_kind: dict[str, list[ProbeResult]] = {}
    for r in results:
        by_kind.setdefault(r.kind, []).append(r)
    print("  by kind:")
    for k in sorted(by_kind, key=lambda x: -len(by_kind[x])):
        g = by_kind[k]
        kr = sum(x.recall_hit for x in g) / len(g)
        print(f"    {k:<22} n={len(g):<5} recall={kr:.3f}")

    # Per-entity-type breakdown — a probe counts in every entity-type it carries.
    by_ek: dict[str, list[ProbeResult]] = {}
    for r in results:
        for ek in r.entity_kinds or {"<none>"}:
            by_ek.setdefault(ek or "<none>", []).append(r)
    print("  by entity-type:")
    for ek in sorted(by_ek, key=lambda x: -len(by_ek[x])):
        g = by_ek[ek]
        er = sum(x.recall_hit for x in g) / len(g)
        print(f"    {ek:<22} n={len(g):<5} recall={er:.3f}")

    ok = True
    if errored:
        print(f"\n  FAIL: {errored} probe search(es) failed — metrics incomplete")
        ok = False
    if args.min_recall > 0.0 and recall < args.min_recall:
        print(f"\n  FAIL: recall {recall:.3f} < --min-recall {args.min_recall}")
        ok = False
    if args.min_recallw > 0.0 and recallw < args.min_recallw:
        print(f"  FAIL: recall-weighted {recallw:.3f} < --min-recallw {args.min_recallw}")
        ok = False
    if args.min_tau > 0.0 and (mean_tau is None or mean_tau < args.min_tau):
        print(
            f"  FAIL: recall-order τ {tau_text} < --min-tau {args.min_tau} "
            f"(enable LOOM_MEMORY_SEARCH_REINFORCEMENT_WEIGHT — see runbook)"
        )
        ok = False
    print("\n✓ gates passed" if ok else "\n✗ gates failed")
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(description="Validate a ClickGap→Loom migration.")
    p.add_argument("--sqlite", required=True, help="Path to the ClickGap memory.db")
    p.add_argument("--org", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--agent", default="clickgap", help="Loom agent the import used")
    p.add_argument("--probes", type=int, default=200, help="Probe queries (default 200)")
    p.add_argument("--top-k", type=int, default=10, help="Retrieval depth (default 10)")
    p.add_argument("--concurrency", type=int, default=8, help="Concurrent searches")
    p.add_argument("--seed", type=int, default=42, help="Sampling seed (reproducible)")
    p.add_argument(
        "--min-recall",
        type=float,
        default=0.0,
        help="Fail if known-item recall@k below this (0=report-only)",
    )
    p.add_argument(
        "--min-recallw",
        type=float,
        default=0.0,
        help="Fail if recall-weighted recall@k below this (0=report-only)",
    )
    p.add_argument(
        "--min-tau",
        type=float,
        default=0.0,
        help="Fail if recall-order agreement τ below this "
        "(0=report-only; use to prove recall ranking is wired)",
    )
    p.add_argument(
        "--show-queries",
        action="store_true",
        help="Print per-probe lines incl. truncated query text "
        "(off by default — assertions are sensitive)",
    )
    args = p.parse_args()
    if not LOOM_BASE_URL:
        print("LOOM_BASE_URL is required (the Loom that holds the import)", file=sys.stderr)
        return 2
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
