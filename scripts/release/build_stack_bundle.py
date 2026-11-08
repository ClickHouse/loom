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
# The bundle keeps the repository's layout, so compose.yaml's paths hold in both.
CONFIG = "deploy/clickhouse-config"
FILES = {
    "LICENSE": "LICENSE",
    "NOTICE": "NOTICE",
    "THIRD_PARTY_NOTICES": "THIRD_PARTY_NOTICES",
    "compose.yaml": "compose.yaml",
    "deploy/standalone/env.example": "env.example",
    f"{CONFIG}/lemmatizers.xml": f"{CONFIG}/lemmatizers.xml",
    f"{CONFIG}/nlp_user_settings.xml": f"{CONFIG}/nlp_user_settings.xml",
    f"{CONFIG}/keeper.xml": f"{CONFIG}/keeper.xml",
    f"{CONFIG}/named_collections.xml.template": f"{CONFIG}/named_collections.xml",
    "deploy/lemmagen3/en.bin": "deploy/lemmagen3/en.bin",
    "deploy/lemmagen3/LICENSE": "deploy/lemmagen3/LICENSE",
    "deploy/lemmagen3/README.md": "deploy/lemmagen3/README.md",
}


def _image_default(image: str) -> str:
    """How compose.yaml names its ClickHouse image: `image` unless overridden."""
    return f"${{LOOM_CLICKHOUSE_IMAGE:-{image}}}"


LATEST_CLICKHOUSE = _image_default("clickhouse/clickhouse-server:latest")
# One exact release, as dev/clickhouse_release.sh prints it.
EXACT_CLICKHOUSE = re.compile(r"clickhouse/clickhouse-server:\d+(?:\.\d+){3}")
# A Loom version as the release URLs name it, without the v.
VERSION_PATTERN = re.compile(r"\d+\.\d+\.\d+(?:-rc\.\d+)?")
_PINNED = re.compile(
    re.escape("${LOOM_CLICKHOUSE_IMAGE:-") + f"({EXACT_CLICKHOUSE.pattern})" + re.escape("}")
)
# A bundle built before LOOM_CLICKHOUSE_IMAGE names its release bare.
_BARE = re.compile(rf"^\s*image: ({EXACT_CLICKHOUSE.pattern})\s*$", re.MULTILINE)


def read_member(archive: Path, name: str) -> str:
    """One file of a bundle as text; a ValueError when it cannot be read."""
    try:
        with tarfile.open(archive) as bundle:
            member = bundle.extractfile(name)
            if member is None:  # a directory or another entry that is not a file
                raise ValueError(f"{archive} has no {name}")
            return member.read().decode()
    except KeyError:
        raise ValueError(f"{archive} has no {name}") from None
    except (OSError, EOFError, tarfile.TarError) as e:  # missing, cut short or not a tar
        reason = getattr(e, "strerror", None) or e
        raise ValueError(f"{archive} is not a readable release bundle: {reason}") from e


def pinned_image(archive: Path) -> str:
    """The ClickHouse image a built bundle names."""
    compose = read_member(archive, "loom-stack/compose.yaml")
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


def stamped(version: str) -> str:
    """install.sh's line naming the release it installs by default."""
    return f"  RELEASE_VERSION={version}\n"


# install.sh's default, which a release's copy replaces with its own version.
UNSTAMPED = stamped("latest")


def stamp_installer(version: str) -> str:
    """The release's install.sh: the repository's, installing `version` by default,
    so the copy `releases/latest` serves installs the release it came from."""
    text = (ROOT / "install.sh").read_text(encoding="utf-8")
    if text.count(UNSTAMPED) != 1:
        raise ValueError(f"install.sh no longer sets {UNSTAMPED.strip()} once")
    return text.replace(UNSTAMPED, stamped(version))


def build(version: str, output_dir: Path, clickhouse_image: str) -> tuple[Path, Path]:
    if not VERSION_PATTERN.fullmatch(version):
        raise ValueError(f"not X.Y.Z or X.Y.Z-rc.N: {version!r}")
    if not EXACT_CLICKHOUSE.fullmatch(clickhouse_image):
        raise ValueError(f"not one ClickHouse release: {clickhouse_image!r}")
    installer_text = stamp_installer(version)  # refuses before anything is written
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
    installer = output_dir / "install.sh"
    installer.write_text(installer_text, encoding="utf-8")
    installer.chmod(0o755)
    return archive, checksum


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    bundle = commands.add_parser(
        "build", help="build the bundle, its checksum and the release's install.sh"
    )
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
    try:
        if args.command == "pinned-in":
            print(pinned_image(args.archive))
        elif args.command == "not-older":
            refuse_older(args.image, args.archives)
        else:
            build(args.version, args.output_dir, args.clickhouse_image)
    except ValueError as e:
        raise SystemExit(f"::error::{e}") from None


if __name__ == "__main__":
    main()
