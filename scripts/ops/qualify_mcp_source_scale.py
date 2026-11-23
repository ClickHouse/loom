"""Hosted-only synthetic source capacity contract; emit bounded numeric evidence."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any

from scripts.ops.qualify_mcp_sources import bucket, preflight, quiet

# Frozen before the first hosted run. Changing these requires a capacity decision.
BOUNDS = {
    "registered_min": 10000,
    "eligible_min": 1000,
    "catalog_p95_seconds": 1.5,
    "catalog_p99_seconds": 3.0,
    "live_p95_seconds": 5.0,
    "live_p99_seconds": 10.0,
    "refresh_all_seconds": 600.0,
    "tenant_first_seconds": 120.0,
    "tenant_first_rounds": 2,
    "source_sockets": 32,
    "queue": 256,
    "rss_bytes": 1073741824,
    "rss_delta_bytes": 536870912,
    "cancel_join_seconds": 5.0,
    "teardown_seconds": 10.0,
}
METRICS = frozenset(
    """
registered registered_distinct eligible eligible_distinct orgs users endpoint_count
healthy_eligible healthy_completed healthy_tenants healthy_tenants_completed
catalog_count catalog_p50 catalog_p95 catalog_p99 live_count live_p50 live_p95 live_p99
refresh_count refresh_p50 refresh_p95 refresh_p99 refresh_all_seconds throughput
healthy_tenant_first_max healthy_tenant_first_round_max queue_highwater inflight_highwater
org_highwater endpoint_highwater process_peak rss_peak_bytes rss_delta_bytes cpu_seconds
process_sockets_peak source_sockets_peak cancel_join_seconds teardown_seconds
owned_tasks_after_teardown source_inflight_after_teardown restart_restored overlap_fenced
store_requests store_read_rows store_read_bytes store_written_rows store_written_bytes
store_memory_peak_bytes document_count chunk_count original_readbacks failed_turns
rate_limited_endpoints denied_endpoints malformed_endpoints stalled_endpoints
store_rss_peak_bytes store_cpu_percent_peak
""".split()
)


def accepted(m: dict[str, float | int]) -> bool:
    return set(m) == METRICS and _workload_passes(m) and _capacity_passes(m)


def _workload_passes(m: dict[str, float | int]) -> bool:
    return all(
        (
            m["registered"] >= BOUNDS["registered_min"],
            m["registered_distinct"] == m["registered"],
            m["eligible"] >= BOUNDS["eligible_min"],
            m["eligible_distinct"] == m["eligible"],
            m["orgs"] >= 2,
            m["users"] >= 2,
            m["endpoint_count"] == 10,
            m["healthy_eligible"]
            == m["healthy_completed"]
            == m["refresh_count"]
            == m["document_count"],
            m["healthy_eligible"] > 0,
            m["chunk_count"] >= m["document_count"],
            m["original_readbacks"] == m["healthy_completed"],
            m["healthy_tenants"] == m["healthy_tenants_completed"],
            m["healthy_tenants"] >= 2,
            m["catalog_count"] >= 500,
        )
    )


def _capacity_passes(m: dict[str, float | int]) -> bool:
    return all(
        (
            m["live_count"] == m["healthy_eligible"],
            m["catalog_p95"] <= BOUNDS["catalog_p95_seconds"],
            m["catalog_p99"] <= BOUNDS["catalog_p99_seconds"],
            m["live_p95"] <= BOUNDS["live_p95_seconds"],
            m["live_p99"] <= BOUNDS["live_p99_seconds"],
            m["refresh_all_seconds"] <= BOUNDS["refresh_all_seconds"],
            m["healthy_tenant_first_max"] <= BOUNDS["tenant_first_seconds"],
            m["healthy_tenant_first_round_max"] <= BOUNDS["tenant_first_rounds"],
            m["queue_highwater"] <= 256,
            m["inflight_highwater"] <= 16,
            m["org_highwater"] <= 4,
            m["endpoint_highwater"] <= 2,
            m["source_sockets_peak"] <= 32,
            m["process_peak"] == 1,
            m["rss_peak_bytes"] <= BOUNDS["rss_bytes"],
            m["rss_delta_bytes"] <= BOUNDS["rss_delta_bytes"],
            m["cancel_join_seconds"] <= 5,
            m["teardown_seconds"] <= 10,
            m["owned_tasks_after_teardown"] == m["source_inflight_after_teardown"] == 0,
            m["restart_restored"] == m["overlap_fenced"] == 1,
            all(
                m[key] > 0
                for key in (
                    "store_requests",
                    "store_read_rows",
                    "store_read_bytes",
                    "store_written_rows",
                    "store_written_bytes",
                    "store_rss_peak_bytes",
                )
            ),
            all(
                m[key] == 2
                for key in (
                    "rate_limited_endpoints",
                    "denied_endpoints",
                    "malformed_endpoints",
                    "stalled_endpoints",
                )
            ),
            m["failed_turns"] > 0,
        )
    )


class Results:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.metrics: dict[str, float | int] = {}
        self.test_passed = False
        self.test_failed = False
        self.collected = 0
        self.cleanup = "pending"
        self.commit = os.environ.get("QUALIFICATION_SHA", "")
        self.authority = os.environ.get("LOOM_STORAGE_AUTHORITY", "unknown")

    def record(self, **values: float | int) -> None:
        assert set(values) <= METRICS
        assert all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values.values())
        self.metrics.update(values)
        self.write()

    def write(self) -> None:
        report = {
            "schema_version": 1,
            "bounds": BOUNDS,
            "commit": self.commit
            if len(self.commit) == 40 and all(c in "0123456789abcdef" for c in self.commit)
            else "",
            "authority": self.authority
            if self.authority in {"clickhouse", "postgres"}
            else "unknown",
            "status": "passed"
            if self.passed()
            else "incomplete"
            if self.cleanup == "pending"
            else "failed",
            "stores_cleanup": self.cleanup,
            "test_passed": self.test_passed,
            "test_failed": self.test_failed,
            "collected": self.collected,
            "metrics": self.metrics,
            "fixture": {
                "text_bytes": 8192,
                "endpoint_count": 10,
                "transport": "asgi",
                "registration": "real_store_seed_from_api_template",
                "embedding": "fake_ngram_1024",
                "extraction": "plain_text_no_extractor",
                "store_work": "clickhouse_query_log_bucket_readbacks",
                "api_worker_processes": 1,
                "asgi_applications": 10,
                "tcp_listeners": 0,
                "connection_rows": 200,
                "registration_api_calls": 200,
                "additional_store_seeded_rows": 9800,
                "unique_remote_id_strings": 1,
                "unique_endpoint_remote_id_pairs": 10,
                "healthy_live_source_ids": 40,
                "catalog_page_rows": 20,
                "scheduler_page_rows": 100,
                "source_turn_seconds": 5,
                "sources_share_endpoint_by_kind_and_fault": True,
            },
            "excluded": [
                "tcp_tls_dns_proxy_capacity",
                "vendor_capacity",
                "deployment_capacity",
                "registration_api_throughput",
                "primary_privacy_qualification",
                "cross_process_replica_handover",
                "postgres_query_work_accounting",
                "process_crash_recovery",
            ],
        }
        encoded = json.dumps(report, separators=(",", ":"), sort_keys=True) + "\n"
        assert len(encoded) <= 16384
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(encoded)
        temporary.replace(self.path)

    def passed(self) -> bool:
        return (
            self.cleanup == "passed"
            and self.collected == 1
            and self.test_passed
            and not self.test_failed
            and accepted(self.metrics)
        )

    def pytest_configure(self, config: Any) -> None:
        config._mcp_scale_result = self

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)
        if self.collected != 1 or {item.name for item in session.items} != {
            "test_source_scale_contract"
        }:
            self.test_failed = True
        self.write()

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.test_failed = True
            self.write()

    def pytest_runtest_logreport(self, report: Any) -> None:
        if report.failed or report.skipped:
            self.test_failed = True
        if report.when == "call" and report.passed:
            self.test_passed = True
        self.write()


def run(result: Results) -> int:
    preflight()
    assert os.environ.get("LOOM_MCP_SOURCE_SCALE") == "github-owned"
    bucket()
    import pytest

    code = int(
        pytest.main(
            [
                "tests/integration/mcp_source_scale_acceptance.py",
                "-q",
                "--tb=no",
                "--show-capture=no",
            ],
            plugins=[result],
        )
    )
    if code:
        result.test_failed = True
    result.write()
    return (
        0
        if not code and result.test_passed and not result.test_failed and accepted(result.metrics)
        else 1
    )


def finalize(result: Results, cleanup: str) -> int:
    raw = result.path.read_bytes()
    assert len(raw) <= 16384
    report = json.loads(raw)
    assert report["bounds"] == BOUNDS and report["commit"] == result.commit
    assert report["authority"] == result.authority and report["schema_version"] == 1
    assert type(report["test_passed"]) is bool and type(report["test_failed"]) is bool
    assert type(report["collected"]) is int and 0 <= report["collected"] <= 10000
    result.test_passed, result.test_failed = report["test_passed"], report["test_failed"]
    result.collected = report["collected"]
    result.record(**report["metrics"])
    result.cleanup = "passed" if cleanup == "0" else "failed"
    result.write()
    return 0 if result.passed() else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--cleanup-status", choices=("0", "1"))
    args = parser.parse_args()
    if args.result.name != "scale-report.json" or not args.result.parent.is_dir():
        parser.error("result must name scale-report.json in an existing directory")
    result = Results(args.result)
    if args.cleanup_status is None:
        result.write()
    try:
        with quiet():
            code = (
                run(result)
                if args.cleanup_status is None
                else finalize(result, args.cleanup_status)
            )
    except Exception:  # noqa: BLE001 — private failures must not reach job output
        result.test_failed = True
        result.cleanup = "failed" if args.cleanup_status is not None else "pending"
        result.write()
        code = 1
    print("MCP source scale: status=" + ("passed" if not code else "failed"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
