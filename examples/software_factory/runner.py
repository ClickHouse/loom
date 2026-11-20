"""Run a frozen cohort using an evaluator-owned, hash-pinned host driver.

The driver is trusted application code, never an agent tool or candidate file.
It must verify real authorization, index, billing and input-bound evidence before
constructing dedicated clients. Importing this module performs no I/O.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path
from types import ModuleType

from .controller import run_active, run_next


def load_driver(path: Path, protocol: dict) -> ModuleType:
    """Execute the same bytes whose digest is frozen in the private protocol."""
    source = path.read_bytes()
    if hashlib.sha256(source).hexdigest() != protocol.get("driver_sha256"):
        raise PermissionError("Host driver differs from the frozen protocol")
    module = ModuleType("factory_host_driver")
    module.__file__ = str(path)
    exec(compile(source, str(path), "exec"), module.__dict__)
    for name in ("verify_eligibility", "prepare", "provider_factory"):
        if not callable(getattr(module, name, None)):
            raise ValueError("Host driver must define " + name)
    return module


def main(argv=None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--protocol", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-order", type=int, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    protocol_path, driver_path = (
        args.protocol.resolve(strict=True),
        args.driver.resolve(strict=True),
    )
    root = args.output.resolve()
    protocol = json.loads(protocol_path.read_bytes())
    driver = load_driver(driver_path, protocol)
    if args.worker_order is not None:
        run_active(
            root,
            protocol,
            args.worker_order,
            root / f"attempt-{args.worker_order:03d}" / "worker",
            verify_eligibility=driver.verify_eligibility,
            prepare=driver.prepare,
            provider_factory=driver.provider_factory,
            context_factory=getattr(driver, "context_factory", None),
        )
        return
    while True:
        result = run_next(
            root,
            protocol,
            verify_eligibility=driver.verify_eligibility,
            worker_command=lambda slot, output: [
                sys.executable,
                "-m",
                "examples.software_factory.runner",
                "--protocol",
                str(protocol_path),
                "--driver",
                str(driver_path),
                "--output",
                str(root),
                "--worker-order",
                str(slot["order"]),
            ],
            cwd=Path(__file__).resolve().parents[2],
            # The worker is trusted host code. Candidate tests receive the
            # independent grader's isolated environment, never this mapping.
            env=dict(os.environ),
        )
        if result is None:
            return
        print(json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
