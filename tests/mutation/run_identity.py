"""Stage one unchanged module and report the bounded mutation pilot."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path


def _run_mutations(command: list[str], stage: Path) -> int:
    process = subprocess.Popen(command, cwd=stage, start_new_session=True)
    try:
        return process.wait(timeout=120)
    finally:
        # The private group contains only this pilot and its forked workers.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=10)


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    source_paths = [
        "server/__init__.py",
        "server/core/__init__.py",
        "server/core/embedding_space.py",
        "tests/__init__.py",
        "tests/fuzz/__init__.py",
        "tests/fuzz/embedding_identity_oracle.py",
        "tests/mutation/test_embedding_identity.py",
    ]
    with tempfile.TemporaryDirectory(prefix="loom-identity-mutation-") as directory:
        stage = Path(directory)
        for name in source_paths:
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, target)
            assert (
                hashlib.sha256(target.read_bytes()).digest()
                == hashlib.sha256((root / name).read_bytes()).digest()
            )
        shutil.copyfile(root / "tests/mutation/pyproject.toml", stage / "pyproject.toml")
        command = [
            sys.executable,
            "-m",
            "mutmut",
            "run",
            "*public_identity*",
            "--max-children",
            "2",
        ]
        result = _run_mutations(command, stage)
        if result:
            raise SystemExit(result)
        report = subprocess.run(
            [sys.executable, "-m", "mutmut", "results", "--all", "true"],
            cwd=stage,
            text=True,
            stdout=subprocess.PIPE,
            check=True,
            timeout=20,
        )
        selected = []
        for line in report.stdout.splitlines():
            if "public_identity" not in line:
                continue
            name, status = line.strip().rsplit(": ", 1)
            if status != "not checked":
                selected.append({"name": name, "status": status})
        assert selected and any(row["status"] == "killed" for row in selected), (
            "No tested public-identity mutants"
        )
        output = {"target": "server.core.embedding_space.public_identity", "mutants": selected}
        actionable = sum(row["status"] not in {"killed", "skipped"} for row in selected)
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as stream:
                stream.write(f"review_count={actionable}\n")
        destination = Path(os.environ["IDENTITY_MUTATION_REPORT"])
        destination.write_text(json.dumps(output, indent=2) + "\n")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            counts = {
                status: sum(row["status"] == status for row in selected)
                for status in sorted({row["status"] for row in selected})
            }
            with open(summary, "a") as stream:
                stream.write(
                    "Endpoint-identity mutation pilot: "
                    + json.dumps(counts, sort_keys=True)
                    + ".\n"
                )
                stream.write(
                    "Survivors require review for a missing assertion or an equivalent mutation; this is not a vulnerability or whole-module coverage claim.\n"
                )
        print(json.dumps(output, indent=2))


if __name__ == "__main__":
    main()
