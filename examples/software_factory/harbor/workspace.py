"""Bind native Claude Code attempts to distinct Anthropic API workspaces."""

from __future__ import annotations

import json
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

MANIFEST_ENV = "FACTORY_ANTHROPIC_WORKSPACE_MANIFEST"
BINDINGS_ENV = "FACTORY_ANTHROPIC_WORKSPACE_BINDINGS"
CUSTOM_HEADERS = "ANTHROPIC_CUSTOM_HEADERS"
API_KEY = "ANTHROPIC_API_KEY"


@dataclass(frozen=True)
class Workspace:
    key: str = field(repr=False)
    selector: str = ""
    id: str = ""

    def resolve(self, model: str) -> Workspace:
        headers = {"x-api-key": self.key, "anthropic-version": "2023-06-01"}
        if self.selector:
            headers["anthropic-workspace-id"] = self.selector
        request = Request(
            "https://api.anthropic.com/v1/models/" + quote(model, safe=""), headers=headers
        )
        try:
            with urlopen(request, timeout=20) as response:
                workspace_id = response.headers.get("anthropic-workspace-id", "")
        except URLError as error:
            status = getattr(error, "code", None)
            raise PermissionError(
                f"Anthropic workspace model check failed (HTTP {status})"
            ) from None
        if not re.fullmatch(r"wrkspc_[a-zA-Z0-9]+", workspace_id):
            raise PermissionError("Anthropic did not return a workspace identity")
        expected = self.selector or self.id
        if expected and workspace_id != expected:
            raise PermissionError("Anthropic resolved a different workspace")
        return Workspace(self.key, self.selector, workspace_id)

    def activate(self):
        os.environ[API_KEY] = self.key
        os.environ[CUSTOM_HEADERS] = (
            f"anthropic-workspace-id: {self.selector}" if self.selector else ""
        )


@contextmanager
def solver_environment():
    values = {name: os.environ.get(name) for name in (API_KEY, CUSTOM_HEADERS)}
    try:
        yield
    finally:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def manifest(path: Path) -> tuple[Workspace, ...]:
    """Read either a list of scoped key files or one key and workspace IDs."""
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError("Expected a workspace manifest object")

    def key_file(name):
        key = (path.parent / Path(name).expanduser()).read_text().strip()
        if not key or key.startswith("sk-ant-admin") or any(char.isspace() for char in key):
            raise ValueError("Expected an Anthropic inference credential in each key file")
        return key

    if set(data) == {"key_files"} and isinstance(data["key_files"], list):
        return tuple(Workspace(key_file(name)) for name in data["key_files"])
    if set(data) == {"key_file", "workspace_ids"} and isinstance(data["workspace_ids"], list):
        key = key_file(data["key_file"])
        identifiers = data["workspace_ids"]
        if not all(
            isinstance(i, str) and re.fullmatch(r"wrkspc_[a-zA-Z0-9]+", i) for i in identifiers
        ):
            raise ValueError("Expected Anthropic workspace IDs")
        return tuple(Workspace(key, selector) for selector in identifiers)
    raise ValueError("Provide scoped key_files or an unscoped key_file and workspace_ids")


def qualify(bindings, *, expected: int, infrastructure_key: str, model: str):
    if len(bindings) != expected:
        raise ValueError(f"Provide exactly {expected} solver workspace bindings")
    resolved = tuple(binding.resolve(model) for binding in bindings)
    unique = {binding.id for binding in resolved}
    if len(unique) != expected:
        raise PermissionError("Every attempt requires a distinct Anthropic workspace")
    if Workspace(infrastructure_key).resolve(model).id in unique:
        raise PermissionError("Keep the setup and judge credential outside solver workspaces")
    return resolved
