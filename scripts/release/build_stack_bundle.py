#!/usr/bin/env python3
"""Build the source-free Docker Compose bundle attached to each release."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
FILES = {
    "LICENSE": "LICENSE",
    "NOTICE": "NOTICE",
    "THIRD_PARTY_NOTICES": "THIRD_PARTY_NOTICES",
    "deploy/standalone/compose.yaml": "compose.yaml",
    "deploy/standalone/env.example": "env.example",
    "deploy/clickhouse-config/lemmatizers.xml": "clickhouse-config/lemmatizers.xml",
    "deploy/clickhouse-config/nlp_user_settings.xml": "clickhouse-config/nlp_user_settings.xml",
    "deploy/clickhouse-config/merge_tree_settings.xml": "clickhouse-config/merge_tree_settings.xml",
    "deploy/clickhouse-config/keeper.xml": "clickhouse-config/keeper.xml",
    "deploy/clickhouse-config/named_collections.xml.template": (
        "clickhouse-config/named_collections.xml"
    ),
    "deploy/lemmagen3/en.bin": "lemmagen3/en.bin",
    "deploy/lemmagen3/LICENSE": "lemmagen3/LICENSE",
    "deploy/lemmagen3/README.md": "lemmagen3/README.md",
}


def build(version: str, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / "loom-stack.tar.gz"
    with tempfile.TemporaryDirectory(prefix="loom-stack-") as raw:
        stage = Path(raw) / "loom-stack"
        for source, destination in FILES.items():
            target = stage / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, target)
        (stage / "VERSION").write_text(version + "\n", encoding="utf-8")
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            bundle.add(stage, arcname="loom-stack")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = output_dir / "loom-stack.tar.gz.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", required=True)
    parser.add_argument("--output-dir", type=Path, default=Path("release/stack"))
    args = parser.parse_args()
    build(args.version, args.output_dir)


if __name__ == "__main__":
    main()
