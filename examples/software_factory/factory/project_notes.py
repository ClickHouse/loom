"""Plain episode notes shared by every arm, outside candidate source."""

from __future__ import annotations

import json
import os
from pathlib import Path

from examples.software_factory.factory.model import digest, tool

NAMES = {"notes_list", "notes_read", "notes_search", "notes_set", "notes_update"}


def schemas():
    string = {"type": "string"}
    scope = {"type": "string", "enum": ["project", "working"]}
    return [
        tool(
            "notes_list",
            "List immutable public project documents or mutable episode notes.",
            {"scope": scope},
        ),
        tool(
            "notes_read",
            "Read an episode note or public project document.",
            {"scope": scope, "key": string},
        ),
        tool(
            "notes_search",
            "Search a literal in episode notes or public project documents.",
            {"scope": scope, "text": string},
        ),
        tool(
            "notes_set",
            "Save an observation in mutable working notes for later sessions. "
            "Public project documents are immutable.",
            {"key": string, "value": string},
        ),
        tool(
            "notes_update",
            "Revise a working note using its content hash to prevent clobbering.",
            {"key": string, "value": string, "expected_sha256": string},
        ),
    ]


class ProjectNotes:
    def __init__(self, path: Path, project: dict[str, str]):
        self.path = Path(path)
        self.project = dict(project)
        for key, value in self.project.items():
            self._validate(key, value)
        if self.path.is_symlink():
            raise PermissionError("Notes store must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self._save({})
        self._load()

    @staticmethod
    def _validate(key, value):
        if not isinstance(key, str) or not 1 <= len(key) <= 128:
            raise ValueError("Note key must contain 1..128 characters")
        if not isinstance(value, str) or len(value) > 16000:
            raise ValueError("Note value must be text of at most 16000 characters")

    def _load(self):
        if self.path.is_symlink():
            raise PermissionError("Notes store must not be a symlink")
        data = json.loads(self.path.read_text())
        if set(data) != {"project_sha256", "working"} or data["project_sha256"] != digest(
            self.project
        ):
            raise PermissionError("Notes store belongs to different project documents")
        if not isinstance(data["working"], dict):
            raise ValueError("Invalid working notes")
        for key, value in data["working"].items():
            self._validate(key, value)
        return data["working"]

    def _save(self, working):
        temporary = self.path.with_name(self.path.name + ".tmp")
        # O_EXCL prevents following an existing scratch symlink.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w") as stream:
                json.dump(
                    {"project_sha256": digest(self.project), "working": working},
                    stream,
                    sort_keys=True,
                )
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def execute(self, name, args):
        if name not in NAMES:
            raise ValueError("Unknown notes operation")
        expected = {
            "notes_list": {"scope"},
            "notes_read": {"scope", "key"},
            "notes_search": {"scope", "text"},
            "notes_set": {"key", "value"},
            "notes_update": {"key", "value", "expected_sha256"},
        }[name]
        if set(args) != expected:
            raise ValueError("Unexpected or missing notes argument")
        working = self._load()
        if name in {"notes_set", "notes_update"}:
            key, value = args["key"], args["value"]
            self._validate(key, value)
            if name == "notes_update" and (
                key not in working or digest(working[key]) != args["expected_sha256"]
            ):
                raise ValueError("Working note changed or is missing")
            working[key] = value
            self._save(working)
            return {
                "written": True,
                "scope": "working",
                "key": key,
                "content_sha256": digest(value),
            }
        if args["scope"] not in {"project", "working"}:
            raise ValueError("Unknown notes scope")
        notes = self.project if args["scope"] == "project" else working
        if name == "notes_list":
            return {"scope": args["scope"], "keys": sorted(notes)}
        if name == "notes_read":
            key = args["key"]
            return {
                "found": key in notes,
                **(
                    {"value": notes[key], "content_sha256": digest(notes[key])}
                    if key in notes
                    else {}
                ),
            }
        if not isinstance(args["text"], str) or not args["text"]:
            raise ValueError("Search text must be nonempty")
        return {
            "matches": [
                {"key": key, "value": value, "content_sha256": digest(value)}
                for key, value in sorted(notes.items())
                if args["text"] in value
            ]
        }
