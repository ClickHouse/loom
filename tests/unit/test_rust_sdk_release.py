"""Reject Rust release archives that differ from the reviewed source."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.release import rust_sdk
from tests.unit import git_repo

COMMIT = "a" * 40
SOURCE = b"""[package]
name = "clickhouse-loom"
version = "0.1.1"
edition = "2021"
rust-version = "1.84"
license = "Apache-2.0"
[features]
default = []
blocking = ["tokio/rt"]
[[example]]
name = "blocking"
required-features = ["blocking"]
[dependencies]
log = "0.4"
tokio = { version = "1", features = ["sync", "time"] }
"""
NORMALIZED = (
    SOURCE.replace(
        b'log = "0.4"',
        b'log = { version = "0.4" }',
    ).replace(
        b'required-features = ["blocking"]',
        b'path = "examples/blocking.rs"\nrequired-features = ["blocking"]',
    )
    + b'\n[lib]\nname = "clickhouse_loom"\npath = "src/lib.rs"\n'
)


@pytest.fixture
def source(tmp_path: Path) -> tuple[Path, dict[str, bytes]]:
    root = tmp_path / "repo"
    root.mkdir()
    git_repo.git(root, "init", "-q")
    files = dict.fromkeys(rust_sdk.REQUIRED - {".cargo_vcs_info.json"}, b"source\n")
    files.update(
        {
            "Cargo.toml": NORMALIZED,
            "Cargo.toml.orig": SOURCE,
            "LICENSE": b"Apache license\n",
            ".cargo_vcs_info.json": json.dumps(
                {
                    "git": {"sha1": COMMIT},
                    "path_in_vcs": "sdk/rust",
                }
            ).encode(),
            "src/client.rs": b"client implementation\n",
        }
    )
    for path, data in files.items():
        if path in {"Cargo.toml.orig", ".cargo_vcs_info.json", "Cargo.lock"}:
            continue
        target = root / rust_sdk.SDK / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(SOURCE if path == "Cargo.toml" else data)
    (root / "LICENSE").write_bytes(files["LICENSE"])
    git_repo.git(root, "add", ".")
    return root, files


def archive(tmp_path: Path, files: dict[str, bytes]) -> Path:
    path = tmp_path / "release.crate"
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            entry = tarfile.TarInfo(f"clickhouse-loom-0.1.1/{name}")
            entry.size = len(data)
            tar.addfile(entry, io.BytesIO(data))
    return path


def test_normalized_package_matches_source(source, tmp_path: Path) -> None:
    root, files = source
    path = archive(tmp_path, files)
    checksum = hashlib.sha256(path.read_bytes()).hexdigest()
    assert rust_sdk.verify_package(path, root, COMMIT, checksum) == checksum


@pytest.mark.parametrize("version", ["01.1.1", "1.2", "1.2.3-01", "1.2.3-", "latest", "1２.1.1"])
def test_invalid_version_is_rejected(source, version: str) -> None:
    root, _ = source
    manifest = root / rust_sdk.SDK / "Cargo.toml"
    manifest.write_bytes(SOURCE.replace(b"0.1.1", version.encode()))
    with pytest.raises(ValueError, match="SemVer"):
        rust_sdk.check_version(root)


@pytest.mark.parametrize("version", ["0.1.1", "1.0.0-rc.1", "1.0.0+build.01"])
def test_independent_semver(source, version: str) -> None:
    root, _ = source
    (root / rust_sdk.SDK / "Cargo.toml").write_bytes(SOURCE.replace(b"0.1.1", version.encode()))
    assert rust_sdk.check_version(root, version) == version
    with pytest.raises(ValueError, match="differs from expected"):
        rust_sdk.check_version(root, "2.0.0")


@pytest.mark.parametrize(
    "name",
    [
        "src/operations.rs",
        "src/wire_models.rs",
        "src/blocking_operations.rs",
        "examples/README.md",
        "tests/generated_contracts.rs",
    ],
)
def test_missing_generated_source_is_rejected(source, tmp_path: Path, name: str) -> None:
    root, files = source
    del files[name]
    with pytest.raises(ValueError, match="missing required files"):
        rust_sdk.verify_package(archive(tmp_path, files), root)


@pytest.mark.parametrize(
    "name", ["src/client.rs", "src/operations.rs", "LICENSE", "Cargo.toml.orig"]
)
def test_source_drift_is_rejected(source, tmp_path: Path, name: str) -> None:
    root, files = source
    files[name] += b"changed\n"
    with pytest.raises(ValueError, match="differs from"):
        rust_sdk.verify_package(archive(tmp_path, files), root)


@pytest.mark.parametrize(
    ("before", "after", "message"),
    [
        (b'version = "0.1.1"', b'version = "0.1.2"', "package.version"),
        (b'version = "0.4"', b'version = "0.5"', "dependencies"),
        (b'blocking = ["tokio/rt"]', b"blocking = []", "features"),
        (b'required-features = ["blocking"]', b"required-features = []", "blocking feature"),
        (b'path = "src/lib.rs"', b'path = "src/other.rs"', "library"),
        (b'name = "clickhouse_loom"', b'name = "other_library"', "library"),
    ],
)
def test_normalized_manifest_drift_is_rejected(source, tmp_path, before, after, message) -> None:
    root, files = source
    files["Cargo.toml"] = files["Cargo.toml"].replace(before, after)
    with pytest.raises(ValueError, match=message):
        rust_sdk.verify_package(archive(tmp_path, files), root)


@pytest.mark.parametrize(
    "option",
    [
        b'crate-type = ["cdylib"]',
        b"proc-macro = true",
        b'required-features = ["blocking"]',
        b'edition = "2018"',
        b"test = false",
        b"doctest = false",
    ],
)
def test_packaged_library_option_drift_is_rejected(source, tmp_path: Path, option: bytes) -> None:
    root, files = source
    files["Cargo.toml"] += option + b"\n"
    with pytest.raises(ValueError, match="library"):
        rust_sdk.verify_package(archive(tmp_path, files), root)


def test_explicit_source_library_options_are_preserved(source, tmp_path: Path) -> None:
    root, files = source
    options = b'crate-type = ["rlib"]\ndoctest = false\n'
    original = SOURCE + b"\n[lib]\n" + options
    (root / rust_sdk.SDK / "Cargo.toml").write_bytes(original)
    files["Cargo.toml.orig"] = original
    files["Cargo.toml"] += options
    assert rust_sdk.verify_package(archive(tmp_path, files), root)


@pytest.mark.parametrize(
    "path", ["./src/lib.rs", "src/./lib.rs", "src//lib.rs", "src/sub/../lib.rs"]
)
def test_normalized_source_library_path_is_accepted(source, tmp_path: Path, path: str) -> None:
    root, files = source
    original = SOURCE + f'\n[lib]\npath = "{path}"\n'.encode()
    (root / rust_sdk.SDK / "Cargo.toml").write_bytes(original)
    files["Cargo.toml.orig"] = original
    assert rust_sdk.verify_package(archive(tmp_path, files), root)


def test_nonignored_source_addition_must_ship(source, tmp_path: Path) -> None:
    root, files = source
    (root / rust_sdk.SDK / "src/added.rs").write_text("additional source")
    with pytest.raises(ValueError, match="src/added.rs"):
        rust_sdk.verify_package(archive(tmp_path, files), root)


def test_unexpected_packaged_source_is_rejected(source, tmp_path: Path) -> None:
    root, files = source
    files["src/extra.rs"] = b"unreviewed source"
    with pytest.raises(ValueError, match="unexpected files"):
        rust_sdk.verify_package(archive(tmp_path, files), root)


@pytest.mark.parametrize(
    ("commit", "checksum", "message"),
    [
        ("b" * 40, None, "VCS commit"),
        (None, "0" * 64, "SHA-256"),
    ],
)
def test_registry_identity_mismatch_is_rejected(
    source, tmp_path, commit, checksum, message
) -> None:
    root, files = source
    with pytest.raises(ValueError, match=message):
        rust_sdk.verify_package(archive(tmp_path, files), root, commit, checksum)


@pytest.mark.parametrize(
    "vcs",
    [
        [],
        {"path_in_vcs": "other"},
        {"path_in_vcs": "sdk/rust", "git": None},
    ],
)
def test_invalid_vcs_is_rejected(source, tmp_path: Path, vcs) -> None:
    root, files = source
    files[".cargo_vcs_info.json"] = json.dumps(vcs).encode()
    with pytest.raises(ValueError, match="VCS"):
        rust_sdk.verify_package(archive(tmp_path, files), root)


@pytest.mark.parametrize("name", ["../escape", "/absolute", "src/../escape", "src//client.rs"])
def test_unsafe_archive_paths_are_rejected(source, tmp_path: Path, name: str) -> None:
    root, files = source
    files[name] = b"bad path"
    with pytest.raises(ValueError, match="archive path"):
        rust_sdk.verify_package(archive(tmp_path, files), root)


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE])
def test_nonregular_archive_entry_is_rejected(source, tmp_path: Path, kind: bytes) -> None:
    root, _ = source
    path = tmp_path / "bad.crate"
    with tarfile.open(path, "w:gz") as tar:
        entry = tarfile.TarInfo("clickhouse-loom-0.1.1/src/link")
        entry.type = kind
        entry.linkname = "lib.rs"
        tar.addfile(entry)
    with pytest.raises(ValueError, match="regular file"):
        rust_sdk.verify_package(path, root)


def test_duplicate_archive_entry_is_rejected(source, tmp_path: Path) -> None:
    root, _ = source
    path = tmp_path / "bad.crate"
    with tarfile.open(path, "w:gz") as tar:
        entry = tarfile.TarInfo("clickhouse-loom-0.1.1/src/lib.rs")
        tar.addfile(entry)
        tar.addfile(entry)
    with pytest.raises(ValueError, match="duplicate archive path"):
        rust_sdk.verify_package(path, root)
