"""Bounded, read-only text exports from pinned public Git trees."""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import PurePosixPath


def ordinary(path):
    return (
        isinstance(path, str)
        and path
        and not path.startswith("/")
        and all(
            part not in {".", ".."} and not part.startswith(".")
            for part in PurePosixPath(path).parts
        )
    )


def git(repo, *args, data=None):
    return subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        check=True,
        env={"PATH": os.defpath, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    ).stdout


def source_files(repo, commit, roots):
    if (
        not re.fullmatch(r"[0-9a-f]{40}", commit)
        or not roots
        or not all(ordinary(root) for root in roots)
    ):
        raise PermissionError("Pin the public commit and index roots")
    if git(repo, "rev-parse", commit + "^{commit}").decode().strip() != commit:
        raise PermissionError("Git source commit differs")
    output = {}
    total = 0
    for record in git(repo, "ls-tree", "-rz", commit, "--", *roots).split(b"\0"):
        if not record:
            continue
        metadata, raw = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode().split()
        path = raw.decode()
        if not ordinary(path):
            continue
        if mode not in {"100644", "100755"} or kind != "blob":
            raise PermissionError("Nonregular indexed source")
        size = int(git(repo, "cat-file", "-s", oid))
        total += size
        if size > 1024 * 1024 or total > 20 * 1024 * 1024:
            raise ValueError("Public source exceeds preparation bounds")
        raw = git(repo, "cat-file", "blob", oid)
        if b"\0" in raw:
            continue
        try:
            output[path] = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue  # Binary fixtures stay in the checkout, outside the text index.
    if not output:
        raise PermissionError("Indexed text tree is empty")
    return output
