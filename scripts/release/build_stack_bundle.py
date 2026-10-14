#!/usr/bin/env python3
"""Build the source-free Docker Compose bundle attached to each release.

The repository runs the latest ClickHouse release; a bundle names the one
that was latest when it was built, so an installed Loom version keeps its
ClickHouse until the operator moves it."""

from __future__ import annotations

import argparse
import hashlib
import re
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


def _image_default(image: str) -> str:
    """How compose.yaml names its ClickHouse image: `image` unless overridden."""
    return f"${{LOOM_CLICKHOUSE_IMAGE:-{image}}}"


LATEST_CLICKHOUSE = _image_default("clickhouse/clickhouse-server:latest")
# One exact release, as dev/clickhouse_release.sh prints it.
EXACT_CLICKHOUSE = re.compile(r"clickhouse/clickhouse-server:\d+(?:\.\d+){3}")
_PINNED = re.compile(
    re.escape("${LOOM_CLICKHOUSE_IMAGE:-") + f"({EXACT_CLICKHOUSE.pattern})" + re.escape("}")
)
# A bundle built before LOOM_CLICKHOUSE_IMAGE names its release bare.
_BARE = re.compile(rf"^\s*image: ({EXACT_CLICKHOUSE.pattern})\s*$", re.MULTILINE)


def pinned_image(archive: Path) -> str:
    """The ClickHouse image a built bundle names."""
    with tarfile.open(archive) as bundle:
        member = bundle.extractfile("loom-stack/compose.yaml")
        compose = member.read().decode() if member else ""
    found = _PINNED.search(compose) or _BARE.search(compose)
    if not found:
        raise ValueError(f"{archive} names no exact ClickHouse release")
    return found.group(1)


def release_number(image: str) -> tuple[int, ...]:
    """`clickhouse/clickhouse-server:26.9.18.11` as (26, 9, 18, 11)."""
    if not EXACT_CLICKHOUSE.fullmatch(image):
        raise ValueError(f"not one ClickHouse release: {image!r}")
    return tuple(int(part) for part in image.rsplit(":", 1)[1].split("."))


def refuse_older(image: str, published: list[Path]) -> None:
    """Raise when `image` is an older release than any of the `published`
    bundles names: an install upgrading to it would start an older ClickHouse
    on data a newer one wrote, which it may not open."""
    newest = max((pinned_image(p) for p in published), key=release_number)
    if release_number(image) < release_number(newest):
        raise ValueError(f"{image} is older than {newest}, which a published bundle names")


def build(version: str, output_dir: Path, clickhouse_image: str) -> tuple[Path, Path]:
    if not EXACT_CLICKHOUSE.fullmatch(clickhouse_image):
        raise ValueError(f"not one ClickHouse release: {clickhouse_image!r}")
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / "loom-stack.tar.gz"
    with tempfile.TemporaryDirectory(prefix="loom-stack-") as raw:
        stage = Path(raw) / "loom-stack"
        for source, destination in FILES.items():
            target = stage / destination
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / source, target)
        compose = stage / "compose.yaml"
        text = compose.read_text(encoding="utf-8")
        if LATEST_CLICKHOUSE not in text:
            raise ValueError(f"compose.yaml no longer defaults to {LATEST_CLICKHOUSE}")
        pinned = _image_default(clickhouse_image)
        compose.write_text(text.replace(LATEST_CLICKHOUSE, pinned), encoding="utf-8")
        (stage / "VERSION").write_text(version + "\n", encoding="utf-8")
        with tarfile.open(archive, "w:gz", format=tarfile.PAX_FORMAT) as bundle:
            bundle.add(stage, arcname="loom-stack")
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum = output_dir / "loom-stack.tar.gz.sha256"
    checksum.write_text(f"{digest}  {archive.name}\n", encoding="utf-8")
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser("build", help="build the bundle and its checksum")
    bundle.add_argument("--version", required=True)
    bundle.add_argument(
        "--clickhouse-image", required=True, help="e.g. clickhouse/clickhouse-server:26.9.18.11"
    )
    bundle.add_argument("--output-dir", type=Path, default=Path("release/stack"))
    read = commands.add_parser("pinned-in", help="print the ClickHouse image a bundle names")
    read.add_argument("archive", type=Path)
    newer = commands.add_parser(
        "not-older", help="fail when an image is older than one a bundle names"
    )
    newer.add_argument("image")
    newer.add_argument("archives", type=Path, nargs="+")
    args = parser.parse_args()
    if args.command == "pinned-in":
        print(pinned_image(args.archive))
    elif args.command == "not-older":
        try:
            refuse_older(args.image, args.archives)
        except ValueError as e:
            raise SystemExit(f"::error::{e}") from None
    else:
        build(args.version, args.output_dir, args.clickhouse_image)


if __name__ == "__main__":
    main()
