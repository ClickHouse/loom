"""What every code.* input shares: the identity block, the `namespaces` list rule,
and the serializer helpers that keep a disclosure off the wire until it fired.

A leaf: it imports nothing from the package.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field, model_validator

from server.core.naming import named
from server.operations.base import MCP_HIDDEN

# The overlay's own bounds (server/code/app/index/pr_overlay.py): files parsed,
# bytes per file, bytes of diff.
PR_FILES_MAX = 50
PR_FILE_MAX_CHARS = 1_000_000
PR_DIFF_MAX_CHARS = 1_500_000


class _CodeIdentity(BaseModel):
    """org/namespace default to the deployment's code plane; agent/user_id
    ride along for trace attribution (never authorization)."""

    org: str = ""
    namespace: str = ""
    agent: str = "-"
    user_id: str = "-"
    # trace attribution: which integration is calling (clickgap|claude-code|…)
    consumer: str = ""


class _PrPostImage(BaseModel):
    """An open PR's post-image sent by the caller, for the ops whose `pr_number`
    overlays the PR on the index. A caller holding the PR's checkout (a CI review
    job) sends the changed files' text and the PR's diff, and the overlay parses
    them instead of reading `refs/pull/N/head` from a git mirror, so any Loom on
    the service answers, one with no mirror (yet) included. Read for this
    request only: nothing sent here is written to the index or stored."""

    # Repo-relative path -> the file's full text at the PR's head, for the files
    # the PR adds or modifies (at most 50, 1,000,000 characters each). Needs `pr_number`.
    pr_files: dict[str, str] = Field(
        default_factory=dict, max_length=PR_FILES_MAX, json_schema_extra=MCP_HIDDEN
    )
    # The PR's unified diff against its merge-base (at most 1,500,000 characters):
    # its hunks say which functions the PR modifies, and its paths name the files
    # it deletes.
    pr_diff: str = Field(default="", max_length=PR_DIFF_MAX_CHARS, json_schema_extra=MCP_HIDDEN)
    # The PR's head and base commits as the caller knows them; they label the
    # answer (`overlay_head_sha`) and never reach git. '' = derived from the text.
    pr_head_sha: str = Field(default="", max_length=64, json_schema_extra=MCP_HIDDEN)
    pr_base_sha: str = Field(default="", max_length=64, json_schema_extra=MCP_HIDDEN)

    @model_validator(mode="after")
    def _check_post_image(self) -> _PrPostImage:
        if not (self.pr_files or self.pr_diff):
            return self
        if not int(getattr(self, "pr_number", 0) or 0):
            raise ValueError("pr_files and pr_diff need the pr_number they belong to")
        for path, text in self.pr_files.items():
            if not _safe_path(path):
                raise ValueError(f"pr_files path {path!r} is not a repo-relative path")
            if len(text) > PR_FILE_MAX_CHARS:
                raise ValueError(f"pr_files[{path!r}] is over {PR_FILE_MAX_CHARS} characters")
        return self


def _safe_path(path: str) -> bool:
    parts = path.split("/")
    return bool(path) and not path.startswith("/") and ".." not in parts and "" not in parts


def _trim_namespaces(v: list[str]) -> list[str]:
    """A `namespaces` list on the wire: trimmed, 1..128 characters each,
    duplicates removed in order (code.search fan-out and code.symbol presence share
    the rule)."""
    out: list[str] = []
    for raw in v:
        ns = raw.strip()
        if not ns or len(ns) > 128:
            raise ValueError("each namespaces entry must be 1..128 characters")
        if ns not in out:
            out.append(ns)
    return out


_GIT_SHA = re.compile(r"[0-9a-f]{7,64}")


def _git_sha(v: str) -> str:
    """A commit id on the wire: empty, or 7..64 hex characters (a SHA-1 or
    SHA-256 object name, full or abbreviated), lower-cased. These values
    reach `git` as revision arguments — at once, or later from the index
    state they are stored in — where anything else could be read as an
    option (`--output=<path>` makes `git log` write a file)."""
    sha = v.strip().lower()
    if sha and not _GIT_SHA.fullmatch(sha):
        raise ValueError("must be a commit sha: 7..64 hex characters")
    return sha


def _lead_namespaces(namespace: str, namespaces: list[str]) -> list[str]:
    """`namespace`, when named and absent from a non-empty list, leads it; a
    blank or "-" namespace names none."""
    if namespaces and named(namespace.strip()) and namespace not in namespaces:
        return [namespace, *namespaces]
    return namespaces


def _drop_empty_presence_keys(data: dict[str, Any]) -> dict[str, Any]:
    """The presence block is on the wire only when it fired: an empty
    `presence` / `summary` is dropped from the payload."""
    if not data.get("presence"):
        data.pop("presence", None)
    if not data.get("summary"):
        data.pop("summary", None)
    return data


def _drop_unfired_symbol_blocks(data: dict[str, Any]) -> dict[str, Any]:
    """The neighbours block and the citing documents ride only when they
    fired, so a plain lookup's payload carries neither."""
    if not data.get("documented_in"):
        data.pop("documented_in", None)
    if not data.get("neighbours"):
        for k in ("neighbours", "anchors", "neighbour_tokens", "anchors_capped"):
            data.pop(k, None)
    return data


def _drop_unset_symbol_context(data: dict[str, Any]) -> dict[str, Any]:
    """The uses-brief and ranking disclosures ride only when they say
    something: `uses_tokens` when a block was budgeted, `picked_by` when
    context was given. Otherwise neither key is on the wire."""
    if not data.get("uses_tokens"):
        data.pop("uses_tokens", None)
    if not data.get("picked_by"):
        data.pop("picked_by", None)
    if not data.get("hierarchy"):
        data.pop("hierarchy", None)  # only a type with edges carries it
    return _drop_unset_resolved_by(data)


def _drop_unset_resolved_by(data: dict[str, Any]) -> dict[str, Any]:
    """`resolved_by` rides only when a tolerant form bound the name; an
    exact hit's payload omits it."""
    if not data.get("resolved_by"):
        data.pop("resolved_by", None)
    return data


_OVERLAY_KEYS = ("overlay", "overlay_head_sha", "overlay_added", "overlay_note")


def _drop_unset_overlay(data: dict[str, Any]) -> dict[str, Any]:
    """The overlay disclosure rides only when the request named a PR
    (`pr_number`): `overlay: true` with the head the post-image came from, or
    `overlay: false` with the reason. A request without one omits the keys."""
    if not data.get("overlay") and not data.get("overlay_note"):
        for k in _OVERLAY_KEYS:
            data.pop(k, None)
    return data
