"""Read a repository evaluation descriptor without importing its adapter."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path, PurePosixPath

FILES = ("suite_lock", "scenario_manifest", "memory_catalog")


def load_evaluation(root, evaluation):
    if not isinstance(evaluation, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", evaluation):
        raise ValueError("Select a published evaluation id")
    root = Path(root).resolve()
    base = root / "examples/software_factory/evals"
    requested = base / evaluation
    directory = requested.resolve(strict=True)
    if (
        requested.is_symlink()
        or root not in directory.parents
        or directory.parent != base.resolve()
    ):
        raise ValueError("Evaluation directory leaves the repository catalog")
    descriptor = directory / "eval.json"
    if descriptor.is_symlink() or not descriptor.is_file():
        raise ValueError("Evaluation descriptor must be a regular public file")
    raw = descriptor.read_bytes()
    data = json.loads(raw)
    if (
        not isinstance(data, dict)
        or set(data) != {"id", "title", "adapter", *FILES}
        or data["id"] != evaluation
        or not isinstance(data["title"], str)
        or not data["title"]
        or not isinstance(data["adapter"], str)
        or not re.fullmatch(
            r"examples\.software_factory\.evals\.[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+", data["adapter"]
        )
    ):
        raise ValueError("Evaluation descriptor identity or schema differs")
    paths = {}
    for name in FILES:
        value = data[name]
        if not isinstance(value, str) or not value or "\\" in value:
            raise ValueError("Evaluation inputs require safe relative paths")
        relative = PurePosixPath(value)
        if relative.is_absolute() or any(part in {".", ".."} for part in relative.parts):
            raise ValueError("Evaluation inputs must stay inside the profile")
        path = (directory / value).resolve(strict=True)
        if directory not in path.parents or not path.is_file():
            raise ValueError("Evaluation input leaves its public profile")
        paths[name] = path
    return {
        "id": data["id"],
        "title": data["title"],
        "adapter": data["adapter"],
        "directory": directory,
        "paths": paths,
        "descriptor_sha256": hashlib.sha256(raw).hexdigest(),
    }
