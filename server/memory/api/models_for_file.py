"""memory.for_file — what Loom knows about one file, shaped for the moment
before an edit.

A constraint an agent must respect while editing `src/x.cpp` ("never call
allocate() here", "this file is generated — edit the template") is only
useful if it arrives BEFORE the edit, without the agent remembering to
search. The plugin's PreToolUse hook on Edit/Write calls this op with the
target path and injects the rendered block as additional context. Memories
reach a file through the `files[]` facet the extractor fills (a match accepts
a repo-relative path or a bare filename, because stored rows use both shapes)
or through a link to code in it (`server.memory.app.code_links`).

Read-only, VIEWER. Rows tagged `constraint` sort first, then pinned, then
confidence. `rendered` is cut at `token_budget` (its header and removal footer
always print, and its "… more" line whenever a row is left out).
`extra_namespaces` (a repository's primary code namespace, where its team file
rules and design decisions live) runs the same query per readable namespace,
merged in the same order, team rows marked with their author. Each hit carries
its `source_resource_id` and its `verification` state; a decision's rendered
line carries the source after its value.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from server.memory.domain.code_change import CodeState
from server.memory.domain.trust import VerificationState


class MemoryForFileInput(BaseModel):
    org: str
    namespace: str
    # Repo-relative path ("src/Storages/MergeTreeData.cpp") or bare filename.
    path: str = Field(min_length=1, max_length=1024)
    # Optional tag filter (any-of), e.g. ["project:loom"] under the plugin's
    # tag-scoped project mode. Empty = no tag filter.
    tags: list[str] = Field(default_factory=list, max_length=16)
    # At most this many rows, across every namespace read.
    top_k: int = Field(default=8, ge=1, le=50)
    # Cap on the rendered block; the hook injects `rendered` verbatim. Its header and
    # removal footer always print, and its "… more" line whenever a row is left out, so a
    # budget smaller than those can be exceeded.
    token_budget: int = Field(default=300, ge=50, le=4000)
    # Conflicted rows are still live facts a reader should see; duplicates,
    # superseded and rejected rows are not.
    include_conflicted: bool = True
    # The repository's primary code namespace (its team file rules and design decisions),
    # read beside `namespace`. Each is checked for read access like `namespace` would be
    # and skipped silently when unreadable; its rows carry `namespace` and render as
    # "Team rule (by <actor>):", or "Decision (by <actor>):" for a decision, which carries
    # its source. The tag filter applies to `namespace` only — a code namespace is
    # already the repository's.
    extra_namespaces: list[str] = Field(default_factory=list, max_length=2)


class _MemoryForFileHit(BaseModel):
    memory_key: str
    memory_type: str
    # The stored JSON text, cut at 600 bytes; `rendered` prints it decoded. The full row is a
    # memory.get away.
    value: str
    # True when `value` was cut.
    value_truncated: bool = False
    tags: list[str] = Field(default_factory=list)
    confidence: float = 1.0
    pinned: bool = False
    status: str = "active"
    agent: str = ""
    updated_at: datetime | None = None
    # Where the row lives — the request's namespace or one of
    # its extra_namespaces.
    namespace: str = ""
    # Who wrote the row (`human:<subject>` / `process:<subject>`);
    # a team row (rule or decision) renders with it.
    actor: str = ""
    # Set when this row and an older one of the same namespace
    # are cross-linked by the conflict check (`conflict_with`): the older's
    # key, author and kind (`decision`, or `rule` for any row that is not a decision).
    # This row is placed directly above it.
    supersedes_key: str = ""
    supersedes: str = ""
    supersedes_kind: Literal["rule", "decision", ""] = ""
    matched_files: list[str] = Field(default_factory=list)
    # Where the row was cited from (a document section, a file, a URL); '' when unset.
    source_resource_id: str = ""
    # The row's attestation (`memory.verify`), as on memory.get; an attested row whose value
    # was cut and has no stored hash reads stale.
    verification: VerificationState = "none"
    # The worst state of the row's links to code in the file ('' when it only names the
    # file in `files`): `fresh`, `moved`, `changed` (that code changed since the row was
    # written), `unresolved` (renamed or removed and not followed) or `missing` (gone).
    code_state: CodeState | Literal[""] = ""


class MemoryForFileOutput(BaseModel):
    # The path matched, normalised to repo-relative form.
    path: str
    # Number of `hits` returned (after the `top_k` cut).
    total: int
    # Matching rows: `constraint`-tagged first, then pinned, then by confidence,
    # then newest.
    hits: list[_MemoryForFileHit]
    # Prompt-ready block, cut at token_budget. Empty when total == 0.
    rendered: str = ""
    # Size of `rendered` at 4 chars per token; 0 when it is empty.
    tokens_estimate: int = 0
    # True when `token_budget` cut rows from `rendered`; `hits` still lists them.
    truncated: bool = False
