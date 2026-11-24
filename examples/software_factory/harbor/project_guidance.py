"""Freeze coverage-based retrieval guidance for the Loom agent image."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name("project-guidance.md")
SETTINGS = Path(".claude/settings.json")


def source_permissions(source_roots):
    search = ("grep", "rg", "egrep", "fgrep", "git grep")
    roots = [root.rstrip("/") for root in source_roots]
    deny = ["Grep"]
    deny.extend(f"Bash({command} * .)" for command in search)
    deny.extend(
        f"Bash({command} *{root}{suffix})"
        for command in (*search, "sed -n", "head", "tail")
        for root in roots
        for suffix in ("", "/*")
    )
    deny.extend(
        f"Bash(cat {prefix}{root}/*)" for root in roots for prefix in ("", "./", "/workspace/")
    )
    return {"permissions": {"deny": deny}}


def claude_settings(workspace, instructions):
    path = workspace / SETTINGS
    if path.is_symlink() or path.parent.is_symlink():
        raise PermissionError("Project permissions must reside in the checkout")
    settings = json.loads(path.read_text()) if path.is_file() else {}
    permissions = settings.setdefault("permissions", {})
    permissions["deny"] = list(
        dict.fromkeys(
            permissions.get("deny", []) + instructions["native_settings"]["permissions"]["deny"]
        )
    )
    return settings


def guidance(source_roots):
    text = TEMPLATE.read_text() + "\nIndexed source roots, when Loom is configured: "
    text += ", ".join(source_roots) + ". Other paths may require local reads.\n"
    return {
        "cohort": "loom-coverage-based-project-instructions",
        "path": "/workspace/CLAUDE.md",
        "text": text,
        "sha256": hashlib.sha256(text.encode()).hexdigest(),
        "applies_to": "loom-enabled",
        "native_settings": source_permissions(source_roots),
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
    settings = destination / SETTINGS
    settings.parent.mkdir()
    settings.write_text(json.dumps(claude_settings(workspace, instructions), indent=2) + "\n")
    return target


def agent_image(images: dict, slot: dict) -> str:
    variant = "baseline" if slot["mode"] == "baseline" else "loom"
    return images[slot["task_id"]]["agent"][variant]
