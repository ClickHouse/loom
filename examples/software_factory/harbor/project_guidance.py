"""Freeze coverage-based retrieval guidance for the Loom agent image."""

from __future__ import annotations

import hashlib
from pathlib import Path

TEMPLATE = Path(__file__).with_name("project-guidance.md")


def guidance(source_roots):
    text = TEMPLATE.read_text() + "\nIndexed source roots, when Loom is configured: "
    text += ", ".join(source_roots) + ". Other paths may require local reads.\n"
    return {
        "cohort": "loom-coverage-based-project-instructions",
        "path": "/workspace/CLAUDE.md",
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "applies_to": "loom-enabled",
    }


def stage(workspace: Path, destination: Path, instructions: dict) -> Path:
    existing = workspace / "CLAUDE.md"
    # A profile with existing project instructions needs an explicit composition policy.
    if existing.exists() or existing.is_symlink():
        raise PermissionError("Refusing to overwrite repository project instructions")
    destination.mkdir()
    target = destination / "CLAUDE.md"
    target.write_text(instructions["text"])
    if hashlib.sha256(target.read_bytes()).hexdigest() != instructions["sha256"]:
        raise PermissionError("Project instructions differ from the frozen protocol")
    return target


def agent_image(images: dict, slot: dict) -> str:
    variant = "baseline" if slot["mode"] == "baseline" else "loom"
    return images[slot["task_id"]]["agent"][variant]
