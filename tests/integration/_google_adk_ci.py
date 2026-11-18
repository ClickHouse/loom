"""Run both native ADK live suites without adding client extras to the server env."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from server.auth import keyring
from server.auth.jwt import mint_token
from tests.integration._resources import ch_client_from_env, purge, unique_org


def _python(directory: Path, client_path: str) -> None:
    report = directory / "python.xml"
    env = dict(os.environ, PYTHONPATH=client_path, LOOM_TEST_SHARD="")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/integration/test_sdk_google_adk_memory.py",
            "-v",
            f"--junitxml={report}",
        ],
        env=env,
        check=True,
    )
    cases = ET.parse(report).findall(".//testcase")
    assert len(cases) == 8, f"expected eight native Python cases, got {len(cases)}"
    assert all(
        case.find(outcome) is None for case in cases for outcome in ("failure", "error", "skipped")
    ), "native Python cases must pass without skips"


def _typescript(directory: Path) -> None:
    report = directory / "typescript.json"
    org = unique_org("google-adk-ts")
    keyring.sign_with_root_only()
    token = mint_token(
        subject="google-adk-test",
        org=org,
        namespaces=["*"],
        roles=["writer"],
        ttl_seconds=600,
    )
    try:
        subprocess.run(
            [
                "npm",
                "--prefix",
                "sdk/typescript",
                "run",
                "test:integ",
                "--",
                "test/integ/google-adk.test.ts",
                "--reporter=json",
                f"--outputFile={report}",
            ],
            env=dict(os.environ, LOOM_API_KEY=token, LOOM_ORG=org),
            check=True,
        )
        result = json.loads(report.read_text())
        assert result["success"] is True, "native TypeScript suite must succeed"
        assert [
            result[field]
            for field in (
                "numTotalTests",
                "numPassedTests",
                "numFailedTests",
                "numPendingTests",
                "numTodoTests",
            )
        ] == [1, 1, 0, 0, 0], "expected one passing native TypeScript case without skips"
    finally:
        failure = sys.exception()
        try:
            _purge_typescript(org)
        except Exception as cleanup_error:  # Cleanup must retain the original test failure.
            if failure is not None:
                raise BaseExceptionGroup(
                    "native TypeScript test and cleanup failed", [failure, cleanup_error]
                ) from None
            raise


def _purge_typescript(org: str) -> None:
    client = ch_client_from_env()
    try:
        left = purge(client, os.environ.get("LOOM_CH_DATABASE", "loom"), frozenset({org}))
    finally:
        client.close()
    assert not left, f"native TypeScript rows survived the purge: {left}"


if __name__ == "__main__":
    for target in ("LOOM_BASE_URL", "LOOM_CH_HOST", "LOOM_JWT_SECRET"):
        assert os.environ.get(target), f"{target} must name the CI-owned integration target"
    with tempfile.TemporaryDirectory(prefix="loom-google-adk-") as temporary:
        directory = Path(temporary)
        _python(directory, sys.argv[1])
        _typescript(directory)
