"""Mixed knowledge load against a running server, checked against the service
levels docs/architecture/document-extraction.md declares ("Service levels").

For `--duration` seconds it runs, at once:

- small-document ingests: a short Markdown file uploaded and ingested every
  `1 / --ingest-rate` seconds, each followed until its job ends;
- one large PDF (`--pdf-pages` pages) and one CSV import (`--csv-rows` rows),
  ingested at the start;
- knowledge.search at `--search-rate` calls a second over what is ingested;
- knowledge.query at `--query-rate` calls a second (a group-by over the CSV's
  dataset, once it is published);
- a sampler reading the namespace's open jobs from knowledge.inspect every
  `--sample-seconds`.

With `--tenants K` the same load, except the one large PDF, runs in K namespaces,
each in an org of its own (`<org>-0` …, so a token must reach them all: auth off,
or an org-wide token), so the server carries K times the load while each
tenant stays within its own rate limits (`server/core/ratelimit.py`). Calls
arrive at their rate whether or not earlier ones have answered (open loop),
so a slow server meets the same load, and a rejected call is not retried. It
reports, per op, the calls, p50 / p95 / p99 latency of the successful ones,
errors by status and the rejected ones (429 and 503); per ingest, the time from
its job's creation to its end (the server's timestamps); and the queue depth over
the run. It exits 1 when a measure is past its declared level or has calls that
failed (not counting rejections), or a small-document ingest did not end ready.
It exits 2 when the server cannot run the load: it does not answer, the token
does not reach every tenant's org, knowledge.inspect reports the ingest worker
off or no extractor in this server's settings, or a canary document is not
taken and published ready within CANARY_S.

The deployment needs a process running the ingest worker (a server started with
`LOOM_BACKGROUND_WORKERS=on`, or a deployment, `LOOM_DEPLOYMENT=1`, under `auto`)
and an extractor (`LOOM_DOCUMENT_EXTRACTOR_URL`). Everything is written into a fresh
`bench-…` namespace of `--org` and left there. Nothing calls a model provider:
run the server on the fake embedder to spend nothing.

    PYTHONPATH=$PWD .venv/bin/python bench/knowledge_load.py --base-url http://localhost:7777 \\
        --duration 120 --json > load.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

DOC = Path(__file__).resolve().parents[1] / "docs" / "architecture" / "document-extraction.md"
# Ops whose latency the run measures, and the ingest-time measure.
OPS = (
    "knowledge.search",
    "knowledge.query",
    "knowledge.upload",
    "knowledge.ingest",
    "knowledge.ingest_status",
)
INGEST_SMALL = "ingest small document"
REJECTED = (429, 503)
OPEN = ("pending", "processing")
# Between one job's knowledge.ingest_status reads: every open job polls from the
# namespace's knowledge.read rate limit.
POLL_S = 1.0
CANARY_S = 60.0  # a deployment that takes no job in this long runs no worker
INGEST_WAIT_S = 600  # past it a job counts as still open: the run reports it, not hangs


def percentile(values: list[float], p: float) -> float:
    """Nearest-rank percentile; 0.0 for no values."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(len(s) - 1, max(0, math.ceil(p * len(s)) - 1))]


def service_levels(text: str) -> dict[str, tuple[float, float]]:
    """Measure to (p95 ms, p99 ms), from the table under `## Service levels`. A row
    that is not a measure and two whole numbers, or one repeated, is a ValueError."""
    marker = "\n## Service levels\n"
    if marker not in text:
        raise ValueError("no '## Service levels' heading")
    section = re.split(r"\n#{2,} ", text.split(marker, 1)[1], maxsplit=1)[0]
    rows = [line.strip() for line in section.splitlines() if line.strip().startswith("|")]
    levels: dict[str, tuple[float, float]] = {}
    for line in rows[2:]:
        cells = [c.strip().strip("`") for c in line.strip("|").split("|")]
        if len(cells) != 3 or not all(re.fullmatch(r"\d[\d,]*", c) for c in cells[1:]):
            raise ValueError(f"not a (measure, p95 ms, p99 ms) row: {line}")
        if cells[0] in levels:
            raise ValueError(f"a repeated row: {line}")
        levels[cells[0]] = (float(cells[1].replace(",", "")), float(cells[2].replace(",", "")))
    if not levels:
        raise ValueError("no rows under '## Service levels'")
    return levels


@dataclass
class Tally:
    """One measure's outcomes over the run."""

    latencies_ms: list[float] = field(default_factory=list)
    errors: Counter[str] = field(default_factory=Counter)

    def add(self, ms: float, status: int) -> None:
        if status == 200 or status == 201:
            self.latencies_ms.append(ms)
        else:
            self.errors[str(status)] += 1

    def summary(self) -> dict[str, Any]:
        v = self.latencies_ms
        rejected = sum(n for code, n in self.errors.items() if int(code) in REJECTED)
        return {
            "ok": len(v),
            "p50_ms": round(percentile(v, 0.5), 1),
            "p95_ms": round(percentile(v, 0.95), 1),
            "p99_ms": round(percentile(v, 0.99), 1),
            "errors": dict(self.errors),
            "rejected": rejected,
        }


