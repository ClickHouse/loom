"""Is memory.search a function of its inputs?

Asks the same question of the same namespace N times in a row and reports
how often the answer set agrees with itself: same top-1, same top-k as a
SET, same top-k in ORDER, mean Jaccard between consecutive runs. Nothing is
written between runs, so any disagreement is the read path's own doing.

Why this bench exists: the trace log can suggest that identical queries disagree, but
the trace row omits search_mode, tags and scope, so many consecutive same-text pairs are
not the same request. With identical parameters the read path should agree with itself,
back to back and across a usage-fold pass. Hence two modes: `--repeats N` for the read
path itself, and `--save` / `--against` for spaced repeats across a worker pass, on any
corpus where version churn is suspected.

    # questions from a file (one per line) …
    python bench/retrieval_determinism.py --base-url $URL --token $TOK \\
        --org <org> --namespace <namespace> --queries q.txt --repeats 3

    # … or the most-repeated real questions from search_trace_log
    python bench/retrieval_determinism.py ... --from-trace --ch-url https://h:8443 \\
        --ch-user u --ch-password p --limit 40

    --min-same-set 0.95   fail (exit 1) below this share — a gate
    --json                machine-readable, for bench/report consumers
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class QueryResult:
    query: str
    runs: list[list[str]]  # top-k keys per run, in order

    @property
    def same_top1(self) -> bool:
        return len({r[0] if r else "" for r in self.runs}) <= 1

    @property
    def same_set(self) -> bool:
        return len({tuple(sorted(r)) for r in self.runs}) <= 1

    @property
    def same_order(self) -> bool:
        return len({tuple(r) for r in self.runs}) <= 1

    @property
    def mean_jaccard(self) -> float:
        js = []
        for a, b in zip(self.runs, self.runs[1:], strict=False):
            sa, sb = set(a), set(b)
            js.append(len(sa & sb) / len(sa | sb) if (sa | sb) else 1.0)
        return round(statistics.fmean(js), 4) if js else 1.0


@dataclass
class Report:
    k: int
    repeats: int
    results: list[QueryResult] = field(default_factory=list)

    def share(self, attr: str) -> float:
        if not self.results:
            return 0.0
        hits = sum(1 for r in self.results if getattr(r, attr))
        return round(hits / len(self.results), 4)

    @property
    def mean_jaccard(self) -> float:
        if not self.results:
            return 0.0
        return round(statistics.fmean(r.mean_jaccard for r in self.results), 4)

    def to_json(self) -> dict[str, Any]:
        return {
            "k": self.k,
            "repeats": self.repeats,
            "queries": len(self.results),
            "same_top1": self.share("same_top1"),
            "same_set": self.share("same_set"),
            "same_order": self.share("same_order"),
            "mean_jaccard": self.mean_jaccard,
            "unstable": [
                {"query": r.query[:120], "runs": r.runs} for r in self.results if not r.same_set
            ][:20],
        }


# ----------------------------------------------------------------------------
# pure helpers (unit-tested)
# ----------------------------------------------------------------------------


def top_keys(search_output: dict[str, Any], k: int) -> list[str]:
    """Top-k memory keys from a memory.search response, in returned order."""
    hits = search_output.get("results") or []
    return [str(h.get("memory_key") or "") for h in hits][:k]


def summarise(runs_by_query: dict[str, list[list[str]]], k: int, repeats: int) -> Report:
    rep = Report(k=k, repeats=repeats)
    for q, runs in runs_by_query.items():
        rep.results.append(QueryResult(query=q, runs=runs))
    return rep


def render(rep: Report) -> str:
    lines = [
        f"retrieval determinism: {len(rep.results)} queries × {rep.repeats} runs, top-{rep.k}",
        f"  same top-1      {rep.share('same_top1'):.1%}",
        f"  same set        {rep.share('same_set'):.1%}",
        f"  same order      {rep.share('same_order'):.1%}",
        f"  mean Jaccard    {rep.mean_jaccard:.3f}",
    ]
    unstable = [r for r in rep.results if not r.same_set]
    for r in unstable[:5]:
        lines.append(f"  unstable: {r.query[:70]!r}")
        for run in r.runs:
            lines.append("      " + ", ".join(run))
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------


def _post(base_url: str, path: str, body: dict[str, Any], token: str) -> dict[str, Any]:
    req = urllib.request.Request(
        base_url + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode() or "{}")


def _ch_rows(
    url: str,
    user: str,
    password: str,
    sql: str,
    params: dict[str, str],
) -> list[dict[str, Any]]:
    # Read-only, credentials in the auth headers (never the URL).
    q = {"default_format": "JSONEachRow", "readonly": "2"}
    q.update({f"param_{k}": v for k, v in params.items()})
    req = urllib.request.Request(
        url.rstrip("/") + "/?" + urllib.parse.urlencode(q),
        data=sql.encode(),
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
    )
    with urllib.request.urlopen(req, timeout=300) as r:
        return [json.loads(line) for line in r.read().decode().splitlines() if line.strip()]


def queries_from_trace(
    ch_url: str,
    user: str,
    password: str,
    org: str,
    ns: str,
    limit: int,
) -> list[str]:
    rows = _ch_rows(
        ch_url,
        user,
        password,
        "SELECT query, count() AS n FROM loom.search_trace_log "
        "WHERE org = {org:String} AND namespace = {ns:String} AND query != '' "
        "AND ts > now() - INTERVAL 7 DAY GROUP BY query ORDER BY n DESC LIMIT {lim:UInt32}",
        {"org": org, "ns": ns, "lim": str(limit)},
    )
    return [str(r["query"]) for r in rows]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--base-url", default="http://localhost:7777")
    ap.add_argument("--token", default="")
    ap.add_argument("--org", default="dev")
    ap.add_argument("--namespace", required=True)
    ap.add_argument("--agent", default="*")
    ap.add_argument("--queries", default="", help="file with one query per line")
    ap.add_argument(
        "--from-trace",
        action="store_true",
        help="take the most repeated queries from search_trace_log",
    )
    ap.add_argument("--ch-url", default="http://localhost:8123")
    ap.add_argument("--ch-user", default="loom")
    ap.add_argument(
        "--ch-password",
        default=os.environ.get("LOOM_CH_PASSWORD", "loomdev"),
        help="default LOOM_CH_PASSWORD, else the dev stack's password",
    )
    ap.add_argument("--limit", type=int, default=40)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--top-k", type=int, default=5)
    ap.add_argument("--search-mode", default="")
    ap.add_argument("--min-same-set", type=float, default=0.0)
    ap.add_argument("--save", default="", help="write this run's queries and top-k to a JSON file")
    ap.add_argument(
        "--against",
        default="",
        help="compare against a saved run: spaced repeats, e.g. across a worker pass",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    saved: dict[str, list[str]] = {}
    if args.against:
        with open(args.against, encoding="utf-8") as fh:
            saved = {str(k): list(v) for k, v in json.load(fh).get("top_keys", {}).items()}

    if saved and not args.queries:
        queries = list(saved.keys())[: args.limit]  # the saved run decides the question set
    elif args.from_trace:
        queries = queries_from_trace(
            args.ch_url,
            args.ch_user,
            args.ch_password,
            args.org,
            args.namespace,
            args.limit,
        )
    elif args.queries:
        with open(args.queries, encoding="utf-8") as fh:
            queries = [line.strip() for line in fh if line.strip()][: args.limit]
    else:
        print("give --queries FILE or --from-trace", file=sys.stderr)
        return 2
    if not queries:
        print("no queries", file=sys.stderr)
        return 2

    runs: dict[str, list[list[str]]] = {}
    for q in queries:
        body: dict[str, Any] = {
            "org": args.org,
            "namespace": args.namespace,
            "agent": args.agent,
            "query": q,
            "top_k": args.top_k,
            "auto_touch": False,
        }
        if args.search_mode:
            body["search_mode"] = args.search_mode
        runs[q] = [saved[q]] if q in saved else []
        for _ in range(args.repeats):
            try:
                out = _post(args.base_url, "/v1/memory.search", body, args.token)
            except Exception as e:  # noqa: BLE001
                print(f"  search failed for {q[:60]!r}: {e}", file=sys.stderr)
                break
            runs[q].append(top_keys(out, args.top_k))
        if not runs[q]:
            runs.pop(q, None)

    rep = summarise(runs, args.top_k, args.repeats + (1 if saved else 0))
    if args.save:
        with open(args.save, "w", encoding="utf-8") as fh:
            json.dump({"k": args.top_k, "top_keys": {q: r[-1] for q, r in runs.items() if r}}, fh)
    print(json.dumps(rep.to_json(), indent=2) if args.json else render(rep))
    if args.min_same_set and rep.share("same_set") < args.min_same_set:
        print(
            f"FAIL same-set {rep.share('same_set'):.3f} < --min-same-set {args.min_same_set}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
