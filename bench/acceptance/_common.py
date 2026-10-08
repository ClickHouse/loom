"""Shared pieces of the acceptance benches: argument parsing, the report,
the ledger cost readout and the explicit-list erasure."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
for _p in (str(ROOT), str(ROOT / "sdk" / "python")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from loom.reference_client import ReferenceClient  # noqa: E402

from bench.bench_namespaces import erase_bench_namespaces  # noqa: E402

# ---------------------------------------------------------------------------
# Metrics and the one-screen report (pure; unit-tested)
# ---------------------------------------------------------------------------


@dataclass
class Metric:
    name: str
    value: float | None
    threshold: float | None = None
    higher_is_better: bool = True
    kind: str = "precision"  # precision | economy | info
    unit: str = ""
    n: int = 0
    note: str = ""

    def passed(self) -> bool | None:
        """True / False against the threshold; None when informational or
        not measured (no threshold, or no value)."""
        if self.threshold is None or self.value is None:
            return None
        if self.higher_is_better:
            return self.value >= self.threshold
        return self.value <= self.threshold

    def fmt_value(self) -> str:
        if self.value is None:
            return "n/a"
        if self.unit == "%":
            return f"{100.0 * self.value:5.1f}%"
        if self.unit == "$":
            return f"${self.value:.4f}"
        if self.unit == "ms":
            return f"{self.value:.0f} ms"
        return f"{self.value:.3f}"

    def fmt_threshold(self) -> str:
        if self.threshold is None:
            return ""
        op = ">=" if self.higher_is_better else "<="
        if self.unit == "%":
            return f"{op} {100.0 * self.threshold:.0f}%"
        if self.unit == "$":
            return f"{op} ${self.threshold:.4f}"
        if self.unit == "ms":
            return f"{op} {self.threshold:.0f} ms"
        return f"{op} {self.threshold:g}"


@dataclass
class Report:
    bench: str
    archetype: str
    base_url: str
    consumer: str
    metrics: list[Metric] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    probe: str = ""
    stats: dict[str, Any] = field(default_factory=dict)
    smoke: bool = False  # a judged bench run at a stated n below its default
    started: float = field(default_factory=time.time)

    def add(self, metric: Metric) -> Metric:
        self.metrics.append(metric)
        return metric

    def note(self, text: str) -> None:
        self.notes.append(text)

    def verdict(self) -> tuple[str, int]:
        """Precision gates first: one failed precision metric fails the bench
        whatever the economy says, and so does a gated precision metric that
        was not measured (a threshold with no value). Then economy. Exit code
        1 on FAIL."""
        precision = [m for m in self.metrics if m.kind == "precision" and m.passed() is not None]
        economy = [m for m in self.metrics if m.kind == "economy" and m.passed() is not None]
        if any(m.passed() is False for m in precision):
            return "FAIL (precision)", 1
        unmeasured = [m for m in self.metrics if m.kind == "precision" and m.threshold is not None]
        if any(m.value is None for m in unmeasured):
            return "FAIL (precision not measured)", 1
        if any(m.passed() is False for m in economy):
            return "FAIL (economy)", 1
        if not precision and not economy:
            return "NO GATES MEASURED", 1
        label = "PASS"
        if self.smoke:
            label += " (smoke only)"
        return label, 0

    def render(self) -> str:
        width = 78
        lines: list[str] = []
        lines.append("=" * width)
        lines.append(f"acceptance/{self.bench} — {self.archetype} — {self.base_url}")
        lines.append(
            f"consumer {self.consumer} · {datetime.now(UTC).strftime('%Y-%m-%d %H:%MZ')}"
            f" · {time.time() - self.started:.0f} s"
        )
        if self.probe:
            lines.append(self.probe)
        lines.append("-" * width)
        for kind, title in (
            ("precision", "precision of what is served (first)"),
            ("economy", "the archetype's economy (second)"),
            ("info", "context"),
        ):
            rows = [m for m in self.metrics if m.kind == kind]
            if not rows:
                continue
            lines.append(title)
            for m in rows:
                p = m.passed()
                mark = "  pass" if p else ("  FAIL" if p is False else "      ")
                n = f"n={m.n}" if m.n else ""
                thr = m.fmt_threshold()
                line = f"  {m.name:<34} {m.fmt_value():>10}  {thr:<12} {n:<8}{mark}"
                if m.note:
                    line += f"  {m.note}"
                lines.append(line.rstrip())
        if self.stats:
            s = self.stats
            lines.append("-" * width)
            lines.append(
                f"wire: {s.get('calls', 0)} calls, {s.get('attempts', 0)} attempts "
                f"({s.get('retries', 0)} retries); failures {s.get('failures_by_code') or {}}"
            )
        for n in self.notes:
            lines.append(f"note: {n}")
        label, _ = self.verdict()
        lines.append("=" * width)
        lines.append(f"VERDICT: {label}")
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        label, code = self.verdict()
        return {
            "bench": self.bench,
            "archetype": self.archetype,
            "base_url": self.base_url,
            "consumer": self.consumer,
            "probe": self.probe,
            "smoke": self.smoke,
            "metrics": [{**asdict(m), "passed": m.passed()} for m in self.metrics],
            "notes": self.notes,
            "stats": self.stats,
            "verdict": label,
            "exit_code": code,
        }


def ratio(num: float, den: float) -> float | None:
    return (num / den) if den else None


def p50(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[len(s) // 2]


# ---------------------------------------------------------------------------
# CLI + client
# ---------------------------------------------------------------------------


def add_common_args(
    ap: argparse.ArgumentParser,
    *,
    namespace_default: str,
    consumer_default: str,
    org_default: str = "clickhouse",
) -> None:
    ap.add_argument("--base-url", default=os.environ.get("LOOM_BASE_URL", "http://localhost:7777"))
    ap.add_argument("--token", default="", help="bearer; else --token-file, LOOM_API_KEY")
    ap.add_argument("--token-file", default="")
    ap.add_argument("--org", default=org_default)
    ap.add_argument("--namespace", default=namespace_default)
    ap.add_argument(
        "--consumer",
        default=consumer_default,
        help="the consumer tag on every call (docs/reference/http-api.md)",
    )
    ap.add_argument("--json", default="", help="write the report as JSON to this path")
    ap.add_argument(
        "--no-outcomes",
        action="store_true",
        help="do not post outcome feedback (memory.outcome / code.outcome)",
    )


def resolve_token(args: argparse.Namespace) -> str:
    if args.token:
        return str(args.token).strip()
    if args.token_file:
        return Path(args.token_file).read_text().strip()
    return (os.environ.get("LOOM_API_KEY") or "").strip()


def make_client(
    args: argparse.Namespace,
    *,
    namespace: str | None = None,
    agent: str = "-",
    timeout_s: float = 30.0,
) -> ReferenceClient:
    return ReferenceClient(
        args.org,
        namespace or args.namespace,
        consumer=args.consumer,
        agent=agent,
        base_url=args.base_url,
        api_key=resolve_token(args),
        timeout_s=timeout_s,
        fail_soft=True,
    )


def nonce() -> str:
    return uuid.uuid4().hex[:6]


def utc_today() -> str:
    return datetime.now(UTC).date().isoformat()


def finish(report: Report, args: argparse.Namespace, client: ReferenceClient | None = None) -> int:
    if client is not None:
        report.stats = client.stats()
    print(report.render())
    if args.json:
        Path(args.json).write_text(json.dumps(report.to_json(), indent=1, default=str))
    return report.verdict()[1]


# ---------------------------------------------------------------------------
# Ledger cost readout + explicit-list erasure
# ---------------------------------------------------------------------------


async def ledger_cost_by_op(
    client: ReferenceClient,
    namespaces: list[str],
    *,
    day: str | None = None,
) -> dict[str, dict[str, float]]:
    """`cost.summary` per namespace, grouped by operation, summed over the
    list: {op: {ops, errors, cost_usd, latency_p50_ms}}. The ledger is the
    server's view of what the run spent upstream (LLM + embeddings), which a
    bench's own judge spend never enters."""
    day = day or utc_today()
    out: dict[str, dict[str, float]] = {}
    for ns in namespaces:
        r = await client.call(
            "cost.summary",
            {"namespace": ns, "from_date": day, "to_date": day, "group_by": ["operation"]},
        )
        if not r.ok:
            continue
        for row in r.data.get("rows") or []:
            op = str(row.get("operation") or "")
            acc = out.setdefault(
                op, {"ops": 0.0, "errors": 0.0, "cost_usd": 0.0, "latency_p50_ms": 0.0}
            )
            acc["ops"] += float(row.get("ops") or 0)
            acc["errors"] += float(row.get("errors") or 0)
            acc["cost_usd"] += float(row.get("cost_usd") or 0.0)
            acc["latency_p50_ms"] = max(
                acc["latency_p50_ms"], float(row.get("latency_p50_ms") or 0.0)
            )
    return out