def verdict(report: dict[str, dict[str, Any]], levels: dict[str, tuple[float, float]]) -> list[str]:
    """The measures past their declared level, each with its numbers. A measure the
    run made no successful call of is past (nothing was measured), and so is one
    with any error that is not a rejection: fast answers to the calls that worked
    say nothing of the ones that failed."""
    past = []
    for measure, (p95, p99) in levels.items():
        got = report.get(measure)
        failed = {c: n for c, n in (got or {}).get("errors", {}).items() if int(c) not in REJECTED}
        if not got or not got["ok"]:
            past.append(f"{measure}: nothing measured")
        elif failed:
            past.append(f"{measure}: {sum(failed.values())} calls failed {failed}")
        elif got["p95_ms"] > p95 or got["p99_ms"] > p99:
            past.append(
                f"{measure}: p95 {got['p95_ms']} ms (level {p95:g}), p99 {got['p99_ms']} ms"
                f" (level {p99:g})"
            )
    return past


class Run:
    """One tenant's load: its namespace, the calls it makes, and what they measured
    into the run's shared `tallies` and `small_states`."""

    def __init__(
        self,
        c: httpx.AsyncClient,
        org: str,
        ns: str,
        deadline: float,
        tallies: dict[str, Tally],
        small_states: Counter[str],
    ) -> None:
        self.c, self.org, self.ns, self.deadline = c, org, ns, deadline
        self.tallies, self.small_states = tallies, small_states
        self.ingests: dict[str, dict[str, Any]] = {}  # large and CSV: their outcome
        self.depth: list[tuple[int, int]] = []  # (sample slot, open jobs)
        self.dataset: dict[str, str] | None = None

    def live(self) -> bool:
        return time.monotonic() < self.deadline

    async def op(self, name: str, body: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        """The call's status (0 when no answer came) and its JSON body, read only for a
        2xx: a proxy's error page is a failed call, tallied like any other."""
        t0 = time.perf_counter()
        try:
            r = await self.c.post(
                f"/v1/{name}", json={"org": self.org, "namespace": self.ns, **body}
            )
            status = r.status_code
        except httpx.HTTPError:
            r, status = None, 0
        out: Any = {}
        if r is not None and 200 <= status < 300:
            try:
                out = r.json()
            except ValueError:
                status = 0  # an answer that is not JSON is no answer
        if name in self.tallies:
            self.tallies[name].add((time.perf_counter() - t0) * 1000, status)
        if not 200 <= status < 300:
            return status, {}
        return status, out if isinstance(out, dict) else {}

    async def ingest(
        self, data: bytes, filename: str, uri: str, wait_s: float = INGEST_WAIT_S
    ) -> tuple[str, float]:
        """Upload and ingest `data`, then wait for its job to end: its state and the
        seconds from the job's creation to its end, by the server's own timestamps, so
        the 1 s poll adds nothing ("not queued" when the upload or the ingest failed,
        "still open" past `wait_s`)."""
        t0 = time.perf_counter()
        try:
            up = await self.c.post(
                "/v1/knowledge.upload",
                params={
                    "org": self.org,
                    "namespace": self.ns,
                    "filename": filename,
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
                content=data,
            )
            status = up.status_code
        except httpx.HTTPError:
            status = 0
        self.tallies["knowledge.upload"].add((time.perf_counter() - t0) * 1000, status)
        if status != 201:
            return "not queued", 0.0
        status, job = await self.op(
            "knowledge.ingest", {"upload_id": up.json()["upload_id"], "source_uri": uri}
        )
        if status != 200:
            return "not queued", 0.0
        polled = time.perf_counter()
        while time.perf_counter() - polled < wait_s:
            await asyncio.sleep(POLL_S)
            _, st = await self.op("knowledge.ingest_status", {"ingestion_id": job["ingestion_id"]})
            if st.get("state") and st["state"] not in OPEN:
                return str(st["state"]), _job_seconds(st)
        return "still open", time.perf_counter() - polled

    async def small_ingest(self, n: int) -> None:
        text = f"# Note {n}\n\nReplica {n % 7} handled request batch {n} in order.\n"
        state, seconds = await self.ingest(text.encode(), f"note-{n}.md", f"repo://notes/{n}.md")
        self.small_states[state] += 1
        if state == "ready":
            self.tallies[INGEST_SMALL].add(seconds * 1000, 200)

    async def one_ingest(self, label: str, data: bytes, filename: str, uri: str) -> None:
        state, seconds = await self.ingest(data, filename, uri)
        self.ingests[label] = {"state": state, "seconds": round(seconds, 2), "bytes": len(data)}
        if label == "csv" and state == "ready":
            _, got = await self.op("knowledge.dataset_get", {"source_uri": uri})
            sets = got.get("datasets") or []
            if sets:
                self.dataset = {"source_uri": uri, "dataset_id": str(sets[0]["dataset_id"])}

    async def search(self, n: int) -> None:
        words = ("replica", "request batch", "order", "region owner", "invoice total")
        await self.op(
            "knowledge.search",
            {"query": words[n % len(words)], "namespace_scope": "this", "top_k": 10},
        )

    async def query(self, n: int) -> None:
        if self.dataset is None:  # the CSV is not published yet
            return
        await self.op(
            "knowledge.query",
            {
                "datasets": [self.dataset],
                "group_by": [{"column": {"column": "region"}}],
                "aggregates": [
                    {"fn": "count", "name": "n"},
                    {"fn": "sum", "column": {"column": "amount"}, "name": "total"},
                ],
            },
        )

    async def paced(self, rate: float, call: Callable[[int], Awaitable[None]]) -> None:
        """`call(n)` every `1 / rate` seconds until the deadline, each on its own task,
        then waits for the ones still running."""
        if rate <= 0:
            return
        running: set[asyncio.Task[None]] = set()
        start, n = time.monotonic(), 0
        while self.live():
            n += 1
            task = asyncio.create_task(call(n))
            running.add(task)
            task.add_done_callback(running.discard)
            await asyncio.sleep(max(0.0, start + n / rate - time.monotonic()))
        await asyncio.gather(*running)

    async def sampler(self, every: float, start: float) -> None:
        """The namespace's open jobs every `every` seconds, as (second of the run,
        depth); a read that failed is no sample, not an empty queue."""
        while self.live():
            status, got = await self.op("knowledge.inspect", {})
            totals = got.get("ingestions") or {}
            if status == 200 and totals.get("pending") is not None:
                at = int((time.monotonic() - start) // every)
                self.depth.append((at, int(totals["pending"]) + int(totals.get("processing") or 0)))
            await asyncio.sleep(every)


def _job_seconds(status: dict[str, Any]) -> float:
    """A finished job's time from its creation to its last update, by the server's
    clock (`knowledge.ingest_status` `created_at` / `updated_at`)."""
    created, updated = (
        datetime.fromisoformat(str(status[k])) for k in ("created_at", "updated_at")
    )
    return (updated - created).total_seconds()


def combined_depth(series: list[list[tuple[int, int]]]) -> dict[int, int]:
    """The open jobs across tenants per sample slot: each tenant's last reading in
    the slot, summed over the tenants that read one there."""
    by_slot: dict[int, dict[int, int]] = {}
    for tenant, samples in enumerate(series):
        for slot, depth in samples:
            by_slot.setdefault(slot, {})[tenant] = depth
    return {slot: sum(d.values()) for slot, d in sorted(by_slot.items())}


def _pdf(pages: int) -> bytes:
    from tests.fixtures.office_files import pdf

    lines = "\n".join(
        f"Line {n}: the regional replicas report their lag each minute." for n in range(30)
    )
    return pdf([f"Page {p}\n{lines}" for p in range(1, pages + 1)], [])


def _csv(rows: int) -> bytes:
    regions = ("emea", "apac", "amer", "latam")
    body = "".join(f"{n},{regions[n % 4]},{n * 1.25:.2f}\n" for n in range(1, rows + 1))
    return ("order_id,region,amount\n" + body).encode()


async def _probe(c: httpx.AsyncClient, orgs: list[str], ns: str) -> str:
    """Why the server cannot run the load, or "": knowledge.inspect in each tenant's
    org must answer (the token reaches it) and report the ingest worker on and an
    extractor configured in this server's settings."""
    for org in orgs:
        try:
            r = await c.post("/v1/knowledge.inspect", json={"org": org, "namespace": ns})
            body = r.json() if r.status_code == 200 else {}
        except (httpx.HTTPError, ValueError) as e:
            return f"knowledge.inspect in {org} got no answer: {type(e).__name__}"
        if r.status_code in (401, 403):
            return f"the token cannot read {org}/{ns}: {r.status_code} {r.text[:300]}"
        totals = (body if isinstance(body, dict) else {}).get("ingestions") or {}
        if not (totals.get("worker_enabled") and totals.get("extractor_configured")):
            return f"knowledge.inspect in {org} said {r.status_code} {totals or r.text[:300]}"
    return ""


def _fresh_tallies() -> dict[str, Tally]:
    return {m: Tally() for m in (*OPS, INGEST_SMALL)}


async def main(args: argparse.Namespace) -> int:
    levels = service_levels(DOC.read_text())
    ns = args.namespace or f"bench-{uuid.uuid4().hex[:8]}"
    # One org per tenant: the per-namespace and per-org rate limits bound each
    # tenant alone, so more tenants load the server harder within them.
    orgs = [args.org] if args.tenants == 1 else [f"{args.org}-{k}" for k in range(args.tenants)]
    headers = {"Authorization": f"Bearer {args.token}"} if args.token else {}
    tallies = _fresh_tallies()
    small_states: Counter[str] = Counter()
    async with httpx.AsyncClient(base_url=args.base_url, timeout=120, headers=headers) as c:
        refused = await _probe(c, orgs, ns)
        if refused:
            print(f"the server cannot run the load: {refused}", file=sys.stderr)
            return 2
        # knowledge.inspect reads this server's settings, not whether any process runs
        # the worker: a canary job, outside the run's tallies, shows one takes jobs.
        canary = Run(c, orgs[0], ns, time.monotonic(), _fresh_tallies(), Counter())
        state, _ = await canary.ingest(
            b"# Canary\n\nReady.\n", "canary.md", "repo://canary.md", CANARY_S
        )
        if state != "ready":
            print(f"the canary ingest ended {state!r} (waited up to {CANARY_S:g} s): a worker"
                  " must take and publish a small document", file=sys.stderr)  # fmt: skip
            return 2
        start = time.monotonic()
        deadline = start + args.duration
        runs = [Run(c, org, ns, deadline, tallies, small_states) for org in orgs]
        tasks = [
            # One large PDF for the whole run; each tenant its own CSV to query.
            runs[0].one_ingest("large pdf", _pdf(args.pdf_pages), "large.pdf", "repo://large.pdf"),
        ]
        for run in runs:
            tasks += [
                run.one_ingest("csv", _csv(args.csv_rows), "orders.csv", "repo://orders.csv"),
                run.sampler(args.sample_seconds, start),
                run.paced(args.ingest_rate, run.small_ingest),
                run.paced(args.search_rate, run.search),
                run.paced(args.query_rate, run.query),
            ]
        await asyncio.gather(*tasks)
    report = {m: t.summary() for m, t in tallies.items()}
    past = verdict(report, levels)
    if small_states.keys() - {"ready"}:
        past.append(f"{INGEST_SMALL}: ended {dict(small_states)}")
    depth = combined_depth([r.depth for r in runs])
    out = {
        "namespace": ns,
        "tenants": orgs,
        "duration_s": args.duration,
        "rates_per_s_per_tenant": {
            "ingest": args.ingest_rate,
            "search": args.search_rate,
            "query": args.query_rate,
        },
        "measures": report,
        "small_ingests": dict(small_states),
        "ingests": {f"{r.org} {k}": v for r in runs for k, v in r.ingests.items()},
        "queue_depth": {
            "max": max(depth.values(), default=0),
            "every_s": args.sample_seconds,
            "by_sample": list(depth.values()),
        },
        "levels": {m: {"p95_ms": a, "p99_ms": b} for m, (a, b) in levels.items()},
        "past_level": past,
    }
    print(json.dumps(out, indent=2) if args.json else _table(out))
    return 1 if past else 0


def _table(out: dict[str, Any]) -> str:
    lines = [
        f"namespace {out['namespace']} in {len(out['tenants'])} orgs, {out['duration_s']} s,"
        f" rates/s per tenant {out['rates_per_s_per_tenant']}"
    ]
    for m, s in out["measures"].items():
        lines.append(
            f"  {m:24} ok {s['ok']:6}  p50 {s['p50_ms']:8} p95 {s['p95_ms']:8} p99 {s['p99_ms']:8}"
            f"  rejected {s['rejected']}  errors {s['errors']}"
        )
    lines.append(f"  ingests {out['ingests']}  small {out['small_ingests']}")
    lines.append(f"  queue depth max {out['queue_depth']['max']}")
    lines.append("  past level: " + ("; ".join(out["past_level"]) or "none"))
    return "\n".join(lines)


def _args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    p.add_argument("--token", default=os.environ.get("LOOM_TOKEN", ""))
    p.add_argument("--org", default="bench")
    p.add_argument("--namespace", default="")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--tenants", type=int, default=1)
    p.add_argument("--ingest-rate", type=float, default=0.2)
    # Within one bench namespace's limits (server/core/ratelimit.py DEFAULT_LIMITS).
    p.add_argument("--search-rate", type=float, default=1.0)
    p.add_argument("--query-rate", type=float, default=1.0)
    p.add_argument("--pdf-pages", type=int, default=200)
    p.add_argument("--csv-rows", type=int, default=20_000)
    p.add_argument("--sample-seconds", type=float, default=2.0)
    p.add_argument("--json", action="store_true")
    return p.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(_args())))
