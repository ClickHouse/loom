"""ClickHouse cost per loom operation, from system.query_log: latency per
memory operation, storage read per call, retrieval calls per step.

Every ClickHouse query loom issues carries `log_comment = "<op> <request_id>"`
(HTTP/MCP) or `"worker.<name>"` (background passes). That makes the query log joinable
to loom's operations, and this script is the join:

    LOOM_CH_PASSWORD=... python bench/ch_cost_by_op.py --ch-url https://host:8443 --hours 24
    python bench/ch_cost_by_op.py ... --request-id 3f2a...        # one request's queries, in order
    python bench/ch_cost_by_op.py ... --json                      # for bench/report consumers
    python bench/ch_cost_by_op.py ... --shapes 15                 # heaviest query shapes, by bytes
    python bench/ch_cost_by_op.py ... --explain 1205642163508112436   # EXPLAIN indexes=1, one shape

Per operation: queries, queries per request (retrieval calls per step),
p50/p95 duration, rows and MiB read per query, peak memory p95, and the share
of the window's ClickHouse time. Untagged queries are reported as one row
(`<untagged>`) so a transport that forgot the tag is visible, not hidden.

`--shapes` ranks query shapes (normalizedQueryHash, so literals and parameters collapse)
by bytes read over the window: calls, GB, MiB and rows per call, result rows per call,
and one sample. Per-op totals say which operation is expensive; the shape says which
statement, and bytes-per-call against result-rows-per-call is the tell for a read that
fetches whole granules to return a handful of rows. `--explain` prints `EXPLAIN indexes
= 1` for the newest query of a shape — the primary key, skip index and
`PrimaryKeyExpand` granule counts under FINAL.

Without the tag, the only way to attribute cost is to group query text by
the table it names over days of log.

Every statement runs with `readonly = 2`, so the tool cannot write even with a
writer's credentials. The user and password travel in the ClickHouse auth
headers (`--ch-user` / `LOOM_CH_USER`, `LOOM_CH_PASSWORD` or `--ch-password`),
never in the URL, where proxies and server logs would record them.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class OpCost:
    op: str
    queries: int
    requests: int
    p50_ms: float
    p95_ms: float
    read_rows_avg: float
    read_mib_total: float
    peak_mem_mib_p95: float
    total_ms: float
    share_of_time: float = 0.0

    @property
    def queries_per_request(self) -> float:
        return round(self.queries / self.requests, 2) if self.requests else 0.0

    def to_json(self) -> dict[str, Any]:
        return {**self.__dict__, "queries_per_request": self.queries_per_request}


@dataclass
class CostReport:
    hours: float
    rows: list[OpCost] = field(default_factory=list)

    @property
    def untagged_share(self) -> float:
        total = sum(r.queries for r in self.rows)
        un = sum(r.queries for r in self.rows if r.op == "<untagged>")
        return round(un / total, 4) if total else 0.0

    def to_json(self) -> dict[str, Any]:
        return {
            "hours": self.hours,
            "untagged_share": self.untagged_share,
            "ops": [r.to_json() for r in self.rows],
        }


# ----------------------------------------------------------------------------
# pure helpers (unit-tested)
# ----------------------------------------------------------------------------


def op_of(log_comment: str) -> str:
    """The operation an entry belongs to: the first token of the tag, or
    `<untagged>`. `worker.<name>` tags have no request id and pass through."""
    tag = (log_comment or "").strip()
    if not tag:
        return "<untagged>"
    return tag.split(" ", 1)[0]


def request_of(log_comment: str) -> str:
    """The request id half of a tag, or '' for workers and untagged rows. A statement
    digest (`sql:<shape> <head>`) follows the tag as trailing tokens and is never the
    request id."""
    parts = (log_comment or "").strip().split(" ")
    if len(parts) < 2 or parts[1].startswith("sql:"):
        return ""
    return parts[1].strip()


def by_op_sql(hours: float, database: str = "loom") -> str:
    """One GROUP BY over the window. `op` and `request` are derived in SQL
    the same way `op_of`/`request_of` do it, so the two never disagree.
    System and migration statements are excluded; the database filter keeps
    a shared service's other tenants out."""
    return f"""
        SELECT
            if(log_comment = '', '<untagged>', splitByChar(' ', log_comment)[1]) AS op,
            count() AS queries,
            uniqExactIf(splitByChar(' ', log_comment)[2],
                        length(splitByChar(' ', log_comment)) >= 2
                        AND NOT startsWith(splitByChar(' ', log_comment)[2], 'sql:')) AS requests,
            quantile(0.5)(query_duration_ms) AS p50_ms,
            quantile(0.95)(query_duration_ms) AS p95_ms,
            avg(read_rows) AS read_rows_avg,
            sum(read_bytes) / 1048576 AS read_mib_total,
            quantile(0.95)(memory_usage) / 1048576 AS peak_mem_mib_p95,
            sum(query_duration_ms) AS total_ms
        FROM system.query_log
        WHERE type = 'QueryFinish'
          AND event_time > now() - INTERVAL {int(hours * 3600)} SECOND
          AND is_initial_query
          AND has(databases, '{database}')
          AND query NOT ILIKE '%system.%'
        GROUP BY op
        ORDER BY total_ms DESC
    """


