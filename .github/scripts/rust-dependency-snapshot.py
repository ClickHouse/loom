"""Convert the resolved SDK crate graph to a GitHub dependency snapshot."""

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

MANIFEST = "sdk/rust/Cargo.toml"
CRATES_IO = "registry+https://github.com/rust-lang/crates.io-index"


def runtime_dependencies(root, nodes):
    pending = [root]
    reached = {root}
    while pending:
        for dep in nodes[pending.pop()]:
            if dep["pkg"] not in reached and any(
                kind["kind"] != "dev" for kind in dep["dep_kinds"]
            ):
                reached.add(dep["pkg"])
                pending.append(dep["pkg"])
    return reached


def resolved_dependencies(metadata):
    root = metadata["resolve"]["root"]
    nodes = {node["id"]: node["deps"] for node in metadata["resolve"]["nodes"]}
    runtime = runtime_dependencies(root, nodes)
    direct = {dep["pkg"] for dep in nodes[root]}
    purls = {}
    for package in metadata["packages"]:
        package_id = package["id"]
        if package_id == root:
            continue
        if package["source"] != CRATES_IO:
            raise ValueError(f"Unexpected package source for {package['name']}")
        purls[package_id] = f"pkg:cargo/{package['name']}@{package['version']}"

    if purls.keys() != nodes.keys() - {root} or not purls.keys() - direct:
        raise ValueError("The resolved SDK graph has missing nodes or no transitive dependencies")
    return {
        purl: {
            "package_url": purl,
            "relationship": "direct" if package_id in direct else "indirect",
            "scope": "runtime" if package_id in runtime else "development",
            "dependencies": sorted({purls[dep["pkg"]] for dep in nodes[package_id]}),
        }
        for package_id, purl in purls.items()
    }


def main():
    metadata_path, snapshot_path = map(Path, sys.argv[1:])
    resolved = resolved_dependencies(json.loads(metadata_path.read_text()))
    snapshot = {
        "version": 0,
        "sha": os.environ["GITHUB_SHA"],
        "ref": os.environ["GITHUB_REF"],
        "job": {"id": os.environ["GITHUB_RUN_ID"], "correlator": "loom-rust-sdk"},
        "detector": {
            "name": "cargo metadata",
            "version": os.environ["CARGO_TOOLCHAIN"],
            "url": "https://doc.rust-lang.org/cargo/commands/cargo-metadata.html",
        },
        "scanned": datetime.now(UTC).isoformat(timespec="seconds"),
        "manifests": {
            MANIFEST: {
                "name": MANIFEST,
                "file": {"source_location": MANIFEST},
                "resolved": resolved,
            },
        },
    }
    snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n")
    indirect = sum(dep["relationship"] == "indirect" for dep in resolved.values())
    print(f"Cargo resolved {len(resolved)} packages, including {indirect} transitive packages.")


if __name__ == "__main__":
    main()
