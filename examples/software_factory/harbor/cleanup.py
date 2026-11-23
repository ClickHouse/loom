"""Remove only exact-name, label-verified containers recorded by this example."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

# Daemon inspection and removal share one bound; candidate execution has its own limit.
DOCKER_CONTROL_TIMEOUT_S = 60


def remove_owned(receipt: Path, *, docker_binary: str = "docker") -> None:
    if receipt.is_symlink():
        raise PermissionError("Container ownership receipt is a symlink")
    data = json.loads(receipt.read_text())
    owner = data["owner"]
    if (
        data.get("kind") != "factory-container-ownership-v1"
        or not re.fullmatch(r"[0-9a-f]{32}", owner)
        or data["name"] != "loom-factory-" + owner
    ):
        raise PermissionError("Invalid container ownership receipt")
    config = receipt.parent / "docker-config"
    config.mkdir(exist_ok=True, mode=0o700)
    env = {"PATH": os.defpath, "DOCKER_CONFIG": str(config), "HOME": str(config)}
    found = subprocess.run(
        [
            docker_binary,
            "inspect",
            "--format",
            '{{index .Config.Labels "loom.factory.run"}}',
            data["name"],
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=DOCKER_CONTROL_TIMEOUT_S,
        check=False,
    )
    if found.returncode:
        if "no such" not in found.stderr.lower():
            raise RuntimeError("Could not verify container ownership for cleanup")
    else:
        if found.stdout.strip() != owner:
            raise PermissionError("Container ownership changed; cleanup refused")
        subprocess.run(
            [docker_binary, "rm", "--force", data["name"]],
            env=env,
            capture_output=True,
            timeout=DOCKER_CONTROL_TIMEOUT_S,
            check=True,
        )
    data["cleaned"] = True
    receipt.write_text(json.dumps(data, indent=2) + "\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    for receipt in args.root.resolve(strict=True).rglob("factory-container.json"):
        if not json.loads(receipt.read_text()).get("cleaned"):
            remove_owned(receipt)


if __name__ == "__main__":
    main()