def shapes_sql(hours: float, database: str = "loom", limit: int = 15) -> str:
    """Query shapes by bytes read. `normalizedQueryHash` folds literals and
    bound parameters, so every call of one statement lands in one row."""
    return f"""
        SELECT
            if(log_comment = '', '<untagged>', splitByChar(' ', log_comment)[1]) AS op,
            toString(normalizedQueryHash(query)) AS shape,
            count() AS calls,
            sum(read_bytes) / 1073741824 AS read_gib_total,
            avg(read_bytes) / 1048576 AS read_mib_per_call,
            avg(read_rows) AS read_rows_per_call,
            avg(result_rows) AS result_rows_per_call,
            quantile(0.5)(query_duration_ms) AS p50_ms,
            substring(replaceRegexpAll(any(query), '\\s+', ' '), 1, 200) AS sample
        FROM system.query_log
        WHERE type = 'QueryFinish'
          AND event_time > now() - INTERVAL {int(hours * 3600)} SECOND
          AND is_initial_query
          AND has(databases, '{database}')
          AND query NOT ILIKE '%system.%'
        GROUP BY op, shape
        ORDER BY sum(read_bytes) DESC
        LIMIT {int(limit)}
    """


def explain_target_sql(hours: float) -> str:
    """The newest query text of one shape, to feed EXPLAIN."""
    return f"""
        SELECT query
        FROM system.query_log
        WHERE type = 'QueryFinish' AND is_initial_query
          AND event_time > now() - INTERVAL {int(hours * 3600)} SECOND
          AND toString(normalizedQueryHash(query)) = {{shape:String}}
        ORDER BY event_time DESC
        LIMIT 1
    """


def render_shapes(rows: list[dict[str, Any]], hours: float) -> str:
    out = [
        f"query shapes by bytes read, last {hours:g} h (bytes/call vs result rows/call "
        "is the tell for a granule-wide read that returns a few rows)",
        f"{'op':<30} {'calls':>7} {'GiB':>8} {'MiB/call':>9} {'rows/call':>10} "
        f"{'res/call':>8} {'p50 ms':>7}  shape",
    ]
    for r in rows:
        out.append(
            f"{str(r['op'])[:30]:<30} {int(r['calls']):>7} {float(r['read_gib_total']):>8.1f} "
            f"{float(r['read_mib_per_call']):>9.1f} {float(r['read_rows_per_call']):>10.0f} "
            f"{float(r['result_rows_per_call']):>8.1f} {float(r['p50_ms']):>7.0f}  {r['shape']}"
        )
        out.append(f"{'':<30} {str(r['sample'])[:150]}")
    return "\n".join(out)


def request_sql(request_id: str) -> str:
    return """
        SELECT event_time_microseconds AS at, query_duration_ms AS ms, read_rows,
               round(memory_usage / 1048576, 1) AS peak_mib,
               substring(replaceRegexpAll(query, '\\\\s+', ' '), 1, 160) AS query
        FROM system.query_log
        WHERE type = 'QueryFinish' AND is_initial_query
          AND endsWith(log_comment, {rid:String})
          AND event_time > now() - INTERVAL 7 DAY
        ORDER BY at
    """


def build_report(hours: float, rows: list[dict[str, Any]]) -> CostReport:
    """Rows as returned by `by_op_sql`, into a report with time shares."""
    total_ms = sum(float(r.get("total_ms") or 0) for r in rows) or 1.0
    out = CostReport(hours=hours)
    for r in rows:
        op = str(r.get("op") or "<untagged>")
        out.rows.append(
            OpCost(
                op=op,
                queries=int(r.get("queries") or 0),
                requests=int(r.get("requests") or 0),
                p50_ms=round(float(r.get("p50_ms") or 0), 1),
                p95_ms=round(float(r.get("p95_ms") or 0), 1),
                read_rows_avg=round(float(r.get("read_rows_avg") or 0)),
                read_mib_total=round(float(r.get("read_mib_total") or 0), 1),
                peak_mem_mib_p95=round(float(r.get("peak_mem_mib_p95") or 0), 1),
                total_ms=float(r.get("total_ms") or 0),
                share_of_time=round(float(r.get("total_ms") or 0) / total_ms, 4),
            )
        )
    out.rows.sort(key=lambda c: -c.total_ms)
    return out


