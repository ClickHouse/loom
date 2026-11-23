"""Freeze the same conditional retrieval instructions into every agent image."""

from __future__ import annotations

import hashlib
from pathlib import Path

TEMPLATE = Path(__file__).with_name("project-guidance.md")


def guidance(source_roots):
    text = TEMPLATE.read_text() + "\nIndexed source roots, when Loom is configured: "
    text += ", ".join(source_roots) + ". Other paths may require local reads.\n"
    return {
        "cohort": "shared-conditional-project-instructions",
        "path": "/workspace/CLAUDE.md",
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "same_for_all_arms": True,
    }


def stage(workspace: Path, instructions: dict):
    target = workspace / "CLAUDE.md"
    # A profile with existing project instructions needs an explicit composition policy.
    if target.exists() or target.is_symlink():
        raise PermissionError("Refusing to overwrite repository project instructions")
    target.write_text(instructions["text"])
    if hashlib.sha256(target.read_bytes()).hexdigest() != instructions["sha256"]:
        raise PermissionError("Project instructions differ from the frozen protocol")
