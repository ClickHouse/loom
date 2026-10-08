"""Update-stage audit: what did each supersession actually do to the fact?

A memory system can be scored at three operations — extraction, updating,
question answering — and the hallucinations that reach the answer are often
introduced while the store is *updated*, not while it is read. Loom measures extraction (Memory
Integrity / False Memory Rate, `bench/memory_integrity.py`)
and answering (every ask bench); this measures the updating stage.

This audit reads consecutive versions of the same identity key from
`loom.memory_versions` (which keeps every version) and has a judge
label what the newer value did to the older one:

  restates    same fact, same content — the write was redundant
  extends     same fact, more detail, nothing removed or changed
  narrows     same fact, detail dropped — an omission the reader will now miss
  corrects    same fact, a value changed (date, number, name)
  contradicts incompatible statements about the same thing, no signal which is right
  unrelated   a different fact under the same key — the key was reused

The *false-supersession rate* is `unrelated + restates`: the versions that
should not have replaced anything (a reuse buries a fact; a restatement is a
row with no information). `narrows` is the omission rate. `corrects` and
`contradicts` are the pairs the temporal and conflict work should be looking at.

    OPENAI_API_KEY=... python bench/supersession_audit.py --org <org> \\
        --namespace <namespace> --days 7 --sample 200 --out /tmp/audit.json

Reads ClickHouse through `server.core.ch` (the server's own `.env`, run from the
repo root), read-only. Pure core (`pair_versions`, `summarize`) tested in
`bench/test_supersession_audit.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

LABELS = ("restates", "extends", "narrows", "corrects", "contradicts", "unrelated")
FALSE_SUPERSESSION = ("unrelated", "restates")

JUDGE_SYSTEM = (
    "You audit updates to a memory store. You are shown the OLDER and the NEWER "
    "value stored under the same key. Reply with exactly one word:\n"
    "restates — same fact, same content (wording may differ)\n"
    "extends — same fact with added detail; nothing removed or changed\n"
    "narrows — same fact with detail removed; nothing added or changed\n"
    "corrects — same fact, but a value changed (a date, number, name, state)\n"
    "contradicts — the two make incompatible claims about the same thing and neither "
    "is marked as the update\n"
    "unrelated — a different fact; the key was reused for something else\n"
    "One word, lowercase, nothing else."
)


@dataclass
class VersionPair:
    namespace: str
    memory_key: str
    older_version: int
    newer_version: int
    older_value: str
    newer_value: str
    older_at: str = ""
    newer_at: str = ""
    label: str = ""


@dataclass
class Summary:
    pairs: int
    labels: Counter = field(default_factory=Counter)

    @property
    def false_supersession_rate(self) -> float:
        return sum(self.labels[k] for k in FALSE_SUPERSESSION) / self.pairs if self.pairs else 0.0

    @property
    def omission_rate(self) -> float:
        return self.labels["narrows"] / self.pairs if self.pairs else 0.0


def pair_versions(rows: list[dict[str, Any]]) -> list[VersionPair]:
    """Consecutive (older, newer) version pairs per key. The identity is
    (namespace, agent, user_id, memory_key) — NOT the session: a supersession
    in loom is a later session writing the same key,
    and the versions table keeps one row per (session, version), so the same
    key's history spans sessions. Ordered by `version` (the write clock).
    `rows` carry namespace, agent, user_id, memory_key, version, value,
    created_at; order and duplicates do not matter. A key with one version
    yields nothing; identical adjacent values are kept (they are the
    `restates` the audit exists to count)."""
    by_key: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for r in rows:
        ident = (
            str(r.get("namespace") or ""),
            str(r.get("agent") or ""),
            str(r.get("user_id") or ""),
            str(r.get("memory_key") or ""),
        )
        by_key.setdefault(ident, []).append(r)
    out: list[VersionPair] = []
    for ident, vs in by_key.items():
        seen: dict[int, dict[str, Any]] = {}
        for r in vs:
            seen[int(r.get("version") or 0)] = r  # last write per version wins
        ordered = [seen[v] for v in sorted(seen)]
        for older, newer in zip(ordered, ordered[1:], strict=False):
            out.append(
                VersionPair(
                    namespace=ident[0],
                    memory_key=ident[3],
                    older_version=int(older.get("version") or 0),
                    newer_version=int(newer.get("version") or 0),
                    older_value=str(older.get("value") or ""),
                    newer_value=str(newer.get("value") or ""),
                    older_at=str(older.get("created_at") or ""),
                    newer_at=str(newer.get("created_at") or ""),
                )
            )
    return out


def parse_label(text: str) -> str:
    t = (text or "").strip().lower().split()
    return t[0].strip(".,") if t and t[0].strip(".,") in LABELS else ""


def summarize(pairs: list[VersionPair]) -> Summary:
    labelled = [p for p in pairs if p.label]
    s = Summary(pairs=len(labelled))
    for p in labelled:
        s.labels[p.label] += 1
    return s


def _identical(p: VersionPair) -> bool:
    return p.older_value.strip() == p.newer_value.strip()


async def _judge(
    client: Any, *, api_key: str, model: str, p: VersionPair, usage: dict[str, int]
) -> str:
    if _identical(p):
        return "restates"  # no call for a byte-identical pair
    body = {
        "model": model,
        "temperature": 0,
        "max_tokens": 4,
        "messages": [
            {"role": "system", "content": JUDGE_SYSTEM},
            {
                "role": "user",
                "content": (
                    f"OLDER (v{p.older_version}):\n{p.older_value[:1500]}\n\n"
                    f"NEWER (v{p.newer_version}):\n{p.newer_value[:1500]}\n\n"
                    "Label:"
                ),
            },
        ],
    }
    url = os.environ.get("BENCH_OPENAI_CHAT_URL", "https://api.openai.com/v1/chat/completions")
    resp = await client.post(
        url, json=body, headers={"Authorization": f"Bearer {api_key}"}, timeout=60.0
    )
    resp.raise_for_status()
    data = resp.json()
    u = data.get("usage") or {}
    usage["tin"] += int(u.get("prompt_tokens") or 0)
    usage["tout"] += int(u.get("completion_tokens") or 0)
    usage["calls"] += 1
    return parse_label(str(data["choices"][0]["message"]["content"] or ""))


async def _fetch(org: str, namespace: str, days: int, ch: Any) -> list[dict[str, Any]]:
    # Every version of every key that received more than one version in the
    # window, across sessions (a supersession is a later session writing the
    # same key). No FINAL — the versions table is the history, not the state.
    return await ch.query_all(
        """
        SELECT namespace, agent, user_id, session_id, memory_key, version, value,
               toString(created_at) AS created_at
        FROM loom.memory_versions
        WHERE org = {org:String} AND namespace = {ns:String}
          AND (org, namespace, agent, user_id, memory_key) IN (
              SELECT org, namespace, agent, user_id, memory_key
              FROM loom.memory_versions
              WHERE org = {org:String} AND namespace = {ns:String}
                AND created_at > now() - INTERVAL {days:UInt32} DAY
              GROUP BY org, namespace, agent, user_id, memory_key
              HAVING uniq(version) > 1)
          AND redacted_at IS NULL
        """,
        params={"org": org, "ns": namespace, "days": int(days)},
    )


async def main_async(args: argparse.Namespace) -> int:
    import httpx

    from server.core.ch import get_client

    ch = get_client()
    await ch.connect()
    rows = await _fetch(args.org, args.namespace, args.days, ch)
    pairs = pair_versions(rows)
    rng = random.Random(args.seed)
    if args.sample and len(pairs) > args.sample:
        pairs = rng.sample(pairs, args.sample)
    identical = sum(1 for p in pairs if _identical(p))
    print(
        f"supersession audit: {args.org}/{args.namespace} days={args.days} "
        f"version rows={len(rows)} pairs={len(pairs)} byte-identical={identical}"
    )
    api_key = os.environ.get("OPENAI_API_KEY", "")
    usage = {"tin": 0, "tout": 0, "calls": 0}
    if not api_key:
        print("OPENAI_API_KEY unset: labelling only byte-identical pairs as restates")
    sem = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient() as client:

        async def one(p: VersionPair) -> None:
            async with sem:
                if _identical(p) or not api_key:
                    p.label = "restates" if _identical(p) else ""
                    return
                try:
                    p.label = await _judge(
                        client, api_key=api_key, model=args.judge, p=p, usage=usage
                    )
                except Exception as e:  # noqa: BLE001 — one failed judge is an unlabelled pair
                    print(f"  judge failed for {p.memory_key}: {e}", file=sys.stderr)

        await asyncio.gather(*[one(p) for p in pairs])
    s = summarize(pairs)
    print(
        f"\n# labelled pairs: {s.pairs}  (judge calls {usage['calls']}, "
        f"tokens in/out {usage['tin']}/{usage['tout']})"
    )
    for k in LABELS:
        n = s.labels[k]
        print(f"  {k:<12} {n:>5}  {100 * n / s.pairs if s.pairs else 0:5.1f}%")
    print(
        "\n  false-supersession rate (unrelated + restates): "
        f"{100 * s.false_supersession_rate:.1f}%"
    )
    print(f"  omission rate (narrows):                        {100 * s.omission_rate:.1f}%")
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "org": args.org,
                    "namespace": args.namespace,
                    "days": args.days,
                    "judge": args.judge,
                    "version_rows": len(rows),
                    "pairs": len(pairs),
                    "labelled": s.pairs,
                    "labels": dict(s.labels),
                    "false_supersession_rate": round(s.false_supersession_rate, 4),
                    "omission_rate": round(s.omission_rate, 4),
                    "examples": {
                        k: [
                            {
                                "key": p.memory_key,
                                "older": p.older_value[:200],
                                "newer": p.newer_value[:200],
                            }
                            for p in pairs
                            if p.label == k
                        ][:5]
                        for k in LABELS
                    },
                },
                indent=1,
                default=str,
            )
        )
        print(f"wrote {args.out}")
    if args.max_false_rate is not None and s.false_supersession_rate > args.max_false_rate:
        print(
            f"GATE: false-supersession rate {s.false_supersession_rate:.3f} > {args.max_false_rate}"
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--org", required=True)
    p.add_argument("--namespace", required=True)
    p.add_argument("--days", type=int, default=7)
    p.add_argument("--sample", type=int, default=200, help="0 = every pair")
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--judge", default="gpt-4o-mini")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--max-false-rate", type=float, default=None)
    p.add_argument("--out", default="")
    return asyncio.run(main_async(p.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