def bench_post(client: ReferenceClient) -> Callable[[str, dict[str, Any]], Awaitable[Any]]:
    """`client` as the `post(path, body)` helper bench_namespaces takes: a failed
    call raises, so its register / erase bookkeeping sees the refusal."""

    async def post(path: str, body: dict[str, Any]) -> Any:
        op = path.removeprefix("/v1/")
        r = await client.call(op, body, timeout_s=120.0)
        if not r.ok:
            raise RuntimeError(f"{op} refused [{r.code}]")
        return r.data

    return post


async def erase_namespaces(client: ReferenceClient, namespaces: list[str]) -> int:
    """List-scoped purging erasure of an explicit list (never a prefix) through
    bench_namespaces; returns how many were submitted. It needs an org_admin
    token: a refusal is logged with the command that finishes the job, and the
    `kind = bench` rows otherwise retire after 24 h idle."""
    if not namespaces:
        return 0
    return await erase_bench_namespaces(
        bench_post(client), org=client.org, namespaces=sorted(set(namespaces))
    )


def erasure_note(submitted: int, total: int) -> str:
    if submitted == total:
        return f"erasure of {total} namespaces submitted"
    return (
        f"erasure of {total - submitted} of {total} namespaces refused (needs org_admin; "
        "they retire after 24 h idle as kind = bench)"
    )
