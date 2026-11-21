"""Record controller and serving-source bytes without environment payloads."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
CONTROLLER_PARTS = (
    "examples/software_factory",
    "sdk/python",
    "bench/bootstrap.py",
    "pyproject.toml",
    "uv.lock",
)


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def command(args: list[str], *, data: bytes | None = None) -> bytes:
    return subprocess.run(args, input=data, capture_output=True, check=True, timeout=600).stdout


def check_files(files: dict) -> None:
    if not files:
        raise PermissionError("A nonempty input manifest is required")
    for name, expected in files.items():
        if sha(Path(name)) != expected:
            raise PermissionError("A frozen input changed: " + name)


def _git(root, *args):
    root = Path(root).resolve(strict=True)
    return subprocess.run(
        [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "core.fsmonitor=false",
            "-C",
            str(root),
            *args,
        ],
        capture_output=True,
        check=True,
        timeout=20,
        env={
            "PATH": os.defpath,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CEILING_DIRECTORIES": str(root.parent),
        },
    ).stdout


def _source_file(path):
    return not (
        path.name == ".env"
        or path.name.startswith(".env.")
        or path.suffix in {".env", ".token", ".pem", ".key"}
    )


def controller_identity(root: Path | None = None) -> dict:
    root = ROOT if root is None else Path(root).resolve(strict=True)
    names = _git(
        root,
        "ls-files",
        "--cached",
        "--others",
        "--exclude-standard",
        "-z",
        "--",
        *CONTROLLER_PARTS,
    )
    relative = {
        name: sha(root / name)
        for name in sorted(set(names.decode().strip("\0").split("\0")))
        if name and (root / name).is_file() and _source_file(Path(name))
    }
    if not relative:
        raise PermissionError("Controller source manifest is empty")
    return {
        "commit": _git(root, "rev-parse", "HEAD").decode().strip(),
        "dirty": bool(_git(root, "status", "--porcelain", "--", *CONTROLLER_PARTS)),
        "relative_files": relative,
        "files": {str(root / name): hashed for name, hashed in relative.items()},
    }


def server_identity(root: Path, container: str, container_root: str) -> dict:
    root = Path(root).resolve(strict=True)
    paths = [
        p
        for part in ("server", "schemas")
        for p in (root / part).rglob("*")
        if p.is_file() and p.suffix in {".py", ".json", ".sql"}
    ]
    relative = {str(p.relative_to(root)): sha(p) for p in paths}
    if not relative:
        raise PermissionError("Runtime source manifest is empty")
    code = (
        "import hashlib,json,sys;from pathlib import Path;"
        "v=json.load(sys.stdin);root=Path(v['root']);"
        "print(json.dumps({p:hashlib.sha256((root/p).read_bytes()).hexdigest() "
        "for p in v['paths']}))"
    )
    actual = json.loads(
        command(
            ["docker", "exec", "-i", container, "/opt/venv/bin/python", "-c", code],
            data=json.dumps({"root": container_root, "paths": list(relative)}).encode(),
        )
    )
    if actual != relative:
        raise PermissionError("Host runtime source differs from the serving container")
    return {
        "commit": _git(root, "rev-parse", "HEAD").decode().strip(),
        "dirty": bool(_git(root, "status", "--porcelain", "--", "server", "schemas")),
        "relative_files": relative,
        "files": {str(root / p): h for p, h in relative.items()},
        "container_source_root": container_root,
        "container_bytes_verified": True,
    }