def render(report: CostReport) -> str:
    head = (
        f"{'op':<34} {'queries':>8} {'q/req':>6} {'p50 ms':>8} {'p95 ms':>8} "
        f"{'rows/q':>10} {'read MiB':>9} {'mem p95':>8} {'time%':>6}"
    )
    lines = [
        f"ClickHouse cost by operation, last {report.hours:g} h "
        f"(untagged share {report.untagged_share:.1%})",
        head,
        "-" * len(head),
    ]
    for r in report.rows:
        lines.append(
            f"{r.op[:34]:<34} {r.queries:>8} {r.queries_per_request:>6} {r.p50_ms:>8.0f} "
            f"{r.p95_ms:>8.0f} {r.read_rows_avg:>10.0f} {r.read_mib_total:>9.1f} "
            f"{r.peak_mem_mib_p95:>8.0f} {r.share_of_time:>6.1%}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------------
# I/O
# ----------------------------------------------------------------------------


_DATABASE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def ch_request(
    url: str,
    user: str,
    password: str,
    sql: str,
    params: dict[str, str] | None = None,
) -> urllib.request.Request:
    """The HTTP request for one statement: credentials in the auth headers,
    `readonly = 2` and the bound parameters in the query string."""
    q = {"default_format": "JSONEachRow", "readonly": "2"}
    for k, v in (params or {}).items():
        q[f"param_{k}"] = v
    return urllib.request.Request(
        url.rstrip("/") + "/?" + urllib.parse.urlencode(q),
        data=sql.encode(),
        headers={"X-ClickHouse-User": user, "X-ClickHouse-Key": password},
    )


def _ch(
    url: str,
    user: str,
    password: str,
    sql: str,
    params: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    try:
        with urllib.request.urlopen(ch_request(url, user, password, sql, params), timeout=300) as r:
            return [json.loads(line) for line in r.read().decode().splitlines() if line.strip()]
    except urllib.error.HTTPError as e:
        body = e.read()[:500].decode(errors="replace")
        raise SystemExit(f"ClickHouse HTTP {e.code}: {body}") from None


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--ch-url", default="http://localhost:8123")
    ap.add_argument("--ch-user", default=os.environ.get("LOOM_CH_USER", "loom"))
    ap.add_argument(
        "--ch-password",
        default=os.environ.get("LOOM_CH_PASSWORD", "loomdev"),
        help="default LOOM_CH_PASSWORD, else the dev stack's password",
    )
    ap.add_argument("--ch-database", default=os.environ.get("LOOM_CH_DATABASE", "loom"))
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--request-id", default="", help="list one request's queries instead")
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--shapes",
        type=int,
        default=0,
        metavar="N",
        help="rank the N heaviest query shapes by bytes read instead",
    )
    ap.add_argument(
        "--explain",
        default="",
        metavar="SHAPE",
        help="EXPLAIN indexes=1 the newest query of this shape hash",
    )
    args = ap.parse_args()
    if not _DATABASE_RE.match(args.ch_database):
        ap.error(f"--ch-database {args.ch_database!r} is not a database name")

    if args.explain:
        rows = _ch(
            args.ch_url,
            args.ch_user,
            args.ch_password,
            explain_target_sql(args.hours),
            {"shape": args.explain},
        )
        if not rows:
            print(f"no query with shape {args.explain} in the last {args.hours:g} h")
            return 1
        text = str(rows[0]["query"])
        print(text.strip()[:2000])
        print()
        plan = _ch(
            args.ch_url,
            args.ch_user,
            args.ch_password,
            "EXPLAIN indexes = 1 " + text.strip().rstrip(";").split(" FORMAT ")[0],
        )
        print("\n".join(str(r.get("explain", "")) for r in plan))
        return 0

    if args.shapes:
        rows = _ch(
            args.ch_url,
            args.ch_user,
            args.ch_password,
            shapes_sql(args.hours, args.ch_database, args.shapes),
        )
        print(
            json.dumps(rows, indent=2, default=str)
            if args.json
            else render_shapes(rows, args.hours)
        )
        return 0

    if args.request_id:
        rows = _ch(
            args.ch_url,
            args.ch_user,
            args.ch_password,
            request_sql(args.request_id),
            {"rid": args.request_id},
        )
        if args.json:
            print(json.dumps(rows, indent=2, default=str))
        else:
            print(f"{len(rows)} queries for request {args.request_id}")
            for r in rows:
                print(
                    f"  {str(r['at'])[11:23]}  {r['ms']:>6} ms  {r['read_rows']:>9} rows  "
                    f"{r['peak_mib']:>7} MiB  {r['query']}"
                )
        return 0

    rows = _ch(args.ch_url, args.ch_user, args.ch_password, by_op_sql(args.hours, args.ch_database))
    report = build_report(args.hours, rows)
    print(json.dumps(report.to_json(), indent=2) if args.json else render(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
