"""Run the owned hosted MCP cases and emit only bounded status/count evidence."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

CASE_NAMES = (
    "writes_refresh",
    "owner_privacy",
    "source_identity",
    "team_copy_grants",
    "copy_consent",
    "account_revocation",
    "owner_grant_revocation",
    "profile_revocation",
    "pause",
    "read_failure",
    "scope_revision",
    "snapshot",
    "schema_drift",
    "partial_listing",
    "cancellation",
    "live",
)
EXPECTED = {f"test_{name}[{kind}]" for name in CASE_NAMES for kind in ("resources", "tools")}


@contextlib.contextmanager
def quiet() -> Iterator[None]:
    """Native libraries, test hooks and exception reprs must not reach job output."""
    sys.stdout.flush()
    sys.stderr.flush()
    stdout, stderr = os.dup(1), os.dup(2)
    try:
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), 1)
            os.dup2(sink.fileno(), 2)
            yield
    finally:
        sys.stdout.flush()
        sys.stderr.flush()
        os.dup2(stdout, 1)
        os.dup2(stderr, 2)
        os.close(stdout)
        os.close(stderr)


class Results:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.cases = {name: "unrun" for name in sorted(EXPECTED)}
        self.collected = 0
        self.errors = 0
        self.exit_code = 1
        self.stage = "preflight"
        commit = os.environ.get("QUALIFICATION_SHA", "")
        self.commit = commit if re.fullmatch(r"[0-9a-f]{40}", commit) else ""
        authority = os.environ.get("LOOM_STORAGE_AUTHORITY", "")
        self.authority = authority if authority in {"clickhouse", "postgres"} else "unknown"
        self.cleanup = "pending"

    def write(self) -> None:
        counts = {
            status: list(self.cases.values()).count(status)
            for status in ("passed", "failed", "skipped", "unrun")
        }
        passed = (
            self.cleanup == "passed"
            and self.exit_code == 0
            and self.errors == 0
            and self.collected == len(EXPECTED)
            and counts["passed"] == len(EXPECTED)
        )
        report = {
            "schema_version": 1,
            "commit": self.commit,
            "authority": self.authority,
            "status": "passed"
            if passed
            else "incomplete"
            if self.cleanup == "pending"
            else "failed",
            "stage": self.stage,
            "stores_cleanup": self.cleanup,
            "test_exit_code": min(max(self.exit_code, 0), 5),
            "expected": len(EXPECTED),
            "collected": self.collected,
            "errors": self.errors,
            "counts": counts,
            "cases": self.cases,
            "seams": [
                "fixture_asgi_https_routing",
                "operator_catalog_input",
                "local_ngram_embedding",
                "text_requires_no_extractor",
            ],
            "excluded": [
                "vendors",
                "tls_dns_proxy",
                "oauth",
                "stdio",
                "legacy_sse",
                "binary",
                "durable_derived_memory",
                "document_admin_export",
                "scale",
                "performance",
            ],
            "dependencies": ["sdk_protected_request_admission"],
        }
        data = json.dumps(report, sort_keys=True, separators=(",", ":")) + "\n"
        assert len(data) <= 8192
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(data, encoding="utf-8")
        temporary.replace(self.path)

    def pytest_collection_finish(self, session: Any) -> None:
        self.collected = len(session.items)
        names = {item.name for item in session.items}
        if names != EXPECTED or self.collected != len(EXPECTED):
            self.errors += 1
        self.write()

    def pytest_collectreport(self, report: Any) -> None:
        if report.failed:
            self.errors += 1
            self.write()

    def pytest_runtest_logreport(self, report: Any) -> None:
        name = report.nodeid.rsplit("::", 1)[-1]
        if name not in self.cases:
            self.errors += 1
        elif report.failed:
            self.cases[name] = "failed"
        elif report.skipped and self.cases[name] != "failed":
            self.cases[name] = "skipped"
        elif report.when == "call" and report.passed and self.cases[name] == "unrun":
            self.cases[name] = "passed"
        self.write()


def preflight() -> None:
    required = {
        "GITHUB_ACTIONS": "true",
        "RUNNER_ENVIRONMENT": "github-hosted",
        "LOOM_MCP_SOURCE_QUALIFICATION": "github-owned",
        "LOOM_ENV_FILE": "",
        "LOOM_CH_HOST": "127.0.0.1",
        "LOOM_AUTH_REQUIRED": "true",
        "LOOM_BACKGROUND_WORKERS": "off",
        "LOOM_LLM_PROVIDER": "fake",
        "LOOM_EMBEDDING_MODEL": "fake-ngram-1024",
        "LOOM_DOCUMENT_OBJECT_STORE": "s3",
        "LOOM_DOCUMENT_S3_ENDPOINT_URL": "http://127.0.0.1:9000",
        "AWS_EC2_METADATA_DISABLED": "true",
    }
    assert all(os.environ.get(key) == value for key, value in required.items())
    assert os.environ["LOOM_STORAGE_AUTHORITY"] in {"clickhouse", "postgres"}
    assert os.environ.get("LOOM_PG_DSN", "") == (
        "postgres://loom:loomdev@127.0.0.1:5432/loom"
        if os.environ["LOOM_STORAGE_AUTHORITY"] == "postgres"
        else ""
    )
    assert re.fullmatch(r"[0-9a-f]{40}", os.environ["QUALIFICATION_SHA"])
    assert not os.environ.get("LOOM_LLM_FALLBACK_MODEL")
    assert all(
        not os.environ.get(key)
        for key in (
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GOOGLE_API_KEY",
            "GEMINI_API_KEY",
            "AWS_SESSION_TOKEN",
            "AWS_PROFILE",
        )
    )
    assert all(
        len(os.environ.get(key, "")) >= 32
        for key in (
            "LOOM_JWT_SECRET",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
        )
    )
    assert re.fullmatch(r"mcp-qualification-[0-9a-f]{24}", os.environ["LOOM_DOCUMENT_S3_BUCKET"])


def bucket() -> None:
    import boto3
    from botocore.config import Config

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["LOOM_DOCUMENT_S3_ENDPOINT_URL"],
        region_name="us-east-1",
        config=Config(retries={"max_attempts": 0}),
    )
    try:
        client.create_bucket(Bucket=os.environ["LOOM_DOCUMENT_S3_BUCKET"])
    finally:
        client.close()


def run(result: Results) -> int:
    preflight()
    result.stage = "bucket"
    result.write()
    bucket()
    result.stage = "tests"
    result.write()
    import pytest

    code = int(
        pytest.main(
            [
                "tests/integration/test_mcp_source_qualification.py",
                "-q",
                "--tb=no",
                "--show-capture=no",
            ],
            plugins=[result],
        )
    )
    result.exit_code = code
    result.stage = "finished"
    result.write()
    return (
        0
        if code == 0
        and result.errors == 0
        and all(state == "passed" for state in result.cases.values())
        and result.collected == len(EXPECTED)
        else 1
    )


def finalize(result: Results, cleanup: str) -> int:
    # Read only our fixed, bounded report; never forward arbitrary JSON to an artifact.
    raw = result.path.read_bytes()
    assert len(raw) <= 8192
    report = json.loads(raw)
    assert report["schema_version"] == 1 and set(report["cases"]) == EXPECTED
    assert all(
        value in {"unrun", "passed", "failed", "skipped"} for value in report["cases"].values()
    )
    assert report["commit"] == result.commit and report["authority"] == result.authority
    for field in ("collected", "errors", "test_exit_code"):
        assert type(report[field]) is int and 0 <= report[field] <= 10000
    result.cases = report["cases"]
    result.collected, result.errors = report["collected"], report["errors"]
    result.exit_code = report["test_exit_code"]
    result.cleanup = "passed" if cleanup == "0" else "failed"
    result.stage = "finished"
    result.write()
    return (
        0
        if cleanup == "0"
        and result.exit_code == 0
        and result.errors == 0
        and all(state == "passed" for state in result.cases.values())
        and result.collected == len(EXPECTED)
        else 1
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--cleanup-status", choices=("0", "1"))
    args = parser.parse_args()
    if args.result.name != "qualification-report.json" or not args.result.parent.is_dir():
        parser.error("result must name qualification-report.json in an existing directory")
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
    except Exception as e:  # noqa: BLE001 — exception text can contain private fixture payloads
        del e
        result.errors += 1
        if args.cleanup_status is not None:
            result.cleanup = "failed"
            result.stage = "finished"
        result.write()
        code = 1
    passed = list(result.cases.values()).count("passed")
    status = (
        "passed"
        if code == 0 and result.cleanup == "passed"
        else ("tests_passed" if code == 0 else "failed")
    )
    print(
        f"MCP source qualification: status={status} "
        f"passed={passed} expected={len(EXPECTED)} errors={result.errors}"
    )
    if args.cleanup_status is not None and os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
            summary.write(
                f"MCP source functional qualification ({result.authority}): {status}; "
                f"passed {passed}/{len(EXPECTED)}, errors {result.errors}.\n"
            )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
