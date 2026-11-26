"""Check the Rust SDK's independent version and distributable source."""

from __future__ import annotations

import argparse
import hashlib
import json
import posixpath
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path, PurePosixPath
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
SDK = Path("sdk/rust")
SEMVER = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\."
    r"(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?",
    flags=re.ASCII,
)
REQUIRED = {
    "src/lib.rs",
    "src/wire_models.rs",
    "src/operations.rs",
    "src/blocking.rs",
    "src/blocking_operations.rs",
    "src/capabilities.rs",
    "README.md",
    "examples/README.md",
    "examples/blocking.rs",
    "tests/generated_contracts.rs",
    "Cargo.lock",
    ".cargo_vcs_info.json",
}


def check_version(root: Path = ROOT, expected: str | None = None) -> str:
    manifest = tomllib.loads((root / SDK / "Cargo.toml").read_text())
    package = manifest.get("package", {})
    version = package.get("version")
    if package.get("name") != "clickhouse-loom":
        raise ValueError("package.name must be clickhouse-loom")
    if not isinstance(version, str) or not SEMVER.fullmatch(version):
        raise ValueError("package.version must be a SemVer string")
    if expected is not None and version != expected:
        raise ValueError(f"crate version {version} differs from expected {expected}")
    return version


def _archive_files(archive: Path, prefix: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    seen: set[str] = set()
    total = 0
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar:
            path = PurePosixPath(member.name)
            if (
                path.is_absolute()
                or ".." in path.parts
                or not path.parts
                or path.parts[0] != prefix
                or str(path) != member.name.rstrip("/")
                or str(path) in seen
            ):
                raise ValueError(f"invalid or duplicate archive path: {member.name}")
            seen.add(str(path))
            if len(seen) > 10_000:
                raise ValueError("archive has too many entries")
            if member.isdir():
                continue
            if not member.isfile() or len(path.parts) < 2:
                raise ValueError(f"archive entry is not a regular file: {member.name}")
            total += member.size
            if member.size < 0 or total > 128 * 1024 * 1024:
                raise ValueError("archive exceeds source size limit")
            stream = tar.extractfile(member)
            if stream is None:
                raise ValueError(f"unreadable archive entry: {member.name}")
            files[str(PurePosixPath(*path.parts[1:]))] = stream.read()
    return files


def _dependencies(manifest: dict[str, Any], key: str) -> dict[str, Any]:
    return {
        name: {"version": spec} if isinstance(spec, str) else spec
        for name, spec in manifest.get(key, {}).items()
    }


def _check_manifest(files: dict[str, bytes], source: bytes) -> None:
    if files.get("Cargo.toml.orig") != source:
        raise ValueError("Cargo.toml.orig differs from source")
    original = tomllib.loads(source.decode())
    packed = tomllib.loads(files.get("Cargo.toml", b"").decode())
    for key, value in original["package"].items():
        if packed.get("package", {}).get(key) != value:
            raise ValueError(f"packaged package.{key} differs from source")
    if packed.get("features", {}) != original.get("features", {}):
        raise ValueError("packaged features differ from source")
    for key in ("dependencies", "dev-dependencies", "build-dependencies", "target"):
        if _dependencies(packed, key) != _dependencies(original, key):
            raise ValueError(f"packaged {key} differ from source")
    if packed.get("lib", {}).get("path") != "src/lib.rs":
        raise ValueError("packaged library must use src/lib.rs")
    library = {
        "name": original["package"]["name"].replace("-", "_"),
        "path": "src/lib.rs",
        **original.get("lib", {}),
    }
    library["path"] = posixpath.normpath(library["path"])
    if packed.get("lib", {}) != library:
        raise ValueError("packaged library options differ from source")
    blocking = [item for item in packed.get("example", []) if item.get("name") == "blocking"]
    if len(blocking) != 1 or blocking[0].get("required-features") != ["blocking"]:
        raise ValueError("blocking example must require the blocking feature")
    if blocking[0].get("path") != "examples/blocking.rs":
        raise ValueError("blocking example path differs from source")


def _source_files(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", str(SDK)],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return sorted({Path(name.decode()) for name in result.stdout.split(b"\0") if name})


def verify_package(
    archive: Path,
    root: Path = ROOT,
    expected_commit: str | None = None,
    expected_sha256: str | None = None,
) -> str:
    version = check_version(root)
    with archive.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    if expected_sha256 is not None and checksum != expected_sha256:
        raise ValueError("archive SHA-256 differs from expected checksum")
    files = _archive_files(archive, f"clickhouse-loom-{version}")
    missing = REQUIRED - files.keys()
    if missing:
        raise ValueError(f"archive is missing required files: {', '.join(sorted(missing))}")
    _check_manifest(files, (root / SDK / "Cargo.toml").read_bytes())
    if files.get("LICENSE") != (root / "LICENSE").read_bytes():
        raise ValueError("packaged LICENSE differs from root LICENSE")
    source_paths = _source_files(root)
    expected_files = {path.relative_to(SDK).as_posix() for path in source_paths}
    expected_files |= {"Cargo.toml.orig", "Cargo.lock", ".cargo_vcs_info.json", "LICENSE"}
    extra = files.keys() - expected_files
    if extra:
        raise ValueError(f"archive contains unexpected files: {', '.join(sorted(extra))}")
    for path in source_paths:
        relative = path.relative_to(SDK).as_posix()
        if relative == "Cargo.toml":
            continue
        if files.get(relative) != (root / path).read_bytes():
            raise ValueError(f"packaged {relative} is missing or differs from source")
    vcs = json.loads(files[".cargo_vcs_info.json"])
    if not isinstance(vcs, dict) or vcs.get("path_in_vcs") != SDK.as_posix():
        raise ValueError("package VCS path must be sdk/rust")
    git = vcs.get("git")
    commit = git.get("sha1") if isinstance(git, dict) else None
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("package VCS commit must be a full Git SHA")
    if expected_commit is not None and commit != expected_commit:
        raise ValueError("package VCS commit differs from expected commit")
    return checksum


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    version = commands.add_parser("check-version")
    version.add_argument("--expected-version")
    package = commands.add_parser("verify-package")
    package.add_argument("archive", type=Path)
    package.add_argument("--expected-commit")
    package.add_argument("--expected-sha256")
    args = parser.parse_args()
    try:
        if args.command == "check-version":
            print(check_version(expected=args.expected_version))
        else:
            print(
                verify_package(
                    args.archive,
                    expected_commit=args.expected_commit,
                    expected_sha256=args.expected_sha256,
                )
            )
    except (ValueError, OSError, tarfile.TarError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"Rust SDK check failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
