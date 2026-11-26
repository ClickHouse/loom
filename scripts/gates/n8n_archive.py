"""Inspect a packed SDK's native extension without extracting or executing it.

Run from the repository root with an independently identified source manifest:

    python -m scripts.gates.n8n_archive package.tgz \
        --source-manifest sdk/typescript/package.json --source-sha256 SHA256 \
        --version VERSION --integrity sha512-BASE64

The integrity may come from a candidate's npm-pack result or registry metadata.
It identifies the input; it does not authenticate its publisher. PASS covers
archive structure and the supplied manifest contract, not application behavior,
publication provenance, signatures or marketplace acceptance.

Inspection refuses paths over 4096 UTF-8 bytes or 32 components before building
parent paths. Archive limits are 32 MiB compressed, 128 MiB decompressed,
8 MiB per file and 20,000 members.
Only POSIX USTAR regular files and directories with octal sizes and unsigned
checksums are accepted. Extension headers are refused before metadata processing.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import json
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, NoReturn

MAX_COMPRESSED = 32 * 1024 * 1024
MAX_TAR = 128 * 1024 * 1024
MAX_MEMBER = 8 * 1024 * 1024
MAX_MEMBERS = 20_000
MAX_PATH_BYTES = 4096
MAX_PATH_DEPTH = 32
NATIVE = "dist/integrations/n8n/"
REQUIRED = {
    NATIVE + name
    for name in (
        "credentials/LoomApi.credentials.js",
        "nodes/Loom/Loom.node.js",
        "nodes/LoomMemory/LoomMemory.node.js",
        "nodes/LoomMemory/LoomChatHistory.js",
        "nodes/client.js",
        "package.json",
        "credentials/loom.svg",
        "nodes/Loom/loom.svg",
        "nodes/LoomMemory/loom.svg",
        "LICENSE",
        "THIRD_PARTY_LICENSES.txt",
    )
}


class ArchiveError(ValueError):
    """A fixed archive-contract diagnostic, without package content."""


def require(condition: object, reason: str) -> None:
    if not condition:
        raise ArchiveError(reason)


def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def refuse_constant(value: str) -> NoReturn:
    raise ArchiveError("non-JSON numeric constant")


def parse_json(data: bytes) -> dict[str, Any]:
    result = json.loads(
        data.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=refuse_constant
    )
    if not isinstance(result, dict):
        raise ArchiveError("manifest must be an object")
    return result


def bounded_read(path: str | Path, limit: int) -> bytes:
    with Path(path).open("rb") as source:
        data = source.read(limit + 1)
    require(len(data) <= limit, "input exceeds inspection bound")
    return data


def export_paths(value: Any) -> set[str]:
    require(
        isinstance(value, dict) and set(value) == {"import", "require"},
        "missing import/require export conditions",
    )
    paths: set[str] = set()
    for condition, runtime, declaration in (
        ("import", ".mjs", ".d.mts"),
        ("require", ".cjs", ".d.cts"),
    ):
        branch = value[condition]
        require(
            isinstance(branch, dict) and list(branch) == ["types", "default"],
            "types must precede default export",
        )
        require(
            branch
            == {
                "types": "./dist/integrations/n8n" + declaration,
                "default": "./dist/integrations/n8n" + runtime,
            },
            "export format mismatch",
        )
        paths.update(target[2:] for target in branch.values())
    return paths


def member_name(member: tarfile.TarInfo) -> str:
    require(
        len(member.name) <= MAX_PATH_BYTES and len(member.name.encode("utf-8")) <= MAX_PATH_BYTES,
        "archive path exceeds inspection bound",
    )
    name = member.name.rstrip("/") if member.isdir() else member.name
    require(name.count("/") < MAX_PATH_DEPTH, "archive path exceeds inspection bound")
    parts = PurePosixPath(name).parts
    require(
        parts
        and parts[0] == "package"
        and "\\" not in name
        and all(part not in (".", "..", "") for part in name.split("/"))
        and str(PurePosixPath(name)) == name,
        "noncanonical archive path",
    )
    return name


def octal_field(value: bytes) -> int:
    digits = value.rstrip(b"\x00 ")
    require(digits and all(48 <= byte <= 55 for byte in digits), "noncanonical TAR number")
    return int(digits, 8)


def archive_header(header: bytes) -> tarfile.TarInfo:
    require(
        header[156:157] in (tarfile.REGTYPE, tarfile.AREGTYPE, tarfile.DIRTYPE),
        "archive extension headers and special files refused",
    )
    require(header[257:265] == b"ustar\x0000", "POSIX USTAR header required")
    size = octal_field(header[124:136])
    checksum = octal_field(header[148:156])
    require(
        checksum == sum(header[:148]) + 8 * 32 + sum(header[156:]),
        "unsigned TAR checksum required",
    )
    member = tarfile.TarInfo.frombuf(header, encoding="utf-8", errors="strict")
    require(member.size == size, "inconsistent TAR size")
    return member


def archive_files(raw: bytes) -> tuple[dict[str, bytes], int]:
    with gzip.GzipFile(fileobj=io.BytesIO(raw)) as compressed:
        unpacked = compressed.read(MAX_TAR + 1)
    require(len(unpacked) <= MAX_TAR, "decompressed archive exceeds inspection bound")
    files: dict[str, bytes] = {}
    names: set[str] = set()
    regular_names: set[str] = set()
    implied_directories: set[str] = set()
    offset = 0
    while offset + tarfile.BLOCKSIZE <= len(unpacked):
        header = unpacked[offset : offset + tarfile.BLOCKSIZE]
        if not any(header):
            break
        require(len(names) < MAX_MEMBERS, "too many archive members")
        member = archive_header(header)
        name = member_name(member)
        require(name not in names, "duplicate archive path")
        names.add(name)
        parents = {str(parent) for parent in PurePosixPath(name).parents if str(parent) != "."}
        require(not parents.intersection(regular_names), "regular file used as directory")
        require(
            not member.isfile() or name not in implied_directories,
            "directory replaced by regular file",
        )
        implied_directories.update(parents)
        offset += tarfile.BLOCKSIZE
        require(0 <= member.size <= MAX_MEMBER, "archive member exceeds inspection bound")
        if member.isdir():
            require(member.size == 0, "directory member contains data")
        else:
            regular_names.add(name)
            data = unpacked[offset : offset + member.size]
            require(len(data) == member.size, "archive member size mismatch")
            files[name.removeprefix("package/")] = data
        offset += ((member.size + tarfile.BLOCKSIZE - 1) // tarfile.BLOCKSIZE) * tarfile.BLOCKSIZE
    tail = unpacked[offset:]
    require(
        len(tail) >= 1024 and len(unpacked) % tarfile.BLOCKSIZE == 0 and not any(tail),
        "invalid TAR termination or trailing data",
    )
    return files, len(unpacked)


def object_field(manifest: dict[str, Any], field: str) -> dict[str, Any]:
    value = manifest.get(field)
    if not isinstance(value, dict):
        raise ArchiveError(field + " must be an object")
    return value


def check_registration(actual: dict[str, Any]) -> None:
    metadata = object_field(actual, "n8n")
    require(
        type(metadata.get("n8nNodesApiVersion")) is int and metadata["n8nNodesApiVersion"] == 1,
        "missing integer n8n API v1",
    )
    require(
        metadata.get("credentials") == [NATIVE + "credentials/LoomApi.credentials.js"],
        "credential entry mismatch",
    )
    require(
        metadata.get("nodes")
        == [NATIVE + "nodes/Loom/Loom.node.js", NATIVE + "nodes/LoomMemory/LoomMemory.node.js"],
        "node entry mismatch",
    )
    peers = object_field(actual, "peerDependencies")
    require(
        isinstance(peers.get("n8n-workflow"), str) and peers["n8n-workflow"],
        "missing workflow peer",
    )
    optional = object_field(object_field(actual, "peerDependenciesMeta"), "n8n-workflow")
    require(optional.get("optional") is True, "workflow peer must be optional")
    for field in (
        "dependencies",
        "optionalDependencies",
        "bundledDependencies",
        "bundleDependencies",
    ):
        dependencies = actual.get(field) or {}
        require(isinstance(dependencies, dict | list), "invalid dependency collection")
        require("n8n-workflow" not in dependencies, "workflow must not be a runtime dependency")


def check_manifest(
    actual: dict[str, Any], expected: dict[str, Any], version: str, files: dict[str, bytes]
) -> None:
    require(
        version and actual.get("version") == expected.get("version") == version, "version mismatch"
    )
    require(
        actual.get("name") == expected.get("name") == "@clickhouse/loom", "package name mismatch"
    )
    require(actual.get("type") == expected.get("type") == "module", "root must be ESM")
    for field in ("n8n", "peerDependencies", "peerDependenciesMeta"):
        require(actual.get(field) == expected.get(field), field + " differs from supplied source")
    check_registration(actual)
    exports = object_field(actual, "exports").get("./integrations/n8n")
    require(
        exports == object_field(expected, "exports").get("./integrations/n8n"),
        "n8n export differs from supplied source",
    )
    for name in REQUIRED | export_paths(exports):
        require(files.get(name, b"").strip(), "missing or empty required member: " + name)
    require(
        not any(
            name.startswith(NATIVE)
            and name.endswith("/package.json")
            and name != NATIVE + "package.json"
            for name in files
        ),
        "unexpected descendant package boundary",
    )
    boundary = parse_json(files[NATIVE + "package.json"])
    require(boundary.get("type") == "commonjs", "native loader boundary must be CommonJS")


def inspect_archive(
    archive: str | Path,
    source_manifest: str | Path,
    source_sha256: str,
    version: str,
    integrity: str,
) -> dict[str, Any]:
    source = bounded_read(source_manifest, MAX_MEMBER)
    require(hashlib.sha256(source).hexdigest() == source_sha256, "source digest mismatch")
    expected = parse_json(source)
    raw = bounded_read(archive, MAX_COMPRESSED)
    digest = "sha512-" + base64.b64encode(hashlib.sha512(raw).digest()).decode("ascii")
    require(digest == integrity, "archive integrity mismatch")
    files, tar_bytes = archive_files(raw)
    require("package.json" in files, "missing root package manifest")
    check_manifest(parse_json(files["package.json"]), expected, version, files)
    return {
        "archive_inspection": "PASS",
        "version": version,
        "integrity": digest,
        "compressed_bytes": len(raw),
        "tar_bytes": tar_bytes,
        "file_count": len(files),
        "source_manifest_sha256": source_sha256,
        "runtime": "UNRUN",
        "publication_source_mapping_and_provenance": "NOT_VERIFIED_BY_THIS_CHECK",
        "marketplace_acceptance": "NOT_ESTABLISHED",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive")
    parser.add_argument("--source-manifest", required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--integrity", required=True)
    args = parser.parse_args(argv)
    try:
        result = inspect_archive(
            args.archive, args.source_manifest, args.source_sha256, args.version, args.integrity
        )
    except (
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        OSError,
        EOFError,
        OverflowError,
        RecursionError,
        tarfile.TarError,
        zlib.error,
    ) as error:
        result = {
            "archive_inspection": "FAIL",
            "error_class": type(error).__name__,
            "runtime": "UNRUN",
        }
        if isinstance(error, ArchiveError):
            result["reason"] = str(error)
        print(json.dumps(result, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
