"""Which retrieval planes earn their keep?

`memory.search` fuses several legs — facts (the ANN read), verbatim chunks,
typed records, the lexical union, the entity graph, the temporal prongs —
and the request-level `execution_planes` mask can switch each one off. No
bench used it, so the contribution of each tier to recall and to cost was
assumed. Ablating memory tiers one at a time typically shows the answer
depends on the corpus. This bench asks that question of loom's planes.

    python bench/plane_ablation.py --base-url $URL --token $TOK \\
        --dataset /path/to/retrieval_qa.json --top-k 10

    --planes "fact|fact,chunk|fact,record|fact,lexical|fact,graph|all"
    --ch-url ... --ch-user ... --ch-password ...   adds ClickHouse read rows and
                                                   query count per arm via the
                                                   request tag
    --json

Per arm: recall@k, MRR, abstain share, client p50/p95 latency, and when
ClickHouse is reachable the median queries and read rows per request. The
dataset is the retrieval_recall.py format (`query`, exact `gold_keys`) or the
quality.py format (`questions[].question`, substring `gold_keys`).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any

try:
    from bench.retrieval_recall import score_one
except ModuleNotFoundError:  # run as a script from anywhere: put the repo root on the path
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from bench.retrieval_recall import score_one

DEFAULT_PLANES = "fact|fact,chunk|fact,record|fact,lexical|fact,graph|all"


@dataclass
class ArmResult:
    planes: str
    n: int = 0
    hits: int = 0
    rr_sum: float = 0.0
    abstains: int = 0
    latencies_ms: list[float] = field(default_factory=list)
    request_ids: list[str] = field(default_factory=list)
    ch_queries_p50: float | None = None
    ch_read_rows_p50: float | None = None

    @property
    def recall(self) -> float:
        return round(self.hits / self.n, 4) if self.n else 0.0

    @property
    def mrr(self) -> float:
        return round(self.rr_sum / self.n, 4) if self.n else 0.0

    @property
    def abstain_share(self) -> float:
        return round(self.abstains / self.n, 4) if self.n else 0.0

    def p(self, q: float) -> float:
        if not self.latencies_ms:
            return 0.0
        xs = sorted(self.latencies_ms)
        return round(xs[min(len(xs) - 1, int(q * len(xs)))], 1)

    def to_json(self) -> dict[str, Any]:
        return {
            "planes": self.planes,
            "n": self.n,
            "recall": self.recall,
            "mrr": self.mrr,
            "abstain_share": self.abstain_share,
            "latency_p50_ms": self.p(0.5),
            "latency_p95_ms": self.p(0.95),
            "ch_queries_p50": self.ch_queries_p50,
            "ch_read_rows_p50": self.ch_read_rows_p50,
        }


# ----------------------------------------------------------------------------
# pure helpers (unit-tested)
# ----------------------------------------------------------------------------


def parse_planes(spec: str) -> list[list[str]]:
    """'fact|fact,chunk|all' → [['fact'], ['fact','chunk'], []]; [] = no mask
    (every plane), which is how `execution_planes` reads an empty list."""
    arms: list[list[str]] = []
    for arm in (spec or DEFAULT_PLANES).split("|"):
        arm = arm.strip()
        if not arm:
            continue
        arms.append([] if arm == "all" else [p.strip() for p in arm.split(",") if p.strip()])
    return arms


def load_questions(path: str) -> tuple[list[dict[str, Any]], str, str]:
    """Questions as {query, gold_keys} plus the dataset's (org, namespace) if
    it names them. Accepts the retrieval_recall and quality dataset shapes."""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    if isinstance(data, dict) and "questions" in data:
        qs = [
            {"query": q.get("query") or q.get("question"), "gold_keys": q["gold_keys"]}
            for q in data["questions"]
        ]
        return qs, str(data.get("org") or ""), str(data.get("namespace") or "")
    qs = []
    for item in data if isinstance(data, list) else []:
        for q in item.get("questions", []):
            qs.append({"query": q.get("question") or q.get("query"), "gold_keys": q["gold_keys"]})
    return qs, "", ""


def score(resp: dict[str, Any], gold_keys: list[str], arm: ArmResult, latency_ms: float) -> None:
    row = score_one(resp, gold_keys)
    arm.n += 1
    if row.hit_rank is not None:
        arm.hits += 1
        arm.rr_sum += 1.0 / row.hit_rank
    if row.abstain:
        arm.abstains += 1
    arm.latencies_ms.append(latency_ms)


def render(arms: list[ArmResult], k: int) -> str:
    head = (
        f"{'planes':<22} {'n':>4} {'recall@' + str(k):>9} {'mrr':>6} {'abstain':>8} "
        f"{'p50 ms':>7} {'p95 ms':>7} {'ch q/req':>9} {'ch rows/req':>12}"
    )
    lines = ["plane ablation", head, "-" * len(head)]
    for a in arms:
        chq = "-" if a.ch_queries_p50 is None else f"{a.ch_queries_p50:.0f}"
        chr_ = "-" if a.ch_read_rows_p50 is None else f"{a.ch_read_rows_p50:,.0f}"
        lines.append(
            f"{a.planes:<22} {a.n:>4} {a.recall:>9.3f} {a.mrr:>6.3f} {a.abstain_share:>8.1%} "
            f"{a.p(0.5):>7.0f} {a.p(0.95):>7.0f} {chq:>9} {chr_:>12}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------


def _post(base_url: str, body: dict[str, Any], token: str, request_id: str) -> dict[str, Any]:
    req = urllib.request.Request(
        base_url + "/v1/memory.search",
        data=json.dumps(body).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "X-Request-Id": request_id,
        },
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read().decode() or "{}")


def _ch_cost(ch_url: str, user: str, password: str, request_ids: list[str]) -> tuple[float, float]:
    """Median (queries, read rows) per request over the tagged query-log rows."""
    # ClickHouse array parameters are literals, not JSON: ['a','b']
    rids_lit = "[" + ",".join("'" + r.replace("'", "\\'") + "'" for r in request_ids) + "]"
    # Read-only, credentials in the auth headers (never the URL).
    q = {"default_format": "JSONEachRow", "readonly": "2", "param_rids": rids_lit}
    sql = (
        "SELECT quantile(0.5)(q) AS qp50, quantile(0.5)(rr) AS rrp50 FROM ("
        "SELECT splitByChar(' ', log_comment)[2] AS rid, count() AS q, sum(read_rows) AS rr "
        "FROM system.query_log WHERE type = 'QueryFinish' AND is_initial_query "
        "AND event_time > now() - INTERVAL 1 DAY AND has({rids:Array(String)}, "
        "splitByChar(' ', log_comment)[2]) GROUP BY rid)"
    )
    req = urllib.request.Request(
        ch_url.rstrip("/") + "/?" + urllib.parse.urlencode(q),
        data=sql.encode(),
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        rows = [json.loads(line) for line in r.read().decode().splitlines() if line.strip()]
    if not rows:
        return 0.0, 0.0
    return float(rows[0].get("qp50") or 0), float(rows[0].get("rrp50") or 0)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--base-url", default="http://localhost:7777")
    ap.add_argument("--token", default=os.environ.get("LOOM_API_KEY", ""))
    ap.add_argument("--org", default="")
    ap.add_argument("--namespace", default="")
    ap.add_argument("--agent", default="*")
    ap.add_argument("--top-k", type=int, default=10)
    ap.add_argument("--search-mode", default="")
    ap.add_argument("--planes", default=DEFAULT_PLANES)
    ap.add_argument("--ch-url", default="")
    ap.add_argument("--ch-user", default="loom")
    ap.add_argument(
        "--ch-password",
        default=os.environ.get("LOOM_CH_PASSWORD", "loomdev"),
        help="default LOOM_CH_PASSWORD, else the dev stack's password",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    questions, ds_org, ds_ns = load_questions(args.dataset)
    org = args.org or ds_org or "clickhouse"
    ns = args.namespace or ds_ns
    if not ns:
        print("--namespace required (dataset names none)", file=sys.stderr)
        return 2

    arms: list[ArmResult] = []
    for planes in parse_planes(args.planes):
        arm = ArmResult(planes=",".join(planes) or "all")
        for q in questions:
            body: dict[str, Any] = {
                "org": org,
                "namespace": ns,
                "agent": args.agent,
                "query": q["query"],
                "top_k": args.top_k,
                "auto_touch": False,
            }
            if planes:
                body["execution_planes"] = planes
            if args.search_mode:
                body["search_mode"] = args.search_mode
            rid = f"ablate-{uuid.uuid4().hex[:12]}"
            t0 = time.perf_counter()
            try:
                resp = _post(args.base_url, body, args.token, rid)
            except Exception as e:  # noqa: BLE001
                print(
                    f"  {arm.planes}: search failed for {q['query'][:50]!r}: {e}", file=sys.stderr
                )
                continue
            score(resp, q["gold_keys"], arm, (time.perf_counter() - t0) * 1000)
            arm.request_ids.append(rid)
        arms.append(arm)

    if args.ch_url:
        time.sleep(10)  # system.query_log flushes every ~7.5 s; one wait for every arm
        for arm in arms:
            if not arm.request_ids:
                continue
            try:
                arm.ch_queries_p50, arm.ch_read_rows_p50 = _ch_cost(
                    args.ch_url,
                    args.ch_user,
                    args.ch_password,
                    arm.request_ids,
                )
            except Exception as e:  # noqa: BLE001
                print(f"  ch cost lookup failed for {arm.planes}: {e}", file=sys.stderr)

    if args.json:
        print(
            json.dumps(
                {"k": args.top_k, "questions": len(questions), "arms": [a.to_json() for a in arms]},
                indent=2,
            )
        )
    else:
        print(render(arms, args.top_k))
    return 0


if __name__ == "__main__":
    sys.exit(main())
